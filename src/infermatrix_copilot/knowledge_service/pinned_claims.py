"""Rule claims and code evidence checked at ONE pinned upstream SHA.

``facts.attest`` observes the upstream's current head: right for the service,
which gates a change against the upstream as it is now. A bootstrap (``kb
init``) writes a whole knowledge base against one pinned baseline instead, and
must prove every rule it writes holds *at that pin* — including that a cited PR
was merged before it. This module does that from a local clone the caller
already has checked out:

* ``PinnedObserver`` implements ``facts.Observer`` over a local git repository
  (bare or not) with ``head()`` fixed to the pin, plus ``is_ancestor`` (which
  the service's Observer does not need and does not have);
* ``check_rules`` extracts each rule's claims exactly as the service does
  (``facts.claims_in``) and observes them at the pin;
* ``Evidence`` / ``evidence_for`` / ``check_evidence`` bind a line range of an
  upstream file at the pin to a content hash, so the range can be re-verified.

Evidence hash: the file text is split with ``str.splitlines()`` (so ``\\n``,
``\\r\\n`` and a missing final newline all count the same), lines ``start`` to
``end`` (1-based, inclusive) are joined with ``\\n`` plus one trailing ``\\n``,
and the UTF-8 bytes are hashed with SHA-256.

Standard library only (plus ``facts``). Nothing here writes anywhere.
"""

from __future__ import annotations

import hashlib
from ..git_objects import tree_entries
import json
import re
import subprocess
from threading import RLock
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .facts import FactsError, claims_in, describe, holds, merge_claims, observe

_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


def _gh_pull(repository: str, number: int) -> dict:
    proc = subprocess.run(["gh", "api", f"repos/{repository}/pulls/{number}"],
                          capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise FactsError(f"{repository} PR #{number}: {proc.stderr.strip()[:300]}")
    try:
        data = json.loads(proc.stdout)
    except ValueError as exc:
        raise FactsError(f"{repository} PR #{number}: unexpected answer") from exc
    if not isinstance(data, dict):
        raise FactsError(f"{repository} PR #{number}: unexpected answer")
    return data


class PinnedObserver:
    """``facts.Observer`` over a local clone, answering for one pinned commit."""

    def __init__(self, repo_dir: str | Path, repository: str, pin: str, *,
                 pull: Callable[[str, int], dict] | None = None):
        self.repo_dir = Path(repo_dir)
        self.repository = repository
        if not _SHA.fullmatch(pin or ""):
            raise FactsError(f"pin must be a 40-hex commit SHA, got {pin!r}")
        self.pin = pin
        self._pull = pull or _gh_pull
        self._commits: set[str] = set()
        # Successful immutable Git reads can be reused within this observer.
        # A fresh observer starts empty; read failures and live PR metadata
        # are never cached. Locking also avoids duplicate concurrent reads.
        self._read_lock = RLock()
        self._entries: dict[tuple[str, str], tuple[str, str] | None] = {}
        self._texts: dict[tuple[str, str], str] = {}
        self._top_levels: dict[str, frozenset[str]] = {}
        self._require_commit(pin)

    def _git(self, *args: str, input: bytes | None = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(["git", "-C", str(self.repo_dir), *args],
                                  capture_output=True, check=False, input=input)
        except OSError as exc:
            raise FactsError(f"git unavailable: {exc}") from exc

    def _require_commit(self, sha: str) -> None:
        if sha in self._commits:
            return
        proc = self._git("cat-file", "-t", sha)
        if proc.returncode != 0 or proc.stdout.decode().strip() != "commit":
            raise FactsError(f"{sha[:12]} is not a commit in {self.repo_dir}")
        self._commits.add(sha)

    def head(self) -> str:
        return self.pin

    def _immutable_commit(self, sha: str) -> str:
        if not isinstance(sha, str) or not sha:
            raise FactsError("a commit identity is required")
        if not _SHA.fullmatch(sha):
            proc = self._git("rev-parse", "--verify", "--end-of-options", sha)
            resolved = proc.stdout.decode().strip()
            if proc.returncode != 0 or not _SHA.fullmatch(resolved):
                raise FactsError(f"cannot resolve commit {sha!r}: {proc.stderr.decode()[:300]}")
            sha = resolved
        self._require_commit(sha)
        return sha

    def top_level(self, sha: str) -> set[str]:
        with self._read_lock:
            sha = self._immutable_commit(sha)
            if sha not in self._top_levels:
                proc = self._git("ls-tree", "--name-only", sha)
                if proc.returncode != 0:
                    raise FactsError(f"cannot list {sha[:12]}: {proc.stderr.decode()[:300]}")
                self._top_levels[sha] = frozenset(line for line in proc.stdout.decode("utf-8", "replace").splitlines() if line)
            return set(self._top_levels[sha])  # callers cannot mutate the cache

    def _entry(self, sha: str, path: str) -> tuple[str, str] | None:
        """``(type, object id)`` of ``path`` in ``sha``'s tree; None when the
        tree has no such entry. Any git failure (unreadable tree, failed lazy
        fetch) raises: it is not evidence that the path is absent."""
        with self._read_lock:
            sha = self._immutable_commit(sha)
            path = path.strip("/")
            if not path:
                return None
            key = (sha, path)
            if key in self._entries:
                return self._entries[key]
            proc = self._git("ls-tree", "-z", sha, "--", path)
            if proc.returncode != 0:
                raise FactsError(f"cannot list {path} at {sha[:12]}: {proc.stderr.decode()[:300]}")
            for mode, kind, oid, name in tree_entries(proc.stdout, malformed="skip"):
                if name.decode("utf-8", "replace") == path:
                    self._entries[key] = (kind, oid)
                    return self._entries[key]
            if not proc.stdout:
                self._entries[key] = None  # an empty successful lookup is immutable
            return None  # unexpected output is not cached

    def path_exists(self, sha: str, path: str) -> bool:
        return self._entry(sha, path) is not None

    def file_text(self, sha: str, path: str) -> str | None:
        """The file's text at ``sha``; None unless ``path`` is a file (blob).
        An entry that exists but cannot be read raises ``FactsError``."""
        with self._read_lock:
            sha = self._immutable_commit(sha)
            key = (sha, path)
            if key in self._texts:
                return self._texts[key]
            entry = self._entry(sha, path)
            if entry is None or entry[0] != "blob":
                return None  # missing, or a directory / submodule: no file text
            proc = self._git("cat-file", "blob", entry[1])
            if proc.returncode != 0:
                raise FactsError(f"cannot read {path} at {sha[:12]}: {proc.stderr.decode()[:300]}")
            self._texts[key] = proc.stdout.decode("utf-8", "replace")
            return self._texts[key]

    def pull(self, number: int) -> dict:
        """The PR as GitHub reports it. Every lookup failure (no ``gh``, a
        network error, a malformed answer) is a ``FactsError``: the upstream
        could not be read, so the caller retries instead of crashing."""
        try:
            data = self._pull(self.repository, number)
        except FactsError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalise any lookup failure
            raise FactsError(f"{self.repository} PR #{number}: {type(exc).__name__}: {exc}"[:400]) from exc
        if not isinstance(data, dict):
            raise FactsError(f"{self.repository} PR #{number}: unexpected answer")
        return data

    def is_ancestor(self, sha: str) -> bool:
        """``sha`` is the pin or one of its ancestors (an unknown SHA is not).
        Raises ``FactsError`` when git cannot answer, so a read failure is
        retried rather than reported as a PR merged after the pin. git exits
        1 both for "not an ancestor" and when it could not read the history
        walk (it then only prints ``error: Could not read``), so a 1 counts
        as "no" only with a silent stderr."""
        if not _SHA.fullmatch(sha or ""):
            return False
        # --batch-check says "<sha> missing" with a silent stderr only when the
        # object is absent; a corrupt object ALSO prints "missing" but reports
        # the unpack error on stderr, and that is a read failure, not absence
        check = self._git("cat-file", "--batch-check", input=f"{sha}\n".encode())
        answer = check.stdout.decode(errors="replace").split()
        if check.returncode != 0 or check.stderr.strip():
            raise FactsError(f"cannot read {sha[:12]}: {check.stderr.decode(errors='replace')[:300]}")
        if answer[1:2] == ["missing"]:
            return False  # a commit this mirror does not have: unknown, so not an ancestor
        if answer[1:2] != ["commit"]:
            return False  # a blob/tree/tag id is not a merge commit
        proc = self._git("merge-base", "--is-ancestor", sha, self.pin)
        if proc.returncode == 0:
            return True
        if proc.returncode == 1 and not proc.stderr.strip():
            return False
        raise FactsError(f"cannot decide whether {sha[:12]} is an ancestor of {self.pin[:12]}: "
                         f"{proc.stderr.decode(errors='replace')[:300]}")


def check_rules(rule_texts: Mapping[str, str], observer: PinnedObserver) -> list[str]:
    """Problems with the claims of active rules (rule ID -> rule text) at the
    pin: every claim must hold, and a cited PR must have been merged into the
    pin's history. Raises ``FactsError`` when the upstream cannot be read."""
    pin = observer.head()
    top = observer.top_level(pin)
    problems: list[str] = []
    seen: dict[tuple, dict] = {}
    for rule_id, text in rule_texts.items():
        for claim in merge_claims(claims_in(text, top, active=True)):
            fact = seen.get(claim.key)
            if fact is None:
                fact = seen[claim.key] = observe(claim, observer, pin)
            if not holds(fact):
                problems.append(f"{rule_id}: {describe(fact)}")
            elif claim.kind == "pr" and not observer.is_ancestor(fact.get("merge_commit_sha", "")):
                problems.append(f"{rule_id}: {observer.repository} PR #{claim.pr} was merged "
                                f"after {pin[:12]} (not in the pinned history)")
    return problems


@dataclass(frozen=True)
class Evidence:
    """Lines ``start``..``end`` (1-based, inclusive) of ``path`` at the pin."""
    path: str
    start: int
    end: int
    sha256: str

    def to_dict(self) -> dict:
        return {"path": self.path, "start": self.start, "end": self.end, "sha256": self.sha256}

    @classmethod
    def from_dict(cls, data: Mapping) -> "Evidence":
        return cls(str(data["path"]), int(data["start"]), int(data["end"]), str(data["sha256"]))


def _range_text(text: str, start: int, end: int) -> str | None:
    lines = text.splitlines()
    if not 1 <= start <= end <= len(lines):
        return None
    return "\n".join(lines[start - 1:end]) + "\n"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def evidence_for(observer: PinnedObserver, path: str, start: int, end: int) -> Evidence:
    """Bind a line range at the pin to its hash (raises ``FactsError`` when the
    file is missing or the range does not fit)."""
    text = observer.file_text(observer.head(), path)
    if text is None:
        raise FactsError(f"{path} does not exist at {observer.head()[:12]}")
    chunk = _range_text(text, start, end)
    if chunk is None:
        raise FactsError(f"{path}:L{start}-L{end} is outside the file at {observer.head()[:12]}")
    return Evidence(path, start, end, _digest(chunk))


def _evidence_chunk(entry: Evidence, observer: PinnedObserver, texts: dict,
                    *, verify_hash: bool) -> tuple[str | None, str | None]:
    pin = observer.head()
    where = f"{entry.path}:L{entry.start}-L{entry.end}"
    if entry.path not in texts:
        texts[entry.path] = observer.file_text(pin, entry.path)
    text = texts[entry.path]
    if text is None:
        return None, f"{where}: file does not exist at {pin[:12]}"
    chunk = _range_text(text, entry.start, entry.end)
    if chunk is None:
        return None, f"{where}: range is outside the file at {pin[:12]}"
    if verify_hash and _digest(chunk) != entry.sha256:
        return None, f"{where}: content differs from the recorded evidence at {pin[:12]}"
    return chunk, None


def evidence_text(observer: PinnedObserver, entry: Evidence, *, verify_hash: bool = True) -> str | None:
    """Materialize a pinned range with the canonical evidence checks.

    Historical comparison readers can select ``verify_hash=False`` to inspect
    changed source at a newer pin; missing/outside ranges then return None.
    Git read failures always propagate. Returned text has no added final newline.
    """
    chunk, problem = _evidence_chunk(entry, observer, {}, verify_hash=verify_hash)
    if problem and verify_hash:
        raise FactsError(problem)
    return chunk[:-1] if chunk is not None else None


def check_evidence(entries: Iterable[Evidence], observer: PinnedObserver) -> list[str]:
    """Problems with recorded ranges and their content hashes at the pin."""
    texts: dict[str, str | None] = {}
    problems = []
    for entry in entries:
        _, problem = _evidence_chunk(entry, observer, texts, verify_hash=True)
        if problem:
            problems.append(problem)
    return problems
