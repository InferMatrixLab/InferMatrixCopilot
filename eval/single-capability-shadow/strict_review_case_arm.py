"""Installed Strict workflow over the frozen PR; forge reads remain offline.

Execute the standard durable child against one frozen forge adapter.
Provider execution, planning, refinement and completion run unchanged.
"""
import argparse
from contextlib import redirect_stdout
from dataclasses import fields
import json
import hashlib
from pathlib import Path
import runpy
import subprocess
import sys


def execution_observations(run_dir):
    """Report canonical trace facts without another completion policy."""
    path=Path(run_dir)/'run_trace.jsonl'
    report={'schema':'strict-stage-observations/1','reader':'RunTrace.events (malformed JSON lines skipped)',
            'reporting_only':True,'agent_outputs':[],'memory_observations':[]}
    if not path.is_file():
        return {**report,'read_status':'missing'}
    from infermatrix_copilot.run_trace import RunTrace
    try:
        report['source_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
        for row in RunTrace(path).events():
            if row.get('kind')=='agent_output':
                report['agent_outputs'].append({key:row.get(key) for key in ['step','ok','status','truncated']})
            elif row.get('kind')=='agent_dispatch':
                report['memory_observations'].append({'step':row.get('step'),'memories':row.get('memories')})
        report['read_status']='read'
    except (OSError,ValueError,TypeError,AttributeError) as exc:
        report.update(read_status='error',error_type=type(exc).__name__)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--host-inputs", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--shim", type=Path, required=True)
    parser.add_argument("--model", default="")
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--capture", action="store_true")
    parser.add_argument("--forge-context-version", type=int, choices=(1, 2), default=1)
    args = parser.parse_args()
    from arm_runtime import execution_profile, host_timeout, workflow_timeout, sdk_runtime
    from durable_worker import runtime_options, wait_result
    from strict_host import forge_cli
    if not args.model:
        args.model = execution_profile(args.shim)['model']
    args.timeout = host_timeout(args.shim)
    helpers = runpy.run_path(str(Path(__file__).with_name("review_case_arm.py")))
    row = helpers["load_case"](args.inputs, args.repo)
    host = helpers["load_host"](args.host_inputs, args.inputs)
    root = Path.cwd()

    def git(*command):
        return subprocess.run(["git", *map(str, command)], check=True, capture_output=True).stdout

    upstream, work = root / "upstream.git", root / "work"
    git("clone", "--quiet", "--bare", "--no-hardlinks", args.repo, upstream)
    git("-C", upstream, "update-ref", "refs/heads/main", row["base_sha"])
    git("-C", upstream, "update-ref", f"refs/pull/{row['pr']}/head", row["expected_head_sha"])
    git("-C", upstream, "symbolic-ref", "HEAD", "refs/heads/main")
    git("clone", "--quiet", upstream, work)
    evidence = work / host["business_context"]["patch_evidence"]["path"]
    evidence.parent.mkdir(parents=True)
    evidence.write_bytes((args.host_inputs / "patch.txt").read_bytes())

    import infermatrix_copilot
    from infermatrix_copilot.providers.codex import CodexTransport
    from infermatrix_copilot.sdk.v1 import StrictRuntime, StrictReviewRequest, RepositoryRef, CarriedFinding
    # The offline coordinator uses a fake native CLI. Live admission must check
    # subscription authentication separately before selecting this adapter.
    CodexTransport.auth_gap = lambda self: None
    alias = row["repository"].split("/")[-1]
    supports_context = "business_context" in {field.name for field in fields(StrictReviewRequest)}
    overrides = {"_env_file": None, "repo_paths": {alias: str(work)},
                 "repo_full_names": {alias: row["repository"]},
                 "mcp_allowed_repo_roots": [str(root)], "mcp_repo_allowlist": [alias],
                 "default_repo": alias, "review_depth": "standard", "strict_backend": "codex",
                 "strict_backend_cli": str(args.shim), "strict_backend_timeout_s": args.timeout,
                 "strict_backend_model": args.model, "review_lens_backends": {},
                 "ensemble_parallel": False, "review_verify_concurrency": 1,
                 "run_root": root / "runs", "memory_db": root / "domain-state" / "debug_memory.db"}
    descriptor = {"kind": "case", "row": row, "host": host}
    if args.forge_context_version != 1:
        descriptor['context_version'] = args.forge_context_version
    with forge_cli(root, descriptor), sdk_runtime(overrides, shim=args.shim, for_worker=True) as configured, \
            StrictRuntime(settings_overrides=configured, **runtime_options(args.shim)) as runtime:
        request = StrictReviewRequest(repository=RepositoryRef(alias, row["repository"]),
                                      pr_number=row["pr"], expected_head_sha=row["expected_head_sha"],
                                      repo_path="", idempotency_key=f"shadow-strict-{row['pr']}",
                                      review_depth="standard",
                                      carried_findings=tuple(CarriedFinding(**item) for item in row["carried_findings"]),
                                      **({"business_context": host["business_context"]} if supports_context else {}))
        first = runtime.reserve_review(request)
        second = runtime.reserve_review(request)
        assert first.created and not second.created and first.run_id == second.run_id
        result = wait_result(runtime, first.run_id, workflow_timeout(args.shim))
        code = 0 if result.state == "done" else 1
        (root / "review.json").write_text(json.dumps(result.payload, indent=2))
        if not args.capture and (code != 0 or result.state != "done"):
            raise RuntimeError(f"Strict did not complete: {result.payload}")
        if not args.capture:
            assert result.review.reviewed_head_sha == row["expected_head_sha"]
            assert result.review.rechecks_complete
    observations=execution_observations(root/'runs'/first.run_id)
    (root/'strict-observations.json').write_text(json.dumps(observations,indent=2))
    return {"execution_observations":observations,
            "package_file": infermatrix_copilot.__file__, "run_id": first.run_id,
            "state": result.state, "reviewed_head_sha": row["expected_head_sha"],
            "executor_exit_code": code, "adjudication": "pending" if args.capture else "scripted assertions passed",
            "frozen_context_delivery": "sdk_request" if supports_context else "original_forge_adapter",
            "execution": "standard durable Strict workflow child", "publication": "disabled"}


if __name__ == "__main__":
    with redirect_stdout(sys.stderr):
        report = main()
    print(json.dumps(report))
