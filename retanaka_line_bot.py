#!/usr/bin/env python3
"""Fetch Tanaka recycle prices and send a daily LINE WORKS message."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
import re
import ssl
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PRICE_URL = "https://gold.tanaka.co.jp/retanaka/price/"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINEWORKS_WEBHOOK_PREFIX = "https://webhook.worksmobile.com/message/"
DEFAULT_STATE_PATH = Path(__file__).resolve().parent / "data" / "retanaka_price_state.json"
DEFAULT_ENV_PATH = Path(__file__).resolve().parent / ".retanaka.env"
MAX_HISTORY = 90
PUBLISHED_AT_FORMAT = "%Y-%m-%d %H:%M"

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table>", re.IGNORECASE | re.DOTALL)
_WHITESPACE_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n+")


@dataclass
class PriceSnapshot:
    published_at: str
    k24: int
    pt: int
    fetched_at: str
    silver_999: int | None = None


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        if value and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]

        os.environ.setdefault(key, value)


def fetch_html(url: str) -> str:
    ssl_context = build_ssl_context()
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; retanaka-line-bot/1.0)",
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        },
    )
    with urlopen(request, timeout=30, context=ssl_context) as response:
        return response.read().decode("utf-8", errors="replace")


def html_to_text(html: str) -> str:
    text = _SCRIPT_STYLE_RE.sub("\n", html)
    text = _TAG_RE.sub("\n", text)
    text = unescape(text)
    text = text.replace("\u3000", " ")
    text = _WHITESPACE_RE.sub(" ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = _BLANK_LINES_RE.sub("\n", text)
    return text.strip()


def _extract_price(text: str, label: str) -> int:
    pattern = re.compile(rf"{re.escape(label)}\s*[¥￥]?\s*([0-9][0-9,]*)")
    match = pattern.search(text)
    if not match:
        raise ValueError(f"{label} の価格を取得できませんでした")

    return int(match.group(1).replace(",", ""))


def _extract_silver_999(html: str) -> int:
    for table_html in _TABLE_RE.findall(html):
        table_text = html_to_text(table_html)
        if "銀製品" in table_text:
            return _extract_price(table_text, "1000（999）")

    raise ValueError("銀製品表の1000（999）価格を取得できませんでした")


def _extract_published_at(text: str) -> str:
    match = re.search(
        r"([0-9]{4}年\s*[0-9]{1,2}月\s*[0-9]{1,2}日\s*[0-9]{1,2}:[0-9]{2})\s*発表",
        text,
    )
    if not match:
        raise ValueError("価格の発表日時を取得できませんでした")

    raw = re.sub(r"\s+", "", match.group(1))
    dt = datetime.strptime(raw, "%Y年%m月%d日%H:%M")
    return dt.strftime(PUBLISHED_AT_FORMAT)


def _format_published_at_for_message(published_at: str) -> str:
    try:
        dt = datetime.strptime(published_at, PUBLISHED_AT_FORMAT)
    except ValueError:
        return published_at

    weekdays = ("月", "火", "水", "木", "金", "土", "日")
    weekday = weekdays[dt.weekday()]
    return dt.strftime(f"%Y年%m月%d日({weekday})%H時%M分")


def parse_snapshot(html: str) -> PriceSnapshot:
    text = html_to_text(html)
    published_at = _extract_published_at(text)
    k24 = _extract_price(text, "K24特定品")
    pt = _extract_price(text, "Pt特定品")
    silver_999 = _extract_silver_999(html)
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return PriceSnapshot(
        published_at=published_at,
        k24=k24,
        pt=pt,
        fetched_at=fetched_at,
        silver_999=silver_999,
    )


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"history": [], "deliveries": {"line": {}, "lineworks": {}}}

    data = json.loads(path.read_text(encoding="utf-8"))
    history = data.get("history")
    if not isinstance(history, list):
        history = []
    normalized_history: list[dict[str, Any]] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        if not isinstance(item.get("published_at"), str):
            continue
        if not isinstance(item.get("k24"), int) or not isinstance(item.get("pt"), int):
            continue
        normalized_history.append(item)

    raw_deliveries = data.get("deliveries")
    if not isinstance(raw_deliveries, dict):
        raw_deliveries = {}

    line = _normalize_delivery(raw_deliveries.get("line"), include_date=True)
    lineworks = _normalize_delivery(raw_deliveries.get("lineworks"))

    # The legacy marker belonged to the previous LINE WORKS-only implementation.
    legacy_last_sent = data.get("last_sent_published_at")
    if not lineworks and isinstance(legacy_last_sent, str):
        lineworks["last_sent_published_at"] = legacy_last_sent

    return {"history": normalized_history, "deliveries": {"line": line, "lineworks": lineworks}}


def _normalize_delivery(value: Any, *, include_date: bool = False) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}

    normalized: dict[str, str] = {}
    last_sent = value.get("last_sent_published_at")
    if isinstance(last_sent, str):
        normalized["last_sent_published_at"] = last_sent

    if include_date:
        last_sent_date = value.get("last_sent_date")
        if isinstance(last_sent_date, str):
            normalized["last_sent_date"] = last_sent_date
        elif isinstance(last_sent, str):
            normalized["last_sent_date"] = last_sent[:10]

    return normalized


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            os.fchmod(tmp_file.fileno(), 0o600)
            json.dump(state, tmp_file, ensure_ascii=False, indent=2)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_name, path)
        path.chmod(0o600)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


@contextmanager
def state_lock(state_path: Path) -> Iterator[bool]:
    try:
        import fcntl
    except ImportError:
        yield True
        return

    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    with lock_path.open("a", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return

        try:
            yield True
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def upsert_history(state: dict[str, Any], snapshot: PriceSnapshot) -> None:
    history = state.setdefault("history", [])
    by_key: dict[str, dict[str, Any]] = {
        item["published_at"]: item for item in history if isinstance(item, dict) and "published_at" in item
    }
    by_key[snapshot.published_at] = asdict(snapshot)

    sorted_items = [by_key[key] for key in sorted(by_key.keys())]
    state["history"] = sorted_items[-MAX_HISTORY:]


def get_previous_snapshot(state: dict[str, Any], published_at: str) -> PriceSnapshot | None:
    candidates: list[dict[str, Any]] = []
    for item in state.get("history", []):
        if not isinstance(item, dict):
            continue
        key = item.get("published_at")
        if isinstance(key, str) and key < published_at:
            candidates.append(item)

    if not candidates:
        return None

    previous = max(candidates, key=lambda item: item["published_at"])
    return PriceSnapshot(
        published_at=previous["published_at"],
        k24=previous["k24"],
        pt=previous["pt"],
        fetched_at=previous.get("fetched_at", ""),
        silver_999=previous.get("silver_999"),
    )


def _format_delta(current: int, previous: int | None) -> str:
    if previous is None:
        return "初回取得"

    delta = current - previous
    sign = "+" if delta > 0 else ""
    return f"{sign}{delta:,}円"


def build_message(current: PriceSnapshot, previous: PriceSnapshot | None) -> str:
    k24_prev = previous.k24 if previous else None
    pt_prev = previous.pt if previous else None
    silver_prev = previous.silver_999 if previous else None
    published_at = _format_published_at_for_message(current.published_at)

    lines = [
        "【田中貴金属 リサイクル価格】",
        f"発表: {published_at}",
        f"K24特定品: {current.k24:,}円/g（前日比 {_format_delta(current.k24, k24_prev)}）",
        f"Pt特定品: {current.pt:,}円/g（前日比 {_format_delta(current.pt, pt_prev)}）",
    ]
    if current.silver_999 is not None:
        lines.append(f"Ag999: {current.silver_999:,}円/g（前日比 {_format_delta(current.silver_999, silver_prev)}）")
    lines.append(f"取得元: {PRICE_URL}")
    return "\n".join(lines)


def build_line_messages(message: str, image_url: str) -> list[dict[str, str]]:
    return [
        {"type": "text", "text": message},
        {
            "type": "image",
            "originalContentUrl": image_url,
            "previewImageUrl": image_url,
        },
    ]


def send_line_message(channel_access_token: str, group_id: str, message: str) -> None:
    ssl_context = build_ssl_context()
    payload = {"to": group_id, "messages": [{"type": "text", "text": message}]}
    request = Request(
        LINE_PUSH_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {channel_access_token}",
            "Content-Type": "application/json",
            "User-Agent": "retanaka-line-bot/1.0",
        },
    )

    try:
        with urlopen(request, timeout=30, context=ssl_context):
            return
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LINE Push APIエラー: HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"LINE Push API接続エラー: {exc}") from exc


def build_lineworks_payload(message: str, button_url: str = PRICE_URL) -> dict[str, Any]:
    return {
        "title": "RE:TANAKA価格",
        "body": {"text": message},
        "button": {
            "label": "価格ページを開く",
            "url": button_url,
        },
    }


def send_lineworks_message(webhook_url: str, message: str, button_url: str = PRICE_URL) -> None:
    ssl_context = build_ssl_context()
    payload = build_lineworks_payload(message, button_url)

    request = Request(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "retanaka-lineworks-bot/1.0",
        },
    )

    try:
        with urlopen(request, timeout=30, context=ssl_context):
            return
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LINE WORKS Webhookエラー: HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"LINE WORKS Webhook接続エラー: {exc}") from exc


def build_ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="送信せず、メッセージ内容のみ出力")
    parser.add_argument("--force-send", action="store_true", help="同一発表時刻でも送信を強制")
    parser.add_argument(
        "--test-lineworks-only",
        action="store_true",
        help="LINE WORKSだけにテストメッセージを送信（価格取得・状態更新なし）",
    )
    parser.add_argument("--state-path", default=str(DEFAULT_STATE_PATH), help="状態ファイルの保存先")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_PATH), help="環境変数ファイルのパス")
    return parser.parse_args(argv)


def _is_newer_than_last_sent(current_published_at: str, last_sent_published_at: Any) -> bool:
    return not isinstance(last_sent_published_at, str) or current_published_at > last_sent_published_at


def _line_is_due(line_state: dict[str, str], published_at: str) -> bool:
    if not _is_newer_than_last_sent(published_at, line_state.get("last_sent_published_at")):
        return False
    return line_state.get("last_sent_date") != published_at[:10]


def _lineworks_is_due(lineworks_state: dict[str, str], published_at: str) -> bool:
    return _is_newer_than_last_sent(published_at, lineworks_state.get("last_sent_published_at"))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    env_file = Path(args.env_file)
    load_env_file(env_file)

    webhook_url = os.getenv("LINEWORKS_WEBHOOK_URL", "").strip()
    if args.test_lineworks_only:
        if not webhook_url.startswith(LINEWORKS_WEBHOOK_PREFIX):
            print("LINEWORKS_WEBHOOK_URL が正しく設定されていません", file=sys.stderr)
            return 2
        try:
            send_lineworks_message(webhook_url, "RE:TANAKA LINE WORKS テスト送信")
        except Exception as exc:  # pragma: no cover
            print(f"LINE WORKSテスト送信に失敗しました: {exc}", file=sys.stderr)
            return 1
        print("LINE WORKSテスト送信完了")
        return 0

    state_path = Path(args.state_path)
    with state_lock(state_path) as acquired:
        if not acquired:
            print("別の実行が状態ファイルを使用中のため終了しました", file=sys.stderr)
            return 1

        state = load_state(state_path)

        try:
            html = fetch_html(PRICE_URL)
            current = parse_snapshot(html)
        except Exception as exc:  # pragma: no cover
            print(f"価格取得に失敗しました: {exc}", file=sys.stderr)
            return 1

        previous = get_previous_snapshot(state, current.published_at)
        message = build_message(current, previous)

        upsert_history(state, current)

        if args.dry_run:
            print(message)
            save_state(state_path, state)
            return 0

        deliveries = state["deliveries"]
        line_state = deliveries["line"]
        lineworks_state = deliveries["lineworks"]
        errors: list[str] = []
        delivered_channels: list[str] = []

        if args.force_send or _line_is_due(line_state, current.published_at):
            line_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
            line_group_id = os.getenv("LINE_GROUP_ID", "").strip()
            if not line_token or not line_group_id:
                errors.append("LINE_CHANNEL_ACCESS_TOKEN または LINE_GROUP_ID が正しく設定されていません")
            else:
                try:
                    send_line_message(line_token, line_group_id, message)
                except Exception as exc:  # pragma: no cover
                    errors.append(f"LINE送信に失敗しました: {exc}")
                else:
                    line_state["last_sent_published_at"] = current.published_at
                    line_state["last_sent_date"] = current.published_at[:10]
                    save_state(state_path, state)
                    delivered_channels.append("LINE")

        if args.force_send or _lineworks_is_due(lineworks_state, current.published_at):
            if not webhook_url.startswith(LINEWORKS_WEBHOOK_PREFIX):
                errors.append("LINEWORKS_WEBHOOK_URL が正しく設定されていません")
            else:
                try:
                    send_lineworks_message(webhook_url, message)
                except Exception as exc:  # pragma: no cover
                    errors.append(f"LINE WORKS送信に失敗しました: {exc}")
                else:
                    lineworks_state["last_sent_published_at"] = current.published_at
                    save_state(state_path, state)
                    delivered_channels.append("LINE WORKS")

        save_state(state_path, state)

        if errors:
            for error in errors:
                print(error, file=sys.stderr)
            return 1

        published_at_for_log = _format_published_at_for_message(current.published_at)
        if delivered_channels:
            print(f"送信完了({', '.join(delivered_channels)}): {published_at_for_log}")
        else:
            print(f"新しい送信対象はありません: {published_at_for_log}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
