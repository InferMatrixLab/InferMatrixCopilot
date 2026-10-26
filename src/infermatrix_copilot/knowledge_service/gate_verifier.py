"""The knowledge verifier: the checks the publisher's local gate runs (design v8).

The publisher is the only thing that merges knowledge PRs; right before each
merge it runs ``verify_change`` on the exact tree that would land (see
``kb_service.local_gate``). A change passes only when all of these hold:

1. L1: whitelisted paths only, regular files only, no new tree issues, on the
   PR's own change AND on the tree that would land (including references to
   rules the change retires or purges);
2. the signed patch manifest equals the change as applied (base independent);
3. the signed block table equals the blocks L1 derives, every block passed,
   the consistency judgements cover the same owner directories and their page
   hashes match the tree that would land;
4. no page in the context set changed between the judged base and the
   effective base.

The verdict's signature and binding (repository, PR, head, issue window) are
checked by the caller. PR content is only ever READ through git objects;
nothing from the PR is executed or installed.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Mapping

from ..git_objects import checked_read, raw_changes, tree_texts

from .l1 import Change, _dangling_refs as dangling_refs, check_changeset, check_tree
from .lifecycle import LifecycleError, Page
from .ops import all_rule_ids
from .verdict import manifests_equal

KNOWLEDGE_PREFIX = "knowledge/"
GOVERNED_PREFIXES = ("knowledge/repos/", "knowledge/general/")
GOVERNED_SUFFIXES = (".md", ".yaml")
EXTERNAL_REF_DIRS = ("skills/", "plugins/", "adapters/", "doc/", "playbooks/")
EXTERNAL_SUFFIXES = (".md", ".yaml", ".yml", ".json", ".txt")


class GateError(RuntimeError):
    """The verifier could not establish a fact it needs (fails closed)."""


# -- git (objects only; nothing from the PR is ever checked out or executed) ---

class Git:
    def __init__(self, cwd: str | Path):
        self.cwd = str(cwd)

    def run(self, *args: str, input: bytes | None = None, ok: tuple[int, ...] = (0,)) -> bytes:
        proc = subprocess.run(["git", *args], cwd=self.cwd, input=input,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if proc.returncode not in ok:
            raise GateError(f"git {' '.join(args)} failed: {proc.stderr.decode(errors='replace')[:400]}")
        return proc.stdout

    def rev(self, name: str) -> str:
        return self.run("rev-parse", "--verify", "--quiet", f"{name}^{{commit}}").decode().strip()

    def tree_of(self, name: str) -> str:
        return self.run("rev-parse", "--verify", f"{name}^{{tree}}").decode().strip()

    def parents(self, sha: str) -> list[str]:
        return self.run("rev-list", "--parents", "-n", "1", sha).decode().split()[1:]

    def is_ancestor(self, older: str, newer: str) -> bool:
        proc = subprocess.run(["git", "merge-base", "--is-ancestor", older, newer], cwd=self.cwd,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False)
        if proc.returncode not in (0, 1):
            raise GateError(f"cannot relate {older[:12]} and {newer[:12]}: "
                            f"{proc.stderr.decode(errors='replace')[:200]}")
        return proc.returncode == 0

    def merge_base(self, a: str, b: str) -> str:
        return self.run("merge-base", a, b).decode().strip()

    def first_parent_chain(self, base: str, head: str) -> list[str]:
        return self.run("rev-list", "--first-parent", "--reverse", f"{base}..{head}").decode().split()

    def merge_tree(self, base: str, head: str) -> str:
        """The tree a merge of ``head`` into ``base`` would produce (PR stage)."""
        proc = subprocess.run(["git", "merge-tree", "--write-tree", base, head], cwd=self.cwd,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if proc.returncode != 0 and b"--write-tree" in proc.stderr:  # git < 2.38: no --write-tree
            return self._index_merge(base, head)
        if proc.returncode != 0:
            raise GateError("the PR does not merge cleanly into the current base")
        return proc.stdout.decode().split()[0]

    def _index_merge(self, base: str, head: str) -> str:
        """Only merges where each path changed on at most one side; anything
        else is reported as not merging cleanly (stricter, never looser)."""
        import tempfile

        with tempfile.TemporaryDirectory() as scratch:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
            for args in (["read-tree", "-i", "-m", self.merge_base(base, head), base, head], ["write-tree"]):
                proc = subprocess.run(["git", *args], cwd=self.cwd, env=env, check=False,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if proc.returncode != 0:
                    raise GateError("the PR does not merge cleanly into the current base")
            return proc.stdout.decode().strip()

    def raw_diff(self, old: str, new: str) -> list[dict]:
        """Every changed path as a manifest entry (renames are D + A)."""
        return checked_read(GateError, lambda: raw_changes(
            self.run("diff", "--raw", "-z", "--no-renames", "--no-abbrev", old, new)))

    def changed_names(self, old: str, new: str) -> list[str]:
        out = self.run("diff", "--name-only", "-z", "--no-renames", old, new, "--", "knowledge/")
        return [name.decode("utf-8", "replace") for name in out.split(b"\0") if name]

    def texts(self, rev: str, prefixes: tuple[str, ...], suffixes: tuple[str, ...]) -> dict[str, str]:
        return checked_read(GateError, tree_texts, self.run, rev, prefixes, suffixes)

    def knowledge_files(self, rev: str) -> dict[str, str]:
        files = self.texts(rev, GOVERNED_PREFIXES, GOVERNED_SUFFIXES)
        return {name[len(KNOWLEDGE_PREFIX):]: text for name, text in files.items()}

    def external_texts(self, rev: str) -> dict[str, str]:
        return self.texts(rev, EXTERNAL_REF_DIRS, EXTERNAL_SUFFIXES)

    def show(self, rev: str, path: str) -> str | None:
        proc = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=self.cwd,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
        return proc.stdout.decode("utf-8", "replace") if proc.returncode == 0 else None


# -- the verification of one change ------------------------------------------------

@dataclass
class Context:
    git: Git
    repository: str
    now: float


def governed(path: str) -> bool:
    """A page the knowledge lifecycle governs (what ``knowledge_files`` reads)."""
    return path.startswith(GOVERNED_PREFIXES) and path.endswith(GOVERNED_SUFFIXES)


def touches_knowledge(entries: Iterable[Mapping[str, str]]) -> bool:
    return any(entry["path"].startswith(KNOWLEDGE_PREFIX) for entry in entries)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _owner_dir(path: str) -> str:
    return str(PurePosixPath(path).parent)


def _scope_root(path: str) -> str:
    parts = PurePosixPath(path).parts
    return "/".join(parts[:2]) if parts[:1] == ("repos",) else parts[0]


def context_paths(touched: set[str], owner_dirs: set[str], post: Mapping[str, str],
                  base: Mapping[str, str]) -> Callable[[str], bool]:
    """The pages whose change since the judged base invalidates L2: the touched
    pages, every page in their owner directories, the scope's _routes.yaml and
    _index.md files (scope root and owner directories), and pages holding rules
    the change supersedes."""
    roots = {_scope_root(path) for path in touched}
    dirs = owner_dirs | {_owner_dir(path) for path in touched}
    superseded: set[str] = set()
    for path in touched:
        text = post.get(path)
        if not text or not path.endswith(".md"):
            continue
        try:
            sections = Page.parse(text).rules()
        except (LifecycleError, ValueError):
            continue
        for section in sections:
            try:
                target = section.footer.supersedes
            except LifecycleError:
                continue
            for rule_id in [t.strip() for t in target.split(",") if t.strip()]:
                superseded.update(p for p, t in base.items()
                                  if p.endswith(".md") and rule_id in all_rule_ids({p: t}))

    def relevant(path: str) -> bool:
        name = PurePosixPath(path).name
        parent = _owner_dir(path)
        return (path in touched or path in superseded or parent in dirs
                or (name in ("_routes.yaml", "_index.md") and parent in roots | dirs))
    return relevant


def verify_change(ctx: Context, *, pr: dict, head_sha: str, pre: str, post: str, final: str,
                  effective_base: str, verdict: dict) -> list[str]:
    """Problems with one PR's knowledge change (empty when it passes).

    ``pre``/``post`` are the commits (or trees) around this PR's change as it
    is applied; ``final`` is the tree that would land; ``effective_base`` is
    the state the change is applied on (current main, or the merge commit's
    first parent). ``verdict`` is the signed verdict the caller verified."""
    number = int(pr["number"])
    entries = ctx.git.raw_diff(pre, post)
    if not touches_knowledge(entries):
        return []
    problems: list[str] = []
    if pr.get("state") != "open":
        problems.append(f"PR #{number} is not open")
    if str((pr.get("head") or {}).get("sha")) != head_sha:
        problems.append(f"PR #{number} head moved from {head_sha[:12]}")

    pre_files, post_files = ctx.git.knowledge_files(pre), ctx.git.knowledge_files(post)
    final_files = ctx.git.knowledge_files(final)
    changes = [Change(e["path"], e["status"] if e["status"] in "AMD" else "M", e["old_mode"], e["new_mode"])
               for e in entries]
    for e in entries:
        if e["status"] not in ("A", "M", "D"):
            problems.append(f"{e['path']}: change type {e['status']} is not allowed")
    touched = {e["path"][len(KNOWLEDGE_PREFIX):] for e in entries if e["path"].startswith(KNOWLEDGE_PREFIX)}

    if verdict.get("source") != "auto":
        problems.append("only auto verdicts merge (there is no human-approved path)")
    if not manifests_equal(verdict.get("manifest") or [], entries):
        problems.append("the signed patch manifest does not match the change as applied")

    result = check_changeset(pre_files, post_files, changes,
                             external_texts=ctx.git.external_texts(final),
                             release=str(verdict.get("release") or ""))
    problems.extend(f"L1 {i.code} {i.path} {i.detail}".rstrip() for i in result.issues)
    # what this change retires or purges must not be cited anywhere in the tree
    # that would land (main may have added a citation meanwhile)
    gone = set(result.retired) | set(result.purged)
    if gone:
        try:
            problems.extend(f"L1 {i.code} {i.path} {i.detail} (in the tree that would land)"
                            for i in dangling_refs(final_files, gone))
        except (LifecycleError, ValueError) as exc:
            problems.append(f"L1 the tree that would land does not parse: {exc}")

    if result.external_refs:
        problems.append("retired rules are still cited outside knowledge/ (needs a companion PR)")
    signed_blocks = verdict.get("blocks") or []
    if {b.block_id for b in result.blocks} != {b.get("block_id") for b in signed_blocks}:
        problems.append("the signed block table does not cover exactly the changed blocks")
    if any(b.get("verdict") != "pass" for b in signed_blocks):
        problems.append("a signed block did not pass")
    owner_dirs = {_owner_dir(b.path) for b in result.blocks}
    consistency = verdict.get("consistency") or []
    if {c.get("owner_dir") for c in consistency} != owner_dirs:
        problems.append("the consistency judgement does not cover exactly the changed owner directories")
    for item in consistency:
        directory = item.get("owner_dir")
        actual = {p: _sha256(t) for p, t in final_files.items()
                  if _owner_dir(p) == directory and p.endswith(".md")}
        if item.get("verdict") != "consistent":
            problems.append(f"{directory}: consistency was not judged consistent")
        if item.get("pages") != actual:
            problems.append(f"{directory}: pages changed since the consistency judgement")
    base_sha = str(verdict["context_base_sha"])
    try:
        if not ctx.git.is_ancestor(base_sha, effective_base):
            problems.append("the judged context base is not an ancestor of the effective base")
        else:
            relevant = context_paths(touched, owner_dirs, post_files, pre_files)
            moved = [n[len(KNOWLEDGE_PREFIX):] for n in ctx.git.changed_names(base_sha, effective_base)]
            stale = sorted(p for p in moved if relevant(p))
            if stale:
                problems.append("context changed since the verdict was judged: " + ", ".join(stale[:5]))
    except GateError as exc:
        problems.append(f"context base: {exc}")
    return problems


def tree_problems(git: Git, base: str, final: str) -> list[str]:
    """L1 on the tree that would land: no tree-level issue that base lacks."""
    before = {(i.code, i.detail) for i in check_tree(git.knowledge_files(base))}
    return [f"L1 {i.code} {i.path} {i.detail}".rstrip()
            for i in check_tree(git.knowledge_files(final)) if (i.code, i.detail) not in before]
