#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Safely save an XServer Server API key for local automation.

The key is read with hidden terminal input, optionally verified via GET /v1/me,
and stored under the project-local .secrets directory with 0600 permissions.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import ssl
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_BASE_URL = "https://api.xserver.ne.jp"
ME_ENDPOINT = API_BASE_URL + "/v1/me"
PROJECT_DIR = Path(__file__).resolve().parents[1]
SECRETS_DIR = PROJECT_DIR / ".secrets"
DEFAULT_KEY_PATH = SECRETS_DIR / "xserver_api_key"
DEFAULT_META_PATH = SECRETS_DIR / "xserver_api_key.meta.json"
CA_BUNDLE_CANDIDATES = (
    Path("/etc/ssl/cert.pem"),
    Path("/opt/homebrew/etc/openssl@3/cert.pem"),
    Path("/opt/homebrew/etc/ca-certificates/cert.pem"),
)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="APIキーを /v1/me で検証せず保存する",
    )
    parser.add_argument(
        "--allow-read-only",
        action="store_true",
        help="permission_type=read のAPIキーも保存する",
    )
    parser.add_argument(
        "--expected-servername",
        default="",
        help="検証結果の servername がこの値と一致することを要求する",
    )
    parser.add_argument(
        "--key-path",
        default=str(DEFAULT_KEY_PATH),
        help="APIキー保存先。既定は .secrets/xserver_api_key",
    )
    parser.add_argument(
        "--meta-path",
        default=str(DEFAULT_META_PATH),
        help="検証メタデータ保存先。既定は .secrets/xserver_api_key.meta.json",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="保存済みAPIキーとメタデータを削除する",
    )
    return parser.parse_args(argv)


def ensure_private_parent(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)


def write_private_text(path: Path, value: str) -> None:
    ensure_private_parent(path)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    os.replace(tmp_path, path)
    os.chmod(path, 0o600)


def delete_if_exists(path: Path) -> None:
    try:
        path.unlink()
        print("deleted: {0}".format(path))
    except FileNotFoundError:
        print("not found: {0}".format(path))


def prompt_api_key(max_attempts: int = 3) -> str:
    for attempt in range(1, max_attempts + 1):
        first = getpass.getpass("XServer API key: ").strip()
        if not first:
            raise RuntimeError("empty API key")
        second = getpass.getpass("Confirm XServer API key: ").strip()
        if first == second:
            return first

        remaining = max_attempts - attempt
        if remaining:
            print("API keys do not match. Please try again. ({0} attempt(s) left)".format(remaining), file=sys.stderr)

    raise RuntimeError("API keys do not match")


def build_ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass

    for candidate in CA_BUNDLE_CANDIDATES:
        if candidate.exists():
            return ssl.create_default_context(cafile=str(candidate))

    return ssl.create_default_context()


def verify_api_key(api_key: str) -> dict[str, Any]:
    request = Request(
        ME_ENDPOINT,
        headers={
            "Authorization": "Bearer {0}".format(api_key),
            "Accept": "application/json",
            "User-Agent": "retanaka-line-bot-xserver-api-key-store/1.0",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=30, context=build_ssl_context()) as response:
            body = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError("XServer API verification failed: HTTP {0}: {1}".format(exc.code, body)) from exc
    except URLError as exc:
        raise RuntimeError("XServer API verification connection error: {0}".format(exc)) from exc

    try:
        data = json.loads(body)
    except ValueError as exc:
        raise RuntimeError("XServer API verification returned invalid JSON: {0}".format(exc)) from exc
    if not isinstance(data, dict):
        raise RuntimeError("XServer API verification response is not an object")
    return data


def build_metadata(api_key: str, verification: dict[str, Any] | None, verified: bool) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "api_base_url": API_BASE_URL,
        "verification_endpoint": ME_ENDPOINT,
        "verified": verified,
        "key_sha256_16": hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16],
    }
    if verification:
        metadata.update(
            {
                "service_type": verification.get("service_type"),
                "expires_at": verification.get("expires_at"),
                "servername": verification.get("servername"),
                "permission_type": verification.get("permission_type"),
            }
        )
    return metadata


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    key_path = Path(args.key_path).expanduser().resolve()
    meta_path = Path(args.meta_path).expanduser().resolve()

    if args.delete:
        delete_if_exists(key_path)
        delete_if_exists(meta_path)
        return 0

    api_key = prompt_api_key()
    verification: dict[str, Any] | None = None
    verified = False

    if not args.no_verify:
        verification = verify_api_key(api_key)
        verified = True

        permission_type = str(verification.get("permission_type", "")).strip().lower()
        if permission_type == "read" and not args.allow_read_only:
            raise RuntimeError("permission_type=read のため保存しません。設定変更用キーを指定してください。")

        expected = str(args.expected_servername).strip()
        actual = str(verification.get("servername", "")).strip()
        if expected and actual != expected:
            raise RuntimeError("servername mismatch: expected={0}, actual={1}".format(expected, actual))

    write_private_text(key_path, api_key + "\n")
    metadata = build_metadata(api_key, verification, verified)
    write_private_text(meta_path, json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")

    print("saved key: {0}".format(key_path))
    print("saved metadata: {0}".format(meta_path))
    print("permissions: key=600, metadata=600, directory=700")
    if metadata.get("permission_type"):
        print("permission_type: {0}".format(metadata["permission_type"]))
    if metadata.get("servername"):
        print("servername: {0}".format(metadata["servername"]))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print("error: {0}".format(exc), file=sys.stderr)
        raise SystemExit(1)
