#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Prompt for a new ScreenshotOne API key and update config.json safely."""

from __future__ import print_function

import argparse
import getpass
import json
import os
import sys
from pathlib import Path


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parent / "config.json"),
        help="config JSON path (default: ./config.json)",
    )
    return parser.parse_args(argv)


def mask(value):
    if not value:
        return "<empty>"
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "..." + value[-4:]


def main(argv):
    args = parse_args(argv)
    config_path = Path(args.config).expanduser().resolve()

    if not config_path.exists():
        print("config file not found: {0}".format(config_path), file=sys.stderr)
        return 1

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        print("invalid JSON config: {0}".format(exc), file=sys.stderr)
        return 1

    if not isinstance(config, dict):
        print("config root must be object", file=sys.stderr)
        return 1

    current_value = str(config.get("screenshotone_access_key", "")).strip()
    print("config: {0}".format(config_path))
    print("current screenshotone_access_key: {0}".format(mask(current_value)))

    new_value = getpass.getpass("new ScreenshotOne access key: ").strip()
    if not new_value:
        print("empty key is not allowed", file=sys.stderr)
        return 1

    confirm_value = getpass.getpass("confirm new key: ").strip()
    if new_value != confirm_value:
        print("confirmation does not match", file=sys.stderr)
        return 1

    config["screenshotone_access_key"] = new_value
    temp_path = Path(str(config_path) + ".tmp")
    temp_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(str(temp_path), str(config_path))
    os.chmod(str(config_path), 0o600)

    print("updated screenshotone_access_key: {0}".format(mask(new_value)))
    print("config permission: 600")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
