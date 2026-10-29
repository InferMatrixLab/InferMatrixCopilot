"""Thin JSON CLI for host-distilled task learning and knowledge feedback."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_MAX_INPUT_BYTES = 512 * 1024


class _InputError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse's default errors echo invalid values and unknown arguments.
        # JSON input is the sole data boundary; never repeat stray credentials.
        self.exit(2, "Invalid learning command; use --help for accepted arguments.\n")


def _read_input(path: str) -> dict[str, Any]:
    try:
        if path == "-":
            content = sys.stdin.read(_MAX_INPUT_BYTES + 1)
            if len(content.encode("utf-8")) > _MAX_INPUT_BYTES:
                raise _InputError("Input exceeds the 512 KiB limit")
        else:
            with Path(path).open("rb") as stream:
                raw = stream.read(_MAX_INPUT_BYTES + 1)
            if len(raw) > _MAX_INPUT_BYTES:
                raise _InputError("Input exceeds the 512 KiB limit")
            content = raw.decode("utf-8")
        value = json.loads(content)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise _InputError("Input must be a readable UTF-8 JSON object") from None
    if not isinstance(value, dict):
        raise _InputError("Input must be a JSON object")
    return value


def _fields(value: dict[str, Any], allowed: set[str]) -> None:
    if set(value) - allowed:
        raise _InputError("Input contains unsupported fields")


def _text(value: dict[str, Any], field: str) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result.strip():
        raise _InputError(f"{field} must be a non-empty string")
    return result


def _learning_service():
    from .learning import TaskLearningService

    return TaskLearningService.from_env()


def _feedback_store():
    from .feedback import KnowledgeFeedbackStore

    return KnowledgeFeedbackStore.from_env()


def _require_agreement(action: str) -> None:
    from .agreement import AgreementStore

    AgreementStore.from_env().require(action)


def _dispatch(args: argparse.Namespace) -> Any:
    if args.command == "status":
        return _learning_service().status(args.task_id)
    if args.command == "report":
        if not 1 <= args.limit <= 1000:
            raise _InputError("limit must be within 1..1000")
        return _feedback_store().report_for_scope(repository=args.repo, limit=args.limit, scope=args.scope)
    if args.command == "sync":
        if not 1 <= args.limit <= 1000:
            raise _InputError("limit must be within 1..1000")
        _require_agreement("learning.sync")
        return _feedback_store().sync_pending(limit=args.limit)
    if args.command == "sync-status":
        return _feedback_store().sync_status()

    value = _read_input(args.input)
    if args.command == "prepare":
        _fields(value, {"task_id", "repository", "repo", "source_pin", "lessons"})
        if "repo" in value and "repository" in value:
            raise _InputError("Use repository or repo, not both")
        if "repo" in value:
            value["repository"] = value.pop("repo")
        task_id = _text(value, "task_id")
        repository = _text(value, "repository")
        source_pin = _text(value, "source_pin")
        lessons = value.get("lessons", [])
        if not isinstance(lessons, list):
            raise _InputError("lessons must be an array")
        _require_agreement("learning.prepare")
        return _learning_service().prepare(
            task_id=task_id, repository=repository, source_pin=source_pin,
            lessons=lessons,
        )
    if args.command == "submit":
        _fields(value, {"task_id", "packet_digest", "proposals"})
        task_id = _text(value, "task_id")
        packet_digest = _text(value, "packet_digest")
        if "proposals" not in value:
            raise _InputError("proposals is required")
        _require_agreement("learning.submit")
        return _learning_service().submit(
            task_id=task_id, packet_digest=packet_digest,
            proposals=value["proposals"],
        )
    _fields(value, {"task_id", "delivery_id", "ratings"})
    task_id = _text(value, "task_id")
    delivery_id = _text(value, "delivery_id")
    ratings = value.get("ratings")
    if not isinstance(ratings, list):
        raise _InputError("ratings must be an array")
    _require_agreement("learning.feedback")
    return _feedback_store().record(
        task_id=task_id, delivery_id=delivery_id, ratings=ratings,
    )


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "agreement":
        from .agreement_cli import main as agreement_main

        return agreement_main(arguments[1:])
    parser = _Parser(prog="infermatrix-copilot learning")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("agreement", help="show agreement, inspect status, accept interactively or revoke")
    for command in ("prepare", "submit", "feedback"):
        child = subcommands.add_parser(command)
        child.add_argument("--input", required=True, help="JSON file, or - for stdin")
    status = subcommands.add_parser("status")
    status.add_argument("task_id")
    report = subcommands.add_parser("report")
    report.add_argument("--repo", default="")
    report.add_argument("--limit", type=int, default=100)
    report.add_argument("--scope", choices=("auto", "local", "central"), default="auto")
    sync = subcommands.add_parser("sync", help="retry pending feedback uploads")
    sync.add_argument("--limit", type=int, default=100)
    subcommands.add_parser("sync-status", help="inspect the local feedback upload queue")
    args = parser.parse_args(arguments)
    try:
        result = _dispatch(args)
        if hasattr(result, "to_dict"):
            result = result.to_dict()
        output = json.dumps(result, ensure_ascii=False, indent=2)
    except _InputError as exc:
        print(json.dumps({"error": {"code": "invalid_input", "message": str(exc)}}))
        return 2
    except Exception as exc:  # noqa: BLE001 - the CLI must not echo private provider failures
        if getattr(exc, "code", "") == "agreement_required":
            print(json.dumps({"error": {"code": "agreement_required", "message":
                "Human agreement required; inspect learning agreement show and sign interactively with learning agreement accept."}}))
            return 3
        # Service/provider errors can contain paths, credentials or remote output.
        # Preserve the local packet; a status request can report its safe state.
        print(json.dumps({"error": {"code": "learning_failed", "message":
            "Learning operation failed; retry with the same task ID or inspect its status."}}))
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
