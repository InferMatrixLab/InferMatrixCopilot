"""KnowledgePaths — the ONE resolver for every mutable-knowledge location.

Until the PR4d cutover activates, every member resolves to exactly the
location its consumer used before this module existed (byte-identity is
pinned by test) — the resolver is a pure refactor that gives the runtime-dir
cutover a single switch to act on later, instead of five scattered
path expressions. The per-repo runtime state home is
`<memory_db dir>/state/<repo>/` (the location `skills_runtime` already
used); the flag-on branch that redirects the LEGACY locations there lands
with the migration machinery (PR4d code), not here.

Debug-memory members deliberately mirror the historical PER-CONSUMER wiring
(they differ today and must keep differing until cutover):
`rebase_backend_db` (v3 agent tools), `shared_write_db`
(`_common.record_debug_memory`), `debug_read_layers`
(`agent_runtime` retrieval, adapter store first).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:  # POSIX-only, same guard as run_status.py / watchdog_learn.py
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore


@dataclass(frozen=True)
class KnowledgePaths:
    """Resolved knowledge locations for one repo. Build via `resolve()`."""

    repo: str
    state_dir: Path            # per-repo runtime state home
    rebase_backend_db: Path    # v3 agent-tool debug memory
    shared_write_db: Path      # shared record_debug_memory target
    debug_read_layers: tuple   # retrieval order (first = highest priority)
    skills_seed_dir: Path      # adapter-tree seed skills (read-only at run time)
    skills_runtime_dir: Path   # runtime candidates/promoted skills
    skills_usage_journal: Path # append-only seed-skill usage journal
    repo_map_cache_dir: Path | None  # adapter repo_map cache (None: no adapter)
    watchdog_overlay: Path     # learned noise-pattern overlay YAML
    watchdog_decisions: Path   # harvested Tier-2 decision log (curator-owned)
    watchdog_harvest_checkpoint: Path
    backups_dir: Path          # versioned non-destructive knowledge backups
    state_lock: Path           # flock wrapping state-dir writes
    knowledge_run_lock: Path   # LOCK_SH per run / LOCK_EX for migration

    @classmethod
    def resolve(cls, settings, repo: str,
                adapter_root: Path | None = None) -> "KnowledgePaths":
        """Resolve for `repo`. `adapter_root` is the repo adapter's directory
        when one is registered (callers already resolve the adapter; passing
        None reproduces every no-adapter fallback exactly).

        With the PR4d cutover ACTIVATED for this repo
        (`Settings.knowledge_runtime_repos`, repo-scoped), every debug
        member converges on the per-repo state-dir store — but only after
        the migration-complete marker validates; a listed repo without it
        raises `KnowledgeStateError` (fail closed, never an empty-store
        start). Unlisted repos resolve the legacy locations
        byte-identically."""
        memory_db = Path(settings.memory_db)
        state = memory_db.parent / "state" / repo
        active = repo and repo in getattr(settings,
                                          "knowledge_runtime_repos", set())
        managed = bool(os.environ.get("COPILOT_RELEASE_ROOT"))
        if active:
            cls._require_migration_marker(state, repo)
            state_db = state / "debug_memory.db"
            adapter_db = state_db
        else:
            adapter_db = (adapter_root / "store" / "debug_memory.db"
                          if adapter_root is not None else None)
        if adapter_root is not None:
            seed_skills = adapter_root / "skills"
            read_layers: tuple = ((state_db,) if active
                                  else (adapter_db, memory_db))
            repo_map_cache = (state / "repo_map" if active
                              else adapter_root / "repo_map")
        else:
            seed_skills = Path(settings.skills_dir)
            read_layers = (state_db,) if active else (memory_db,)
            repo_map_cache = state / "repo_map" if active else None
        if managed and not active:
            # Installed releases are immutable seeds. Learned data belongs to
            # the stable user state, and remains available after an upgrade.
            # Keep the legacy/global stores as read fallbacks; no implicit
            # schema migration or modification of the authoring checkout.
            state_db = state / "debug_memory.db"
            read_layers = tuple(dict.fromkeys((state_db, *read_layers)))
            repo_map_cache = state / "repo_map"
        return cls(
            repo=repo,
            state_dir=state,
            rebase_backend_db=state_db if active or managed else memory_db,
            shared_write_db=(state_db if managed else
                             adapter_db if adapter_db is not None else memory_db),
            debug_read_layers=read_layers,
            skills_seed_dir=seed_skills,
            skills_runtime_dir=state / "skills_runtime",
            skills_usage_journal=state / "skills_usage.jsonl",
            repo_map_cache_dir=repo_map_cache,
            watchdog_overlay=state / "watchdog_overlay.yaml",
            watchdog_decisions=state / "watchdog_decisions.jsonl",
            watchdog_harvest_checkpoint=state / "watchdog_harvested.json",
            backups_dir=state / "backups",
            state_lock=state / "locks" / "state.lock",
            knowledge_run_lock=state / "locks" / "knowledge.lock",
        )

    MIGRATION_MARKER = "MIGRATION_COMPLETE.json"

    @classmethod
    def _require_migration_marker(cls, state_dir: Path, repo: str) -> dict:
        """Validate the durable migration-complete marker for an ACTIVATED
        repo (design round-3 F8): missing/unparseable/incomplete marker,
        a marker COPIED from another repo, an unknown schema version, or
        a missing/corrupt target store ⇒ `KnowledgeStateError`. The
        marker's recorded digest is deliberately NOT required to equal
        the live store's — runs append knowledge after activation by
        design, so digest equality would fail on the first legitimate
        write; the fail-closed contract is about WIRING (right repo,
        right schema, a real readable store), not frozen content.
        Returns the parsed marker."""
        import json
        import sqlite3

        marker = state_dir / cls.MIGRATION_MARKER
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise KnowledgeStateError(
                f"knowledge runtime ACTIVATED for {repo!r} but "
                f"{marker} is missing — run `infermatrix-copilot "
                "migrate-knowledge` first, or remove the repo from "
                "IMX_KNOWLEDGE_RUNTIME")
        except (OSError, ValueError) as exc:
            raise KnowledgeStateError(
                f"knowledge runtime ACTIVATED for {repo!r} but {marker} "
                f"is unreadable: {exc}")
        if str(data.get("schema")) != "v2" \
                or not (data.get("digests") or {}).get("target_db"):
            raise KnowledgeStateError(
                f"{marker} is incomplete (schema/digests.target_db) or "
                f"names an unknown schema ({data.get('schema')!r}) — "
                "re-run the migration")
        if str(data.get("repo") or "") != repo:
            raise KnowledgeStateError(
                f"{marker} belongs to repo {data.get('repo')!r}, not "
                f"{repo!r} — a copied/stale marker never activates")
        target_db = state_dir / "debug_memory.db"
        try:
            from .debug_memory import DebugMemory

            dm = DebugMemory.open_readonly(target_db)
            # the marker's schema CLAIM is not evidence — the store
            # itself must carry the v2 columns AND a v2 FTS mirror (a
            # legacy, empty, or column-incomplete mirror would activate
            # a store whose retrieval silently misses fields;
            # PR-boundary F9 + round-2 F5). Column EXISTENCE is the
            # check — a generic MATCH's empty result proves nothing on
            # a fresh store.
            if not dm.schema_v2:
                raise KnowledgeStateError(
                    f"{target_db} is not schema v2 despite the marker's "
                    "claim — re-run the migration")
            required_fts = {"symptom", "root_cause", "fix_summary",
                            "module", "repo", "key", "tags", "watch_outs"}
            if not required_fts <= dm._fts_columns:
                raise KnowledgeStateError(
                    f"{target_db}'s FTS mirror lacks columns "
                    f"{sorted(required_fts - dm._fts_columns)} — re-run "
                    "the migration (its upgrade rebuilds the mirror)")
            fts_sql = (dm._conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = "
                "'entries_fts'").fetchone() or ("",))[0] or ""
            from .debug_memory import (fts5_unindexed_columns,
                                       is_fts5_table)
            if not is_fts5_table(fts_sql) or \
                    fts5_unindexed_columns(fts_sql):
                raise KnowledgeStateError(
                    f"{target_db}'s mirror is not a fully-indexed FTS5 "
                    "table — re-run the migration (round-3 F4)")
            # …and the mirror must actually BE an FTS table: a plain
            # table masquerading as `entries_fts` carries the right
            # column names yet fails every MATCH (hook iteration-2
            # finding) — the usability probe keeps that from activating
            dm._conn.execute(
                "SELECT rowid FROM entries_fts "
                "WHERE entries_fts MATCH '\"probe\"' "
                "ORDER BY rank LIMIT 1").fetchone()  # rank: FTS5-only
        except KnowledgeStateError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise KnowledgeStateError(
                f"knowledge runtime ACTIVATED for {repo!r} but the "
                f"migrated store {target_db} is missing, unreadable, or "
                f"has no usable FTS mirror ({exc}) — restore from "
                "backups or re-run the migration")
        return data


class KnowledgeStateError(RuntimeError):
    """The PR4d cutover is ACTIVATED for a repo whose migration-complete
    marker is missing or invalid — resolving paths would silently start
    with empty knowledge, so resolution fails CLOSED instead. The v3
    prelude turns this into BLOCKED; generic consumers surface it through
    their traced-degradation guards."""


class KnowledgeLockHeld(RuntimeError):
    """The per-repo knowledge lock is exclusively held (a knowledge
    migration is in progress) — runs must not start mid-migration."""


class KnowledgeRunLock:
    """Shared/exclusive flock on `state/<repo>/locks/knowledge.lock`.

    Every copilot RUN for a repo holds it SHARED for its whole lifetime;
    the knowledge-migration tool takes it EXCLUSIVE. Shared holders never
    contend with each other, so runs are unaffected day-to-day — but a
    migration cannot start while any potential store writer is alive, and
    no run can start mid-migration (the lock-census TOCTOU the design
    review closed). POSIX-only; on platforms without flock both sides
    degrade together to no enforcement."""

    def __init__(self, lock_path: Path):
        self.lock_path = Path(lock_path)
        self._fd: int | None = None

    def _open(self) -> int:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        return os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)

    def _acquire(self, mode: int) -> "KnowledgeRunLock":
        if fcntl is None:  # pragma: no cover - Windows
            return self
        fd = self._open()
        try:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            if mode == fcntl.LOCK_SH:
                raise KnowledgeLockHeld(
                    f"knowledge lock {self.lock_path} refused (shared "
                    "acquire): held exclusively — a knowledge migration is "
                    "in progress; retry after it completes")
            raise KnowledgeLockHeld(
                f"knowledge lock {self.lock_path} refused (exclusive "
                "acquire): a run for this repo is active")
        self._fd = fd
        return self

    def acquire_shared(self) -> "KnowledgeRunLock":
        """Run-side: non-blocking shared acquire (fails only against a
        migration's exclusive hold)."""
        return self._acquire(fcntl.LOCK_SH if fcntl else 0)

    def acquire_exclusive(self) -> "KnowledgeRunLock":
        """Migration-side: non-blocking exclusive acquire (fails while any
        run holds shared)."""
        return self._acquire(fcntl.LOCK_EX if fcntl else 0)

    def release(self) -> None:
        if self._fd is not None:
            if fcntl is not None:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
