"""Read and export knowledge-use records without loading a model backend."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile


def _write_export(destination: str, content: str) -> None:
    if destination == "-":
        print(content)
        return
    path = Path(destination).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    from . import KnowledgeTrace, KnowledgeTraceError, markdown

    parser = argparse.ArgumentParser(prog="infermatrix-copilot knowledge-trace",
                                     description="Inspect per-step knowledge and decision evidence.")
    parser.add_argument("--root", type=Path, help="Trace store (Strict: <run-dir>/knowledge_trace)")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("show", "export", "query"):
        sub = commands.add_parser(name)
        sub.add_argument("--root", type=Path, default=argparse.SUPPRESS,
                         help="Trace store; defaults to KNOWLEDGE_TRACE_ROOT")
        if name == "query":
            sub.add_argument("--repo", required=True)
            sub.add_argument("--rule", default="")
            sub.add_argument("--limit", type=int, default=100)
        else:
            sub.add_argument("trace_id")
            if name == "show":
                sub.add_argument("--json", action="store_true")
                sub.add_argument("--cursor")
                sub.add_argument("--limit", type=int, default=100)
            else:
                sub.add_argument("--format", choices=("json", "markdown"), default="markdown")
                sub.add_argument("--out", required=True, help="Output file, or - for stdout")
    args = parser.parse_args(argv)
    try:
        # Inspecting existing records works even when collection is disabled.
        recorder = KnowledgeTrace(root=args.root, mode="required")
        if args.command == "query":
            result = recorder.query(repository=args.repo, rule_id=args.rule, limit=args.limit)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            options = ({"cursor": args.cursor, "limit": args.limit}
                       if args.command == "show" else {})
            report = recorder.report(args.trace_id, **options)
            if args.command == "export":
                # Export all ordered events, rather than silently stopping at
                # the report API's first page. Session size is bounded upstream.
                events = list(report["events"])
                cursor = report.get("next_cursor")
                while cursor is not None:
                    page = recorder.report(args.trace_id, cursor=cursor)
                    events.extend(page["events"])
                    cursor = page.get("next_cursor")
                report["events"] = events
                report["next_cursor"] = None
            as_json = args.json if args.command == "show" else args.format == "json"
            content = json.dumps(report, ensure_ascii=False, indent=2) if as_json else markdown(report)
            if args.command == "show":
                print(content)
            else:
                _write_export(args.out, content)
        return 0
    except (KnowledgeTraceError, OSError, ValueError, KeyError) as exc:
        print(f"knowledge-trace: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
