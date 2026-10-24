"""Skills — procedural knowledge, governed more strictly than debug memory.

Agents may only PROPOSE skills (candidates file); promotion to a real SKILL.md
is a curator/human action. Facts recorded freely, knowledge promoted via gates.
"""

from __future__ import annotations

from ..persistence import atomic_write_bytes

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml


def read_usage_counts(journal: str | Path) -> dict[str, int]:
    """{skill name: journal usage count} — the read side of the usage
    prior (seed frontmatter run_count is frozen at run time, so ranking
    adds these journal counts on top; a write-only journal would silently
    remove the proven-skill tie-breaker)."""
    journal = Path(journal)
    if not journal.is_file():
        return {}
    counts: dict[str, int] = {}
    for line in journal.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            name = str(json.loads(line).get("name") or "")
        except ValueError:
            continue  # torn tail tolerated, like every jsonl reader here
        if name:
            counts[name] = counts.get(name, 0) + 1
    return counts


def append_usage(journal: str | Path, name: str) -> None:
    """Append one seed-skill usage record to the runtime usage journal
    (flock + fsync). Seed `SKILL.md` files are READ-ONLY at run time
    (Rev 8 §10) — the usage prior that `touch()` used to write into their
    frontmatter accumulates here instead."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        fcntl = None  # type: ignore
    journal = Path(journal)
    journal.parent.mkdir(parents=True, exist_ok=True)
    entry = json.dumps({"name": name,
                        "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                       ensure_ascii=False)
    with open(journal, "a", encoding="utf-8") as fh:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.write(entry + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _write_durable(path: Path, text: str) -> None:
    """Crash-safe file rewrite: UNIQUE same-directory temp + fsync(file) +
    atomic rename + fsync(dir). A crash mid-write leaves the old content
    intact — a truncated candidates file or half a SKILL.md must be
    impossible — and the unique temp name means two concurrent writers can
    never truncate each other's inode or install a half-written payload
    (last rename wins whole)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, text.encode("utf-8"), mode=0o600, directory_fsync=_fsync_dir)


def _fsync_dir(directory: Path) -> None:
    """Best-effort directory fsync — a durability upgrade on POSIX, a no-op
    where directories cannot be opened (Windows): the rename above already
    landed, so skipping the fsync must never turn a successful write into a
    reported failure."""
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:  # pragma: no cover - non-POSIX
        return
    try:
        os.fsync(dir_fd)
    except OSError:  # pragma: no cover - fs without dir-fsync
        pass
    finally:
        try:
            os.close(dir_fd)
        except OSError:  # pragma: no cover — a landed rename must never
            pass         # be reported as failure by cleanup trouble


@dataclass
class Skill:
    """One parsed SKILL.md: its frontmatter metadata (`name`, `description`,
    `trigger`, `modules`, `status`, `run_count`) plus the markdown `body`.
    `modules` is the join key for module-scoped retrieval; `run_count` is the
    usage prior that breaks ranking ties toward proven skills."""

    name: str
    description: str = ""
    trigger: str = ""
    modules: list[str] = field(default_factory=list)
    status: str = "active"
    run_count: int = 0
    body: str = ""


def _parse_skill(path: Path) -> Skill | None:
    """Parse a `SKILL.md` at `path` into a `Skill`, or None when the file lacks
    the leading `---` frontmatter fence or the YAML is malformed. Missing scalar
    fields fall back to defaults (name defaults to the containing dir name), so a
    partial-but-valid file still loads rather than being dropped."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None
    try:
        _, fm, body = text.split("---", 2)
        meta = yaml.safe_load(fm)
    except (ValueError, yaml.YAMLError):
        return None
    if meta is None:
        # safe_load returns None for BLANK frontmatter and for an
        # explicit `null`/`~` scalar alike — only genuinely blank text
        # earns the field defaults; a written-out null (or comment-only
        # doc resolving to null) is skipped like any non-mapping
        # (verification-round finding)
        if fm.strip():
            return None
        meta = {}
    if not isinstance(meta, dict):
        return None  # list/scalar/falsey frontmatter has no fields
    return Skill(
        name=meta.get("name", path.parent.name),
        description=str(meta.get("description", "")),
        trigger=str(meta.get("trigger", "")),
        modules=list(meta.get("modules", []) or []),
        status=str(meta.get("status", "active")),
        run_count=int(meta.get("run_count", 0) or 0),
        body=body.strip(),
    )


class SkillStore:
    """Directory of promoted `<name>/SKILL.md` skills plus a `_candidates.json`
    holding agent-proposed but not-yet-promoted skills. Enforces the governance
    split: agents may only `propose`; `promote` (writing a real SKILL.md) is a
    curator/human action."""

    def __init__(self, directory: str | Path):
        """Bind to the skills `directory`; candidates live in `_candidates.json`
        under it. Neither path is required to exist yet."""
        self.directory = Path(directory)
        self.candidates_file = self.directory / "_candidates.json"

    def _candidates_lock(self):
        """Exclusive flock serializing the candidates read-modify-write —
        concurrent `propose()`/`promote()` calls (runs share the knowledge
        lock only SHARED) must not lose each other's updates. Returns an
        open fd holder context manager; a no-op on platforms without
        fcntl."""
        import contextlib

        try:
            import fcntl
        except ImportError:  # pragma: no cover - Windows
            return contextlib.nullcontext()

        @contextlib.contextmanager
        def _lock():
            self.directory.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.directory / "_candidates.lock",
                         os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

        return _lock()

    def load_all(self) -> list[Skill]:
        """Parse and return every active promoted skill under the directory
        (sorted by path). Non-active or unparseable `SKILL.md` files are skipped;
        an absent directory yields an empty list."""
        skills = []
        if self.directory.exists():
            for p in sorted(self.directory.glob("*/SKILL.md")):
                s = _parse_skill(p)
                if s and s.status == "active":
                    skills.append(s)
        return skills

    def find(self, query: str = "", module: str = "", k: int = 3,
             extra_run_counts: dict | None = None) -> list[Skill]:
        """Return up to `k` active skills ranked for the given `query`/`module`.
        Ranking key (descending): module match first, then count of query words
        found in description+trigger, then `run_count` as the usage tie-breaker.
        `extra_run_counts` adds journal-accumulated usage on top of the
        frontmatter count (seed files are read-only at run time, so their
        usage prior lives in the runtime journal — the read side the
        write-only journal was missing). With a query or module supplied,
        zero-relevance skills are dropped; with neither, the top `k` by
        run_count are returned as a default surface."""
        extra = extra_run_counts or {}

        def relevance(s: Skill) -> tuple:
            """(module_hit, query-word overlap) — the RELEVANCE filter;
            usage counts never make an unrelated skill relevant
            (round-3 F7)."""
            module_hit = 1 if module and module in s.modules else 0
            text_hit = sum(
                1 for w in query.lower().split()
                if w in (s.description + " " + s.trigger).lower()
            )
            return (module_hit, text_hit)

        def score(s: Skill) -> tuple:
            """Rank tuple: relevance first, then the usage prior
            (frontmatter + journal) strictly as the tie-breaker."""
            return (*relevance(s), s.run_count + int(extra.get(s.name, 0)))

        ranked = sorted(self.load_all(), key=score, reverse=True)
        return [s for s in ranked[:k]
                if relevance(s) != (0, 0) or not (query or module)]

    # -- write gate: propose -> candidate; promote is curator/human ----------
    def propose(self, *, name: str, description: str, body: str,
                modules: list[str] | None = None,
                identity: str = "") -> None:
        """Record a proposed skill (keyed by `name`) into `_candidates.json` with
        a `proposed_at` timestamp — the only write agents are permitted. Re-using
        a name overwrites its candidate. No SKILL.md is created until `promote`.
        `identity` is an optional caller-defined stable key (the curator stores
        its module+key pair) so a re-proposal of the SAME pattern can be told
        apart from a slug collision without parsing prose."""
        with self._candidates_lock():
            candidates = self._load_candidates()
            candidates[name] = {
                "name": name, "description": description, "body": body,
                "modules": modules or [], "proposed_at": time.time(),
            }
            if identity:
                candidates[name]["identity"] = identity
            _write_durable(self.candidates_file,
                           json.dumps(candidates, indent=2,
                                      ensure_ascii=False))

    def propose_if_new_identity(self, *, base_name: str, identity: str,
                                description: str, body: str,
                                modules: list[str] | None = None
                                ) -> str | None:
        """Atomic identity-deduped proposal (the curator's write path):
        under the candidates flock, in ONE critical section — if any
        pending candidate already carries `identity`, nothing is written
        (returns None); otherwise the name is allocated (`base_name`, or a
        digest-suffixed variant when the base is taken by a DIFFERENT
        identity; a suffix collision refuses with None rather than
        overwriting) and the candidate is recorded. Concurrent curators
        can therefore never re-propose one identity or overwrite each
        other's colliding names."""
        import hashlib

        with self._candidates_lock():
            candidates = self._load_candidates()
            if any(str(c.get("identity", "")) == identity
                   for c in candidates.values()):
                return None

            def _taken(candidate_name: str) -> bool:
                # pending candidates AND promoted skills both occupy the
                # name: a same-named candidate would let a later
                # promote() overwrite an ACTIVE skill
                return candidate_name in candidates or \
                    (self.directory / candidate_name / "SKILL.md").exists()

            name = base_name
            if _taken(name):
                name = base_name + "-" + hashlib.sha256(
                    identity.encode("utf-8")).hexdigest()[:8]
                if _taken(name):
                    return None  # digest collision too: refuse loudly
            candidates[name] = {
                "name": name, "description": description, "body": body,
                "modules": modules or [], "proposed_at": time.time(),
                "identity": identity,
            }
            _write_durable(self.candidates_file,
                           json.dumps(candidates, indent=2,
                                      ensure_ascii=False))
            return name

    def promote(self, name: str) -> Path:
        """Curator action: turn the candidate `name` into a real `<name>/SKILL.md`
        (frontmatter + body), remove it from the candidates file, and return the
        written path. Raises `KeyError` if no such candidate exists. The new skill
        starts `status: active`, `run_count: 0`, dated today."""
        with self._candidates_lock():
            candidates = self._load_candidates()
            if name not in candidates:
                raise KeyError(f"no skill candidate named {name!r}")
            c = candidates.pop(name)
            skill_dir = self.directory / name
            skill_dir.mkdir(parents=True, exist_ok=True)
            path = skill_dir / "SKILL.md"
            fm = {
                "name": name, "description": c["description"], "trigger": "",
                "modules": c["modules"], "status": "active",
                "created_at": time.strftime("%Y-%m-%d"), "run_count": 0,
            }
            _write_durable(
                path,
                "---\n" + yaml.safe_dump(fm, sort_keys=False,
                                         allow_unicode=True)
                + "---\n\n" + c["body"] + "\n",
            )
            _write_durable(self.candidates_file,
                           json.dumps(candidates, indent=2,
                                      ensure_ascii=False))
            return path

    def touch(self, name: str) -> bool:
        """Record one use of the promoted skill `name`: bump `run_count` and
        stamp `last_used_at` in its frontmatter (the body is preserved
        byte-for-byte). Returns False when no such skill file exists or the
        frontmatter cannot be parsed — usage tracking never raises."""
        path = self.directory / name / "SKILL.md"
        try:
            text = path.read_text(encoding="utf-8")
            _, fm, body = text.split("---", 2)
            meta = yaml.safe_load(fm) or {}
            meta["run_count"] = int(meta.get("run_count", 0) or 0) + 1
            meta["last_used_at"] = time.strftime("%Y-%m-%d")
            _write_durable(
                path,
                "---\n" + yaml.safe_dump(meta, sort_keys=False,
                                          allow_unicode=True) + "---" + body)
            return True
        except (OSError, ValueError, yaml.YAMLError):
            return False

    def candidates(self) -> dict:
        """The current proposed-but-unpromoted skills, keyed by name."""
        return self._load_candidates()

    def _load_candidates(self) -> dict:
        """Read and return the candidates map from `_candidates.json`, or an empty
        dict when the file does not exist yet."""
        if self.candidates_file.exists():
            return json.loads(self.candidates_file.read_text(encoding="utf-8"))
        return {}

    def render_for_prompt(self, skills: list[Skill]) -> str:
        """Format the given `skills` into a markdown block for prompt injection —
        a heading plus each skill's name, description, and body truncated to 1500
        chars (bounds prompt cost). Returns "" for an empty list so the caller can
        omit the section entirely."""
        if not skills:
            return ""
        parts = ["## Relevant skills (distilled past lessons)"]
        for s in skills:
            parts.append(f"### {s.name}\n{s.description}\n{s.body[:1_500]}")
        return "\n\n".join(parts)
