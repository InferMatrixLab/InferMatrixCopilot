"""Standalone shared feedback service, without model or knowledge dependencies."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="infermatrix-feedback", description="Serve the shared trusted-provider knowledge feedback ledger")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--state-dir", default=os.environ.get("KNOWLEDGE_FEEDBACK_SERVER_STATE_DIR", ""))
    parser.add_argument("--public-url", help="HTTPS reverse proxy origin; required for non-loopback bindings")
    args = parser.parse_args(argv)
    from .feedback_http import create_server
    from .feedback_hub import FeedbackHubStore

    server = None
    try:
        state = Path(args.state_dir).expanduser() if args.state_dir else Path.home() / ".infermatrix-copilot" / "feedback-server"
        store = FeedbackHubStore(state)
        server = create_server(store, token=os.environ.get("KNOWLEDGE_FEEDBACK_SERVER_TOKEN", ""),
                               host=args.host, port=args.port, public_url=args.public_url)
        # Fail startup on storage errors, before reporting readiness.
        store.report(limit=1)
        print(json.dumps({"service": "knowledge-feedback/1", "host": args.host,
                          "port": server.server_address[1]}), flush=True)
        server.serve_forever(poll_interval=0.25)
        return 0
    except KeyboardInterrupt:
        return 0
    except (ValueError, OSError, sqlite3.Error):
        # Configuration errors never include environment values or credentials.
        print(json.dumps({"error": {"code": "startup_failed", "message": "Check feedback token, listener and state configuration"}}), file=sys.stderr)
        return 2
    finally:
        if server is not None:
            server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
