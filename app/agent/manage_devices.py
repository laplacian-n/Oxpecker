"""CLI for managing memory-service device credentials — deliberately out-of-band from the
HTTP API itself (see memory_service/app.py docstring for why)."""
from __future__ import annotations

import argparse
import sys

from .memory_service.devices import DeviceStore


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage memory-service device credentials")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="register a new device, prints its token once")
    p_add.add_argument("device_id")

    p_rotate = sub.add_parser("rotate", help="issue a new token for an existing device id")
    p_rotate.add_argument("device_id")

    p_revoke = sub.add_parser("revoke", help="revoke a device's access")
    p_revoke.add_argument("device_id")

    sub.add_parser("list", help="list all known devices (no tokens shown)")

    args = parser.parse_args()
    store = DeviceStore()

    if args.command == "add":
        token = store.add(args.device_id)
        print(f"device_id: {args.device_id}")
        print(f"token:     {token}")
        print("(shown once — store it now; the server only keeps a hash)")
    elif args.command == "rotate":
        token = store.rotate(args.device_id)
        print(f"device_id: {args.device_id}")
        print(f"new token: {token}")
        print("(old token is now invalid)")
    elif args.command == "revoke":
        ok = store.revoke(args.device_id)
        print("revoked" if ok else f"unknown device_id: {args.device_id}")
        return 0 if ok else 1
    elif args.command == "list":
        for d in store.list_devices():
            status = "REVOKED" if d["revoked"] else "active"
            print(f"{d['device_id']:20s} {status:8s} created={d['created_at']:.0f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
