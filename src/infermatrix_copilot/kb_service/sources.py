"""Read-only inputs of the knowledge service: the knowledge repository clone and
GitHub. Nothing here writes to GitHub; the service holds no write credential.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..git_objects import checked_read, raw_changes, tree_entries, tree_texts

KNOWLEDGE_SUFFIXES = (".md", ".yaml")
EXTERNAL_REF_DIRS = ("skills/", "plugins/", "adapters/", "doc/", "playbooks/")
MAX_BODY_CHARS = 3000
MAX_CHANGED_PATHS = 50
MAX_DIFF_BYTES = 8 * 1024


class SourceError(RuntimeError):
    """A read of git or GitHub failed; the caller retries on a later tick."""


class KnowledgeRepo:
    """A local clone of the knowledge repository (InferMatrixCopilot)."""

    def __init__(self, path: str | Path, *, remote: str = "origin", branch: str = "main"):
        self.path = Path(path)
        self.remote = remote
        self.branch = branch

    def _git(self, *args: str, input: bytes | None = None) -> bytes:
        proc = subprocess.run(["git", "-C", str(self.path), *args], input=input,
                              capture_output=True, check=False)
        if proc.returncode != 0:
            raise SourceError(f"git {' '.join(args)} failed: {proc.stderr.decode(errors='replace')[:500]}")
        return proc.stdout

    def fetch(self) -> str:
        self._git("fetch", "--quiet", self.remote, self.branch)
        return self.main_sha()

    def main_sha(self) -> str:
        return self._git("rev-parse", f"{self.remote}/{self.branch}").decode().strip()

    def _texts(self, rev: str, prefixes: tuple[str, ...], suffixes: tuple[str, ...] | None) -> dict[str, str]:
        return checked_read(SourceError, tree_texts, self._git, rev, prefixes, suffixes or None)

    def fetch_pull(self, number: int) -> str:
        """Fetch a pull request's head as data (never checked out); its SHA."""
        self._git("fetch", "--quiet", self.remote, f"+refs/pull/{int(number)}/head:refs/kb/pull/{int(number)}")
        return self._git("rev-parse", f"refs/kb/pull/{int(number)}").decode().strip()

    def merge_base(self, a: str, b: str) -> str:
        return self._git("merge-base", a, b).decode().strip()

    def is_ancestor(self, older: str, newer: str) -> bool:
        proc = subprocess.run(["git", "-C", str(self.path), "merge-base", "--is-ancestor", older, newer],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return proc.returncode == 0

    def diff_text(self, old: str, new: str, paths: list[str]) -> str:
        """The textual diff of ``paths`` (evidence for a candidate)."""
        if not paths:
            return ""
        args = ["diff", old, new, "--", *paths] if old else ["show", "--format=", new, "--", *paths]
        try:
            return self._git(*args).decode("utf-8", "replace")
        except SourceError:
            return ""

    def parents(self, sha: str) -> list[str]:
        return self._git("rev-list", "--parents", "-n", "1", sha).decode().split()[1:]

    def show(self, rev: str, path: str) -> str | None:
        try:
            return self._git("show", f"{rev}:{path}").decode("utf-8", "replace")
        except SourceError:
            return None

    def export(self, rev: str, dest: str | Path) -> Path:
        """Materialize the tree at ``rev`` under ``dest`` (no checkout involved)."""
        import io
        import tarfile

        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(self._git("archive", "--format=tar", rev))) as tar:
            tar.extractall(dest, filter="data")
        return dest

    def raw_manifest(self, old: str, new: str) -> list[dict]:
        """Every changed path as a verdict manifest entry (renames are D + A),
        the exact form the verifier recomputes."""
        return checked_read(SourceError, lambda: raw_changes(
            self._git("diff", "--raw", "-z", "--no-renames", "--no-abbrev", old, new)))

    def first_parent_commits(self, old: str, new: str) -> list[tuple[str, list[str], float, str]]:
        """(sha, parents, commit time, message) on new's first-parent chain
        after old, oldest first."""
        out = self._git("log", "--first-parent", "--reverse", "-z", "--format=%H %P%n%ct%n%B",
                        f"{old}..{new}").decode("utf-8", "replace")
        commits = []
        for record in out.split("\0"):
            if not record.strip():
                continue
            head, _, rest = record.lstrip("\n").partition("\n")
            stamp, _, message = rest.partition("\n")
            shas = head.split()
            commits.append((shas[0], shas[1:], float(stamp or 0), message))
        return commits

    def blob_ids(self, rev: str, paths: list[str]) -> dict[str, str]:
        """path -> "<mode> <type> <blob id>" at ``rev`` ("" for an absent path):
        the mode is part of the identity (an executable bit is a change)."""
        if not paths:
            return {}
        found = {name.decode("utf-8", "replace"): f"{mode} {kind} {oid}"
                 for mode, kind, oid, name in tree_entries(self._git("ls-tree", "-z", "--full-tree", rev, "--", *paths))}
        return {path: found.get(path, "") for path in paths}

    def changed_names(self, old: str, new: str) -> list[str]:
        if not old:  # a root commit
            out = self._git("ls-tree", "-r", "-z", "--name-only", new)
        else:
            out = self._git("diff", "--name-only", "-z", "--no-renames", old, new)
        return [name.decode("utf-8", "replace") for name in out.split(b"\0") if name]

    def knowledge_files(self, rev: str) -> dict[str, str]:
        """Knowledge-relative path -> text for the governed tree at ``rev``."""
        files = self._texts(rev, ("knowledge/repos/", "knowledge/general/"), KNOWLEDGE_SUFFIXES)
        return {name.removeprefix("knowledge/"): text for name, text in files.items()}

    def top_level_knowledge(self, rev: str) -> dict[str, str]:
        """knowledge/*.md at the top of the tree (AGENTS.md, README.md, ...) and
        the format declaration: not governed by the gate, but served with every
        snapshot."""
        files = self._texts(rev, ("knowledge/",), (".md", "_format.yaml"))
        return {name.removeprefix("knowledge/"): text for name, text in files.items()
                if "/" not in name.removeprefix("knowledge/")}

    def external_texts(self, rev: str) -> dict[str, str]:
        """Text files outside knowledge/ that may cite rule IDs (companion PRs)."""
        return self._texts(rev, EXTERNAL_REF_DIRS, (".md", ".yaml", ".yml", ".json", ".txt"))


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    body: str
    merged_at: str
    merge_commit_sha: str
    author: str
    changed_files: tuple[str, ...]
    diff_excerpt: str
    base_ref: str = ""
    head_ref: str = ""
    head_sha: str = ""

    def evidence(self) -> dict:
        return {
            "source_reference": f"PR #{self.number}",
            "title": self.title,
            "body": self.body[:MAX_BODY_CHARS],
            "merged_at": self.merged_at,
            "merge_commit_sha": self.merge_commit_sha,
            "base_ref": self.base_ref,
            "head_ref": self.head_ref,
            "head_sha": self.head_sha,
            "changed_files": list(self.changed_files[:MAX_CHANGED_PATHS]),
            "diff_excerpt": self.diff_excerpt,
        }


class GitHubReader:
    """Minimal read-only REST client. ``fetch(url) -> (status, json)`` is
    injectable for tests; by default it uses urllib with an optional read-only
    token from ``KB_GITHUB_READ_TOKEN`` (anonymous for public repositories)."""

    API = "https://api.github.com"

    def __init__(self, *, fetch: Callable[[str], Any] | None = None, token: str | None = None,
                 fetch_diff: Callable[[str, int], str] | None = None):
        self._fetch = fetch or self._urllib_fetch
        self._fetch_diff = fetch_diff or self._urllib_diff
        self._token = token if token is not None else os.environ.get("KB_GITHUB_READ_TOKEN", "")

    def _urllib_fetch(self, url: str) -> Any:
        request = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
            **({"Authorization": f"Bearer {self._token}"} if self._token else {}),
        })
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, ValueError, TimeoutError) as exc:
            raise SourceError(f"GitHub read failed: {url}: {exc}") from exc

    def get(self, path: str, **params: Any) -> Any:
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        return self._fetch(f"{self.API}{path}{query}")

    def _urllib_diff(self, url: str, max_bytes: int) -> str:
        request = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github.diff", "X-GitHub-Api-Version": "2022-11-28",
            **({"Authorization": f"Bearer {self._token}"} if self._token else {}),
        })
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read(max_bytes + 1)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise SourceError("GitHub could not read the complete PR diff") from exc
        if len(data) > max_bytes:
            raise SourceError(f"PR diff exceeds {max_bytes} bytes; no partial extraction")
        return data.decode("utf-8", "replace")

    def merged_prs_since(self, full_name: str, since_iso: str, *, after_number: int = 0,
                         limit: int = 50, max_pages: int = 10) -> list[tuple[int, str]]:
        """``(number, merged_at)`` of PRs merged at or after ``since_iso``, in
        merge-time order, bounded to ``limit``. Every page of the interval is
        read before sorting (search order is not merge order), so a caller that
        advances its cursor to the last RETURNED merge time never skips a PR:
        anything not returned merged at or after that time and is found again
        (events are idempotent). The cursor is the pair (merge time, PR number):
        PRs at exactly ``since_iso`` with a number <= ``after_number`` are
        already consumed, so many PRs sharing one merge second cannot stall it."""
        query = f"repo:{full_name} is:pr is:merged merged:>={since_iso}"
        found: dict[int, str] = {}
        for page in range(1, max_pages + 1):
            data = self.get("/search/issues", q=query, sort="updated", order="asc",
                            per_page=100, page=page)
            items = data.get("items", [])
            for item in items:
                merged_at = str((item.get("pull_request") or {}).get("merged_at") or "")
                if merged_at:
                    found[int(item["number"])] = merged_at
            if len(items) < 100:
                break
        ordered = sorted(
            (pair for pair in found.items()
             if (pair[1], pair[0]) > (since_iso, after_number)),
            key=lambda pair: (pair[1], pair[0]))
        return ordered[:limit]

    def pull_request(self, full_name: str, number: int) -> PullRequest:
        pr = self.get(f"/repos/{full_name}/pulls/{number}")
        files = self.get(f"/repos/{full_name}/pulls/{number}/files", per_page=100)
        paths = knowledge_changed_paths((item.get("filename", "") for item in files), limit=None)
        excerpt = knowledge_diff_excerpt(files, max_bytes=MAX_DIFF_BYTES)
        return PullRequest(
            number=int(pr["number"]), title=str(pr.get("title") or ""),
            body=str(pr.get("body") or ""), merged_at=str(pr.get("merged_at") or ""),
            merge_commit_sha=str(pr.get("merge_commit_sha") or ""),
            author=str((pr.get("user") or {}).get("login") or ""),
            changed_files=tuple(paths), diff_excerpt=excerpt,
            base_ref=str((pr.get("base") or {}).get("ref") or ""),
            head_ref=str((pr.get("head") or {}).get("ref") or ""),
            head_sha=str((pr.get("head") or {}).get("sha") or ""),
        )

    def merged_pr_history(self, full_name: str, *, limit: int = 1000, before: str,
                          max_pages: int = 1000) -> list[dict]:
        """Latest merged PRs as of ``before``, replayed oldest first.

        REST pagination avoids Search's 1,000-result ceiling. Closed pulls
        include unmerged PRs, and update order differs from merge order. We
        scan until a page's update boundary is older than the selected merge
        boundary (updated_at >= merged_at), or the list ends. An incomplete
        window is refused rather than silently represented as complete.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise SourceError("history limit must be a positive integer")
        found: dict[int, dict] = {}
        for page in range(1, max_pages + 1):
            batch = self.get(f"/repos/{full_name}/pulls", state="closed", sort="updated",
                             direction="desc", per_page=100, page=page)
            if not isinstance(batch, list):
                raise SourceError("GitHub returned a malformed PR-history page")
            for item in batch:
                merged = str(item.get("merged_at") or "")
                if merged and merged <= before:
                    number = int(item["number"])
                    found[number] = {"number": number, "merged_at": merged,
                                     "merge_commit_sha": str(item.get("merge_commit_sha") or "")}
            ordered = sorted(found.values(), key=lambda pr: (pr["merged_at"], pr["number"]), reverse=True)
            boundary = str(batch[-1].get("updated_at") or "") if batch else ""
            # Strict < includes all ties before choosing by PR number.
            if len(batch) < 100 or len(ordered) >= limit and boundary and boundary < ordered[limit - 1]["merged_at"]:
                return list(reversed(ordered[:limit]))
        raise SourceError(f"PR-history pagination exceeded {max_pages} pages; the requested window is incomplete")

    def history_evidence(self, full_name: str, number: int, *, max_bytes: int = 500_000) -> dict:
        """A complete PR discussion and patch, bounded by refusal, not truncation.

        Raw evidence is only returned in memory. Review-comment reply IDs
        preserve the thread; reviews and issue replies preserve chronology.
        The service's existing small ``pull_request`` excerpt is unchanged.
        """
        root = f"/repos/{full_name}/pulls/{int(number)}"
        pr = self.get(root)
        payload = {"number": int(pr["number"]), "title": str(pr.get("title") or ""),
                   "body": str(pr.get("body") or ""), "merged_at": str(pr.get("merged_at") or ""),
                   "merge_commit_sha": str(pr.get("merge_commit_sha") or "")}
        fields = {
            "files": (root + "/files", ("filename", "status")),
            "reviews": (root + "/reviews", ("id", "body", "state", "submitted_at", "commit_id")),
            "threads": (root + "/comments", ("id", "in_reply_to_id", "body", "path", "line",
                                              "original_line", "created_at", "commit_id")),
            "replies": (f"/repos/{full_name}/issues/{int(number)}/comments", ("id", "body", "created_at")),
        }
        def check_size() -> None:
            if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > max_bytes:
                raise SourceError(f"PR #{number} evidence exceeds {max_bytes} bytes; no partial extraction")

        check_size()
        for kind, (endpoint, keys) in fields.items():
            payload[kind] = []
            for page in range(1, 101):
                batch = self.get(endpoint, per_page=100, page=page)
                if not isinstance(batch, list):
                    raise SourceError(f"PR #{number} returned malformed {kind}")
                payload[kind].extend({key: item.get(key) for key in keys} for item in batch)
                check_size()
                if len(batch) < 100:
                    break
            else:
                raise SourceError(f"PR #{number} {kind} pagination is incomplete")
        # GitHub's files endpoint has a documented 3,000-file ceiling.
        if len(payload["files"]) != int(pr.get("changed_files") or 0):
            raise SourceError(f"PR #{number} changed-file evidence is incomplete")
        # Per-file REST patches can be omitted/truncated. Read the complete
        # diff representation separately, refusing oversized evidence.
        payload["diff"] = self._fetch_diff(self.API + root, max_bytes)
        check_size()
        return payload

    def issue_comments(self, full_name: str, number: int, *, after_id: int = 0, since: str = "",
                       max_pages: int = 20) -> list[dict]:
        """Comments on an issue with an id above ``after_id``, oldest first,
        updated after ``since`` (just before the last seen one's creation) so a caller
        that advances both past what it read is never stuck behind a window of
        ``max_pages`` pages."""
        out: list[dict] = []
        params = {"since": since} if since else {}
        for page in range(1, max_pages + 1):
            batch = self.get(f"/repos/{full_name}/issues/{int(number)}/comments", **params,
                             per_page=100, page=page) or []
            out.extend(c for c in batch if int(c.get("id") or 0) > after_id)
            if len(batch) < 100:
                break
        return sorted(out, key=lambda c: int(c.get("id") or 0))

    def latest_release(self, full_name: str) -> dict | None:
        releases = self.get(f"/repos/{full_name}/releases", per_page=5)
        for release in releases:
            if not release.get("draft") and not release.get("prerelease"):
                return {"tag": str(release["tag_name"]), "published_at": str(release.get("published_at") or "")}
        return None

    def tags(self, full_name: str, pattern: str) -> list[str]:
        regex = re.compile(_glob_to_regex(pattern))
        return [str(t["name"]) for t in self.get(f"/repos/{full_name}/tags", per_page=100)
                if regex.fullmatch(str(t["name"]))]


def _glob_to_regex(pattern: str) -> str:
    return "".join(".*" if ch == "*" else re.escape(ch) for ch in pattern)


from ..knowledge_service.evidence import (
    BUGFIX_MARKERS, bugfix_record_identity, parse_bugfix_record,
    knowledge_changed_paths, knowledge_diff_excerpt,
)


def bugfix_lesson(record) -> dict | None:
    """A verified pr_debug fix record (drop file or mailbox comment, v1/v2) as a
    lesson; None when malformed. ``repo_key`` is its repository identity."""
    identity = bugfix_record_identity(record)
    if identity is None:
        return None
    repo, run, event_id = identity
    groups = record.get("groups")
    if not repo or not run or not isinstance(groups, list) or not groups:
        return None
    lines = [str(record.get("title") or "Copilot debug run")]
    for group in groups[:10]:
        if not isinstance(group, dict) or not isinstance(group.get("files") or [], list):
            return None
        lines += [f"- failure: {str(group.get('signature', ''))[:300]}",
                  f"  root cause: {str(group.get('root_cause', ''))[:2000]}",
                  f"  fix: {str(group.get('fix_summary', ''))[:2000]}",
                  f"  verified by: {str(group.get('verification', ''))[:1000]}",
                  "  files: " + ", ".join(str(f)[:300] for f in (group.get("files") or [])[:20])]
    return {"repo_key": repo, "event_id": event_id, "run_ref": run,
            "summary": "\n".join(lines)[:MAX_BODY_CHARS], "diff_excerpt": ""}


def resolve_repository(key: str, registry) -> str | None:
    """The one configured repository ``key`` names (full name, case-insensitive,
    or exact alias); None when none does; "?" when it is ambiguous."""
    matches = {name for name, lc in registry.items()
               if (lc.full_name and lc.full_name.casefold() == key.casefold()) or name == key}
    if len(matches) > 1:
        return "?"
    return next(iter(matches), None)


def load_bugfix_drops(directory: str | Path, registry, repo: str) -> list[dict]:
    """Fix records that runs on this host dropped for ``repo``. Records naming
    another configured repository stay for its turn; an ambiguous identity is
    quarantined (renamed ``.quarantined``), never rewritten."""
    directory = Path(directory)
    lessons = []
    if not directory.is_dir():
        return lessons
    for path in sorted(directory.glob("*.json")):
        try:
            lesson = bugfix_lesson(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        if lesson is None:
            continue  # malformed: left for inspection
        owner = resolve_repository(lesson["repo_key"], registry)
        if owner == "?":
            path.rename(path.with_suffix(".quarantined"))
            continue
        if owner == repo:
            lessons.append({**lesson, "path": str(path)})
    return lessons


def mailbox_records(comments, authors: set[str]) -> list[tuple[int, dict]]:
    """(comment id, record) of marked fix-record comments by allowed authors.
    The mailbox is a public issue: anyone else's comment is ignored."""
    out = []
    for comment in comments:
        login = str((comment.get("user") or {}).get("login") or "").lower()
        body = str(comment.get("body") or "")
        if login not in authors:
            continue
        record = parse_bugfix_record(body)
        if record is None:
            continue
        try:
            out.append((int(comment["id"]), record))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def load_lessons(inbox: str | Path, repo: str) -> list[dict]:
    """Copilot run lessons dropped by runs on this host (``inbox/lessons/<repo>/``).
    Each file: {event_id, repo, run_ref, summary, diff_excerpt?}. Malformed files
    are skipped (and left for inspection), never guessed at."""
    directory = Path(inbox) / "lessons" / repo
    lessons = []
    if not directory.is_dir():
        return lessons
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("repo") != repo or not data.get("event_id") \
                or not data.get("summary"):
            continue
        lessons.append({
            "event_id": str(data["event_id"]), "run_ref": str(data.get("run_ref") or ""),
            "summary": str(data["summary"])[:MAX_BODY_CHARS],
            "diff_excerpt": str(data.get("diff_excerpt") or "")[:MAX_DIFF_BYTES],
            "path": str(path),
        })
    return lessons
