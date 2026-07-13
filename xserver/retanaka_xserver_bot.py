#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Xserver Cron runner for Tanaka recycle prices -> LINE WORKS Incoming Webhook.

Python 3.6+ (Xserver shared hosting compatible) / standard library only.
"""

from __future__ import print_function

import argparse
import fcntl
import json
import os
import re
import ssl
import subprocess
import sys
import tempfile
import time as time_module
from contextlib import contextmanager
from datetime import datetime, timezone
from email.header import Header
from email.mime.text import MIMEText
from email.utils import make_msgid
from html import unescape
from pathlib import Path
from typing import NamedTuple, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_PRICE_URL = "https://gold.tanaka.co.jp/retanaka/price/"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINEWORKS_WEBHOOK_PREFIX = "https://webhook.worksmobile.com/message/"
SCREENSHOT_API_URL = "https://api.screenshotone.com/take"
DEFAULT_SECTION_SELECTOR = "#contents article > section"
DEFAULT_WAIT_SELECTOR = "#price_tables"
DEFAULT_SCREENSHOT_RETENTION_HOURS = 72
MAX_SCREENSHOT_BYTES = 10 * 1024 * 1024

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n+")

INTERNAL_TS_FORMAT = "%Y-%m-%d %H:%M"
DISPLAY_WEEKDAYS = ("月", "火", "水", "木", "金", "土", "日")
MAX_HISTORY = 90


class PriceSnapshot(NamedTuple):
    published_at: str
    k24: int
    pt: int
    silver_999: Optional[int]
    fetched_at: str


def make_empty_state():
    return {
        "schema_version": 3,
        "history": [],
        "last_observed_published_at": None,
        "deliveries": {
            "line": {
                "last_sent_published_at": None,
                "last_sent_date": None,
            },
            "lineworks": {
                "last_sent_published_at": None,
            },
        },
        "error_alert_dates": {},
        "pending_recovery_alerts": {},
        "test_email_sent_at": None,
    }


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parent / "config.json"),
        help="config JSON path (default: ./config.json)",
    )
    parser.add_argument("--dry-run", action="store_true", help="LINE WORKS送信せず通知内容のみ出力")
    parser.add_argument("--force-send", action="store_true", help="同一発表時刻でも送信")
    parser.add_argument("--test-lineworks-only", action="store_true", help="状態を変更せずLINE WORKSだけに送信")
    return parser.parse_args(argv)


def parse_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
    return default


def normalize_path(config_dir, raw_value, default_value):
    raw = str(raw_value).strip() if raw_value is not None else ""
    path = Path(raw or default_value)
    if not path.is_absolute():
        path = config_dir / path
    return str(path)


def read_alert_config_only(path):
    config_path = Path(path)
    config_dir = config_path.resolve().parent
    default_state = config_dir / "storage" / "retanaka_price_state.json"
    config = {
        "alert_email": "",
        "alert_email_from": "retanaka-bot@localhost",
        "sendmail_path": "/usr/sbin/sendmail",
        "state_file": str(default_state),
        "lineworks_webhook_url": "",
        "price_url": DEFAULT_PRICE_URL,
    }

    if not config_path.exists():
        return config

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return config

    if not isinstance(data, dict):
        return config

    config["alert_email"] = str(data.get("alert_email", "")).strip()
    config["alert_email_from"] = (
        str(data.get("alert_email_from", "retanaka-bot@localhost")).strip() or "retanaka-bot@localhost"
    )
    config["sendmail_path"] = str(data.get("sendmail_path", "/usr/sbin/sendmail")).strip() or "/usr/sbin/sendmail"
    config["state_file"] = normalize_path(config_dir, data.get("state_file"), str(default_state))
    webhook_url = str(data.get("lineworks_webhook_url", "")).strip()
    if webhook_url.startswith(LINEWORKS_WEBHOOK_PREFIX):
        config["lineworks_webhook_url"] = webhook_url
    config["price_url"] = str(data.get("price_url", DEFAULT_PRICE_URL)).strip() or DEFAULT_PRICE_URL
    return config


def read_config(path):
    config_path = Path(path)
    config_dir = config_path.resolve().parent
    if not config_path.exists():
        raise RuntimeError("config file not found: {0}".format(config_path))

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise RuntimeError("invalid JSON config: {0}".format(exc))

    if not isinstance(config, dict):
        raise RuntimeError("config root must be object")

    required = ["lineworks_webhook_url"]
    for key in required:
        value = str(config.get(key, "")).strip()
        if not value:
            raise RuntimeError("missing required config key: {0}".format(key))
        config[key] = value

    if not config["lineworks_webhook_url"].startswith(LINEWORKS_WEBHOOK_PREFIX):
        raise RuntimeError("lineworks_webhook_url must start with {0}".format(LINEWORKS_WEBHOOK_PREFIX))

    line_token = str(config.get("line_channel_access_token", "")).strip()
    line_group_id = str(config.get("line_group_id", "")).strip()
    if bool(line_token) != bool(line_group_id):
        raise RuntimeError("line_channel_access_token and line_group_id must be configured together")
    config["line_channel_access_token"] = line_token
    config["line_group_id"] = line_group_id

    config["price_url"] = str(config.get("price_url", DEFAULT_PRICE_URL)).strip() or DEFAULT_PRICE_URL
    config["timezone"] = str(config.get("timezone", "Asia/Tokyo")).strip() or "Asia/Tokyo"
    config["alert_email"] = str(config.get("alert_email", "")).strip()
    config["alert_email_from"] = (
        str(config.get("alert_email_from", "retanaka-bot@localhost")).strip() or "retanaka-bot@localhost"
    )
    config["sendmail_path"] = str(config.get("sendmail_path", "/usr/sbin/sendmail")).strip() or "/usr/sbin/sendmail"

    default_state = config_dir / "storage" / "retanaka_price_state.json"
    config["state_file"] = normalize_path(config_dir, config.get("state_file"), str(default_state))
    config["lock_file"] = normalize_path(
        config_dir,
        config.get("lock_file"),
        str(config_dir / "storage" / "retanaka_price.lock"),
    )

    # Screenshot settings
    config["enable_section_screenshot"] = parse_bool(config.get("enable_section_screenshot", False), False)
    config["require_screenshot"] = parse_bool(config.get("require_screenshot", True), True)
    config["screenshot_api_url"] = (
        str(config.get("screenshot_api_url", SCREENSHOT_API_URL)).strip() or SCREENSHOT_API_URL
    )
    config["screenshot_selector"] = (
        str(config.get("screenshot_selector", DEFAULT_SECTION_SELECTOR)).strip() or DEFAULT_SECTION_SELECTOR
    )
    config["screenshot_wait_for_selector"] = (
        str(config.get("screenshot_wait_for_selector", DEFAULT_WAIT_SELECTOR)).strip() or DEFAULT_WAIT_SELECTOR
    )
    raw_hide = config.get("screenshot_hide_selectors", ["#contents article > section > p.buttons"])
    if isinstance(raw_hide, str):
        hide_selectors = [item.strip() for item in raw_hide.split(",") if item.strip()]
    elif isinstance(raw_hide, list):
        hide_selectors = [str(item).strip() for item in raw_hide if str(item).strip()]
    else:
        hide_selectors = []
    config["screenshot_hide_selectors"] = hide_selectors

    try:
        retention = int(config.get("screenshot_retention_hours", DEFAULT_SCREENSHOT_RETENTION_HOURS))
    except Exception:
        retention = DEFAULT_SCREENSHOT_RETENTION_HOURS
    config["screenshot_retention_hours"] = retention if retention > 0 else DEFAULT_SCREENSHOT_RETENTION_HOURS
    try:
        delete_after = int(config.get("screenshot_delete_after_seconds", 0))
    except Exception:
        delete_after = 0
    config["screenshot_delete_after_seconds"] = delete_after if delete_after >= 0 else 0

    if config["enable_section_screenshot"]:
        access_key = str(config.get("screenshotone_access_key", "")).strip()
        if not access_key:
            raise RuntimeError("missing required config key: screenshotone_access_key")
        config["screenshotone_access_key"] = access_key

        public_base = str(config.get("screenshot_public_base_url", "")).strip().rstrip("/")
        if not public_base.startswith("https://"):
            raise RuntimeError("screenshot_public_base_url must start with https://")
        config["screenshot_public_base_url"] = public_base

        default_public_dir = config_dir / "public_images"
        config["screenshot_public_dir"] = normalize_path(
            config_dir,
            config.get("screenshot_public_dir"),
            str(default_public_dir),
        )

    # Keep this script robust on shared hosting even without env vars.
    os.environ["TZ"] = config["timezone"]
    if hasattr(time_module, "tzset"):
        time_module.tzset()

    return config


def fetch_html(url):
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; retanaka-xserver-bot/1.0)",
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        },
    )
    context = build_ssl_context()
    with urlopen(request, timeout=30, context=context) as response:
        return response.read().decode("utf-8", errors="replace")


def html_to_text(html):
    text = _SCRIPT_STYLE_RE.sub("\n", html)
    text = _TAG_RE.sub("\n", text)
    text = unescape(text)
    text = text.replace("\u3000", " ")
    text = _WHITESPACE_RE.sub(" ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = _BLANK_LINES_RE.sub("\n", text)
    return text.strip()


def extract_price(text, label):
    pattern = re.compile(r"{0}\s*[¥￥]?\s*([0-9][0-9,]*)".format(re.escape(label)))
    match = pattern.search(text)
    if not match:
        raise RuntimeError("{0} の価格を取得できませんでした".format(label))
    return int(match.group(1).replace(",", ""))


def extract_published_at(text):
    match = re.search(
        r"([0-9]{4}年\s*[0-9]{1,2}月\s*[0-9]{1,2}日\s*[0-9]{1,2}:[0-9]{2})\s*発表",
        text,
    )
    if not match:
        raise RuntimeError("価格の発表日時を取得できませんでした")

    raw = re.sub(r"\s+", "", match.group(1))
    dt = datetime.strptime(raw, "%Y年%m月%d日%H:%M")
    return dt.strftime(INTERNAL_TS_FORMAT)


def parse_snapshot(html):
    text = html_to_text(html)
    return PriceSnapshot(
        published_at=extract_published_at(text),
        k24=extract_price(text, "K24特定品"),
        pt=extract_price(text, "Pt特定品"),
        silver_999=extract_silver_999_price(html),
        fetched_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def extract_silver_999_price(html):
    silver_section = re.search(
        r'<section\b[^>]*\bid=["\']ag_price["\'][^>]*>(.*?)</section>',
        html,
        re.IGNORECASE | re.DOTALL,
    )
    if silver_section:
        section_text = html_to_text(silver_section.group(0))
        match = re.search(
            r"1000\s*[（(]\s*999\s*[）)]\s*[¥￥]?\s*([0-9][0-9,]*)",
            section_text,
        )
        if match:
            return int(match.group(1).replace(",", ""))

    tables = re.finditer(
        r"<table\b(?P<attributes>[^>]*)>(?P<contents>.*?)</table>",
        html,
        re.IGNORECASE | re.DOTALL,
    )
    for match in tables:
        table = match.group(0)
        table_text = html_to_text(table)
        attributes = match.group("attributes").lower()
        if "silver" not in attributes and "銀製品" not in table_text:
            continue
        try:
            return extract_price(table_text, "1000（999）")
        except RuntimeError:
            try:
                return extract_price(table_text, "1000(999)")
            except RuntimeError:
                try:
                    return extract_price(table_text, "銀(999)")
                except RuntimeError:
                    try:
                        return extract_price(table_text, "銀999")
                    except RuntimeError:
                        continue
    return None


def load_state(path):
    state_path = Path(path)
    if not state_path.exists():
        return make_empty_state()

    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except ValueError:
        return make_empty_state()

    return migrate_state(data)


def migrate_state(data):
    if not isinstance(data, dict):
        return make_empty_state()

    source_schema = data.get("schema_version")
    history = data.get("history")
    if not isinstance(history, list):
        history = []

    normalized = []
    for item in history:
        if not isinstance(item, dict):
            continue
        published_at = item.get("published_at")
        k24 = item.get("k24")
        pt = item.get("pt")
        if not isinstance(published_at, str):
            continue
        if not isinstance(k24, int) or not isinstance(pt, int):
            continue
        normalized.append(
            {
                "published_at": published_at,
                "k24": k24,
                "pt": pt,
                "silver_999": item.get("silver_999") if isinstance(item.get("silver_999"), int) else None,
                "fetched_at": str(item.get("fetched_at", "")),
            }
        )

    deliveries = data.get("deliveries")
    if not isinstance(deliveries, dict):
        deliveries = {}
    raw_line = deliveries.get("line")
    if not isinstance(raw_line, dict):
        raw_line = {}
    raw_lineworks = deliveries.get("lineworks")
    if not isinstance(raw_lineworks, dict):
        raw_lineworks = {}

    line_last_sent_date = raw_line.get("last_sent_date", data.get("line_last_sent_date"))
    if not isinstance(line_last_sent_date, str):
        line_last_sent_date = None
    line_last_sent_published_at = raw_line.get("last_sent_published_at")
    if not isinstance(line_last_sent_published_at, str):
        line_last_sent_published_at = None
    lineworks_last_sent_published_at = raw_lineworks.get(
        "last_sent_published_at", data.get("lineworks_last_sent_published_at")
    )
    if not isinstance(lineworks_last_sent_published_at, str):
        legacy_last_sent = data.get("last_sent_published_at")
        lineworks_last_sent_published_at = legacy_last_sent if isinstance(legacy_last_sent, str) else None
    last_observed_published_at = data.get("last_observed_published_at")
    if not isinstance(last_observed_published_at, str):
        last_observed_published_at = None
    raw_error_alert_dates = data.get("error_alert_dates")
    if not isinstance(raw_error_alert_dates, dict):
        raw_error_alert_dates = {}
    error_alert_dates = {}
    for key, value in raw_error_alert_dates.items():
        if isinstance(key, str) and isinstance(value, str):
            error_alert_dates[key] = value
    raw_pending_recovery_alerts = data.get("pending_recovery_alerts")
    if not isinstance(raw_pending_recovery_alerts, dict):
        raw_pending_recovery_alerts = {}
    pending_recovery_alerts = {}
    is_legacy_schema = source_schema is None or (
        type(source_schema) is int and source_schema <= 2
    )
    for key, value in raw_pending_recovery_alerts.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        subject = value.get("subject")
        body = value.get("body")
        message_id = value.get("message_id")
        occurred_at = value.get("occurred_at")
        transport = value.get("transport")
        if is_legacy_schema:
            transport = "email"
        elif transport not in ("lineworks", "email"):
            continue
        if not all(isinstance(item, str) and item for item in (subject, body, occurred_at)):
            continue
        pending_alert = {
            "transport": transport,
            "subject": subject,
            "body": body,
            "occurred_at": occurred_at,
        }
        if transport == "email" and isinstance(message_id, str) and message_id:
            pending_alert["message_id"] = message_id
        elif transport == "email":
            continue
        pending_recovery_alerts[key] = pending_alert
    test_email_sent_at = data.get("test_email_sent_at")
    if not isinstance(test_email_sent_at, str):
        test_email_sent_at = None

    state = make_empty_state()
    state.update(
        {
            "history": normalized,
            "last_observed_published_at": last_observed_published_at,
            "deliveries": {
                "line": {
                    "last_sent_published_at": line_last_sent_published_at,
                    "last_sent_date": line_last_sent_date,
                },
                "lineworks": {"last_sent_published_at": lineworks_last_sent_published_at},
            },
            "error_alert_dates": error_alert_dates,
            "pending_recovery_alerts": pending_recovery_alerts,
            "test_email_sent_at": test_email_sent_at,
        }
    )
    return state


def ensure_private_storage_directory(directory):
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(str(directory), 0o700)
    except Exception:
        pass


def fsync_directory(directory):
    try:
        directory_fd = os.open(str(directory), os.O_RDONLY)
    except Exception:
        return
    try:
        os.fsync(directory_fd)
    except Exception:
        pass
    finally:
        os.close(directory_fd)


def save_state(path, state):
    state_path = Path(path)
    ensure_private_storage_directory(state_path.parent)
    fd = None
    tmp_path = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=".{0}.".format(state_path.name), suffix=".tmp", dir=str(state_path.parent)
        )
        tmp_path = Path(tmp_name)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp_path), str(state_path))
        tmp_path = None
        try:
            os.chmod(str(state_path), 0o600)
        except Exception:
            pass
        fsync_directory(state_path.parent)
    finally:
        if fd is not None:
            os.close(fd)
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except Exception:
                pass


@contextmanager
def acquire_process_lock(path):
    lock_path = Path(path)
    ensure_private_storage_directory(lock_path.parent)
    handle = lock_path.open("a+")
    try:
        try:
            os.chmod(str(lock_path), 0o600)
        except Exception:
            pass
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, OSError):
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def upsert_history(state, snapshot):
    by_published = {}
    for item in state.get("history", []):
        key = item.get("published_at")
        if isinstance(key, str):
            by_published[key] = item

    by_published[snapshot.published_at] = {
        "published_at": snapshot.published_at,
        "k24": snapshot.k24,
        "pt": snapshot.pt,
        "silver_999": snapshot.silver_999,
        "fetched_at": snapshot.fetched_at,
    }

    sorted_items = [by_published[key] for key in sorted(by_published.keys())]
    state["history"] = sorted_items[-MAX_HISTORY:]


def get_previous_snapshot(state, published_at):
    candidates = []
    for item in state.get("history", []):
        key = item.get("published_at")
        if isinstance(key, str) and key < published_at:
            candidates.append(item)

    if not candidates:
        return None

    previous = max(candidates, key=lambda item: item["published_at"])
    return PriceSnapshot(
        published_at=previous["published_at"],
        k24=int(previous["k24"]),
        pt=int(previous["pt"]),
        silver_999=previous.get("silver_999") if isinstance(previous.get("silver_999"), int) else None,
        fetched_at=str(previous.get("fetched_at", "")),
    )


def get_previous_day_snapshot(state, published_at):
    current_dt = datetime.strptime(published_at, INTERNAL_TS_FORMAT)
    current_date = current_dt.date()

    candidates = []
    for item in state.get("history", []):
        key = item.get("published_at")
        if not isinstance(key, str):
            continue
        try:
            item_dt = datetime.strptime(key, INTERNAL_TS_FORMAT)
        except ValueError:
            continue
        if item_dt.date() < current_date:
            candidates.append((item_dt.date(), item))

    if not candidates:
        return None

    target_date = max(date_key for date_key, _ in candidates)
    same_day_items = [item for date_key, item in candidates if date_key == target_date]
    previous = min(same_day_items, key=lambda item: item["published_at"])
    return PriceSnapshot(
        published_at=previous["published_at"],
        k24=int(previous["k24"]),
        pt=int(previous["pt"]),
        silver_999=previous.get("silver_999") if isinstance(previous.get("silver_999"), int) else None,
        fetched_at=str(previous.get("fetched_at", "")),
    )


def format_delta(current, previous):
    if previous is None:
        return "初回取得"
    delta = current - previous
    sign = "+" if delta > 0 else ""
    return "{0}{1:,}円".format(sign, delta)


def format_published_at_display(published_at):
    dt = datetime.strptime(published_at, INTERNAL_TS_FORMAT)
    weekday = DISPLAY_WEEKDAYS[dt.weekday()]
    return dt.strftime("%Y年%m月%d日") + "({0})".format(weekday) + dt.strftime("%H時%M分")


def should_deliver_line(state, snapshot, today_key):
    line_delivery = state.get("deliveries", {}).get("line", {})
    return (
        snapshot.published_at.startswith(today_key)
        and line_delivery.get("last_sent_date") != today_key
        and line_delivery.get("last_sent_published_at") != snapshot.published_at
    )


def should_deliver_lineworks(state, snapshot):
    lineworks_delivery = state.get("deliveries", {}).get("lineworks", {})
    return lineworks_delivery.get("last_sent_published_at") != snapshot.published_at


def is_new_observation(state, snapshot):
    last_observed = state.get("last_observed_published_at")
    return not isinstance(last_observed, str) or snapshot.published_at > last_observed


def is_equal_observation(state, snapshot):
    return state.get("last_observed_published_at") == snapshot.published_at


def build_message(snapshot, previous_day, price_url):
    previous_day_k24 = previous_day.k24 if previous_day else None
    previous_day_pt = previous_day.pt if previous_day else None
    previous_day_silver = previous_day.silver_999 if previous_day else None

    lines = [
        "【田中貴金属 リサイクル価格】",
        "発表：{0}".format(format_published_at_display(snapshot.published_at)),
        "前日比基準：{0}".format(
            format_published_at_display(previous_day.published_at) if previous_day else "初回取得"
        ),
        "",
        "[金]",
        "K24特定品：{0:,}円/g".format(snapshot.k24),
        "前日比：{0}".format(format_delta(snapshot.k24, previous_day_k24)),
        "",
        "[プラチナ]",
        "Pt特定品：{0:,}円/g".format(snapshot.pt),
        "前日比：{0}".format(format_delta(snapshot.pt, previous_day_pt)),
    ]

    if snapshot.silver_999 is not None:
        lines.extend(
            [
                "",
                "[銀]",
                "銀(999)：{0:,}円/g".format(snapshot.silver_999),
                "前日比：{0}".format(format_delta(snapshot.silver_999, previous_day_silver)),
            ]
        )

    lines.append("")
    lines.append("取得元：{0}".format(price_url))
    return "\n".join(lines)


def build_screenshot_api_url(config):
    params = {
        "url": config["price_url"],
        "access_key": config["screenshotone_access_key"],
        "selector": config["screenshot_selector"],
        "selector_algorithm": "clip",
        "selector_scroll_into_view": "true",
        "wait_for_selector": config["screenshot_wait_for_selector"],
        "wait_for_selector_algorithm": "at_least_one",
        "error_on_selector_not_found": "true",
        "block_ads": "true",
        "format": "png",
        "viewport_width": "1440",
        "viewport_height": "4000",
        "device_scale_factor": "1",
    }
    if config.get("screenshot_hide_selectors"):
        params["hide_selectors"] = config["screenshot_hide_selectors"]
    return "{0}?{1}".format(config["screenshot_api_url"], urlencode(params, doseq=True))


def fetch_section_screenshot_bytes(config):
    screenshot_url = build_screenshot_api_url(config)
    request = Request(
        screenshot_url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; retanaka-xserver-bot/1.0)",
            "Accept": "image/png,image/*;q=0.9,*/*;q=0.5",
        },
    )
    context = build_ssl_context()
    try:
        with urlopen(request, timeout=60, context=context) as response:
            content_type = str(response.headers.get("Content-Type", ""))
            payload = response.read()
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError("スクリーンショットAPIエラー: HTTP {0}: {1}".format(exc.code, body))
    except URLError as exc:
        raise RuntimeError("スクリーンショットAPI接続エラー: {0}".format(exc))

    if not payload:
        raise RuntimeError("スクリーンショットAPIが空レスポンスを返しました")

    if len(payload) > MAX_SCREENSHOT_BYTES:
        raise RuntimeError("スクリーンショット画像サイズが大きすぎます ({0} bytes)".format(len(payload)))

    if ("image/" not in content_type.lower()) and (not payload.startswith(b"\x89PNG")):
        snippet = payload[:400].decode("utf-8", errors="replace")
        raise RuntimeError("スクリーンショットAPIエラー: {0}".format(snippet))

    return payload


def save_public_screenshot(config, image_bytes, published_at):
    public_dir = Path(config["screenshot_public_dir"])
    public_dir.mkdir(parents=True, exist_ok=True)

    dt = datetime.strptime(published_at, INTERNAL_TS_FORMAT)
    stamp = dt.strftime("%Y%m%d_%H%M")
    nonce = os.urandom(5).hex()
    filename = "retanaka_{0}_{1}.png".format(stamp, nonce)
    image_path = public_dir / filename

    image_path.write_bytes(image_bytes)
    try:
        os.chmod(str(image_path), 0o644)
    except Exception:
        pass

    image_url = config["screenshot_public_base_url"].rstrip("/") + "/" + filename
    return str(image_path), image_url


def find_public_screenshot(config, published_at):
    public_dir = Path(config["screenshot_public_dir"])
    if not public_dir.exists() or not public_dir.is_dir():
        return None, None

    stamp = datetime.strptime(published_at, INTERNAL_TS_FORMAT).strftime("%Y%m%d_%H%M")
    candidates = sorted(public_dir.glob("retanaka_{0}_*.png".format(stamp)))
    for image_path in candidates:
        if image_path.is_file():
            image_url = config["screenshot_public_base_url"].rstrip("/") + "/" + image_path.name
            return str(image_path), image_url
    return None, None


def cleanup_old_screenshots(config):
    public_dir = Path(config.get("screenshot_public_dir", ""))
    if not public_dir.exists() or not public_dir.is_dir():
        return

    retention_seconds = int(config.get("screenshot_retention_hours", DEFAULT_SCREENSHOT_RETENTION_HOURS)) * 3600
    deadline = time_module.time() - retention_seconds

    for entry in public_dir.iterdir():
        if not entry.is_file():
            continue
        if not entry.name.startswith("retanaka_") or not entry.name.endswith(".png"):
            continue

        try:
            if entry.stat().st_mtime < deadline:
                entry.unlink()
        except Exception:
            continue


def build_lineworks_payload(text_message, image_url=None, price_url=None):
    button_url = image_url or price_url or DEFAULT_PRICE_URL
    button_label = "価格表画像を見る" if image_url else "価格ページを開く"
    body_text = text_message
    heading = "【田中貴金属 リサイクル価格】"
    if body_text.startswith(heading):
        body_text = body_text[len(heading) :].lstrip("\r\n")
    return {
        "title": "RE:TANAKA価格",
        "body": {"text": body_text},
        "button": {
            "label": button_label,
            "url": button_url,
        },
    }


def build_line_messages(text_message, image_url=None):
    messages = [{"type": "text", "text": text_message}]
    if image_url:
        messages.append(
            {
                "type": "image",
                "originalContentUrl": image_url,
                "previewImageUrl": image_url,
            }
        )
    return messages


def summarize_screenshot_error_for_user(error_text):
    lowered = str(error_text).lower()
    if (
        "http 401" in lowered
        or "http 403" in lowered
        or "access_key" in lowered
        or "access key" in lowered
        or "unauthorized" in lowered
        or "forbidden" in lowered
        or "token" in lowered
    ):
        return "ScreenshotOneのアクセストークン切れ、または無効な可能性があります。"
    if "http 429" in lowered or "quota" in lowered or "limit" in lowered:
        return "ScreenshotOneの利用上限に達した可能性があります。"
    if "selector_not_found" in lowered or ("selector" in lowered and "not found" in lowered):
        return "価格表エリアの検出に失敗しました。"
    if "timeout" in lowered or "timed out" in lowered:
        return "ScreenshotOneの応答がタイムアウトしました。"
    return "ScreenshotOne障害のため画像を取得できませんでした。"


def build_alert_email(subject, body, in_reply_to=None, references=None):
    message = MIMEText(body, _subtype="plain", _charset="utf-8")
    message["Subject"] = Header(subject, "utf-8")
    message["Message-ID"] = make_msgid()
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references
    return message


def send_alert_email(config, subject, body, in_reply_to=None, references=None):
    recipient = str(config.get("alert_email", "")).strip()
    if not recipient:
        return False

    sendmail_path = str(config.get("sendmail_path", "/usr/sbin/sendmail")).strip() or "/usr/sbin/sendmail"
    if not os.path.exists(sendmail_path):
        raise RuntimeError("sendmail not found: {0}".format(sendmail_path))

    message = build_alert_email(subject, body, in_reply_to, references)
    message["From"] = str(config.get("alert_email_from", "retanaka-bot@localhost")).strip() or "retanaka-bot@localhost"
    message["To"] = recipient

    process = subprocess.Popen(
        [sendmail_path, "-t", "-i"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = process.communicate(message.as_bytes())
    if process.returncode != 0:
        raise RuntimeError(
            "sendmail failed: exit={0}, stderr={1}".format(
                process.returncode,
                stderr.decode("utf-8", errors="replace").strip(),
            )
        )
    return message["Message-ID"]


def classify_error(error, alert_key=None):
    error_text = str(error).lower()
    if alert_key == "config_error" or any(
        marker in error_text
        for marker in ("config file", "invalid json config", "config root", "missing required config")
    ):
        return {
            "cause": "BOTの設定ファイルに不足または不正な値があります。",
            "bot_action": "設定が直るまで、この処理は実行できません。",
            "required_action": "設定ファイルを確認してください。技術情報に記載された項目が手掛かりです。",
        }
    if alert_key == "screenshot_error" or "スクリーンショットapi" in error_text or "screenshotone" in error_text:
        cause = "スクリーンショット取得サービスでエラーが発生しました。"
        if "http 401" in error_text or "http 403" in error_text:
            cause = "スクリーンショット取得サービスの認証またはアクセス権限が拒否されました。"
        elif "http 429" in error_text or "quota" in error_text or "limit" in error_text:
            cause = "スクリーンショット取得サービスの利用上限に達しました。"
        return {
            "cause": cause,
            "bot_action": "次の価格更新時に画像取得を再試行します。",
            "required_action": "繰り返す場合はScreenshotOneの稼働状況、認証情報、利用上限を確認してください。",
        }
    if alert_key in ("line_delivery_error", "line_limit_error", "lineworks_delivery_error") or any(
        marker in error_text for marker in ("line api", "line works webhook", "line works")
    ):
        return {
            "cause": "通知先サービスが送信要求を受け付けませんでした。",
            "bot_action": "次回の毎分実行で、未送信の通知を自動的に再試行します。",
            "required_action": "繰り返す場合は通知先の稼働状況、Webhook、認証情報を確認してください。",
        }
    if any(
        marker in error_text
        for marker in ("価格を取得できませんでした", "価格の発表日時を取得できませんでした", "銀製品")
    ):
        return {
            "cause": "価格ページの表示内容をBOTが読み取れませんでした。",
            "bot_action": "次回の毎分実行で自動的に再試行します。",
            "required_action": "繰り返す場合は価格ページの表示変更を確認してください。",
        }
    if "connection reset by peer" in error_text or "connection reset" in error_text:
        cause = "接続先との通信が途中で切断されました。"
    elif "timeout" in error_text or "timed out" in error_text:
        cause = "接続先からの応答が時間内に返りませんでした。"
    elif "name or service not known" in error_text or "temporary failure in name resolution" in error_text:
        cause = "接続先の名前解決に失敗しました。"
    elif "ssl" in error_text or "certificate" in error_text:
        cause = "接続先との安全な通信を確立できませんでした。"
    elif "http 401" in error_text or "http 403" in error_text:
        cause = "接続先の認証またはアクセス権限が拒否されました。"
    elif "http 429" in error_text or "quota" in error_text or "limit" in error_text:
        cause = "接続先の利用上限に達したため、要求が受け付けられませんでした。"
    elif re.search(r"http 5[0-9]{2}", error_text):
        cause = "接続先で一時的なサーバーエラーが発生しました。"
    else:
        cause = "予期しないエラーが発生しました。"
    return {
        "cause": cause,
        "bot_action": "次回の毎分実行で自動的に再試行します。",
        "required_action": "通常は対応不要です。繰り返す場合は接続先の障害状況を確認してください。",
    }


def redact_sensitive_text(value, config=None):
    text = str(value)
    if isinstance(config, dict):
        for key in (
            "line_channel_access_token",
            "line_group_id",
            "lineworks_webhook_url",
            "screenshotone_access_key",
            "alert_email",
        ):
            secret = str(config.get(key, "")).strip()
            if len(secret) >= 4:
                text = text.replace(secret, "[伏字]")
    text = re.sub(
        r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+",
        r"\1[伏字]",
        text,
    )
    text = re.sub(
        r"(?i)((?:access[_ -]?(?:token|key)|api[_ -]?key|token|secret|password)\s*[=:]\s*)[^\s,;&]+",
        r"\1[伏字]",
        text,
    )
    return text


def format_error_alert_body(body_lines, alert_key=None, config=None):
    error_text = None
    formatted_lines = []
    for line in body_lines:
        if line.startswith("エラー: "):
            error_text = redact_sensitive_text(line[len("エラー: ") :], config)
        else:
            formatted_lines.append(redact_sensitive_text(line, config))
    if error_text is None:
        return "\n".join(formatted_lines)

    classified = classify_error(error_text, alert_key)
    formatted_lines.extend(
        [
            "原因: {0}".format(classified["cause"]),
            "BOTの動作: {0}".format(classified["bot_action"]),
            "必要な対応: {0}".format(classified["required_action"]),
            "技術情報: {0}".format(error_text),
        ]
    )
    return "\n".join(formatted_lines)


def build_lineworks_fallback_body(body, lineworks_error):
    quoted_original = "\n".join(
        "> {0}".format(line) for line in body.splitlines()
    )
    return "\n".join(
        [
            "LINE WORKSへ通知できなかったためメールへ切り替えました。",
            "",
            "LINE WORKSの技術情報: {0}".format(lineworks_error),
            "",
            "以下は元の通知です。",
            quoted_original,
        ]
    )


def build_recovery_alert_body(original_body):
    quoted_original = "\n".join(
        "> {0}".format(line) for line in original_body.splitlines()
    )
    return "\n".join(
        [
            "RE:TANAKA BOT のエラーは自動復旧済み・対応不要です。",
            "",
            "以下は元のエラー通知です。",
            quoted_original,
        ]
    )


def build_recovery_alert_email(pending_alert):
    subject = "Re: {0}".format(pending_alert["subject"])
    message_id = pending_alert["message_id"]
    return build_alert_email(
        subject,
        build_recovery_alert_body(pending_alert["body"]),
        in_reply_to=message_id,
        references=message_id,
    )


def deliver_recovery_alert(config, state, pending_alert):
    pending_alert["body"] = redact_sensitive_text(pending_alert["body"], config)
    body = build_recovery_alert_body(pending_alert["body"])
    subject = "Re: {0}".format(pending_alert["subject"])
    if pending_alert.get("transport", "email") == "email":
        message_id = pending_alert["message_id"]
        return bool(
            send_alert_email(
                config,
                subject,
                body,
                in_reply_to=message_id,
                references=message_id,
            )
        )

    try:
        send_lineworks_webhook(
            config["lineworks_webhook_url"],
            build_lineworks_alert_payload(subject, body, config),
        )
        try:
            maybe_send_recovery_alert(config, state, "lineworks_delivery_error")
        except Exception as recovery_error:
            safe_error = redact_sensitive_text(recovery_error, config)
            print("LINE WORKS復旧メール送信に失敗しました: {0}".format(safe_error), file=sys.stderr)
        return True
    except Exception as lineworks_error:
        fallback_body = build_lineworks_fallback_body(
            body, redact_sensitive_text(lineworks_error, config)
        )
        message_id = send_alert_email(
            config, "RE:TANAKA BOT: LINE WORKS送信エラー", fallback_body
        )
        pending_alerts = state.get("pending_recovery_alerts")
        if not isinstance(pending_alerts, dict):
            pending_alerts = {}
            state["pending_recovery_alerts"] = pending_alerts
        if message_id and "lineworks_delivery_error" not in pending_alerts:
            pending_alerts["lineworks_delivery_error"] = {
                "transport": "email",
                "subject": "RE:TANAKA BOT: LINE WORKS送信エラー",
                "body": fallback_body,
                "message_id": message_id,
                "occurred_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        return bool(message_id)


def maybe_send_recovery_alert(config, state, alert_key):
    pending_alerts = state.get("pending_recovery_alerts")
    if not isinstance(pending_alerts, dict):
        return False
    pending_alert = pending_alerts.get(alert_key)
    if not isinstance(pending_alert, dict):
        return False

    if not deliver_recovery_alert(config, state, pending_alert):
        return False
    del pending_alerts[alert_key]
    alert_dates = state.get("error_alert_dates")
    if isinstance(alert_dates, dict):
        alert_dates.pop(alert_key, None)
    return True


def ensure_error_alert_dates(state):
    alert_dates = state.get("error_alert_dates")
    if not isinstance(alert_dates, dict):
        alert_dates = {}
        state["error_alert_dates"] = alert_dates
    return alert_dates


def was_error_alert_sent_today(state, alert_key, today_key):
    alert_dates = state.get("error_alert_dates")
    if not isinstance(alert_dates, dict):
        return False
    return alert_dates.get(alert_key) == today_key


def mark_error_alert_sent(state, alert_key, today_key):
    alert_dates = ensure_error_alert_dates(state)
    alert_dates[alert_key] = today_key


def build_lineworks_alert_payload(subject, body, config):
    payload = build_lineworks_payload(
        body, None, config.get("price_url") or DEFAULT_PRICE_URL
    )
    payload["title"] = subject
    return payload


def deliver_error_alert(config, state, alert_key, subject, body):
    body = redact_sensitive_text(body, config)
    if alert_key == "lineworks_delivery_error":
        message_id = send_alert_email(config, subject, body)
        return {"transport": "email", "message_id": message_id} if message_id else False

    try:
        send_lineworks_webhook(
            config["lineworks_webhook_url"],
            build_lineworks_alert_payload(subject, body, config),
        )
        try:
            maybe_send_recovery_alert(config, state, "lineworks_delivery_error")
        except Exception as recovery_error:
            safe_error = redact_sensitive_text(recovery_error, config)
            print("LINE WORKS復旧メール送信に失敗しました: {0}".format(safe_error), file=sys.stderr)
        return {"transport": "lineworks"}
    except Exception as lineworks_error:
        fallback_body = build_lineworks_fallback_body(
            body, redact_sensitive_text(lineworks_error, config)
        )
        message_id = send_alert_email(
            config, "RE:TANAKA BOT: LINE WORKS送信エラー", fallback_body
        )
        pending_alerts = state.get("pending_recovery_alerts")
        if not isinstance(pending_alerts, dict):
            pending_alerts = {}
            state["pending_recovery_alerts"] = pending_alerts
        if message_id and "lineworks_delivery_error" not in pending_alerts:
            pending_alerts["lineworks_delivery_error"] = {
                "transport": "email",
                "subject": "RE:TANAKA BOT: LINE WORKS送信エラー",
                "body": fallback_body,
                "message_id": message_id,
                "occurred_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        return {"transport": "email", "message_id": message_id} if message_id else False


def maybe_send_error_alert(config, state, alert_key, subject, body_lines):
    today_key = datetime.now().strftime("%Y-%m-%d")
    if was_error_alert_sent_today(state, alert_key, today_key):
        return False

    body = format_error_alert_body(body_lines, alert_key, config)
    delivery = deliver_error_alert(config, state, alert_key, subject, body)
    if not delivery:
        return False
    mark_error_alert_sent(state, alert_key, today_key)
    pending_alerts = state.get("pending_recovery_alerts")
    if not isinstance(pending_alerts, dict):
        pending_alerts = {}
        state["pending_recovery_alerts"] = pending_alerts
    pending = {
        "transport": delivery["transport"],
        "subject": subject,
        "body": body,
        "occurred_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if delivery["transport"] == "email":
        pending["message_id"] = delivery["message_id"]
    if alert_key not in pending_alerts:
        pending_alerts[alert_key] = pending
    return True


def maybe_send_test_email(config, state):
    recipient = str(config.get("alert_email", "")).strip()
    if not recipient:
        return False
    if state.get("test_email_sent_at"):
        return False

    send_alert_email(
        config,
        "RE:TANAKA BOT: テストメール",
        "\n".join(
            [
                "RE:TANAKA BOT の初回テストメールです。",
                "",
                "日時: {0}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                "このメールは初回通常実行時に1回だけ送信されます。",
            ]
        ),
    )
    state["test_email_sent_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return True


def is_line_limit_error(error):
    message = str(error).lower()
    return "http 429" in message or "quota" in message or "monthly" in message or "limit" in message


def maybe_send_delivery_error_alert(config, state, channel, error, published_at):
    if channel == "line" and is_line_limit_error(error):
        alert_key = "line_limit_error"
        subject = "RE:TANAKA BOT: LINE送信上限エラー"
    elif channel == "line":
        alert_key = "line_delivery_error"
        subject = "RE:TANAKA BOT: LINE送信エラー"
    else:
        alert_key = "lineworks_delivery_error"
        subject = "RE:TANAKA BOT: LINE WORKS送信エラー"

    return maybe_send_error_alert(
        config,
        state,
        alert_key,
        subject,
        [
            "RE:TANAKA BOT の{0}送信でエラーが発生しました。".format(
                "LINE" if channel == "line" else "LINE WORKS"
            ),
            "",
            "日時: {0}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            "対象発表時刻: {0}".format(published_at),
            "エラー: {0}".format(error),
        ],
    )


def send_lineworks_webhook(webhook_url, payload):
    request = Request(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "retanaka-lineworks-bot/1.0",
        },
        method="POST",
    )

    context = build_ssl_context()
    try:
        with urlopen(request, timeout=30, context=context):
            return
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError("LINE WORKS Webhookエラー: HTTP {0}: {1}".format(exc.code, body))
    except URLError as exc:
        raise RuntimeError("LINE WORKS Webhook接続エラー: {0}".format(exc))


def send_line_messages(token, group_id, messages):
    payload = {"to": group_id, "messages": messages}
    request = Request(
        LINE_PUSH_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer {0}".format(token),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    context = build_ssl_context()
    try:
        with urlopen(request, timeout=30, context=context):
            return
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError("LINE API エラー: HTTP {0}: {1}".format(exc.code, body))
    except URLError as exc:
        raise RuntimeError("LINE API 接続エラー: {0}".format(exc))


def build_ssl_context():
    try:
        import certifi  # optional dependency, available in many Python builds

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def capture_screenshot_if_enabled(config, snapshot):
    if not config.get("enable_section_screenshot", False):
        return None, None

    cleanup_old_screenshots(config)
    image_path, image_url = find_public_screenshot(config, snapshot.published_at)
    if image_path:
        return image_path, image_url

    image_bytes = fetch_section_screenshot_bytes(config)
    image_path, image_url = save_public_screenshot(config, image_bytes, snapshot.published_at)
    return image_path, image_url


def _run(argv, preloaded_config=None):
    args = parse_args(argv)
    alert_config = read_alert_config_only(args.config)
    state = load_state(alert_config["state_file"])

    try:
        config = preloaded_config if preloaded_config is not None else read_config(args.config)
    except Exception as exc:
        safe_error = redact_sensitive_text(exc, alert_config)
        print("設定読み込みに失敗しました: {0}".format(safe_error), file=sys.stderr)
        if not args.dry_run and not args.test_lineworks_only:
            try:
                if maybe_send_error_alert(
                    alert_config,
                    state,
                    "config_error",
                    "RE:TANAKA BOT: 設定読み込みエラー",
                    [
                        "RE:TANAKA BOT の設定読み込みでエラーが発生しました。",
                        "",
                        "日時: {0}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                        "設定ファイル: {0}".format(args.config),
                        "エラー: {0}".format(safe_error),
                    ],
                ):
                    print("設定読み込みエラー通知を送信しました", file=sys.stderr)
            except Exception as alert_exc:
                print(
                    "設定読み込みエラー通知送信に失敗しました: {0}".format(
                        redact_sensitive_text(alert_exc, alert_config)
                    ),
                    file=sys.stderr,
                )
            try:
                save_state(alert_config["state_file"], state)
            except Exception:
                pass
        return 2

    state = load_state(config["state_file"])
    if not args.dry_run and not args.test_lineworks_only:
        try:
            if maybe_send_recovery_alert(config, state, "config_error"):
                print("設定読み込みエラーからの復旧通知を送信しました", file=sys.stderr)
                save_state(config["state_file"], state)
        except Exception as recovery_exc:
            print(
                "設定読み込みエラーからの復旧通知送信に失敗しました: {0}".format(
                    redact_sensitive_text(recovery_exc, config)
                ),
                file=sys.stderr,
            )
        try:
            if maybe_send_test_email(config, state):
                print("初回テストメールを送信しました", file=sys.stderr)
                save_state(config["state_file"], state)
        except Exception as exc:
            print(
                "初回テストメール送信に失敗しました: {0}".format(
                    redact_sensitive_text(exc, config)
                ),
                file=sys.stderr,
            )

    today_key = datetime.now().strftime("%Y-%m-%d")

    try:
        html = fetch_html(config["price_url"])
        current = parse_snapshot(html)
    except Exception as exc:
        safe_error = redact_sensitive_text(exc, config)
        print("価格取得に失敗しました: {0}".format(safe_error), file=sys.stderr)
        if not args.dry_run and not args.test_lineworks_only:
            try:
                if maybe_send_error_alert(
                    config,
                    state,
                    "price_fetch_error",
                    "RE:TANAKA BOT: 価格取得エラー",
                    [
                        "RE:TANAKA BOT の価格取得でエラーが発生しました。",
                        "",
                        "日時: {0}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                        "取得元: {0}".format(config["price_url"]),
                        "エラー: {0}".format(safe_error),
                    ],
                ):
                    print("価格取得エラー通知を送信しました", file=sys.stderr)
            except Exception as alert_exc:
                print(
                    "価格取得エラー通知送信に失敗しました: {0}".format(
                        redact_sensitive_text(alert_exc, config)
                    ),
                    file=sys.stderr,
                )
            save_state(config["state_file"], state)
        return 1

    if not args.dry_run and not args.test_lineworks_only:
        try:
            if maybe_send_recovery_alert(config, state, "price_fetch_error"):
                print("価格取得エラーからの復旧通知を送信しました", file=sys.stderr)
                save_state(config["state_file"], state)
        except Exception as recovery_exc:
            print(
                "価格取得エラーからの復旧通知送信に失敗しました: {0}".format(
                    redact_sensitive_text(recovery_exc, config)
                ),
                file=sys.stderr,
            )

    previous_day = get_previous_day_snapshot(state, current.published_at)
    message = build_message(current, previous_day, config["price_url"])
    published_date_key = datetime.strptime(current.published_at, INTERNAL_TS_FORMAT).strftime("%Y-%m-%d")

    if args.test_lineworks_only:
        screenshot_url = None
        if config.get("enable_section_screenshot", False):
            try:
                _, screenshot_url = capture_screenshot_if_enabled(config, current)
            except Exception as exc:
                safe_error = redact_sensitive_text(exc, config)
                if config.get("require_screenshot", True):
                    print("LINE WORKS限定テスト用スクリーンショット取得に失敗しました: {0}".format(safe_error), file=sys.stderr)
                    return 1
                print("LINE WORKS限定テスト用スクリーンショット取得に失敗しました: {0}".format(safe_error), file=sys.stderr)
        try:
            send_lineworks_webhook(
                config["lineworks_webhook_url"],
                build_lineworks_payload(message, screenshot_url, config["price_url"]),
            )
        except Exception as exc:
            print(
                "LINE WORKS限定テスト送信に失敗しました: {0}".format(
                    redact_sensitive_text(exc, config)
                ),
                file=sys.stderr,
            )
            return 1
        print("LINE WORKS限定テスト送信完了")
        return 0

    if not args.force_send:
        if state.get("last_observed_published_at") is None:
            if not args.dry_run:
                upsert_history(state, current)
                state["last_observed_published_at"] = current.published_at
                state["deliveries"]["lineworks"]["last_sent_published_at"] = current.published_at
                if published_date_key == today_key and config.get("line_channel_access_token"):
                    state["deliveries"]["line"]["last_sent_published_at"] = current.published_at
                    state["deliveries"]["line"]["last_sent_date"] = today_key
                save_state(config["state_file"], state)
                print("初回観測のため現在の発表を送信せず登録しました")
                return 0
        elif current.published_at < state["last_observed_published_at"]:
            print("前回観測より古い発表のため送信をスキップしました")
            return 0

    observation_is_new = args.force_send or is_new_observation(state, current)
    observation_is_equal = is_equal_observation(state, current)
    if observation_is_new and not args.dry_run:
        upsert_history(state, current)
        state["last_observed_published_at"] = current.published_at
        save_state(config["state_file"], state)

    send_line = bool(config.get("line_channel_access_token")) and (
        args.force_send or (observation_is_new or observation_is_equal) and should_deliver_line(state, current, today_key)
    )
    send_lineworks = args.force_send or (
        (observation_is_new or observation_is_equal) and should_deliver_lineworks(state, current)
    )

    screenshot_url = None
    screenshot_path = None
    screenshot_error = None

    if args.dry_run:
        print(message)
        if not send_line:
            print("通常実行時はLINE送信をスキップします")
        if not send_lineworks:
            print("通常実行時はLINE WORKS送信をスキップします")
        if config.get("enable_section_screenshot", False):
            try:
                screenshot_path, screenshot_url = capture_screenshot_if_enabled(config, current)
                print("スクリーンショットURL: {0}".format(screenshot_url))
            except Exception as exc:
                screenshot_error = redact_sensitive_text(exc, config)
                print("スクリーンショット取得に失敗: {0}".format(screenshot_error), file=sys.stderr)
                print(
                    "画像なし理由: {0}".format(summarize_screenshot_error_for_user(screenshot_error)),
                    file=sys.stderr,
                )

        if screenshot_error and config.get("require_screenshot", True):
            return 1
        return 0

    if not send_line and not send_lineworks:
        print("送信対象の新しい発表ではないため送信をスキップしました")
        save_state(config["state_file"], state)
        return 0

    if config.get("enable_section_screenshot", False):
        try:
            screenshot_path, screenshot_url = capture_screenshot_if_enabled(config, current)
        except Exception as exc:
            screenshot_error = redact_sensitive_text(exc, config)
            if config.get("require_screenshot", True):
                print("スクリーンショット取得に失敗しました: {0}".format(screenshot_error), file=sys.stderr)
                try:
                    if maybe_send_error_alert(
                        config,
                        state,
                        "screenshot_error",
                        "RE:TANAKA BOT: スクリーンショット取得エラー",
                        [
                            "RE:TANAKA BOT のスクリーンショット取得でエラーが発生しました。",
                            "",
                            "日時: {0}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                            "対象発表時刻: {0}".format(current.published_at),
                            "エラー: {0}".format(screenshot_error),
                        ],
                    ):
                        print("スクリーンショット取得エラー通知を送信しました", file=sys.stderr)
                except Exception as alert_exc:
                    print(
                        "スクリーンショット取得エラー通知送信に失敗しました: {0}".format(
                            redact_sensitive_text(alert_exc, config)
                        ),
                        file=sys.stderr,
                    )
                save_state(config["state_file"], state)
                return 1

        if screenshot_error is None:
            try:
                if maybe_send_recovery_alert(config, state, "screenshot_error"):
                    print("スクリーンショット取得エラーからの復旧通知を送信しました", file=sys.stderr)
                    save_state(config["state_file"], state)
            except Exception as recovery_exc:
                print(
                    "スクリーンショット取得エラーからの復旧通知送信に失敗しました: {0}".format(
                        redact_sensitive_text(recovery_exc, config)
                    ),
                    file=sys.stderr,
                )

    if screenshot_error:
        message = (
            message
            + "\n※画像なし: ScreenshotOne障害のため、テキストのみ送信しました。"
            + "\n※理由: {0}".format(summarize_screenshot_error_for_user(screenshot_error))
        )
        try:
            if maybe_send_error_alert(
                config,
                state,
                "screenshot_error",
                "RE:TANAKA BOT: スクリーンショット取得エラー",
                [
                    "RE:TANAKA BOT のスクリーンショット取得でエラーが発生しました。",
                    "",
                    "日時: {0}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                    "対象発表時刻: {0}".format(current.published_at),
                    "エラー: {0}".format(screenshot_error),
                ],
            ):
                print("スクリーンショット取得エラー通知を送信しました", file=sys.stderr)
        except Exception as alert_exc:
            print(
                "スクリーンショット取得エラー通知送信に失敗しました: {0}".format(
                    redact_sensitive_text(alert_exc, config)
                ),
                file=sys.stderr,
            )

    delivery_failed = False
    if send_line:
        try:
            send_line_messages(
                config["line_channel_access_token"],
                config["line_group_id"],
                build_line_messages(message, screenshot_url),
            )
        except Exception as exc:
            delivery_failed = True
            safe_error = redact_sensitive_text(exc, config)
            print("LINE送信に失敗しました: {0}".format(safe_error), file=sys.stderr)
            try:
                if maybe_send_delivery_error_alert(config, state, "line", safe_error, current.published_at):
                    save_state(config["state_file"], state)
            except Exception as alert_exc:
                print(
                    "LINE送信エラー通知送信に失敗しました: {0}".format(
                        redact_sensitive_text(alert_exc, config)
                    ),
                    file=sys.stderr,
                )
        else:
            state["deliveries"]["line"]["last_sent_published_at"] = current.published_at
            state["deliveries"]["line"]["last_sent_date"] = published_date_key
            try:
                recovered = maybe_send_recovery_alert(config, state, "line_delivery_error")
                limit_recovered = maybe_send_recovery_alert(config, state, "line_limit_error")
                if recovered or limit_recovered:
                    print("LINE送信エラーからの復旧通知を送信しました", file=sys.stderr)
            except Exception as recovery_exc:
                print(
                    "LINE送信エラーからの復旧通知送信に失敗しました: {0}".format(
                        redact_sensitive_text(recovery_exc, config)
                    ),
                    file=sys.stderr,
                )
            save_state(config["state_file"], state)

    if send_lineworks:
        try:
            send_lineworks_webhook(
                config["lineworks_webhook_url"],
                build_lineworks_payload(message, screenshot_url, config["price_url"]),
            )
        except Exception as exc:
            delivery_failed = True
            safe_error = redact_sensitive_text(exc, config)
            print("LINE WORKS送信に失敗しました: {0}".format(safe_error), file=sys.stderr)
            try:
                if maybe_send_delivery_error_alert(config, state, "lineworks", safe_error, current.published_at):
                    save_state(config["state_file"], state)
            except Exception as alert_exc:
                print(
                    "LINE WORKS送信エラー通知メール送信に失敗しました: {0}".format(
                        redact_sensitive_text(alert_exc, config)
                    ),
                    file=sys.stderr,
                )
        else:
            state["deliveries"]["lineworks"]["last_sent_published_at"] = current.published_at
            try:
                if maybe_send_recovery_alert(config, state, "lineworks_delivery_error"):
                    print("LINE WORKS送信エラーからの復旧通知メールを送信しました", file=sys.stderr)
            except Exception as recovery_exc:
                print(
                    "LINE WORKS送信エラーからの復旧通知メール送信に失敗しました: {0}".format(
                        redact_sensitive_text(recovery_exc, config)
                    ),
                    file=sys.stderr,
                )
            save_state(config["state_file"], state)

    if delivery_failed:
        return 1

    if screenshot_url:
        print("送信完了(画像リンク+テキスト): {0}".format(format_published_at_display(current.published_at)))
    else:
        print("送信完了(テキスト): {0}".format(format_published_at_display(current.published_at)))
    return 0


def main(argv):
    args = parse_args(argv)
    try:
        config = read_config(args.config)
    except Exception:
        return _run(argv)

    with acquire_process_lock(config["lock_file"]) as acquired:
        if not acquired:
            print("別プロセスが実行中のためスキップしました")
            return 0
        return _run(argv, preloaded_config=config)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
