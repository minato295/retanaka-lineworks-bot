#!/usr/bin/env python3
"""Prompt for LINE credentials and store them in macOS Keychain."""

import getpass
import re
import subprocess
import sys


ACCOUNT = "retanaka-bot"
TOKEN_SERVICE = "RE:TANAKA LINE channel access token"
GROUP_SERVICE = "RE:TANAKA LINE group ID"
SECRET_SERVICE = "RE:TANAKA LINE channel secret"


def store(service, value):
    subprocess.run(
        [
            "/usr/bin/security",
            "add-generic-password",
            "-U",
            "-a",
            ACCOUNT,
            "-s",
            service,
            "-w",
            value,
        ],
        check=True,
    )


def store_setup():
    token = getpass.getpass("LINE channel access token: ").strip()
    channel_secret = getpass.getpass("LINE channel secret: ").strip()
    if not token:
        raise ValueError("LINE channel access token is empty")
    if not channel_secret:
        raise ValueError("LINE channel secret is empty")

    store(TOKEN_SERVICE, token)
    store(SECRET_SERVICE, channel_secret)
    print("LINE token and channel secret stored in macOS Keychain.")


def store_group():
    group_id = getpass.getpass("LINE group ID: ").strip()
    if not re.fullmatch(r"C[0-9a-fA-F]{32}", group_id):
        raise ValueError("LINE group ID must be C followed by 32 hexadecimal characters")

    store(GROUP_SERVICE, group_id)
    print("LINE group ID stored in macOS Keychain.")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["setup"]:
        store_setup()
        return
    if argv == ["group"]:
        store_group()
        return
    raise SystemExit("usage: store_line_credentials_in_keychain.py setup|group")


if __name__ == "__main__":
    main()
