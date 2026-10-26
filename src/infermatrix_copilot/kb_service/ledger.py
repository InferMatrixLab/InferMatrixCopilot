"""The knowledge service's ledger: one SQLite file, every table keyed by repo.

It is the single source of truth for knowledge events, candidate operations,
gate verdicts, change sets, activations, retirements, sweeps, the human queue,
outbox items and scheduler cursors. A repository's failure or pause never
touches another repository's rows.

Only one service instance may run: ``acquire_lease`` takes a heartbeat lease
row inside a write transaction; a stale lease (heartbeat older than its TTL) can
be taken over, a live one cannot.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 2
# version -> statements that upgrade from the previous version
_MIGRATIONS = {
    2: ("ALTER TABLE changesets ADD COLUMN pending_item TEXT NOT NULL DEFAULT ''",),
}
GLOBAL = "*"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS repo_state (
    repo TEXT PRIMARY KEY,
    generation INTEGER NOT NULL DEFAULT 1,
    mode TEXT NOT NULL DEFAULT 'shadow',
    paused INTEGER NOT NULL DEFAULT 0,
    pause_reason TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cursors (
    repo TEXT NOT NULL, name TEXT NOT NULL, value TEXT NOT NULL, updated_at REAL NOT NULL,
    PRIMARY KEY (repo, name)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL, source TEXT NOT NULL, external_id TEXT NOT NULL,
    payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL, updated_at REAL NOT NULL,
    UNIQUE (repo, source, external_id)
);
CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY, repo TEXT NOT NULL, event_id INTEGER,
    changeset_id TEXT, operation TEXT NOT NULL, content_sha256 TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT NOT NULL,
    changeset_id TEXT, block_id TEXT NOT NULL DEFAULT '', layer TEXT NOT NULL,
    verdict TEXT NOT NULL, model TEXT NOT NULL DEFAULT '', model_version TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS changesets (
    id TEXT PRIMARY KEY, repo TEXT NOT NULL, kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', generation INTEGER NOT NULL,
    branch TEXT NOT NULL DEFAULT '', pr_number INTEGER, head_sha TEXT NOT NULL DEFAULT '',
    merge_sha TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}',
    pending_item TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS activations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, snapshot TEXT NOT NULL, previous TEXT NOT NULL DEFAULT '',
    activated_at REAL NOT NULL, detail TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS retirements (
    repo TEXT NOT NULL, rule_id TEXT NOT NULL, page TEXT NOT NULL,
    retired_at_release TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'retired',
    updated_at REAL NOT NULL, PRIMARY KEY (repo, rule_id)
);
CREATE TABLE IF NOT EXISTS sweeps (
    id TEXT PRIMARY KEY, repo TEXT NOT NULL, from_sha TEXT NOT NULL, to_sha TEXT NOT NULL,
    tier TEXT NOT NULL, status TEXT NOT NULL, report TEXT NOT NULL DEFAULT '{}',
    started_at REAL NOT NULL, finished_at REAL
);
CREATE TABLE IF NOT EXISTS human_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT NOT NULL, changeset_id TEXT,
    reason TEXT NOT NULL, created_at REAL NOT NULL, resolved_at REAL
);
CREATE TABLE IF NOT EXISTS outbox (
    id TEXT PRIMARY KEY, repo TEXT NOT NULL, kind TEXT NOT NULL, generation INTEGER NOT NULL,
    expires_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'written',
    payload_sha256 TEXT NOT NULL, ack TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL, acked_at REAL
);
CREATE INDEX IF NOT EXISTS events_by_status ON events (repo, status);
CREATE INDEX IF NOT EXISTS changesets_by_status ON changesets (repo, status);
CREATE INDEX IF NOT EXISTS outbox_by_status ON outbox (repo, status);
"""


class LeaseError(RuntimeError):
    """Another live knowledge-service instance holds the lease."""


class Ledger:
    def __init__(self, path: str | Path, *, clock=time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        with self.tx() as cur:
            row = cur.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                cur.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            else:
                version = int(row["value"])
                if version > SCHEMA_VERSION:
                    raise RuntimeError(f"ledger schema {version} is newer than this code ({SCHEMA_VERSION})")
                columns = {r["name"] for r in cur.execute("PRAGMA table_info(changesets)")}
                for target in range(version + 1, SCHEMA_VERSION + 1):
                    for statement in _MIGRATIONS[target]:
                        # idempotent: a fresh CREATE already has the column
                        if "ADD COLUMN pending_item" in statement and "pending_item" in columns:
                            continue
                        cur.execute(statement)
                cur.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Cursor]:
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            yield cur
        except BaseException:
            cur.execute("ROLLBACK")
            raise
        else:
            cur.execute("COMMIT")

    # -- lease ---------------------------------------------------------------

    def acquire_lease(self, owner: str | None = None, *, ttl: float = 120.0) -> str:
        owner = owner or f"{os.uname().nodename}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        now = self._clock()
        with self.tx() as cur:
            row = cur.execute("SELECT value FROM meta WHERE key='lease'").fetchone()
            if row is not None:
                held = json.loads(row["value"])
                if held["owner"] != owner and now - held["heartbeat"] < held["ttl"]:
                    raise LeaseError(f"lease held by {held['owner']}")
            cur.execute(
                "INSERT INTO meta VALUES ('lease', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps({"owner": owner, "heartbeat": now, "ttl": ttl}),),
            )
        return owner

    def live_lease(self) -> str:
        """The owner of a lease whose heartbeat is still within its TTL, else
        ``""``: a running service instance (``kb serve``) renews it; a stopped
        one has released it or let it expire."""
        row = self._conn.execute("SELECT value FROM meta WHERE key='lease'").fetchone()
        if row is None:
            return ""
        held = json.loads(row["value"])
        if self._clock() - float(held["heartbeat"]) < float(held["ttl"]):
            return str(held["owner"])
        return ""

    def heartbeat(self, owner: str) -> None:
        with self.tx() as cur:
            row = cur.execute("SELECT value FROM meta WHERE key='lease'").fetchone()
            held = json.loads(row["value"]) if row else None
            if held is None or held["owner"] != owner:
                raise LeaseError("lease was lost")
            held["heartbeat"] = self._clock()
            cur.execute("UPDATE meta SET value=? WHERE key='lease'", (json.dumps(held),))

    @contextmanager
    def lease(self, *, ttl: float = 900.0, renew_every: float = 60.0) -> Iterator[str]:
        """Hold the single-writer lease for a unit of work; raises LeaseError
        when another live process holds it. A keeper thread (its own SQLite
        connection) renews it while the work blocks on slow model calls; the
        work's final writes must still go through ``fenced`` because a stalled
        process can lose the lease anyway."""
        owner = self.acquire_lease(ttl=ttl)
        stop = threading.Event()

        def keep() -> None:
            keeper = Ledger(self.path, clock=self._clock)
            try:
                while not stop.wait(renew_every):
                    try:
                        keeper.heartbeat(owner)
                    except LeaseError:
                        return
            finally:
                keeper.close()

        thread = threading.Thread(target=keep, name="kb-lease-keeper", daemon=True)
        thread.start()
        try:
            yield owner
        finally:
            stop.set()
            thread.join(timeout=5)
            self.release_lease(owner)

    @contextmanager
    def fenced(self, owner: str) -> Iterator[sqlite3.Cursor]:
        """A write transaction that commits only while ``owner`` still holds a
        live lease: a worker that lost its lease (expiry, takeover) writes
        nothing, so two workers can never both stage the same events."""
        with self.tx() as cur:
            row = cur.execute("SELECT value FROM meta WHERE key='lease'").fetchone()
            held = json.loads(row["value"]) if row else None
            if held is None or held["owner"] != owner or self._clock() - held["heartbeat"] >= held["ttl"]:
                raise LeaseError("lease was lost; refusing to write")
            yield cur

    def release_lease(self, owner: str) -> None:
        with self.tx() as cur:
            row = cur.execute("SELECT value FROM meta WHERE key='lease'").fetchone()
            if row and json.loads(row["value"])["owner"] == owner:
                cur.execute("DELETE FROM meta WHERE key='lease'")

    # -- repo state and generations -----------------------------------------

    def ensure_repo(self, repo: str, mode: str) -> bool:
        """Register ``repo`` or record its configured mode. A CHANGE of mode
        bumps the generation in the same transaction, so nothing issued under
        the previous mode (e.g. a merge item before a switch to shadow) survives
        a round trip back. Returns whether the generation changed."""
        now = self._clock()
        with self.tx() as cur:
            row = cur.execute("SELECT mode FROM repo_state WHERE repo=?", (repo,)).fetchone()
            changed = row is not None and row["mode"] != mode
            if row is None:
                cur.execute("INSERT INTO repo_state (repo, mode, updated_at) VALUES (?, ?, ?)",
                            (repo, mode, now))
            elif changed:
                cur.execute(
                    "UPDATE repo_state SET mode=?, generation=generation+1, updated_at=? WHERE repo=?",
                    (mode, now, repo))
            cur.execute(
                "INSERT OR IGNORE INTO repo_state (repo, mode, updated_at) VALUES (?, 'global', ?)",
                (GLOBAL, now),
            )
        return changed

    def repo_state(self, repo: str) -> dict[str, Any]:
        row = self._conn.execute("SELECT * FROM repo_state WHERE repo=?", (repo,)).fetchone()
        if row is None:
            raise KeyError(f"unknown repo in ledger: {repo}")
        return dict(row)

    def all_repo_states(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM repo_state ORDER BY repo")]

    def bump_generation(self, repo: str, *, pause: bool, reason: str) -> int:
        """Invalidate every outstanding outbox item of ``repo`` (or of all repos
        for ``GLOBAL``) and optionally pause it. Returns the new generation."""
        with self.tx() as cur:
            row = cur.execute("SELECT generation FROM repo_state WHERE repo=?", (repo,)).fetchone()
            if row is None:
                raise KeyError(f"unknown repo in ledger: {repo}")
            generation = int(row["generation"]) + 1
            cur.execute(
                "UPDATE repo_state SET generation=?, paused=?, pause_reason=?, updated_at=? WHERE repo=?",
                (generation, 1 if pause else 0, reason if pause else "", self._clock(), repo),
            )
        return generation

    def resume(self, repo: str) -> int:
        """Unpause; bumps the generation too, so nothing issued while paused revives."""
        return self.bump_generation(repo, pause=False, reason="")

    # -- cursors -------------------------------------------------------------

    def get_cursor(self, repo: str, name: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM cursors WHERE repo=? AND name=?", (repo, name)).fetchone()
        return row["value"] if row else None

    def cursors_with_prefix(self, repo: str, prefix: str) -> dict[str, str]:
        rows = self._conn.execute("SELECT name, value FROM cursors WHERE repo=? AND name LIKE ? ESCAPE '\\'",
                                  (repo, prefix.replace("\\", "\\\\").replace("%", "\\%")
                                   .replace("_", "\\_") + "%")).fetchall()
        return {row["name"]: row["value"] for row in rows}

    def set_cursor(self, repo: str, name: str, value: str, *, owner: str | None = None) -> None:
        with self.fenced(owner) if owner is not None else self.tx() as cur:
            cur.execute(
                "INSERT INTO cursors VALUES (?, ?, ?, ?) ON CONFLICT(repo, name) "
                "DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (repo, name, value, self._clock()),
            )

    # -- events --------------------------------------------------------------

    def record_event(self, repo: str, source: str, external_id: str, payload: dict) -> int | None:
        """Insert an event once (idempotent by repo/source/external_id)."""
        now = self._clock()
        with self.tx() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO events (repo, source, external_id, payload, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (repo, source, external_id, json.dumps(payload, sort_keys=True), now, now),
            )
            if cur.rowcount == 0:
                return None
            return int(cur.lastrowid)

    def event_by_external_id(self, repo: str, source: str, external_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM events WHERE repo=? AND source=? AND external_id=?",
                                 (repo, source, external_id)).fetchone()
        return {**dict(row), "payload": json.loads(row["payload"])} if row else None

    def events(self, repo: str, status: str, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE repo=? AND status=? ORDER BY id LIMIT ?",
            (repo, status, limit)).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def event(self, event_id: int) -> dict[str, Any]:
        row = self._conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if row is None:
            raise ValueError(f"source event {event_id} does not exist")
        return {**dict(row), "payload": json.loads(row["payload"])}

    def changesets_for_events(self, event_ids: set[int]) -> list[dict]:
        """Whole source batches and their refinement descendants, never a partial batch."""
        rows = [_changeset_row(r) for r in self._conn.execute("SELECT * FROM changesets")]
        selected = set()
        for row in rows:
            ids = set(row["detail"].get("event_ids") or [])
            if ids & event_ids:
                if not ids <= event_ids:
                    raise ValueError("event settlement must cover the whole existing source batch")
                selected.add(row["id"])
        while True:
            descendants = {row["id"] for row in rows if any(
                item.get("changeset") in selected for item in row["detail"].get("refine_history") or [])}
            if descendants <= selected:
                break
            selected |= descendants
        return [row for row in rows if row["id"] in selected]

    def settle_reviewed_events(self, owner: str, rows: list[dict], changesets: list[dict], receipt: str) -> None:
        """Apply verified manual dispositions atomically; preserve model verdicts."""
        import hashlib

        from ..knowledge_service.signing import canonical_json

        now = self._clock()
        with self.fenced(owner) as cur:
            for item in rows:
                source = item["source_event"]
                row = cur.execute("SELECT * FROM events WHERE id=?", (source["id"],)).fetchone()
                if row is None or any(row[k] != source[k] for k in ("repo", "source", "external_id", "created_at")) \
                        or hashlib.sha256(canonical_json(json.loads(row["payload"]))).hexdigest() != source["payload_sha256"]:
                    raise ValueError("source event identity or payload changed before settlement")
                completed = json.dumps({"outcome": "manual_reviewed_merged", "receipt": receipt,
                                        "disposition": item["outcome"], "reason": item["reason"]}, sort_keys=True)
                if row["status"] == "done" and row["detail"] == completed:
                    continue
                if row["status"] != source["status"] or row["detail"] != source["detail"]:
                    raise ValueError("source event changed before settlement")
                cur.execute("UPDATE events SET status='done', detail=?, updated_at=? WHERE id=?",
                            (completed, now, source["id"]))
            completed_changesets = set()
            for change in changesets:
                row = cur.execute("SELECT status, detail FROM changesets WHERE id=?", (change["id"],)).fetchone()
                detail = json.loads(row["detail"])
                if detail.get("manual_reviewed_event_settlement") == receipt and row["status"] == "manual_reviewed_merged":
                    completed_changesets.add(change["id"])
                    continue
                if row["status"] != change["status"] or detail != change["detail"]:
                    raise ValueError("source changeset changed before settlement")
                detail["manual_reviewed_event_settlement"] = receipt
                cur.execute("UPDATE changesets SET status='manual_reviewed_merged', detail=?, updated_at=? WHERE id=?",
                            (json.dumps(detail, sort_keys=True), now, change["id"]))
                completed_changesets.add(change["id"])
            for queue in cur.execute("SELECT id, changeset_id FROM human_queue WHERE resolved_at IS NULL").fetchall():
                if queue["changeset_id"] in completed_changesets:
                    cur.execute("UPDATE human_queue SET resolved_at=? WHERE id=?", (now, queue["id"]))

    def set_event_status(self, event_id: int, status: str, detail: str = "") -> None:
        with self.tx() as cur:
            cur.execute(
                "UPDATE events SET status=?, detail=?, attempts=attempts+?, updated_at=? WHERE id=?",
                (status, detail, 1 if status == "error" else 0, self._clock(), event_id),
            )

    # -- change sets, verdicts, queue ----------------------------------------

    @staticmethod
    def new_changeset_id(repo: str, kind: str) -> str:
        return f"{repo}-{kind}-{uuid.uuid4().hex[:12]}"

    def set_event_statuses(self, owner: str, updates: list[tuple[int, str, str]]) -> None:
        """Fenced (event id, status, detail) transitions."""
        now = self._clock()
        with self.fenced(owner) as cur:
            for event_id, status, detail in updates:
                cur.execute("UPDATE events SET status=?, detail=?, updated_at=? WHERE id=?",
                            (status, detail, now, event_id))

    def stage_intake(self, owner: str, repo: str, changeset_id: str, *, detail: dict, status: str,
                     verdicts: list[dict], human_reason: str, drafted_events: list[int],
                     kind: str = "intake", expected_status: str | None = None,
                     expected_generation: int | None = None, expected_detail: dict | None = None,
                     companion: dict | None = None) -> str:
        """Create a change set with its verdicts, human-queue entry and event
        transitions in ONE fenced transaction (any change-set kind)."""
        now = self._clock()
        if companion is not None:
            if (expected_status != "review_pending" or status != "human"
                    or companion["id"] == changeset_id
                    or companion["detail"]["for_changeset"] != changeset_id):
                raise ValueError("invalid companion review continuation")
            status = "companion_pending"
            detail = {**detail, "companion": companion["id"]}
        with self.fenced(owner) as cur:
            generation = cur.execute("SELECT generation FROM repo_state WHERE repo=?", (repo,)).fetchone()
            if expected_generation is not None:
                original = cur.execute("SELECT generation, detail FROM changesets WHERE id=?", (changeset_id,)).fetchone()
                if (original is None or generation is None
                        or original["generation"] != expected_generation
                        or generation["generation"] != expected_generation
                        or (expected_detail is not None and json.loads(original["detail"]) != expected_detail)):
                    raise ValueError("review intent or repository generation changed")
            if expected_status is None:
                cur.execute(
                    "INSERT INTO changesets (id, repo, kind, status, generation, detail, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (changeset_id, repo, kind, status, int(generation["generation"]),
                     json.dumps(detail, sort_keys=True), now, now))
            else:
                cur.execute(
                    "UPDATE changesets SET status=?, detail=?, updated_at=? "
                    "WHERE id=? AND repo=? AND kind=? AND status=?",
                    (status, json.dumps(detail, sort_keys=True), now,
                     changeset_id, repo, kind, expected_status))
                if cur.rowcount != 1:
                    raise ValueError('changeset no longer has the expected review intent')
            if companion is not None:
                cur.execute(
                    "INSERT INTO changesets (id, repo, kind, status, generation, detail, created_at, updated_at) "
                    "VALUES (?, ?, 'companion', 'companion_staged', ?, ?, ?, ?)",
                    (companion["id"], repo, int(generation["generation"]),
                     json.dumps(companion["detail"], sort_keys=True), now, now))
                cur.execute("INSERT INTO human_queue (repo, changeset_id, reason, created_at) VALUES (?, ?, ?, ?)",
                            (repo, companion["id"], f"review and merge the companion PR for {changeset_id}", now))
            for verdict in verdicts:
                cur.execute(
                    "INSERT INTO verdicts (repo, changeset_id, block_id, layer, verdict, model, model_version, "
                    "detail, created_at) VALUES (?, ?, ?, ?, ?, ?, '', ?, ?)",
                    (repo, changeset_id, verdict.get("block_id", ""), verdict["layer"], verdict["verdict"],
                     verdict.get("model", ""), json.dumps(verdict.get("detail") or {}, sort_keys=True), now))
            if human_reason:
                cur.execute("INSERT INTO human_queue (repo, changeset_id, reason, created_at) VALUES (?, ?, ?, ?)",
                            (repo, changeset_id, human_reason, now))
            for event_id in drafted_events:
                cur.execute("UPDATE events SET status='drafted', detail=?, updated_at=? WHERE id=? AND status='pending'",
                            (changeset_id, now, event_id))
        return changeset_id

    def create_changeset(self, repo: str, kind: str, *, detail: dict | None = None) -> str:
        changeset_id = f"{repo}-{kind}-{uuid.uuid4().hex[:12]}"
        generation = self.repo_state(repo)["generation"]
        now = self._clock()
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO changesets (id, repo, kind, generation, detail, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (changeset_id, repo, kind, generation, json.dumps(detail or {}, sort_keys=True), now, now),
            )
        return changeset_id

    def update_changeset(self, changeset_id: str, **fields: Any) -> None:
        allowed = {"status", "branch", "pr_number", "head_sha", "merge_sha", "detail", "pending_item"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown changeset fields: {sorted(unknown)}")
        if "detail" in fields:
            fields["detail"] = json.dumps(fields["detail"], sort_keys=True)
        if "pending_item" in fields:
            fields["pending_item"] = json.dumps(fields["pending_item"], sort_keys=True) if fields["pending_item"] else ""
        assignments = ", ".join(f"{key}=?" for key in fields)
        with self.tx() as cur:
            cur.execute(
                f"UPDATE changesets SET {assignments}, updated_at=? WHERE id=?",
                (*fields.values(), self._clock(), changeset_id),
            )

    def changeset(self, changeset_id: str) -> dict[str, Any]:
        row = self._conn.execute("SELECT * FROM changesets WHERE id=?", (changeset_id,)).fetchone()
        if row is None:
            raise KeyError(changeset_id)
        return _changeset_row(row)

    def changesets(self, repo: str, statuses: tuple[str, ...]) -> list[dict[str, Any]]:
        marks = ",".join("?" for _ in statuses)
        rows = self._conn.execute(
            f"SELECT * FROM changesets WHERE repo=? AND status IN ({marks}) ORDER BY created_at",
            (repo, *statuses)).fetchall()
        return [_changeset_row(r) for r in rows]

    def changesets_for_pr(self, repo: str, number: int) -> list[dict[str, Any]]:
        """Every change set of ``repo`` that has PR ``number``, whatever its status."""
        rows = self._conn.execute(
            "SELECT * FROM changesets WHERE repo=? AND pr_number=? ORDER BY created_at", (repo, number)).fetchall()
        return [_changeset_row(r) for r in rows]

    def changesets_of_kind(self, repo: str, kind: str) -> list[dict[str, Any]]:
        """Every change set of ``kind`` in ``repo``, whatever its status."""
        rows = self._conn.execute(
            "SELECT * FROM changesets WHERE repo=? AND kind=? ORDER BY created_at", (repo, kind)).fetchall()
        return [_changeset_row(r) for r in rows]

    def record_verdict(self, repo: str, *, layer: str, verdict: str, changeset_id: str | None = None,
                       block_id: str = "", model: str = "", model_version: str = "",
                       detail: dict | None = None) -> None:
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO verdicts (repo, changeset_id, block_id, layer, verdict, model, model_version, "
                "detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (repo, changeset_id, block_id, layer, verdict, model, model_version,
                 json.dumps(detail or {}, sort_keys=True), self._clock()),
            )

    def verdicts(self, changeset_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM verdicts WHERE changeset_id=? ORDER BY id", (changeset_id,)).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]

    def enqueue_human(self, repo: str, reason: str, changeset_id: str | None = None) -> None:
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO human_queue (repo, changeset_id, reason, created_at) VALUES (?, ?, ?, ?)",
                (repo, changeset_id, reason, self._clock()),
            )

    def human_queue(self, repo: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM human_queue WHERE resolved_at IS NULL"
        args: tuple = ()
        if repo is not None:
            sql += " AND repo=?"
            args = (repo,)
        return [dict(r) for r in self._conn.execute(sql + " ORDER BY id", args)]

    def settle_reviewed_holds(self, owner: str, rows: list[dict]) -> None:
        """Close exact reviewed alerts, retaining their evidence and original text.

        The caller verifies the receipts. This transaction fences the scheduler
        and refuses a changed queue row rather than resolving a different alert.
        It grants no knowledge trust and never changes a model verdict.
        """
        now = self._clock()
        with self.fenced(owner) as cur:
            for item in rows:
                source, resolution = item["hold"], item["resolution"]
                row = cur.execute("SELECT * FROM human_queue WHERE id=?", (source["id"],)).fetchone()
                if row is None or any(row[k] != source[k] for k in
                                      ("repo", "changeset_id", "reason", "created_at")):
                    raise ValueError("human alert changed before settlement")
                name = f"human_resolution:{source['id']}"
                value = json.dumps({**resolution, "hold": source}, sort_keys=True)
                previous = cur.execute("SELECT value FROM cursors WHERE repo='*' AND name=?", (name,)).fetchone()
                if row["resolved_at"] is not None:
                    if previous is None or previous["value"] != value:
                        raise ValueError("human alert was resolved by different evidence")
                    continue
                cur.execute("INSERT INTO cursors (repo, name, value, updated_at) VALUES ('*', ?, ?, ?)",
                            (name, value, now))
                cur.execute("UPDATE human_queue SET resolved_at=? WHERE id=?", (now, source["id"]))

    # -- retirements and activations ------------------------------------------

    def record_retirement(self, repo: str, rule_id: str, page: str, release: str) -> None:
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO retirements VALUES (?, ?, ?, ?, 'retired', ?) ON CONFLICT(repo, rule_id) "
                "DO UPDATE SET page=excluded.page, retired_at_release=excluded.retired_at_release, "
                "status='retired', updated_at=excluded.updated_at",
                (repo, rule_id, page, release, self._clock()),
            )

    def purge_eligible(self, repo: str, current_release: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM retirements WHERE repo=? AND status='retired' AND retired_at_release<>?",
            (repo, current_release)).fetchall()
        return [dict(r) for r in rows]

    def mark_purged(self, repo: str, rule_id: str) -> None:
        with self.tx() as cur:
            cur.execute("UPDATE retirements SET status='purged', updated_at=? WHERE repo=? AND rule_id=?",
                        (self._clock(), repo, rule_id))

    def record_activation(self, snapshot: str, detail: dict | None = None) -> None:
        previous = self.active_snapshot() or ""
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO activations (snapshot, previous, activated_at, detail) VALUES (?, ?, ?, ?)",
                (snapshot, previous, self._clock(), json.dumps(detail or {}, sort_keys=True)),
            )

    def active_snapshot(self) -> str | None:
        row = self._conn.execute(
            "SELECT snapshot FROM activations ORDER BY id DESC LIMIT 1").fetchone()
        return row["snapshot"] if row else None

    # -- outbox bookkeeping ---------------------------------------------------

    def record_outbox(self, item_id: str, repo: str, kind: str, generation: int,
                      expires_at: float, payload_sha256: str) -> None:
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO outbox (id, repo, kind, generation, expires_at, payload_sha256, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (item_id, repo, kind, generation, expires_at, payload_sha256, self._clock()),
            )

    def ack_outbox(self, item_id: str, status: str, ack: dict) -> None:
        if status not in {"acked", "rejected"}:
            raise ValueError("ack status must be acked or rejected")
        with self.tx() as cur:
            cur.execute(
                "UPDATE outbox SET status=?, ack=?, acked_at=? WHERE id=? AND status='written'",
                (status, json.dumps(ack, sort_keys=True), self._clock(), item_id),
            )

    def outbox_items(self, repo: str, status: str = "written") -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM outbox WHERE repo=? AND status=? ORDER BY created_at", (repo, status))]


def _changeset_row(row) -> dict[str, Any]:
    data = dict(row)
    data["detail"] = json.loads(row["detail"])
    data["pending_item"] = json.loads(row["pending_item"]) if row["pending_item"] else None
    return data

