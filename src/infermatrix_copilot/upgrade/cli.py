"""Model-free upgrade controls, usable even when an application cannot start."""

from __future__ import annotations

import argparse
import json
import sys

from .manager import UpdateManager
from .storage import UpgradeError


def main(argv=None, *, root=None) -> int:
    parser = argparse.ArgumentParser(prog="infermatrix-copilot upgrade")
    parser.add_argument("action", nargs="?", choices=("status", "rollback", "pin", "unpin", "check", "now"), default="now")
    parser.add_argument("version", nargs="?")
    parser.add_argument("--version", dest="target_version", default="")
    parser.add_argument("--check", action="store_true", help="check availability without installing")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--channel", choices=("stable", "preview"))
    parser.add_argument("--auto", choices=("on", "off"))
    args = parser.parse_args(argv)
    manager = UpdateManager(root)
    try:
        values = {}
        if args.channel:
            values["channel"] = args.channel
        if args.auto:
            values["auto_update"] = args.auto == "on"
        if values:
            manager.configure(**values)
        if args.action == "status" or (values and args.action == "now" and not args.check):
            result = manager.status()
        elif args.action == "rollback":
            result = manager.rollback()
        elif args.action == "pin":
            if not args.version:
                parser.error("pin requires VERSION")
            result = manager.pin(args.version)
        elif args.action == "unpin":
            manager.configure(pinned_version="")
            result = {"state": "unpinned"}
        else:
            result = manager.check(install=not (args.check or args.action == "check"), version=args.target_version)
        if args.json:
            print(json.dumps(result, sort_keys=True))
        else:
            for key, value in result.items():
                print(f"{key}: {json.dumps(value) if isinstance(value, (dict, list)) else value}")
        return 0
    except (UpgradeError, OSError) as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"Upgrade failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
