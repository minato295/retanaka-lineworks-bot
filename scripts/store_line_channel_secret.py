#!/usr/bin/env python3
"""Store only the LINE channel secret; never send a message or contact a server."""

import getpass
import re
import subprocess


def main():
    secret = getpass.getpass("LINE channel secret (hidden): ").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{32}", secret):
        raise SystemExit("Channel secret must contain 32 hexadecimal characters.")
    command = ('add-generic-password -U -a retanaka-bot '
               '-s "RE:TANAKA LINE channel secret" -w ' + secret + '\n')
    result = subprocess.run(
        ["/usr/bin/security", "-i"], input=command, text=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if result.returncode:
        raise SystemExit("Keychain save failed.")
    check = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-a", "retanaka-bot",
         "-s", "RE:TANAKA LINE channel secret"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if check.returncode:
        raise SystemExit("Keychain verification failed.")
    print("LINE channel secret saved to macOS Keychain. No messages were sent.")


if __name__ == "__main__":
    main()
