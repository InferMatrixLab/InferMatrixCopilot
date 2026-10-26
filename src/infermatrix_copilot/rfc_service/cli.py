"""Portable RFC commands, shared by standalone and Copilot entry points."""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .models import RFCError


def _data(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("JSON payload must be an object")
    return value


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="infermatrix-rfc", description="Create and track RFCs locally or through an RFC service")
    parser.add_argument("--state-dir", default=os.environ.get("RFC_STATE_DIR"))
    parser.add_argument("--config", default=os.environ.get("RFC_CONFIG"), help="Local provider JSON configuration")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--service-url", default=os.environ.get("RFC_SERVICE_URL", ""))
    target.add_argument("--local", action="store_true", help="Explicitly use local state even when RFC_SERVICE_URL is set")
    parser.add_argument("--token-env", default="RFC_TOKEN", help="Environment variable containing your RFC user token")
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("bootstrap-admin", help="Create the first administrator and print its token once")
    bootstrap.add_argument("--name", default="Administrator")
    serve = commands.add_parser("serve", help="Run the local HTTP UI and background reconciler")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8765, type=int)
    serve.add_argument("--public-url")
    serve.add_argument("--no-worker", action="store_true")
    serve.add_argument("--interval", type=float, default=30)
    commands.add_parser("capabilities")
    request = commands.add_parser("request", help="Invoke a versioned RFC action")
    request.add_argument("action")
    request.add_argument("--data", help="JSON payload file; '-' reads stdin")
    draft = commands.add_parser("draft", help="Validate and store a structured draft without publishing")
    draft.add_argument("--data", required=True)
    imp = commands.add_parser("import", help="Store an existing Markdown RFC as a draft")
    imp.add_argument("--file", required=True)
    imp.add_argument("--repo", required=True, dest="repo_id")
    imp.add_argument("--title")
    imp.add_argument("--data", help="Optional extracted feature/criterion metadata JSON")
    imp.add_argument("--source", help="Optional SourceRef JSON file")
    listing = commands.add_parser("list")
    listing.add_argument("--repo", dest="repo_id", default="")
    listing.add_argument("--query", default="")
    listing.add_argument("--state", default="")
    listing.add_argument("--enrolled", choices=("true", "false"))
    listing.add_argument("--offset", type=int)
    listing.add_argument("--limit", type=int)
    commands.add_parser("service-status", help="Read worker and provider availability")
    commands.add_parser("diagnostics", help="Read administrator operational diagnostics")
    grants = commands.add_parser("grants", help="Read administrator repository and RFC access summaries")
    grants.add_argument("--repo", dest="repo_id", default="")
    check = commands.add_parser("check-repository", help="Verify configured repository access without writing the source")
    check.add_argument("repo_id")
    for name in ("status", "next", "enroll", "decision", "work", "export"):
        command = commands.add_parser(name)
        command.add_argument("rfc_id")
        if name in {"enroll", "decision", "work"}:
            command.add_argument("--data")
        if name == "export":
            command.add_argument("--format", default="json", choices=("json", "markdown"))
            command.add_argument("--out")
    publish = commands.add_parser("publish", help="Queue publication of the exact previewed draft")
    publish.add_argument("rfc_id")
    publish.add_argument("--content-digest", required=True)
    publish.add_argument("--idempotency-key", required=True)
    publish.add_argument("--expected-revision", help="Require the reviewed tracking revision to remain current")
    publish.add_argument("--post", action="store_true", help="Explicitly authorize outward publication")
    publish.add_argument("--data", help="Additional source publication options JSON")
    sync = commands.add_parser("sync", help="Queue an RFC refresh or run the local reconciler")
    sync.add_argument("--rfc", dest="rfc_id")
    mode = sync.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--watch", action="store_true")
    sync.add_argument("--interval", type=float, default=30)
    sync.add_argument("--limit", type=int, default=10)
    chat_worker = commands.add_parser("chat-worker", help="Execute authorized chat jobs in the local workspace")
    chat_mode = chat_worker.add_mutually_exclusive_group()
    chat_mode.add_argument("--once", action="store_true")
    chat_mode.add_argument("--watch", action="store_true")
    chat_worker.add_argument("--interval", type=float, default=1)
    chat_worker.add_argument("--limit", type=int, default=1)
    operation = commands.add_parser("operation")
    operation.add_argument("operation_id")
    operations = commands.add_parser("operations")
    operations.add_argument("--data")
    migration = commands.add_parser("migrate-personal-agent", help="Preview or apply an authenticated legacy tracking import")
    migration.add_argument("--source-dir", required=True)
    migration.add_argument("--repo-id", required=True)
    migration.add_argument("--rfc-id", required=True)
    migration.add_argument("--apply", action="store_true", help="Apply the reviewed import; default is comparison only")
    return parser


def _run_cycle(service, limit: int) -> dict[str, Any]:
    return {"sync": service.sync_due(), "processed": service.process_pending(limit=limit)}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    from .client import RFCClient, RFCClientError

    service_url = "" if args.local else args.service_url
    state = Path(args.state_dir).expanduser() if args.state_dir else Path.home() / ".infermatrix-copilot" / "rfc"
    try:
        command = args.command
        if command in {"bootstrap-admin", "serve", "chat-worker"} or (command == "sync" and not args.rfc_id):
            if service_url:
                raise RFCClientError("This command requires local state; use --local explicitly")
            from .application import build_service

            service = build_service(state, config_path=args.config)
            if command == "bootstrap-admin":
                # Deliberately the only unauthenticated user-management command.
                # The service refuses a second bootstrap, and stores token hashes.
                _print(service.bootstrap_admin(args.name))
                return 0
            if args.interval <= 0:
                raise ValueError("interval must be positive")
            if command == "chat-worker":
                if args.limit < 1:
                    raise ValueError("limit must be positive")
                if not service.chat.enabled:
                    raise RFCClientError("Chat is not enabled in this execution workspace", code="chat_disabled")
                while True:
                    _print({"chat": [service.chat.process() for _ in range(args.limit)]})
                    if not args.watch:
                        return 0
                    time.sleep(args.interval)
            if command == "sync":
                if args.limit < 1:
                    raise ValueError("limit must be positive")
                while True:
                    _print(_run_cycle(service, args.limit))
                    if not args.watch:
                        return 0
                    time.sleep(args.interval)
            from .http import create_server

            server = create_server(service, host=args.host, port=args.port, public_url=args.public_url)
            stop = threading.Event()
            from .runtime import WorkerMonitor, wait_for_work
            monitor = WorkerMonitor(service, stop)
            def worker():
                next_scan = 0
                while not stop.is_set():
                    service.store.work_available.clear()
                    monitor.state("operations", busy=True)
                    failed = False
                    try:
                        if time.monotonic() >= next_scan:
                            service.sync_due()
                            next_scan = time.monotonic() + min(30, args.interval)
                        service.process_pending(limit=10)
                    except Exception:
                        failed = True
                        print("RFC reconciliation failed; inspect the operation and audit history", file=sys.stderr)
                    finally:
                        monitor.state("operations", error=failed)
                    wait_for_work(service, stop, args.interval)
            thread = None
            translation_thread = None
            chat_threads = []
            if not args.no_worker:
                monitor.register("operations", "operations")
                if service.translations.enabled:
                    monitor.register("translations", "translations")
                if service.chat.enabled:
                    for index in range(2):
                        monitor.register("chat", f"chat-{index}")
                monitor.start()
                thread = threading.Thread(target=worker, name="rfc-operations", daemon=True)
                thread.start()
                if service.translations.enabled:
                    def translate_worker():
                        next_reconcile = 0
                        while not stop.is_set():
                            monitor.state("translations", busy=True)
                            failed = False
                            try:
                                if time.monotonic() >= next_reconcile:
                                    service.translations.reconcile()
                                    next_reconcile = time.monotonic() + 30
                                service.translations.process()
                            except Exception:
                                failed = True
                                print("RFC translation unavailable; source views remain accessible", file=sys.stderr)
                            finally:
                                monitor.state("translations", error=failed)
                            stop.wait(1)
                    translation_thread = threading.Thread(target=translate_worker, name="rfc-translations", daemon=True)
                    translation_thread.start()
                if service.chat.enabled:
                    def chat_worker_loop(index):
                        name = f"chat-{index}"
                        while not stop.is_set():
                            monitor.state(name, busy=True)
                            failed = False
                            try:
                                service.chat.process()
                            except Exception:
                                failed = True
                                print("RFC chat unavailable; inspect the authenticated conversation status", file=sys.stderr)
                            finally:
                                monitor.state(name, error=failed)
                            stop.wait(1)
                    for index in range(2):
                        chat_thread = threading.Thread(target=chat_worker_loop, args=(index,), name=f"rfc-chat-{index}", daemon=True)
                        chat_thread.start()
                        chat_threads.append(chat_thread)
            print(f"RFC service: {server.public_url}", flush=True)
            try:
                server.serve_forever()
            finally:
                stop.set()
                server.server_close()
                if thread:
                    thread.join(timeout=2)
                if translation_thread:
                    translation_thread.join(timeout=2)
                for chat_thread in chat_threads:
                    chat_thread.join(timeout=2)
                monitor.join()
            return 0
        client = RFCClient(state_dir=state, service_url=service_url,
                           token=os.environ.get(args.token_env, ""), config_path=args.config)
        if command == "request":
            result = client.dispatch(args.action, _data(args.data))
        elif command == "migrate-personal-agent":
            if service_url:
                raise RFCClientError("Migration requires local state; use --local explicitly")
            from .application import build_service
            from .migration import migrate

            service = build_service(state, config_path=args.config)
            result = migrate(service, service.authenticate(os.environ.get(args.token_env, "")),
                             args.source_dir, args.repo_id, args.rfc_id, apply=args.apply)
        elif command == "capabilities":
            result = client.capabilities()
        elif command == "draft":
            result = client.draft(_data(args.data))
        elif command == "import":
            path = Path(args.file)
            with path.open(encoding="utf-8", newline="") as source_file:
                body = source_file.read()
            heading = next((line[2:].strip() for line in body.splitlines() if line.startswith("# ")), path.stem)
            payload = {**_data(args.data), "repo_id": args.repo_id, "title": args.title or heading, "body": body}
            if args.source:
                payload["source"] = _data(args.source)
            result = client.draft(payload)
        elif command == "list":
            payload = {"repo_id": args.repo_id, "query": args.query, "state": args.state, "view": "summary"}
            for key in ("enrolled", "offset", "limit"):
                if getattr(args, key) is not None:
                    payload[key] = getattr(args, key)
            result = client.dispatch("rfcs.list", payload)
        elif command == "service-status":
            result = client.dispatch("service.status")
        elif command == "diagnostics":
            result = client.dispatch("service.diagnostics")
        elif command == "grants":
            result = client.dispatch("grants.list", {"repo_id": args.repo_id})
        elif command == "check-repository":
            result = client.dispatch("repositories.check", {"repo_id": args.repo_id})
        elif command == "status":
            result = client.status(args.rfc_id)
        elif command == "next":
            value = client.status(args.rfc_id)
            result = {"rfc_id": args.rfc_id, "next_actions": value.get("next_actions", [])}
        elif command in {"enroll", "decision", "work"}:
            result = client.dispatch("rfcs." + command, {**_data(args.data), "rfc_id": args.rfc_id})
        elif command == "publish":
            if not args.post:
                raise RFCClientError("Publication requires --post after reviewing the draft")
            payload = {**_data(args.data), "rfc_id": args.rfc_id,
                "content_digest": args.content_digest, "idempotency_key": args.idempotency_key, "post": True}
            if args.expected_revision is not None:
                payload["expected_revision"] = args.expected_revision
            result = client.dispatch("rfcs.publish", payload)
        elif command == "sync":
            if args.watch:
                raise RFCClientError("--watch reconciles local state; use sync --rfc for a queued refresh")
            result = client.sync(args.rfc_id)
        elif command == "operation":
            result = client.operation(args.operation_id)
        elif command == "operations":
            result = client.dispatch("operations.list", _data(args.data))
        else:  # export
            result = client.export(args.rfc_id, args.format)
            content = result.get("content", result.get("markdown")) if args.format == "markdown" else json.dumps(result, ensure_ascii=False, indent=2)
            if not isinstance(content, str):
                raise RFCClientError("RFC export did not return Markdown", code="invalid_response")
            if args.out:
                Path(args.out).write_text(content, encoding="utf-8", newline="")
                return 0
            if args.format == "markdown":
                print(content)
                return 0
        _print(result)
        return 0
    except (RFCError, RFCClientError, ValueError, OSError) as exc:
        code = getattr(exc, "code", "invalid_request")
        print(json.dumps({"error": {"code": code, "message": str(exc)}}, ensure_ascii=False), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
