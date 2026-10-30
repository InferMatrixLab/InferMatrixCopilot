"""Create-only draft PR publication for host-distilled knowledge tasks."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

from ..push import PushPolicy, guard_push


class LearningPublicationError(ValueError):
    pass


def _run(command, workspace):
    try:
        done = subprocess.run(command, cwd=workspace, text=True, capture_output=True,
                              timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LearningPublicationError(f"publication command unavailable ({type(exc).__name__})") from exc
    if done.returncode:
        # Tool output can contain authenticated remote URLs or private paths.
        raise LearningPublicationError(f"publication {command[0]} {command[1]} failed")
    return done.stdout.strip()


class LearningPublisher:
    """The destination is host configuration, never model-supplied data."""

    def __init__(self, environ=None, *, state_dir=None):
        from .learning_config import learning_environment

        self.environ = dict(learning_environment() if environ is None else environ)
        self.state_dir = state_dir

    def _require_agreement(self):
        from .agreement import AgreementStore

        return AgreementStore.from_env(state_dir=self.state_dir, environ=self.environ).require("learning_publication")

    def publish(self, workspace: Path, *, task_digest: str, head: str,
                packet_digest: str, rules: int, outcomes: dict,
                base_sha: str = "", documents: dict | None = None,
                source_pin: str = "", source_repository: str = "") -> dict:
        mode = self.environ.get("KNOWLEDGE_LEARNING_PUBLICATION", "local")
        branch = "knowledge/chat-" + task_digest[:24]
        if mode == "local":
            return {"status": "local", "branch": branch, "head_sha": head}
        if mode != "draft_pr":
            raise LearningPublicationError("KNOWLEDGE_LEARNING_PUBLICATION must be local or draft_pr")
        self._require_agreement()
        if source_pin and (not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", source_pin)
                           or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", source_repository)):
            raise LearningPublicationError("publication source provenance is invalid")
        repository = self.environ.get("KNOWLEDGE_LEARNING_REPOSITORY", "")
        base = self.environ.get("KNOWLEDGE_LEARNING_BASE", "main")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise LearningPublicationError("draft PR publication requires a fixed owner/repository")
        if not base or base.startswith("-") or _run(["git", "check-ref-format", "--branch", base], workspace) != base:
            raise LearningPublicationError("publication base branch is invalid")
        if self.environ.get("ALLOW_PUSH") != "1" or self.environ.get("ALLOW_POST") != "1":
            return {"status": "publication_blocked", "reason": "draft PR requires ALLOW_PUSH=1 and ALLOW_POST=1",
                    "branch": branch, "head_sha": head}
        remote = f"https://github.com/{repository}.git"
        documents = documents or {}
        if (not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base_sha) or not documents
                or any(not re.fullmatch(r"knowledge/(?:repos/[a-z0-9._-]+|general)/.+/?rules(?:-[a-z0-9-]+)?\.md", p)
                       and not re.fullmatch(r"knowledge/repos/[a-z0-9._-]+/rules(?:-[a-z0-9-]+)?\.md", p) for p in documents)):
            raise LearningPublicationError("publication requires an exact validated owner-document manifest")
        if (_run(["git", "rev-parse", "HEAD"], workspace) != head
                or _run(["git", "rev-parse", "HEAD^"], workspace) != base_sha
                or _run(["git", "status", "--porcelain", "--untracked-files=all"], workspace)
                or set(_run(["git", "diff", "--name-only", base_sha, head], workspace).splitlines()) != set(documents)):
            raise LearningPublicationError("publication head differs from the isolated knowledge commit")
        for page, expected in documents.items():
            path = workspace / page
            if path.is_symlink() or not path.resolve().is_relative_to(workspace.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise LearningPublicationError("publication document differs from the validated bytes")
        # Verify the whole PR against the real configured destination, not only
        # the last commit. A feature-branch source must never carry unrelated
        # committed work into an otherwise narrow knowledge PR.
        target_ref = "refs/infermatrix-learning/" + task_digest + "/publication-base"
        self._require_agreement()
        _run(["git", "fetch", "--no-tags", "--no-write-fetch-head", remote,
              "refs/heads/" + base + ":" + target_ref], workspace)
        if (_run(["git", "merge-base", base_sha, target_ref], workspace) != base_sha
                or set(_run(["git", "diff", "--name-only", target_ref + "..." + head], workspace).splitlines()) != set(documents)):
            raise LearningPublicationError("knowledge baseline carries work outside the destination base")
        marker = f"<!-- infermatrix-chat-learning:{packet_digest} -->"
        command = ["gh", "pr", "list", "--repo", repository, "--head", branch, "--state", "all",
                   "--json", "url,headRefOid,baseRefName,body,isDraft,state"]

        def existing():
            self._require_agreement()
            try:
                rows = json.loads(_run(command, workspace))
            except (ValueError, TypeError) as exc:
                raise LearningPublicationError("draft PR recovery returned invalid data") from exc
            if not isinstance(rows, list) or len(rows) > 1:
                raise LearningPublicationError("draft PR branch has ambiguous reviews")
            if not rows:
                return None
            row = rows[0]
            if (row.get("headRefOid") != head or row.get("baseRefName") != base
                    or marker not in row.get("body", "") or not row.get("url")
                    or row.get("isDraft") is not True):
                raise LearningPublicationError("existing review does not match this exact draft")
            return {"status": "draft_pr", "url": row["url"], "head_sha": head, "branch": branch,
                    "state": row.get("state", "OPEN")}

        prior = existing()
        if prior:
            return prior
        self._require_agreement()
        observed = _run(["git", "ls-remote", "--heads", remote, "refs/heads/" + branch], workspace)
        if observed:
            entries = [line.split() for line in observed.splitlines()]
            if entries != [[head, "refs/heads/" + branch]]:
                raise LearningPublicationError("remote learning branch already contains different work")
        else:
            decision = guard_push(PushPolicy(allowed=True, remote=remote, branch=branch, create_only=True),
                                  [base, "main", "master"])
            if not decision.allowed:
                raise LearningPublicationError(decision.reason)
            self._require_agreement()
            _run(list(decision.command), workspace)
        # Recover before create as well as on retry: upload and review creation
        # may each have succeeded before their command transport failed.
        prior = existing()
        if prior:
            return prior
        body = (f"Host-distilled maintenance rules: {rules} executable owner-scoped rules.\n\n"
                f"Learning outcomes: {outcomes.get('positive', 0)} positive, {outcomes.get('negative', 0)} negative. "
                "The local chat remains private. Existing knowledge review and quality gates apply.\n\n"
                + (f"Host source repository: `{source_repository}`; source pin: `{source_pin}`.\n\n"
                   if source_pin else "") + marker + "\n")
        body_file = workspace.parent / "pull-request-body.txt"
        self._require_agreement()
        body_file.write_text(body, encoding="utf-8")
        try:
            self._require_agreement()
            _run(["gh", "pr", "create", "--repo", repository, "--base", base, "--head", branch,
                  "--draft", "--title", "knowledge: retain task maintenance lessons", "--body-file", str(body_file)], workspace)
        except LearningPublicationError:
            prior = existing()
            if prior:
                return prior
            raise
        prior = existing()
        if prior is None:
            raise LearningPublicationError("created draft PR could not be verified; retry to recover it")
        return prior
