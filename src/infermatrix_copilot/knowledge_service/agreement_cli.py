"""Interactive named acknowledgement; never accept from flags or stdin pipes."""
from __future__ import annotations

import argparse
import json
import sys

from .agreement import AgreementRequiredError, AgreementStore


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "Invalid agreement command; use --help for accepted arguments.\n")


def _error(code, message):
    print(json.dumps({"error": {"code": code, "message": message}}))


def _show(document):
    print(document["text"])
    print("Agreement version:", document["version"])
    print("Agreement SHA256:", document["sha256"])
    print("Configured destinations:")
    print(json.dumps(document["context"], ensure_ascii=False, indent=2, sort_keys=True))
    print("Configuration SHA256:", document["context_sha256"])


def main(argv=None):
    parser = _Parser(prog="infermatrix-copilot learning agreement")
    parser.add_argument("command", choices=("show", "status", "accept", "revoke"), nargs="?", default="show")
    args = parser.parse_args(argv)
    try:
        store = AgreementStore.from_env()
        if args.command == "status":
            print(json.dumps(store.status(), ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "revoke":
            print(json.dumps(store.revoke(), ensure_ascii=False, sort_keys=True))
            return 0
        document = store.document()
        if args.command == "show":
            _show(document)
            return 0
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            _error("interactive_required", "Agreement was not accepted. Use an interactive terminal; piped or automated acceptance is unavailable.")
            return 2
        _show(document)
        signer = input("Enter your name to acknowledge these disclosures: ")
        confirmation = input("Type exactly I AGREE to enable optional learning and feedback: ")
        if not signer.strip() or confirmation != "I AGREE":
            _error("agreement_not_accepted", "Agreement was not accepted; learning and feedback remain disabled.")
            return 2
        # Resolve again after prompts. A destination or terms change while the
        # person was reading cannot authorize an unseen configuration.
        current = AgreementStore.from_env()
        if current.document() != document:
            _error("agreement_changed", "Agreement was not accepted because its terms or destinations changed; read them again.")
            return 2
        result = current._accept(signer, version=document["version"], agreement_sha256=document["sha256"],
                                 context_sha256=document["context_sha256"], confirmation=confirmation)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["status"] == "accepted" else 2
    except (EOFError, KeyboardInterrupt):
        _error("agreement_not_accepted", "Agreement was not accepted; learning and feedback remain disabled.")
        return 2
    except (AgreementRequiredError, OSError, ValueError, TypeError):
        _error("agreement_unavailable", "Agreement operation could not complete; inspect agreement status before retrying.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
