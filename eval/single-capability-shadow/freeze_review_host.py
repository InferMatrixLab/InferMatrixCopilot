"""Freeze host evidence using installed ReviewBot serializers.

Changed-file fixtures use frozen GitHub file pages through the installed host
codec when explicitly bound, or the legacy exact-Git reconstruction. No network,
model calls or publication occur.
"""
import argparse
from dataclasses import asdict
from datetime import datetime, UTC
import hashlib
import json
from pathlib import Path
import runpy
import subprocess
import tempfile
from unittest.mock import patch
from uuid import UUID


def frozen_changed_files(case, manifest, row):
    """Replay raw file pages through the host's existing GitHub codec."""
    name = row["changed_files_input"]
    if name != "github-files.json":
        raise ValueError("unsupported frozen changed-files input")
    raw = (case / name).read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest["files"][name]["sha256"]:
        raise ValueError("frozen changed-files input changed")
    pages = json.loads(raw)
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise ValueError("frozen changed-files input must contain API pages")
    from omni_reviewbot.github_client import GitHubClient

    class FrozenFiles(GitHubClient):
        def _paged(self, path):
            if path != f"/repos/{self.repo_slug}/pulls/{row['pr']}/files?per_page=100":
                raise AssertionError("unexpected frozen file read")
            return [item for page in pages for item in page]

        def _request(self, *args, **kwargs):
            raise AssertionError("frozen file codec cannot access the network")

    changed = FrozenFiles(row["repository"]).list_files(row["pr"])
    if [(item.path, item.status) for item in changed] != [
        (item["path"], item["status"]) for item in row["changed_paths"]
    ]:
        raise ValueError("frozen changed-files paths do not match the request")
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = (args.case / "request.json").read_bytes()
    manifest = json.loads((args.case / "manifest.json").read_text())
    assert hashlib.sha256(raw).hexdigest() == manifest["files"]["request.json"]["sha256"]
    row = json.loads(raw)
    from omni_reviewbot.models import ChangedFile, PullRequest
    from omni_reviewbot.diff_budget import diff_size_facts
    from omni_reviewbot.review.evidence_workspace import materialize_patch_evidence
    from omni_reviewbot.review_ci import ReviewCISnapshot

    def git(*command):
        return subprocess.run(["git", "-C", str(args.repo), *command], check=True,
                              capture_output=True).stdout

    assert git("rev-parse", "HEAD").decode().strip() == row["expected_head_sha"]
    helpers = runpy.run_path(str(Path(__file__).with_name("review_case_arm.py")))
    # Both review arms and host preparation enforce the same source contract.
    helpers["load_case"](args.case, args.repo)
    diff_base, diff_head = helpers["review_diff_revisions"](row, git)
    changed = []
    for item in (() if row.get("changed_files_input") else row["changed_paths"]):
        path = item["path"]
        counts = git("diff", "--numstat", "-z", diff_base, diff_head, "--", path)
        added, deleted, _ = counts.decode().split("\t", 2)
        delta = git("diff", "--no-ext-diff", "--no-textconv", diff_base, diff_head, "--", path).decode()
        # This case contains text modifications only. Fail rather than invent
        # counts or rename handling for a future unsupported fixture.
        assert item["status"] == "modified" and added.isdigit() and deleted.isdigit()
        hunk = delta.find("@@")
        assert hunk >= 0
        changed.append(ChangedFile(path=path, status=item["status"], patch=delta[hunk:],
                                   additions=int(added), deletions=int(deleted)))
    if row.get("changed_files_input"):
        changed = frozen_changed_files(args.case, manifest, row)
    pull = PullRequest(**row["business_context"]["pull"])
    identity = hashlib.sha256(raw).hexdigest()[:32]
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        with patch("omni_reviewbot.review.evidence_workspace.uuid.uuid4", return_value=UUID(identity)):
            evidence = materialize_patch_evidence(workspace, pull, changed)
        evidence_bytes = (workspace / evidence["path"]).read_bytes()
    ci = ReviewCISnapshot(head_sha=pull.head_sha, base_ref=pull.base_ref,
                          checked_at=datetime.now(UTC).isoformat(), read_status="unavailable",
                          reason="CI read API unavailable")
    provenance = ("Offline historical replay: changed-file rows, counts and patches are reconstructed "
                  "from the frozen Git base/head, not an archived GitHub API response. The original "
                  "host serializer retains its GitHub-shaped header. CI was not independently read; "
                  "checked_at records packet preparation only.")
    if row.get("changed_files_input"):
        provenance = ("Offline controlled replay: changed-file pages were captured later from GitHub "
                      "and decoded by the installed host codec. They are not an archived historical "
                      "API view. Source/head parity is separate evidence; CI at the feedback boundary "
                      "is unavailable. checked_at records packet preparation only.")
    packet = {"request_sha256": hashlib.sha256(raw).hexdigest(), "patch_uuid": identity,
              "changed_files": [asdict(item) for item in changed], "ci_snapshot": asdict(ci),
              "business_context": {**row["business_context"], "ci_evidence": ci.to_context(),
                                   "diff_size": diff_size_facts(changed), "patch_evidence": evidence,
                                   "host_evidence_provenance": provenance}}
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "packet.json").write_text(json.dumps(packet, indent=2) + "\n")
    (args.output / "patch.txt").write_bytes(evidence_bytes)
    files = {name: hashlib.sha256((args.output / name).read_bytes()).hexdigest()
             for name in ("packet.json", "patch.txt")}
    (args.output / "manifest.json").write_text(json.dumps({"files": files}, indent=2) + "\n")
    print(json.dumps({"files": files, "diff_size": packet["business_context"]["diff_size"]}))


if __name__ == "__main__":
    main()
