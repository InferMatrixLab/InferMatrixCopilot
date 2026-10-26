"""Run one frozen Direct case through an installed baseline or candidate.

The caller supplies a ledger-guarded provider shim. This driver never publishes
and never reads labels. Its working directory must be private to the arm.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from contextlib import contextmanager
from unittest.mock import patch
from uuid import UUID
import hashlib
import json
from pathlib import Path
import subprocess


@contextmanager
def candidate_runtime(options, repository):
    """Use the public durable SDK and its registered scoped sandbox profile."""
    from infermatrix_copilot.sdk.v1 import ReviewRuntime, ReviewRuntimeConfig, provider_descriptor
    from arm_runtime import sdk_runtime
    from durable_worker import runtime_options
    from ledger import Refused
    if 'session_schema' not in provider_descriptor('codex').capabilities:
        raise Refused('candidate Direct requires the scoped structured session capability')
    with sdk_runtime({'_env_file': None}, shim=options.shim, for_worker=True) as configured:
        profile = {'provider': 'codex', 'command': [str(options.shim)],
                   'model': configured['strict_backend_model'],
                   'idle_timeout_s': configured['strict_backend_timeout_s'],
                   'absolute_timeout_s': configured['strict_backend_timeout_s'],
                   'execution_config': configured['native_session_profiles']['codex'],
                   'effort': configured['strict_backend_effort']}
        if options.model and options.model != profile['model']:
            raise Refused('candidate Direct model differs from its frozen profile')
        config = ReviewRuntimeConfig(repository, str(options.repo), str(options.repo.parent),
                                    backend='codex', run_root=str(Path.cwd() / 'runs'),
                                    direct_profiles={'codex': profile})
        with ReviewRuntime(config=config, **runtime_options(options.shim)) as runtime:
            yield runtime, config, profile


def load_host(root: Path, case: Path) -> dict:
    manifest = json.loads((root / "manifest.json").read_text())
    for name in ("packet.json", "patch.txt"):
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != manifest["files"][name]:
            raise ValueError(f"frozen host evidence changed: {name}")
    packet = json.loads((root / "packet.json").read_text())
    if packet["request_sha256"] != hashlib.sha256((case / "request.json").read_bytes()).hexdigest():
        raise ValueError("host packet belongs to a different review request")
    relative = Path(packet["business_context"]["patch_evidence"]["path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("host patch path must be contained")
    return packet


def review_diff_revisions(request: dict, git) -> tuple[str, str]:
    """Keep historical two-revision inputs; explicitly pin new PR merge bases.

    Advancing the target branch must not add its unrelated changes to a PR's
    review evidence. A new PR case binds both the target SHA and the unique
    merge base, rather than silently changing existing paid fixtures.
    """
    base, head = request["base_sha"], request["expected_head_sha"]
    semantics = request.get("diff_semantics", "exact-revisions-v1")
    if semantics == "exact-revisions-v1":
        if request.get("diff_base_sha", base) != base:
            raise ValueError("exact-revision diff base does not match frozen base")
        return base, head
    if semantics != "pull-request-merge-base-v1":
        raise ValueError("unsupported frozen review diff semantics")
    pinned = request.get("diff_base_sha")
    if not pinned:
        raise ValueError("PR diff requires a frozen merge base")
    bases = git("merge-base", "--all", base, head).decode().splitlines()
    if bases != [pinned]:
        raise ValueError("PR diff merge base does not match frozen evidence")
    return pinned, head


def load_case(root: Path, repo: Path) -> dict:
    manifest = json.loads((root / "manifest.json").read_text())
    # Validate only model inputs, deliberately leaving labels unread.
    for name in ("request.json", "review.diff", "source-manifest.json"):
        raw = (root / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != manifest["files"][name]["sha256"]:
            raise ValueError(f"frozen input changed: {name}")
    request = json.loads((root / "request.json").read_text())
    if request["publication"] != "disabled":
        raise ValueError("shadow publication must be disabled")
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True,
                              capture_output=True).stdout
    if git("rev-parse", "HEAD").decode().strip() != request["expected_head_sha"]:
        raise ValueError("checkout does not match frozen head")
    base, head = review_diff_revisions(request, git)
    # Preserve the original command for legacy fixtures; new PR fixtures also
    # disable local text conversion, matching the source/host parity freeze.
    options = ("--no-textconv",) if request.get("diff_semantics") == "pull-request-merge-base-v1" else ()
    diff = git("diff", "--no-ext-diff", *options, "--binary", base, head)
    if diff != (root / "review.diff").read_bytes():
        raise ValueError("checkout diff does not match frozen evidence")
    if git("status", "--porcelain"):
        raise ValueError("shadow checkout must be clean")
    return request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=("baseline", "candidate"), required=True)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--shim", type=Path, required=True)
    parser.add_argument("--host-inputs", type=Path)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--model", default="")
    options = parser.parse_args()
    row = load_case(options.inputs, options.repo)
    host = load_host(options.host_inputs, options.inputs) if options.host_inputs else None
    if host:
        row["business_context"] = host["business_context"]
    import infermatrix_copilot
    from infermatrix_copilot.sdk.v1 import (
        CarriedFinding, ChangedPath, DirectReviewRequest, RepositoryRef,
    )
    repository = RepositoryRef(alias=row["repository"].split("/")[-1], full_name=row["repository"])
    request = DirectReviewRequest(
        review_id=f"shadow-{row['pr']}", repository=repository,
        pr_number=row["pr"], expected_head_sha=row["expected_head_sha"],
        title=row["title"], body=row["body"],
        changed_paths=tuple(ChangedPath(**item) for item in row["changed_paths"]),
        carried_findings=tuple(CarriedFinding(**item) for item in row["carried_findings"]),
        diff=(options.inputs / "review.diff").read_text(),
    )
    if options.arm == "baseline":
        from infermatrix_copilot.sdk.v1 import DirectClient as SDKClient
        from omni_reviewbot.direct_client import DirectClient
        from omni_reviewbot.codex_runner import CodexRunner
        from omni_reviewbot.models import ChangedFile, PullRequest, ReviewOutcome
        from omni_reviewbot.pipeline import ReviewPipeline
        sdk = SDKClient(knowledge_context_path=Path.cwd() / "knowledge-context.sqlite")
        # Historical replay admits the frozen target explicitly. This is not a
        # claim that the archived, merged PR is open on GitHub today.
        pull = PullRequest(**row["business_context"]["pull"])
        class FrozenGitHub:
            def get_pull(self, number):
                assert number == row["pr"]
                return pull

            def list_files(self, number):
                assert number == row["pr"]
                return [ChangedFile(**item) for item in (host["changed_files"] if host else row["changed_paths"])]

            def list_review_context(self, number, **kwargs):
                assert number == row["pr"]
                return row["business_context"]["pr_review_context"]

        class FrozenRepository:
            @contextmanager
            def checkout(self, frozen_pull, changed_files):
                workspace = Path.cwd() / "workspace"
                subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(options.repo), str(workspace)],
                               check=True, capture_output=True)
                subprocess.run(["git", "-C", str(workspace), "checkout", "--quiet", "--detach", pull.head_sha],
                               check=True, capture_output=True)
                yield workspace

        class FrozenLedger:
            def carried_pr_findings(self, repo_slug, number):
                assert repo_slug == row["repository"] and number == row["pr"]
                return row["carried_findings"]

        captured = {}
        class PlannedDirect(DirectClient):
            def review(self, *args, **kwargs):
                context = super().review(*args, **kwargs)
                if host:
                    context["host_evidence_provenance"] = host["business_context"]["host_evidence_provenance"]
                return context

        class ComputationPipeline(ReviewPipeline):
            def publish(self, result, **kwargs):
                assert kwargs["post"] is False
                captured["result"] = result
                return ReviewOutcome(status="shadow_computed", body="")

        pipeline = ComputationPipeline(
            repo_slug=row["repository"], github=FrozenGitHub(), repository=FrozenRepository(),
            direct=PlannedDirect(sdk=sdk, repo_name=repository.alias, repo_slug=repository.full_name),
            codex=CodexRunner(command=[str(options.shim)], repo_slug=row['repository'],
                              timeout_seconds=options.timeout))
        pipeline.ledger = FrozenLedger()
        trace = {}
        # Use the original orchestration and validation body; collect its exact
        # context without replacing the provider or any completion predicate.
        def run():
            return pipeline._run(pr_number=row["pr"], expected_head=row["expected_head_sha"],
                                 post=False, trace=trace)
        if host:
            from omni_reviewbot.review_ci import ReviewCISnapshot
            snapshot = ReviewCISnapshot(**host["ci_snapshot"])
            with patch("omni_reviewbot.pipeline.collect_review_ci", return_value=snapshot), patch(
                "omni_reviewbot.review.evidence_workspace.uuid.uuid4", return_value=UUID(host["patch_uuid"])
            ):
                outcome = run()
            patch_path = Path.cwd() / "workspace" / host["business_context"]["patch_evidence"]["path"]
            assert patch_path.read_bytes() == (options.host_inputs / "patch.txt").read_bytes()
            for key in ("ci_evidence", "diff_size", "patch_evidence", "pr_review_context", "existing_feedback_status"):
                assert trace["direct_context"][key] == host["business_context"][key], key
        else:
            outcome = run()
        if outcome.status != "shadow_computed":
            raise RuntimeError(f"baseline did not reach computation boundary: {outcome.status}")
        (Path.cwd() / "context.json").write_text(json.dumps(trace["direct_context"], indent=2))
        payload = asdict(captured["result"])
        run_id = None
    else:
        from infermatrix_copilot.sdk.v1 import DirectReviewRunRequest
        from arm_runtime import workflow_timeout
        from durable_worker import wait_result
        if host:
            workspace = Path.cwd() / "input-workspace"
            if not workspace.exists():
                subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(options.repo), str(workspace)],
                               check=True, capture_output=True)
                subprocess.run(["git", "-C", str(workspace), "remote", "set-url", "origin",
                                f"https://github.com/{row['repository']}.git"], check=True, capture_output=True)
                evidence = workspace / host["business_context"]["patch_evidence"]["path"]
                evidence.parent.mkdir(parents=True)
                evidence.write_bytes((options.host_inputs / "patch.txt").read_bytes())
            options.repo = workspace
        with candidate_runtime(options, repository) as (runtime, config, profile):
            run_request = DirectReviewRunRequest(
                review=request, repo_path=str(options.repo), idempotency_key=f"shadow-direct-{row['pr']}",
                profile=profile, business_context=row["business_context"])
            first = runtime.reserve_direct_review(run_request)
            second = runtime.reserve_direct_review(run_request)
            assert first.run_id == second.run_id and not second.created
            result = wait_result(runtime, first.run_id, workflow_timeout(options.shim))
            if result.state != "done":
                raise RuntimeError(f"Direct did not complete: {result.payload}")
            payload = result.review.direct_result
            run_id = first.run_id
            if host:
                frozen = Path(config.run_root) / run_id / "workspace" / host["business_context"]["patch_evidence"]["path"]
                assert frozen.read_bytes() == (options.host_inputs / "patch.txt").read_bytes()
        with candidate_runtime(options, repository) as (runtime, _, _):
            recovered = runtime.find_review(run_request.idempotency_key, expected_head_sha=row["expected_head_sha"])
            assert recovered.run_id == run_id
            assert runtime.get_result(run_id).review.direct_result == payload
    (Path.cwd() / "review.json").write_text(json.dumps(payload, indent=2))
    print(json.dumps({"package_file": infermatrix_copilot.__file__, "run_id": run_id,
                      "reviewed_head_sha": payload["reviewed_head_sha"], "completion_validated": True,
                      "publication": "disabled", "arm": options.arm}))


if __name__ == "__main__":
    main()
