"""Trace schema v1: durable records of model calls, tool calls, decisions and outcomes.

Shared by the copilot and the review bot, so the expensive models' work can be
replayed and turned into datasets for cheaper models plus workflows. Layout
under a trace root::

    records/<YYYY-MM-DD>.jsonl   one JSON record per line (append-only)
    blobs/<aa>/<sha256>.gz       content-addressed payloads (prompts, replies)
    index.db                     SQLite index: id, kind, run, repo, PR, rule
                                 IDs, model, outcome, workflow, unit, fingerprint
                                 -> the record itself

A record (``schema: trace/1``)::

    {"schema", "id", "kind", "at",
     "context":  {run_id, playbook, step, repo, changeset_id, pr, rule_ids,
                  workflow, unit_id, item, fingerprint, of, ...},
     "model":    {role, provider, model, effort, served_model},   # model_call
     "usage", "seconds",
     "inputs":   {name: "sha256:<hex>"},      # blobs, never inline text
     "outputs":  {name: "sha256:<hex>"},
     "result":   {...},                       # decision / outcome / replay / tool_call
     "error":    "",
     "env":      {copilot_version, copilot_sha, ...}}

``kind`` is ``model_call``, ``auth_probe`` (native authentication control,
never a model invocation), ``tool_call`` (one agent tool invocation: its
arguments and result as blobs, ``result`` = tool, ok, refused, out_of_scope,
bytes), ``decision`` (a gate/review verdict), ``outcome`` (what happened later:
merged, closed, retired, accepted...) or ``replay``. Outcomes are the gold
labels for evaluating a cheaper model.

Context keys understood by the improvement engine: ``workflow`` (the enrolled
workflow's name), ``unit_id`` (one step call or agent loop), ``item`` (the
external object a unit acts on, for pairing), ``fingerprint`` (the declared
configuration hash) and ``of`` (an outcome's unit record id).

Everything is redacted before it is hashed or written: known token shapes
(GitHub, Anthropic, OpenAI, AWS, bearer headers, private keys) and the value
of every environment variable whose name marks it as a secret.

Index schema and migration (changelog)
---------------------------------------
* v0 (implicit ``user_version`` 0): 13 columns, positional inserts.
* v2 (``PRAGMA user_version = 2``): adds ``workflow``, ``unit_id`` and
  ``fingerprint`` columns with indexes.

The JSONL records are the only source of truth; the index is derived. Opening
a store never changes an existing index's schema: a v0 index is used in
*compatibility mode* (writes omit the new columns, queries on them raise
:class:`IndexNotMigrated`). Only the explicit :meth:`TraceStore.migrate_index`
upgrades it, under a cross-process ``flock`` and only once every writer is paused;
:meth:`TraceStore.rebuild_index` rebuilds atomically (temp file + rename) under
the same gate and to an explicit target schema, and :meth:`TraceStore.compare_index`
verifies a live index without touching it.

Standard library only; importing it loads nothing else from the package.
"""

from __future__ import annotations

from .persistence import atomic_write_bytes
from .redaction import REDACTED, _secret_values, redact

import contextvars
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

SCHEMA = "trace/1"
KINDS = ("model_call", "auth_probe", "configuration_probe", "tool_call", "decision", "outcome", "replay")
SCHEMA_VERSION = 2
_CONTEXT: contextvars.ContextVar[dict] = contextvars.ContextVar("trace_context", default={})
_STORE: contextvars.ContextVar["TraceStore | None"] = contextvars.ContextVar("trace_store", default=None)


# index columns per schema version; the record JSON is always the last column
_COLUMNS_V0 = ("id", "kind", "at", "run_id", "playbook", "repo", "changeset_id", "pr",
               "rule_ids", "role", "model", "outcome", "record")
_COLUMNS_V2 = _COLUMNS_V0[:-1] + ("workflow", "unit_id", "fingerprint", "record")
_NEW_COLUMNS = ("workflow", "unit_id", "fingerprint")
_INDEXED = ("kind", "run_id", "repo", "changeset_id", "pr", "model", "outcome")


class IndexNotMigrated(RuntimeError):
    """A query needs an index column the live index does not have yet."""


class MigrationError(RuntimeError):
    """An index migration/rebuild was refused or failed; the index is unchanged."""


def _try_lock_exclusive(fd: int) -> bool:
    """Non-blocking exclusive lock on ``fd``: ``flock`` on POSIX, a one-byte
    ``msvcrt.locking`` region on Windows. Either is released by the OS when
    the holder exits. False when another holder has it."""
    try:
        import fcntl
    except ImportError:  # Windows
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")  # the region to lock must exist
            os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


@contextmanager
def file_lock(path: str | Path, *, blocking: bool = True, timeout: float = 30.0) -> Iterator[bool]:
    """Cross-process exclusive lock on ``path`` (an ``flock`` / ``msvcrt``
    region, released by the OS when the holder exits; the file is never
    unlinked). Yields True when held. Non-blocking: yields False at once when
    another holder has it. Blocking: polls up to ``timeout`` seconds, then
    yields False. Used by the improvement engine for its ledgers and cycle."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    held = False
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        deadline = time.monotonic() + timeout
        while True:
            held = _try_lock_exclusive(fd)
            if held or not blocking or time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        try:
            yield held
        finally:
            if held:
                _unlock(fd)
    finally:
        os.close(fd)


def _unlock(fd: int) -> None:
    try:
        import fcntl
    except ImportError:  # Windows
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    fcntl.flock(fd, fcntl.LOCK_UN)


def _redact_obj(value: Any, environ: Mapping[str, str] | None = None) -> Any:
    if isinstance(value, str):
        return redact(value, environ)
    if isinstance(value, dict):
        return {(redact(k, environ) if isinstance(k, str) else k): _redact_obj(v, environ)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_obj(v, environ) for v in value]
    return value


def _redact_native(value, environ):
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            secret = isinstance(key, str) and isinstance(item, str) and re.search(
                r"(?:api_?key|access_?token|refresh_?token|secret|password|passwd|authorization|credentials?)$", key, re.I)
            clean[redact(key, environ) if isinstance(key, str) else key] = REDACTED if secret else _redact_native(item, environ)
        return clean
    if isinstance(value, (list, tuple)):
        return [_redact_native(item, environ) for item in value]
    return _redact_obj(value, environ)


# -- context -----------------------------------------------------------------------

@contextmanager
def trace_context(**fields: Any) -> Iterator[dict]:
    """Bind context fields (run_id, playbook, repo, changeset_id, ...) to every
    record written inside the block, nested blocks adding to outer ones."""
    merged = {**_CONTEXT.get(), **{k: v for k, v in fields.items() if v is not None}}
    token = _CONTEXT.set(merged)
    try:
        yield merged
    finally:
        _CONTEXT.reset(token)


def current_context() -> dict:
    """The bound context as recorded (private ``_`` fields are not recorded)."""
    return {k: v for k, v in _CONTEXT.get().items() if not k.startswith("_")}


@contextmanager
def bind_store(store: "TraceStore | None") -> Iterator["TraceStore | None"]:
    """Make ``store`` the process/task-local sink that the capture points
    (``tools.dispatch``, ``LLM.create``) write to; ``None`` unbinds."""
    token = _STORE.set(store)
    try:
        yield store
    finally:
        _STORE.reset(token)


def current_store() -> "TraceStore | None":
    """The bound trace store, or None when nothing captures full-fidelity records."""
    return _STORE.get()


def accept_attempt(attempt: int) -> None:
    """Mark which attempt of the current draft was accepted. The caller binds a
    holder as ``_accepted={}``; the accepted call is then identified by its
    ``draft_key`` and ``attempt``, and only that call inherits the decision."""
    holder = _CONTEXT.get().get("_accepted")
    if isinstance(holder, dict):
        holder["attempt"] = int(attempt)


def accepted_key(draft_key: str, holder: Mapping[str, Any]) -> str | None:
    return f"{draft_key}#{holder['attempt']}" if "attempt" in holder else None


_ENV: dict | None = None


def environment_fingerprint(*, refresh=False) -> dict:
    """Which code produced a record; offline recovery may recheck disk identity."""
    global _ENV
    if refresh:
        _ENV = None
    if _ENV is None:
        from . import __version__

        sha = ""
        try:
            proc = subprocess.run(["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"],
                                  capture_output=True, text=True, timeout=5, check=False)
            sha = proc.stdout.strip() if proc.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            pass
        from .source_fingerprint import package_tree_hash, source_tree_hash
        fingerprint = {"copilot_version": __version__, "copilot_sha": sha,
                       "runtime_package_sha256": package_tree_hash(Path(__file__).resolve().parent)}
        root = Path(__file__).resolve().parents[2]
        if (root / "src").is_dir() and (root / "playbooks").is_dir():
            fingerprint["source_tree_sha"] = source_tree_hash(root)
        _ENV = fingerprint
    return dict(_ENV)


# -- the store ------------------------------------------------------------------------

class TraceStore:
    _lock_hook: Callable[[], None] | None = None  # test seam: runs once the migration lock is held

    def __init__(self, root: str | Path, *, clock=time.time, environ: Mapping[str, str] | None = None):
        self.root = Path(root)
        self._clock = clock
        self._environ = environ
        self._lock = threading.Lock()

    def begin_call(self, entry: Mapping[str, Any]) -> "NativeCallArchive":
        """Publish inputs before dispatch; an unfinished archive survives a kill.

        Attempts are not extra model_call records. Their final receipt links to
        the single existing record, so usage accounting does not count twice.
        """
        return NativeCallArchive(self, entry)

    def validate_native_attempt(self, record: dict, *, require_complete=True, allow_failed=False) -> dict:
        """Validate an original native archive against its matching trace."""
        attempt_id = record.get("result", {}).get("native_attempt_id", "")
        if not isinstance(attempt_id, str) or not re.fullmatch(r"[0-9a-f]{32}", attempt_id):
            raise ValueError("native attempt archive is missing")
        path = self.root / "attempts" / attempt_id / "attempt.json"
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("native attempt must be a regular local archive")
        attempt = json.loads(path.read_text())
        statuses = ('inflight', 'complete', 'failed', 'interrupted', 'canceled_before_dispatch') if allow_failed else ('inflight', 'complete')
        if (record.get("schema") != SCHEMA or record.get("kind") != "model_call" or (record.get("error") and not allow_failed)
                or attempt.get("schema") != "native-attempt/1" or attempt.get("id") != attempt_id
                or (attempt.get("error") and not allow_failed) or attempt.get("status") not in statuses):
            raise ValueError("native attempt is not a successful or unfinished original call")
        for facts in (record.get('result', {}), attempt):
            for key in ('native_call_sent', 'native_call_completed', 'native_outcome_pending'):
                if key in facts and type(facts[key]) is not bool:
                    raise ValueError('native attempt dispatch facts must be booleans')
            cleanup = facts.get('process_cleanup')
            if cleanup is not None:
                from .process_execution import validate_cleanup_diagnostics
                validate_cleanup_diagnostics(cleanup)
                if facts.get('native_outcome_pending') is not True:
                    raise ValueError('native cleanup diagnostics require an original pending outcome')
            if not allow_failed and facts.get('native_outcome_pending') is True:
                raise ValueError('native attempt outcome remains pending')
        complete = attempt['status'] != 'inflight'
        if ((require_complete and not complete)
                or (complete and attempt.get("trace_id") != record.get("id"))
                or (not complete and attempt.get("trace_id"))):
            raise ValueError("native attempt is incomplete or bound to another call")
        if (attempt.get("inputs") != record.get("inputs")
                or attempt.get("context") != record.get("context")
                or attempt.get("env") != record.get("env")):
            raise ValueError("native attempt identity differs from the call record")
        model = record.get("model", {})
        if attempt.get("model", {}).get("native_reasoning_level", "") != model.get("native_reasoning_level", ""):
            raise ValueError("native attempt reasoning level differs from the call record")
        if any(attempt.get("model", {}).get(key, "") != model.get(key, "")
               for key in ("role", "provider", "model", "effort")):
            raise ValueError("native attempt model differs from the call record")
        sessions = attempt.get("native_session_ids", [])
        if (not isinstance(sessions, list) or any(not isinstance(item, str) for item in sessions)
                or sessions != record.get("result", {}).get("native_session_ids", [])):
            raise ValueError("native attempt sessions differ from the call record")
        outputs = record.get("outputs", {})
        if not outputs.get("reply") and not allow_failed:
            raise ValueError("native call has no archived reply")
        for blob in [*record.get("inputs", {}).values(), *outputs.values()]:
            self.blob(blob)
        if complete:
            cleanup = record.get('result', {}).get('process_cleanup')
            if attempt.get('process_cleanup') != cleanup:
                raise ValueError('native attempt cleanup diagnostics differ from the call record')
            if allow_failed and (attempt.get('error', '') != record.get('error', '')
                    or any(attempt.get(key) != record.get('result', {}).get(key)
                           for key in ('native_call_sent', 'native_call_completed', 'native_outcome_pending'))):
                raise ValueError('native attempt outcome differs from the call record')
            if attempt.get("outputs") != outputs:
                raise ValueError("native attempt output references differ from the call record")
            if (attempt.get("usage", {}) != record.get("usage", {})
                    or attempt.get("cost_usd") != record.get("result", {}).get("cost_usd")
                    or attempt.get("seconds") != record.get("seconds")
                    or attempt.get("served_model", "") != model.get("served_model", "")):
                raise ValueError("native attempt billing facts differ from the call record")
            self.blob(attempt["native_events_sha256"])
            if (outputs.get("native_events") is not None
                    and attempt["native_events_sha256"] != outputs["native_events"]):
                raise ValueError("native attempt events differ from the call record")
        return attempt

    def validate_native_attempt_outcome(self, record: dict, *, outcome="complete", require_complete=True) -> dict:
        """Prove a requested terminal outcome without promoting unknown calls.

        Diagnostic readers may inspect failed/pending archives through the raw
        validator. Recovery and accounting use this stricter outcome policy.
        """
        if outcome not in ("complete", "failed"):
            raise ValueError("recovery outcome must be complete or failed")
        failed = outcome == "failed"
        if failed:
            facts = record.get("result", {})
            if (not isinstance(record.get("error"), str) or not record["error"]
                    or facts.get("native_call_sent") is not True
                    or facts.get("native_call_completed") is not True
                    or facts.get("native_outcome_pending") is not False):
                raise ValueError("failed recovery requires an explicitly completed, non-pending failed call")
        attempt = self.validate_native_attempt(record, require_complete=require_complete, allow_failed=failed)
        if attempt["status"] not in ("inflight", outcome):
            raise ValueError("native attempt status conflicts with requested recovery outcome")
        if failed and (attempt.get("native_outcome_pending") is True or attempt.get("process_cleanup") is not None):
            raise ValueError("native attempt outcome remains pending")
        return attempt

    def recover_native_attempt(self, attempt_id: str, record_id: str, *,
                               writers_paused=None, offline_confirmed=False, outcome="complete") -> dict:
        """Finalize an interrupted archive from one existing journal record.

        No record is appended, model dispatched, index rebuilt or budget settled.
        As with index repair, callers must establish that writers are stopped.
        Failed recovery requires explicit completed-call facts and preserves the
        original failure; it cannot reconcile a pending native execution.
        """
        if outcome not in ("complete", "failed"):
            raise ValueError("recovery outcome must be complete or failed")
        if not isinstance(attempt_id, str) or not re.fullmatch(r"[0-9a-f]{32}", attempt_id):
            raise ValueError("invalid native attempt identity")
        with self._migration_lock():
            self._require_paused(writers_paused, offline_confirmed)
            matches = [record for record, _ in self.iter_records()
                       if record.get("id") == record_id
                       or record.get("result", {}).get("native_attempt_id") == attempt_id]
            if (len(matches) != 1 or matches[0].get("id") != record_id
                    or matches[0].get("result", {}).get("native_attempt_id") != attempt_id):
                raise ValueError("native attempt needs one unambiguous original journal record")
            record = matches[0]
            attempt = self.validate_native_attempt_outcome(record, outcome=outcome, require_complete=False)
            if attempt["status"] == outcome:
                return attempt
            events_path = self.root / "attempts" / attempt_id / "native-events.jsonl"
            if events_path.is_symlink():
                raise ValueError("native event stream must be a regular local file")
            events = events_path.read_text() if events_path.exists() else ""
            if self.blob(record["outputs"]["native_events"]) != events:
                raise ValueError("native event stream differs from the completed record")
            archive = NativeCallArchive.__new__(NativeCallArchive)
            archive.store, archive.id, archive.path = self, attempt_id, events_path.parent
            archive.events_path, archive.meta = events_path, attempt
            archive.session_ids = set(attempt.get("native_session_ids", []))
            archive.finish(record, {"reply": self.blob(record["outputs"]["reply"]),
                                   **{key: record.get('result', {})[key]
                                      for key in ('native_call_sent', 'native_call_completed', 'native_outcome_pending')
                                      if key in record.get('result', {})},
                "usage": record.get("usage", {}), "seconds": record.get("seconds"),
                "cost_usd": record.get("result", {}).get("cost_usd"),
                "served_model": record.get("model", {}).get("served_model", ""),
                "error": record.get("error", "")}, status=outcome)
            return self.validate_native_attempt_outcome(record, outcome=outcome)

    # blobs -----------------------------------------------------------------------
    def put_blob(self, content: str | bytes) -> str:
        """Store redacted text; bytes must be UTF-8 text (redaction needs text)."""
        if isinstance(content, bytes):
            try:
                content = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise TypeError("trace blobs are text: binary payloads cannot be redacted") from exc
        data = redact(content, self._environ).encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        path = self.root / "blobs" / digest[:2] / f"{digest}.gz"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(path, gzip.compress(data, mtime=0))
        return f"sha256:{digest}"

    def blob(self, ref: str) -> str:
        digest = ref.removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise KeyError(ref)
        data = gzip.decompress((self.root / "blobs" / digest[:2] / f"{digest}.gz").read_bytes())
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError(f"blob {ref} is corrupt")
        return data.decode("utf-8")

    # index schema ----------------------------------------------------------------
    @property
    def index_path(self) -> Path:
        return self.root / "index.db"

    @staticmethod
    def _create_schema(conn: sqlite3.Connection, version: int) -> None:
        """Create the ``records`` table for ``version`` in a fresh database."""
        extra = ", workflow TEXT, unit_id TEXT, fingerprint TEXT" if version >= 2 else ""
        conn.execute(f"""CREATE TABLE IF NOT EXISTS records (
            id TEXT PRIMARY KEY, kind TEXT, at REAL, run_id TEXT, playbook TEXT, repo TEXT,
            changeset_id TEXT, pr INTEGER, rule_ids TEXT, role TEXT, model TEXT, outcome TEXT{extra},
            record TEXT)""")
        for column in _INDEXED + (_NEW_COLUMNS if version >= 2 else ()):
            conn.execute(f"CREATE INDEX IF NOT EXISTS records_{column} ON records({column})")
        conn.execute(f"PRAGMA user_version = {int(version)}")

    @staticmethod
    def _version(conn: sqlite3.Connection) -> int:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])

    def _db(self, path: Path | None = None) -> sqlite3.Connection:
        """Open the index. A fresh database is created at the current schema; an
        existing one is used as-is (compatibility mode when older) — opening
        never migrates."""
        self.root.mkdir(parents=True, exist_ok=True)
        path = path or self.index_path
        fresh = not path.exists() or path.stat().st_size == 0
        conn = sqlite3.connect(path, timeout=30)
        if fresh:
            with conn:
                self._create_schema(conn, SCHEMA_VERSION)
        return conn

    def index_version(self) -> int:
        """The live index's schema version (0 for a pre-versioning index)."""
        with self._db() as conn:
            return self._version(conn)

    @staticmethod
    def _columns(version: int) -> tuple[str, ...]:
        return _COLUMNS_V2 if version >= 2 else _COLUMNS_V0

    def _insert(self, conn: sqlite3.Connection, record: dict, line: str, *, replace: bool = False) -> None:
        """Named-column insert, so new code writes an old (v0) index too."""
        columns = self._columns(self._version(conn))
        verb = "INSERT OR REPLACE" if replace else "INSERT"
        conn.execute(f"{verb} INTO records ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                     _row(record, line, columns))

    # records ---------------------------------------------------------------------
    def append(self, kind: str, *, inputs: Mapping[str, str] | None = None,
               outputs: Mapping[str, str] | None = None, model: Mapping[str, Any] | None = None,
               result: Mapping[str, Any] | None = None, usage: Mapping[str, Any] | None = None,
               seconds: float | None = None, error: str = "", context: Mapping[str, Any] | None = None,
               ) -> dict:
        """Write one record; text in ``inputs``/``outputs`` goes to blobs."""
        if kind not in KINDS:
            raise ValueError(f"unknown trace record kind: {kind}")
        now = self._clock()
        record = {
            "schema": SCHEMA, "id": f"{int(now * 1000)}-{uuid.uuid4().hex[:12]}", "kind": kind, "at": now,
            "context": {**current_context(), **(context or {})},
            "model": dict(model or {}), "usage": dict(usage or {}),
            "seconds": seconds,
            "result": dict(result or {}),
            "error": error or "",
            "env": environment_fingerprint(),
        }
        if kind == 'model_call':
            from .model_usage import observe_model_call
            observe_model_call(_redact_obj(record, self._environ))
        record['inputs'] = {k: self.put_blob(v) for k, v in (inputs or {}).items()}
        record['outputs'] = {k: self.put_blob(v) for k, v in (outputs or {}).items()}
        # every string of the record, keys included, before it is serialized or indexed
        record = _redact_obj(record, self._environ)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        day = dt.datetime.fromtimestamp(now, dt.timezone.utc).date().isoformat()
        with self._lock:
            path = self.root / "records" / f"{day}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            with self._db() as conn:
                self._insert(conn, record, line)
        return record

    def get(self, record_id: str) -> dict:
        with self._db() as conn:
            row = conn.execute("SELECT record FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise KeyError(record_id)
        return json.loads(row[0])

    def query(self, *, kind: str | None = None, run_id: str | None = None, repo: str | None = None,
              changeset_id: str | None = None, pr: int | None = None, rule_id: str | None = None,
              role: str | None = None, model: str | None = None, playbook: str | None = None,
              workflow: str | None = None, unit_id: str | None = None, fingerprint: str | None = None,
              limit: int = 1000) -> list[dict]:
        clauses, args = [], []
        for column, value in (("kind", kind), ("run_id", run_id), ("repo", repo),
                              ("changeset_id", changeset_id), ("pr", pr), ("role", role),
                              ("model", model), ("playbook", playbook), ("workflow", workflow),
                              ("unit_id", unit_id), ("fingerprint", fingerprint)):
            if value is not None:
                clauses.append(f"{column}=?")
                args.append(value)
        if rule_id is not None:
            clauses.append("rule_ids LIKE ?")
            args.append(f"%,{rule_id},%")
        sql = "SELECT record FROM records" + (" WHERE " + " AND ".join(clauses) if clauses else "") \
            + " ORDER BY at, id LIMIT ?"
        with self._db() as conn:
            if any(v is not None for v in (workflow, unit_id, fingerprint)) and self._version(conn) < 2:
                raise IndexNotMigrated(
                    f"index.db is schema {self._version(conn)}: run `improve migrate-index` "
                    "before querying by workflow/unit_id/fingerprint")
            return [json.loads(row[0]) for row in conn.execute(sql, (*args, int(limit)))]

    # jsonl scans (the source of truth) -----------------------------------------
    def iter_records(self, *, kind: str | None = None) -> Iterator[tuple[dict, str]]:
        """Read authoritative JSONL, optionally selecting one record kind.

        Filtering never uses the derived index or skips unreadable journal lines.
        Control records may share model context without being model outcomes.
        """
        if kind is not None and kind not in KINDS:
            raise ValueError("unknown record kind")
        for path in sorted((self.root / "records").glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    record = json.loads(line)
                    if kind is None or record.get("kind") == kind:
                        yield record, line

    # migration gate ---------------------------------------------------------------
    @contextmanager
    def _migration_lock(self) -> Iterator[Path]:
        """Cross-process exclusive lock for index migrations/rebuilds: an
        advisory ``flock`` on ``index.migrate.lock``. The kernel releases it
        when the holder exits, so there is no stale-lock state and no takeover
        path to race on (an unlink-and-recreate or rename-aside recovery lets
        a delayed contender remove a FRESH holder's lock; this cannot). The
        file itself is never unlinked: a contender that already opened the
        old inode would otherwise lock an orphan while a newcomer locks a new
        one. The payload (pid, time) is informational only. POSIX uses
        ``flock``; Windows locks byte 0 with ``msvcrt.locking``."""
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self.root / "index.migrate.lock"
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")  # byte 0 is the lock region; the payload follows it
            if not _try_lock_exclusive(fd):
                try:
                    # read the payload from byte 1 through our own handle: on
                    # Windows the locked byte 0 is unreadable by another handle
                    os.lseek(fd, 1, os.SEEK_SET)
                    holder = json.loads(os.read(fd, 4096).decode("utf-8") or "{}")
                except (OSError, ValueError):
                    holder = {}
                raise MigrationError(f"another migration holds {lock} (pid {holder.get('pid', '?')})")
            if self._lock_hook is not None:
                self._lock_hook()
            try:
                # the payload sits after the locked region (byte 0 on Windows)
                # and is informational only; truncate-then-write keeps it fresh
                os.lseek(fd, 1, os.SEEK_SET)
                os.write(fd, json.dumps({"pid": os.getpid(), "at": time.time()}).encode("utf-8"))
                yield lock
            finally:
                try:
                    os.ftruncate(fd, 1)
                except OSError:
                    pass
                _unlock(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _require_paused(writers_paused: Callable[[], tuple[bool, str]] | None, offline_confirmed: bool) -> None:
        if offline_confirmed:
            return
        if writers_paused is None:
            raise MigrationError("no writer-pause check supplied; pass writers_paused=... or offline_confirmed=True")
        paused, detail = writers_paused()
        if not paused:
            raise MigrationError(f"writers are not paused: {detail}")

    def migrate_index(self, *, writers_paused: Callable[[], tuple[bool, str]] | None = None,
                      offline_confirmed: bool = False, backup: bool = True,
                      fail_after_alter: bool = False) -> dict:
        """Upgrade the live index to the current schema in one transaction:
        backup, ``ALTER TABLE ADD COLUMN`` for each missing column, backfill of
        every existing row from its stored JSON, indexes, ``user_version``.
        Verified afterwards (no row with a JSON value but a NULL column; every
        workflow's indexed id set equals the JSONL scan). Refused unless the
        writers are paused. ``fail_after_alter`` is a test hook that raises inside
        the transaction to prove it rolls back."""
        with self._migration_lock():
            self._require_paused(writers_paused, offline_confirmed)
            if not self.index_path.exists():
                with self._db():
                    pass  # created fresh at the current schema
                return {"migrated": False, "from": SCHEMA_VERSION, "to": SCHEMA_VERSION, "backfilled": 0}
            backup_path = None
            if backup:
                backup_path = self.root / f"index.db.bak-{int(self._clock())}"
                shutil.copy2(self.index_path, backup_path)
            conn = sqlite3.connect(self.index_path, timeout=30, isolation_level=None)
            try:
                before = self._version(conn)
                if before >= SCHEMA_VERSION:
                    return {"migrated": False, "from": before, "to": before, "backfilled": 0,
                            "backup": str(backup_path) if backup_path else ""}
                conn.execute("BEGIN IMMEDIATE")
                try:
                    present = {row[1] for row in conn.execute("PRAGMA table_info(records)")}
                    for column in _NEW_COLUMNS:
                        if column not in present:
                            conn.execute(f"ALTER TABLE records ADD COLUMN {column} TEXT")
                    if fail_after_alter:
                        raise RuntimeError("injected migration failure")
                    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    backfilled = self._backfill(conn)
                    caught_up = self._catch_up(conn)  # records written while the index missed them
                    for column in _NEW_COLUMNS:
                        conn.execute(f"CREATE INDEX IF NOT EXISTS records_{column} ON records({column})")
                    # verified INSIDE the transaction: a failed check rolls the
                    # schema back too, so a legacy writer never meets schema 2
                    report = self._verify_conn(conn)
                    if not report["ok"]:
                        raise MigrationError(f"verification failed, migration rolled back: {report}")
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()
            return {"migrated": True, "from": before, "to": SCHEMA_VERSION, "backfilled": backfilled,
                    "caught_up": caught_up, "backup": str(backup_path) if backup_path else "",
                    "verify": report}

    @staticmethod
    def _backfill(conn: sqlite3.Connection) -> int:
        """Fill the new columns of every row lacking them from the row's JSON."""
        rows = conn.execute("SELECT id, record FROM records WHERE workflow IS NULL "
                            "AND unit_id IS NULL AND fingerprint IS NULL").fetchall()
        for record_id, line in rows:
            ctx = (json.loads(line).get("context") or {})
            conn.execute("UPDATE records SET workflow=?, unit_id=?, fingerprint=? WHERE id=?",
                         (_text(ctx.get("workflow")), _text(ctx.get("unit_id")),
                          _text(ctx.get("fingerprint")), record_id))
        return len(rows)

    def _catch_up(self, conn: sqlite3.Connection) -> int:
        """Insert every JSONL record the index lacks (a writer that raced the
        index, or a legacy writer whose insert failed); the JSONL is the truth."""
        indexed = {row[0] for row in conn.execute("SELECT id FROM records")}
        added = 0
        for record, line in self.iter_records():
            if record["id"] not in indexed:
                self._insert(conn, record, line, replace=True)
                indexed.add(record["id"])
                added += 1
        return added

    def verify_index(self) -> dict:
        """Read-only check of the live index against the JSONL records: the id
        sets are equal, and (schema >= 2) no row carries a JSON context value in
        a NULL column and each workflow's indexed ids equal the JSONL scan's."""
        with self._db() as conn:
            return self._verify_conn(conn)

    def _verify_conn(self, conn: sqlite3.Connection) -> dict:
        """The verification over ``conn`` (inside a migration's own transaction
        it sees the uncommitted state, so a failure can still roll back)."""
        jsonl_ids: set[str] = set()
        by_workflow: dict[str, set[str]] = {}
        for record, _ in self.iter_records():
            jsonl_ids.add(record["id"])
            wf = _text((record.get("context") or {}).get("workflow"))
            if wf:
                by_workflow.setdefault(wf, set()).add(record["id"])
        report: dict = {"ok": True, "jsonl": len(jsonl_ids), "problems": []}
        index_ids = {row[0] for row in conn.execute("SELECT id FROM records")}
        report["indexed"] = len(index_ids)
        if index_ids != jsonl_ids:
            report["problems"].append(
                f"id sets differ: {len(jsonl_ids - index_ids)} missing from index, "
                f"{len(index_ids - jsonl_ids)} extra in index")
        version = self._version(conn)
        report["schema"] = version
        if version >= 2:
            for column in _NEW_COLUMNS:
                nulls = 0
                for (line,) in conn.execute(f"SELECT record FROM records WHERE {column} IS NULL"):
                    if _text((json.loads(line).get("context") or {}).get(column)):
                        nulls += 1
                if nulls:
                    report["problems"].append(f"{nulls} rows have context.{column} but a NULL column")
            for wf, ids in by_workflow.items():
                indexed = {row[0] for row in conn.execute("SELECT id FROM records WHERE workflow=?", (wf,))}
                if indexed != ids:
                    report["problems"].append(f"workflow {wf!r}: index has {len(indexed)} ids, jsonl {len(ids)}")
        report["ok"] = not report["problems"]
        return report

    def rebuild_index(self, *, to_schema: int = SCHEMA_VERSION,
                      writers_paused: Callable[[], tuple[bool, str]] | None = None,
                      offline_confirmed: bool = False) -> int:
        """Re-create the index from the JSONL records into a temp file and swap it
        in atomically; refused unless the writers are paused (the live index is
        never unlinked in place). ``to_schema=0`` builds the pre-versioning 13
        column table so an older pin can write it again. Returns the row count
        and raises MigrationError if the rebuilt id set differs from the JSONL."""
        if to_schema not in (0, SCHEMA_VERSION):
            raise MigrationError(f"unknown target schema {to_schema}")
        with self._migration_lock():
            if self.index_path.exists():  # a restore into an empty root has no live index to clobber
                self._require_paused(writers_paused, offline_confirmed)
            tmp = self.root / f"index.db.tmp-{int(self._clock())}-{uuid.uuid4().hex[:6]}"
            count = self._build_index(tmp, to_schema)
            os.replace(tmp, self.index_path)
            # a record appended between the build and the swap is caught up
            # from the JSONL (the truth) before the index is declared good
            with self._lock, self._db() as conn:
                count += self._catch_up(conn)
                report = self._verify_conn(conn)
            if not report["ok"]:
                raise MigrationError(f"rebuilt index does not match the records: {report}")
            return count

    def _build_index(self, path: Path, to_schema: int) -> int:
        conn = sqlite3.connect(path, timeout=30)
        try:
            with conn:
                self._create_schema(conn, to_schema)
                count = 0
                for record, line in self.iter_records():
                    self._insert(conn, record, line, replace=True)
                    count += 1
        finally:
            conn.close()
        return count

    def compare_index(self) -> dict:
        """Read-only verification that never touches the live index: rebuilds a
        throwaway index in a temp directory and compares it with the live one
        (row ids, schema-2 columns), then deletes the temp copy."""
        report = self.verify_index()
        with tempfile.TemporaryDirectory(prefix="trace-index-compare-") as tmpdir:
            tmp = Path(tmpdir) / "index.db"
            count = self._build_index(tmp, SCHEMA_VERSION)
            fresh = sqlite3.connect(tmp)
            try:
                fresh_rows = {row[0]: row[1:] for row in fresh.execute(
                    "SELECT id, workflow, unit_id, fingerprint FROM records")}
            finally:
                fresh.close()
        report["rebuilt"] = count
        with self._db() as conn:
            if self._version(conn) >= 2:
                live_rows = {row[0]: row[1:] for row in conn.execute(
                    "SELECT id, workflow, unit_id, fingerprint FROM records")}
                mismatched = sum(1 for rid, cols in fresh_rows.items() if live_rows.get(rid) != cols)
                if mismatched:
                    report["problems"].append(f"{mismatched} rows differ in workflow/unit_id/fingerprint")
        report["ok"] = not report["problems"]
        return report

class NativeCallArchive:
    """One isolated durable call journal under the runtime's trace directory."""

    def __init__(self, store: TraceStore, entry: Mapping[str, Any]):
        self.store = store
        self.id = uuid.uuid4().hex
        self.path = store.root / "attempts" / self.id
        self.path.mkdir(parents=True, mode=0o700)
        self.events_path = self.path / "native-events.jsonl"
        self.session_ids: set[str] = set()
        inputs = {"system": str(entry.get("system", ""))}
        if "messages" in entry:
            inputs["messages"] = json.dumps(entry["messages"] or [], ensure_ascii=False, default=str)
        else:
            inputs["prompt"] = str(entry.get("prompt", ""))
        self.meta = {"schema": "native-attempt/1", "id": self.id, "status": "inflight",
                     "at": store._clock(), "context": current_context(),
                     "model": {**{k: entry.get(k, "") for k in ("role", "provider", "model", "effort")},
                               **({"native_reasoning_level": entry["native_reasoning_level"]} if "native_reasoning_level" in entry else {})},
                     "inputs": {k: store.put_blob(value) for k, value in inputs.items()},
                     "usage": {}, "cost_usd": None, "served_model": "", "seconds": None,
                     "native_session_ids": [], "env": environment_fingerprint()}
        self._save()

    def _save(self):
        data = _redact_obj(self.meta, self.store._environ)
        atomic_write_bytes(self.path / "attempt.json",
                           (json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))

    def event(self, event: Mapping[str, Any]):
        event = _redact_native(dict(event), self.store._environ)
        prior_ids = set(self.session_ids)
        for payload in (event, event.get("payload")):
            if isinstance(payload, dict):
                for key in ("sessionId", "session_id", "requestId", "request_id"):
                    if isinstance(payload.get(key), str):
                        self.session_ids.add(payload[key])
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        payload = event.get("payload")
        changed = prior_ids != self.session_ids
        if event.get("type") == "session.updated" and isinstance(payload, dict) and payload.get("modelId"):
            self.meta["served_model"] = str(payload["modelId"])
            changed = True
        if event.get("type") == "result" and isinstance(event.get("usage"), dict):
            self.meta["native_usage"] = event["usage"]
            changed = True
        if changed:
            self.meta["native_session_ids"] = sorted(self.session_ids)
            self._save()

    def payload(self) -> dict:
        return {"native_attempt_id": self.id,
                "native_session_ids": sorted(self.session_ids),
                "native_events": self.events_path.read_text(encoding="utf-8") if self.events_path.exists() else ""}

    def finish(self, receipt: Mapping[str, Any] | None, entry: Mapping[str, Any], *, status: str):
        receipt = receipt or {}
        self.meta.update({"status": status, "finished_at": self.store._clock(),
                          "trace_id": receipt.get("id", ""),
                          "outputs": receipt.get("outputs") or {"reply": self.store.put_blob(str(entry.get("reply", "")))},
                          "usage": entry.get("usage") or {}, "cost_usd": entry.get("cost_usd"),
                          "served_model": entry.get("served_model", ""), "seconds": entry.get("seconds"),
                          "error": entry.get("error", ""), "native_session_ids": sorted(self.session_ids),
                          "native_events_sha256": self.store.put_blob(self.payload()["native_events"])})
        self.meta.update({key: entry[key] for key in ('native_call_sent', 'native_call_completed', 'native_outcome_pending') if key in entry})
        if 'process_cleanup' in entry:
            from .process_execution import validate_cleanup_diagnostics
            self.meta['process_cleanup'] = validate_cleanup_diagnostics(entry['process_cleanup'])
        self._save()


def native_process_fields(entry):
    """Canonical raw diagnostics projection shared by both recorder shapes."""
    result = {}
    if 'process_cleanup' in entry:
        from .process_execution import validate_cleanup_diagnostics
        result['process_cleanup'] = validate_cleanup_diagnostics(entry['process_cleanup'])
    process = entry.get('native_process')
    if process is None:
        return {}, result
    outputs = {'native_' + channel: process[channel] for channel in ('stdout', 'stderr')}
    result.update({'native_exit': {**{key: process[key] for key in ('returncode', 'reason')},
        **{channel + '_sha256': hashlib.sha256(process[channel].encode()).hexdigest()
           for channel in ('stdout', 'stderr')}}})
    return outputs, result


class NativeCompletionRecorder:
    """Canonical prompt-shaped native receipts; domains retain their accounts."""

    def __init__(self, store, *, strict=False):
        self.store, self.strict = store, strict

    def begin_call(self, entry):
        return self.store.begin_call(entry)

    def durable(self):
        return NativeCompletionRecorder(self.store, strict=True)

    def __call__(self, entry):
        try:
            process_outputs, process_result = native_process_fields(entry)
            return self.store.append('model_call',
                inputs={name: entry[name] for name in ('system', 'prompt')},
                outputs={'reply': entry['reply'],
                         **process_outputs,
                         **({'native_events': entry['native_events']} if 'native_events' in entry else {})},
                model={**{k: entry.get(k, '') for k in ('role', 'provider', 'model', 'effort', 'served_model')},
                       **({'native_reasoning_level': entry['native_reasoning_level']} if 'native_reasoning_level' in entry else {})},
                usage=entry.get('usage') or {}, seconds=entry.get('seconds'),
                result={'stop_reason': entry.get('stop_reason', ''),
                        'max_budget_usd': entry.get('max_budget_usd'), 'cost_usd': entry.get('cost_usd'),
                        **({'subscription': entry['subscription']} if 'subscription' in entry else {}),
                        **{k: entry[k] for k in ('native_attempt_id', 'native_session_ids') if k in entry},
                        **{k: entry[k] for k in ('native_call_sent', 'native_call_completed', 'native_outcome_pending') if k in entry},
                        **({'session_outcome': entry['session_outcome']} if 'session_outcome' in entry else {}),
                        **process_result,
                        **({'fallback_from': entry['fallback_from']} if entry.get('fallback_from') else {})},
                error=entry.get('error', ''))
        except Exception:
            if self.strict:
                raise
            return None



def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _row(record: dict, line: str, columns: tuple[str, ...] = _COLUMNS_V0) -> tuple:
    ctx = record.get("context") or {}
    model = record.get("model") or {}
    result = record.get("result") or {}
    rule_ids = ctx.get("rule_ids") or []
    pr = str(ctx.get("pr") or "")
    values = {
        "id": record["id"], "kind": record["kind"], "at": record["at"],
        "run_id": _text(ctx.get("run_id")), "playbook": _text(ctx.get("playbook")),
        "repo": _text(ctx.get("repo")), "changeset_id": _text(ctx.get("changeset_id")),
        "pr": int(pr) if pr.isdigit() else None,
        "rule_ids": "," + ",".join(map(str, rule_ids)) + "," if rule_ids else "",
        "role": _text(model.get("role")), "model": _text(model.get("model")),
        "outcome": _text(result.get("outcome") or result.get("status")),
        "workflow": _text(ctx.get("workflow")), "unit_id": _text(ctx.get("unit_id")),
        "fingerprint": _text(ctx.get("fingerprint")), "record": line,
    }
    return tuple(values[c] for c in columns)
