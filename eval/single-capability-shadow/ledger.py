"""Shadow-only admission ledger; independent of production jobs and budgets."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import sqlite3
from pathlib import Path


class Refused(RuntimeError):
    pass


def plan_call_limit(plan):
    """Only a new, explicitly revised plan can select an expanded ceiling."""
    value = plan.get("native_call_ceiling", 36)
    revisions = {"full-workflow-20261012": 116, "native-generation-20261016": 206,
                 "foundation-revision-20261017": 233, "lease-direct-20261017": 235,
                 "lease-strict-20261017": 279,
                 "lease-strict-integrity-20261017": 323,
                 "lease-strict-policy-20261017": 367,
                 "direct-current-20261017": 371,
                 "foundation-current-20261017": 399,
                 "foundation-policy-20261017": 427,
                 "history-current-20261017": 439,
                 "history-scope-20261017": 459,
                 "history-citation-20261017": 479,
                 "history-sources-20261017": 499,
                 "history-obligations-20261017": 519}
    revision = plan.get("budget_revision")
    maximum = revisions.get(revision, 36) if isinstance(revision, str) else 36
    if type(value) is not int or not 1 <= value <= maximum:
        raise Refused(f"native_call_ceiling must be an integer from 1 through {maximum} for its explicit budget revision")
    if 'retained_prior_call_allowances' in plan:
        retained = plan['retained_prior_call_allowances']
        limits = plan.get('native_call_limits')
        if (type(retained) is not int or retained < 0
                or not isinstance(limits, dict) or not limits
                or any(type(limit) is not int or limit < 1 for limit in limits.values())):
            raise Refused('freeze nonnegative retained prior allowances and positive paired case limits')
        if retained + 2 * sum(limits.values()) > value:
            raise Refused('plan or call ceiling invalid: retained prior allowances plus paired case '
                          'allowances exceed the frozen native call ceiling')
    return value


class Ledger:
    @staticmethod
    def read_completed(path, plan, call_id, *, case_id, arm, request_digest):
        """Read retained plans without migrations, reservations or WAL writes."""
        path = Path(path).resolve()
        if not path.exists():
            return None
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        try:
            expected = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            row = connection.execute('SELECT digest,call_limit FROM plan').fetchone()
            if not row or row[0] != expected or not 1 <= row[1] <= plan_call_limit(plan):
                raise Refused('retained shadow plan differs from its original ledger')
            row = connection.execute('SELECT case_id,arm,request_digest,status,result,result_digest FROM calls WHERE id=?',
                                     (call_id,)).fetchone()
            if row is None:
                return None
            if row[:3] != (case_id, arm, request_digest):
                raise Refused('call identity changed')
            if row[3] != 'complete':
                raise Refused('original call outcome is unknown; reconcile before proceeding')
            if hashlib.sha256(row[4].encode()).hexdigest() != row[5]:
                raise Refused('completed receipt checksum differs')
            return json.loads(row[4])
        finally:
            connection.close()

    def __init__(self, path, plan, *, call_limit=None):
        frozen_limit = plan_call_limit(plan)
        if call_limit is None:
            call_limit = frozen_limit
        if "native_call_ceiling" in plan and call_limit != frozen_limit:
            raise Refused("call_limit differs from the frozen native_call_ceiling")
        if type(call_limit) is not int or call_limit < 1:
            raise ValueError("call_limit must be a positive integer")
        if call_limit > frozen_limit:
            raise Refused("call_limit exceeds the frozen protocol ceiling")
        limits = plan.get("native_call_limits")
        if limits is not None and (not isinstance(limits, dict) or not limits or any(
                not isinstance(case, str) or not case or type(limit) is not int or limit < 1
                for case, limit in limits.items())):
            raise ValueError("native_call_limits must map case IDs to positive integers")
        self.case_limits = dict(limits) if limits is not None else None
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS plan (singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                                             digest TEXT NOT NULL, call_limit INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS calls (
                id TEXT PRIMARY KEY, case_id TEXT NOT NULL, arm TEXT NOT NULL,
                request_digest TEXT NOT NULL, status TEXT NOT NULL,
                result TEXT, result_digest TEXT,
                UNIQUE(case_id, arm, id));
        """)
        digest = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        try:
            with self.transaction():
                self.db.execute("INSERT OR IGNORE INTO plan VALUES (1, ?, ?)", (digest, call_limit))
                if self.db.execute("SELECT digest, call_limit FROM plan").fetchone() != (digest, call_limit):
                    raise Refused("shadow plan or call ceiling differs from the original ledger")
        except BaseException:
            self.db.close()
            raise

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    def reserve(self, call_id, *, case_id, arm, request_digest):
        """Return a completed receipt or reserve one new native call.

        The driver uses a stable call ID. A reservation without a terminal
        receipt cannot be replayed, even with a newly invented call ID.
        """
        if arm not in {"baseline", "candidate"}:
            raise ValueError("unknown shadow arm")
        if not call_id or not case_id or not request_digest:
            raise ValueError("call identity must be complete")
        with self.transaction():
            prior = self.db.execute(
                "SELECT case_id, arm, request_digest, status, result, result_digest FROM calls WHERE id=?",
                (call_id,)).fetchone()
            if prior:
                if prior[:3] != (case_id, arm, request_digest):
                    raise Refused("call identity changed")
                if prior[3] != "complete":
                    raise Refused("original call outcome is unknown; reconcile before proceeding")
                if hashlib.sha256(prior[4].encode()).hexdigest() != prior[5]:
                    raise Refused("completed receipt checksum differs")
                return json.loads(prior[4])
            if self.db.execute("SELECT 1 FROM calls WHERE status!='complete'").fetchone():
                raise Refused("pilot has an unresolved original call; concurrency is one")
            if self.case_limits is not None:
                if case_id not in self.case_limits:
                    raise Refused("case is absent from the frozen native call plan")
                case_used = self.db.execute(
                    "SELECT COUNT(*) FROM calls WHERE case_id=? AND arm=?", (case_id, arm)).fetchone()[0]
                if case_used >= self.case_limits[case_id]:
                    raise Refused("frozen case native call ceiling exhausted")
            used = self.db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
            limit = self.db.execute("SELECT call_limit FROM plan").fetchone()[0]
            if used >= limit:
                raise Refused("frozen native call ceiling exhausted")
            self.db.execute("INSERT INTO calls VALUES (?, ?, ?, ?, 'reserved', NULL, NULL)",
                            (call_id, case_id, arm, request_digest))
        return None

    def complete(self, call_id, receipt):
        """Persist the actual result; usage/cost fields may explicitly be null."""
        payload = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self.transaction():
            row = self.db.execute("SELECT status, result_digest FROM calls WHERE id=?", (call_id,)).fetchone()
            if row is None:
                raise Refused("call was never reserved")
            if row[0] == "complete":
                if row[1] != digest:
                    raise Refused("terminal receipt cannot be replaced")
                return
            self.db.execute("UPDATE calls SET status='complete', result=?, result_digest=? WHERE id=?",
                            (payload, digest, call_id))

    def close(self):
        self.db.close()
