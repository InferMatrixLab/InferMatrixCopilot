"""Run an installed arm's maintenance audit against hash-bound frozen witnesses.

The source-reader boundary supplies frozen archives or committed Git witnesses. Native dispatch,
prompt construction, semantic response validation and result construction remain
the installed arm's implementation. --source-repo exercises pinned source acquisition.
"""
import argparse
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import infermatrix_copilot
from infermatrix_copilot.kb_service import maintenance_audit
from arm_runtime import model_gateway, judgment_context


def pinned_source(capsule, role, repository):
    """Use the application's committed-source reader, without an evidence stub."""
    from infermatrix_copilot.kb_service.git_source import GitSource
    source = GitSource(repository)
    observer = SimpleNamespace(file_text=lambda pin, path: source.read(pin, path).decode('utf-8'))
    runtime = SimpleNamespace(judge=role, upstream_facts=lambda _: observer)
    lifecycle = SimpleNamespace(full_name=capsule['unit']['repository'])
    if maintenance_audit.source_evidence(runtime, lifecycle, capsule['unit']) != capsule['evidence']:
        raise ValueError('committed source differs from the frozen maintenance witnesses')
    return runtime, lifecycle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--shim", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-repo", type=Path,
                        help="Read original witnesses from this frozen local Git repository")
    args = parser.parse_args()
    settings, role = judgment_context(args.shim, args.model)
    # Labels are not read by the evaluated arm. Bind only its execution input.
    manifest = json.loads((args.inputs / "manifest.json").read_text())
    data = (args.inputs / "maintenance-input.json").read_bytes()
    expected = manifest["files"]["maintenance-input.json"]
    if len(data) != expected["bytes"] or hashlib.sha256(data).hexdigest() != expected["sha256"]:
        raise ValueError("frozen maintenance input changed")
    capsule = json.loads(data)
    records = []
    if args.source_repo:
        rt, lifecycle = pinned_source(capsule, role, args.source_repo)
        source_scope = nullcontext()
    else:
        rt, lifecycle = SimpleNamespace(judge=role), None
        source_scope = patch.object(maintenance_audit, 'source_evidence', return_value=capsule['evidence'])
    # Authentication is preflighted by the coordinator; the shim admits only
    # model execution, not login probes. Factory injection selects the real
    # installed transport, not a replacement model implementation.
    gateway = model_gateway(settings, shim=settings.strict_backend_cli, recorder=records.append)
    try:
        with source_scope:
            result = maintenance_audit.audit_unit(rt, lifecycle, capsule["unit"], gateway)
        report = {"status": "validated", "result": result}
    except Exception as exc:
        report = {"status": "failed", "error_type": type(exc).__name__, "error": str(exc)}
    report.update(package_file=infermatrix_copilot.__file__, records=records,
                  input_sha256=expected["sha256"], source_acquisition=(
                      "pinned local Git objects" if args.source_repo else "frozen archived witnesses"))
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({key: report[key] for key in ("status", "package_file", "input_sha256")}))
    if report["status"] != "validated":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
