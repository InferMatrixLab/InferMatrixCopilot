"""Offline installed Strict playbook driver; only forge/model boundaries are fake.

The standard durable child receives frozen forge reads and evaluation policy.
Provider dispatch and worker scheduling retain their production implementations.
"""
import json
import hashlib
import os
from contextlib import redirect_stdout
from pathlib import Path
import runpy
import sys

from infermatrix_copilot.providers.codex import CodexTransport
from infermatrix_copilot.sdk.v1 import StrictRuntime, StrictReviewRequest, RepositoryRef
from arm_runtime import execution_profile, host_timeout, workflow_timeout, sdk_runtime
from durable_worker import runtime_options, wait_result
from strict_host import forge_cli
import infermatrix_copilot


def main():
    shim, fixture_file = sys.argv[1:]
    fixture = runpy.run_path(fixture_file)
    root = Path.cwd()
    os.environ.update(GIT_AUTHOR_DATE="2000-01-01T00:00:00Z", GIT_COMMITTER_DATE="2000-01-01T00:00:00Z")
    work, head = fixture["_pr_checkout"](root)
    # Offline fixture has no subscription identity; no authentication probe or
    # external GitHub request is authorized by this conformance check.
    CodexTransport.auth_gap = lambda self: None
    overrides = {"_env_file": None, "repo_paths": {"widget": str(work)},
                 "repo_full_names": {"widget": "acme/widget"},
                 "mcp_allowed_repo_roots": [str(root)], "mcp_repo_allowlist": ["widget"],
                 "default_repo": "widget", "review_depth": "light", "strict_backend": "codex",
                 "strict_backend_cli": shim, "strict_backend_timeout_s": host_timeout(shim),
                 "strict_backend_model": execution_profile(shim)['model'],
                 "run_root": root / "runs"}
    descriptor = {"kind": "fixture", "fixture": str(Path(fixture_file).resolve()),
                  "sha256": hashlib.sha256(Path(fixture_file).read_bytes()).hexdigest(), "head": head}
    with forge_cli(root, descriptor), sdk_runtime(overrides, shim=shim, for_worker=True) as configured, \
            StrictRuntime(settings_overrides=configured, **runtime_options(shim)) as runtime:
        request = StrictReviewRequest(repository=RepositoryRef("widget", "acme/widget"),
                                      pr_number=7, expected_head_sha=head, repo_path="",
                                      idempotency_key="offline-strict-guard", review_depth="light")
        first = runtime.reserve_review(request)
        second = runtime.reserve_review(request)
        assert first.created and not second.created and first.run_id == second.run_id
        result = wait_result(runtime, first.run_id, workflow_timeout(shim))
        assert result.state == "done", result.payload
        assert result.review.reviewed_head_sha == head
    return {"package_file": infermatrix_copilot.__file__, "run_id": first.run_id,
                      "reviewed_head_sha": head, "state": result.state,
                      "execution": "standard durable Strict playbook child"}


if __name__ == "__main__":
    with redirect_stdout(sys.stderr):
        report = main()
    print(json.dumps(report))
