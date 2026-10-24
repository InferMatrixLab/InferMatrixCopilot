"""Durable maintenance runs, immutable audit outcomes, usage and call budgets.

These additive tables never rewrite intake, verdict, activation or pin history.
Reservations are charged before dispatch. A recovered reservation is evidence
of a possibly executed call, never permission to dispatch it a second time.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import hashlib
import json
import math
from zoneinfo import ZoneInfo

from ..budgeting import settlement_charge
from .ledger import LeaseError
from .maintenance_units import fresh_history

SHANGHAI = ZoneInfo("Asia/Shanghai")
OUTCOMES = frozenset({"verified", "contradicted", "unknown", "execution_error"})
_SCHEMA = """
CREATE TABLE IF NOT EXISTS maintenance_runs (
 id TEXT PRIMARY KEY, cycle_date TEXT NOT NULL, kind TEXT NOT NULL,
 request_id TEXT NOT NULL, snapshot TEXT NOT NULL, policy_sha256 TEXT NOT NULL,
 status TEXT NOT NULL, detail TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS maintenance_progress (
 id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, status TEXT NOT NULL,
 detail TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS maintenance_items (
 run_id TEXT NOT NULL, unit_id TEXT NOT NULL, unit TEXT NOT NULL,
 outcome TEXT, detail TEXT, created_at REAL NOT NULL, finished_at REAL,
 PRIMARY KEY(run_id, unit_id));
CREATE TABLE IF NOT EXISTS maintenance_findings (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL, unit_id TEXT NOT NULL,
 outcome TEXT NOT NULL, detail TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS maintenance_requests (
 id TEXT PRIMARY KEY, repo TEXT NOT NULL, detail TEXT NOT NULL,
 status TEXT NOT NULL, run_id TEXT, created_at REAL NOT NULL, acked_at REAL);
CREATE TABLE IF NOT EXISTS maintenance_budget (
 id TEXT PRIMARY KEY, day TEXT NOT NULL, repo TEXT NOT NULL, owner TEXT NOT NULL,
 lane TEXT NOT NULL, cost_kind TEXT NOT NULL, worst_microusd INTEGER NOT NULL,
 charged_microusd INTEGER NOT NULL, actual_microusd INTEGER,
 status TEXT NOT NULL, outcome TEXT, metadata TEXT NOT NULL,
 created_at REAL NOT NULL, settled_at REAL);
CREATE TABLE IF NOT EXISTS maintenance_usage (
 id TEXT PRIMARY KEY, unit_id TEXT NOT NULL, repo TEXT NOT NULL, kind TEXT NOT NULL,
 count INTEGER NOT NULL, detail TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS maintenance_budget_policy (
 day TEXT PRIMARY KEY, limit_microusd INTEGER NOT NULL, fair_microusd INTEGER NOT NULL,
 released_at REAL, release_reason TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS maintenance_resolutions (
 id TEXT PRIMARY KEY, finding_id TEXT NOT NULL, decision TEXT NOT NULL,
 actor TEXT NOT NULL, evidence TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS maintenance_items_by_unit ON maintenance_items(unit_id, finished_at);
CREATE INDEX IF NOT EXISTS maintenance_budget_by_day ON maintenance_budget(day, lane);
"""


class MaintenanceConflict(ValueError):
    """An immutable identity was reused for different input or outcome."""


class BudgetExceeded(RuntimeError):
    pass


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _money(value):
    if isinstance(value, bool):
        raise ValueError("cost must be finite and nonnegative")
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError("cost must be finite and nonnegative")
        return int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
    except (InvalidOperation, TypeError):
        raise ValueError("cost must be finite and nonnegative") from None


def cycle_date(now):
    """The most recent Shanghai 01:00 slot, including across UTC midnight."""
    return (datetime.fromtimestamp(now, SHANGHAI) - timedelta(hours=1)).date().isoformat()


def budget_date(now):
    return datetime.fromtimestamp(now, SHANGHAI).date().isoformat()


class MaintenanceStore:
    def __init__(self, ledger, *, daily_limit_usd=50, fair_reserve_usd=10, lease_owner=None):
        self.ledger = ledger
        self.limit = _money(daily_limit_usd)
        self.fair_reserve = _money(fair_reserve_usd)
        if self.fair_reserve > self.limit:
            raise ValueError("fair reserve exceeds daily budget")
        self.lease_owner = lease_owner
        # Separate additive schema: opening an existing ledger preserves its
        # own schema version and every original row.
        self.ledger._conn.executescript(_SCHEMA)

    def _now(self, now):
        value = self.ledger._clock() if now is None else float(now)
        if not math.isfinite(value):
            raise ValueError("invalid maintenance clock")
        return value

    @contextmanager
    def _tx(self, *, request=False):
        if self.lease_owner is not None:
            with self.ledger.fenced(self.lease_owner) as cur:
                yield cur
        else:
            with self.ledger.tx() as cur:
                # CLI requests may be queued while the scheduler runs. Other
                # writers cannot borrow the live scheduler's lease identity.
                if not request and self.ledger.live_lease():
                    raise LeaseError("maintenance writes require the scheduler lease owner")
                yield cur

    @staticmethod
    def _run_row(row):
        return {**dict(row), "detail": json.loads(row["detail"])} if row else None

    def run(self, run_id):
        return self._run_row(self.ledger._conn.execute("SELECT * FROM maintenance_runs WHERE id=?", (run_id,)).fetchone())

    def runs(self):
        return [self._run_row(row) for row in self.ledger._conn.execute(
            "SELECT * FROM maintenance_runs ORDER BY created_at, id")]

    def begin_cycle(self, *, snapshot, policy_sha256, now=None, request_id=None,
                    eligible_repos=None, detail=None):
        now = self._now(now)
        kind, day = ("request" if request_id else "nightly"), cycle_date(now)
        run_id = f"request:{request_id}" if request_id else f"nightly:{day}"
        initial = {**(detail or {})}
        if eligible_repos is not None:
            initial["eligible_public_repos"] = sorted(set(eligible_repos))
        with self._tx() as cur:
            existing = cur.execute("SELECT * FROM maintenance_runs WHERE id=?", (run_id,)).fetchone()
            if existing:
                # First snapshot/policy of a date remain pinned across restart
                # and newer provider commits; callers resume those exact inputs.
                return {**self._run_row(existing), "created": False, "new": False}
            if request_id and not cur.execute("SELECT 1 FROM maintenance_requests WHERE id=?", (request_id,)).fetchone():
                raise ValueError("unknown maintenance request")
            cur.execute("INSERT INTO maintenance_runs VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (run_id, day, kind, request_id or "", snapshot, policy_sha256,
                         "running", _json(initial), now, now))
        return {**self.run(run_id), "created": True, "new": True}

    def mark_run(self, run_id, status, detail=None, *, now=None):
        if status not in {"running", "complete", "completed", "failed", "limited", "paused", "cancelled"}:
            raise ValueError("invalid maintenance run status")
        status = "complete" if status == "completed" else status
        now, detail = self._now(now), detail or {}
        with self._tx() as cur:
            row = cur.execute("SELECT * FROM maintenance_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            combined = {**json.loads(row["detail"]), **detail}
            old_eligible = json.loads(row["detail"]).get("eligible_public_repos")
            if old_eligible is not None and combined.get("eligible_public_repos") != old_eligible:
                raise MaintenanceConflict("nightly eligible repository denominator is frozen")
            if row["status"] == "complete" and status != "complete":
                raise MaintenanceConflict("completed maintenance run cannot reopen")
            if row["status"] != status or row["detail"] != _json(combined):
                cur.execute("INSERT INTO maintenance_progress(run_id,status,detail,created_at) VALUES (?,?,?,?)",
                            (run_id, status, _json(detail), now))
                cur.execute("UPDATE maintenance_runs SET status=?,detail=?,updated_at=? WHERE id=?",
                            (status, _json(combined), now, run_id))
        return self.run(run_id)

    def enqueue_request(self, request_id, repo, detail=None, *, now=None):
        now, encoded = self._now(now), _json(detail or {})
        if not request_id or not repo:
            raise ValueError("request needs identity and repository")
        with self._tx(request=True) as cur:
            row = cur.execute("SELECT * FROM maintenance_requests WHERE id=?", (request_id,)).fetchone()
            if row and (row["repo"] != repo or row["detail"] != encoded):
                raise MaintenanceConflict("maintenance request identity changed")
            if row is None:
                cur.execute("INSERT INTO maintenance_requests VALUES (?,?,?,?,?,?,?)",
                            (request_id, repo, encoded, "pending", None, now, None))
        row = self.ledger._conn.execute("SELECT * FROM maintenance_requests WHERE id=?", (request_id,)).fetchone()
        return {**dict(row), "detail": json.loads(row["detail"])}

    def pending_requests(self):
        return [{**dict(row), "detail": json.loads(row["detail"])} for row in self.ledger._conn.execute(
            "SELECT * FROM maintenance_requests WHERE status='pending' ORDER BY created_at,id")]

    def ack_request(self, request_id, run_id, *, now=None):
        now = self._now(now)
        with self._tx() as cur:
            run = cur.execute("SELECT * FROM maintenance_runs WHERE id=?", (run_id,)).fetchone()
            row = cur.execute("SELECT * FROM maintenance_requests WHERE id=?", (request_id,)).fetchone()
            if not row or not run or run["request_id"] != request_id:
                raise ValueError("request does not match maintenance run")
            if row["run_id"] and row["run_id"] != run_id:
                raise MaintenanceConflict("request already acknowledged by another run")
            cur.execute("UPDATE maintenance_requests SET status='acked',run_id=?,acked_at=COALESCE(acked_at,?) WHERE id=?",
                        (run_id, now, request_id))

    def add_items(self, run_id, units, *, now=None):
        now = self._now(now)
        with self._tx() as cur:
            run = cur.execute("SELECT * FROM maintenance_runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)
            for unit in units:
                if unit.get("snapshot") != run["snapshot"]:
                    raise MaintenanceConflict("unit snapshot differs from pinned maintenance run")
                if hashlib.sha256(unit["text"].encode()).hexdigest() != unit["content_sha256"]:
                    raise ValueError("maintenance unit content digest mismatch")
                if unit.get("lane", "priority") not in ("fair", "priority"):
                    raise ValueError("invalid maintenance item lane")
                encoded = _json(unit)
                row = cur.execute("SELECT unit FROM maintenance_items WHERE run_id=? AND unit_id=?",
                                  (run_id, unit["unit_id"])).fetchone()
                if row and row["unit"] != encoded:
                    raise MaintenanceConflict("maintenance unit identity changed within run")
                if row is None:
                    if run["status"] == "complete":
                        raise MaintenanceConflict("completed run cannot gain new audit items")
                    cur.execute("INSERT INTO maintenance_items VALUES (?,?,?,?,?,?,?)",
                                (run_id, unit["unit_id"], encoded, None, None, now, None))
        return self.items(run_id)

    def items(self, run_id):
        items = [{**dict(row), "unit": json.loads(row["unit"]),
                 "detail": json.loads(row["detail"]) if row["detail"] else None}
                for row in self.ledger._conn.execute(
                    "SELECT * FROM maintenance_items WHERE run_id=? ORDER BY unit_id", (run_id,))]
        return sorted(items, key=lambda item: (
            item["unit"].get("selection_rank", float("inf")), item["unit_id"]))

    def record_outcome(self, run_id, unit_id, outcome, *, detail=None, now=None):
        if outcome not in OUTCOMES:
            raise ValueError("invalid maintenance audit outcome")
        now, encoded = self._now(now), _json(detail or {})
        with self._tx() as cur:
            row = cur.execute("SELECT * FROM maintenance_items WHERE run_id=? AND unit_id=?", (run_id, unit_id)).fetchone()
            if row is None:
                raise KeyError(unit_id)
            if row["outcome"]:
                if row["outcome"] != outcome or row["detail"] != encoded:
                    raise MaintenanceConflict("audit outcome is immutable; create a new run for re-review")
            else:
                cur.execute("UPDATE maintenance_items SET outcome=?,detail=?,finished_at=? WHERE run_id=? AND unit_id=?",
                            (outcome, encoded, now, run_id, unit_id))
                # All audit observations are owner-addressable, including
                # positive examples that may become human-confirmed cases.
                finding_id = _digest([run_id, unit_id, outcome, detail or {}])
                cur.execute("INSERT INTO maintenance_findings VALUES (?,?,?,?,?,?)",
                            (finding_id, run_id, unit_id, outcome, encoded, now))
        return next(item for item in self.items(run_id) if item["unit_id"] == unit_id)

    def history(self):
        result = {}
        for row in self.ledger._conn.execute(
                "SELECT * FROM maintenance_items WHERE outcome IS NOT NULL ORDER BY finished_at,run_id"):
            unit = json.loads(row["unit"])
            result[row["unit_id"]] = {"unit_id": row["unit_id"], "run_id": row["run_id"],
                "outcome": row["outcome"], "finished_at": row["finished_at"],
                "content_sha256": unit["content_sha256"], "upstream_pin": unit.get("upstream_pin", ""),
                "snapshot": unit["snapshot"], "detail": json.loads(row["detail"])}
        return result

    def findings(self, *, run_id=None):
        sql = "SELECT f.*,i.unit FROM maintenance_findings f JOIN maintenance_items i ON f.run_id=i.run_id AND f.unit_id=i.unit_id"
        rows = self.ledger._conn.execute(sql + (" WHERE f.run_id=?" if run_id else "") + " ORDER BY f.created_at,f.id",
                                         (run_id,) if run_id else ())
        return [{**dict(row), "detail": json.loads(row["detail"]), "unit": json.loads(row["unit"])} for row in rows]

    def finding(self, finding_id):
        row = self.ledger._conn.execute(
            "SELECT f.*,i.unit FROM maintenance_findings f JOIN maintenance_items i "
            "ON f.run_id=i.run_id AND f.unit_id=i.unit_id WHERE f.id=?", (finding_id,)).fetchone()
        return {**dict(row), "detail": json.loads(row["detail"]), "unit": json.loads(row["unit"])} if row else None

    def record_resolution(self, finding_id, decision, actor, evidence, *, resolution_id=None, now=None):
        """Append human/operator resolution history; never alter audit verdicts."""
        if not actor or not decision or not evidence:
            raise ValueError("resolution needs an actor, decision and evidence")
        encoded = _json(evidence)
        resolution_id = resolution_id or _digest([finding_id, decision, actor, evidence])
        now = self._now(now)
        with self._tx() as cur:
            if not cur.execute("SELECT 1 FROM maintenance_findings WHERE id=?", (finding_id,)).fetchone():
                raise KeyError(finding_id)
            row = cur.execute("SELECT * FROM maintenance_resolutions WHERE id=?", (resolution_id,)).fetchone()
            if row and (row["finding_id"], row["decision"], row["actor"], row["evidence"]) != (finding_id, decision, actor, encoded):
                raise MaintenanceConflict("resolution identity changed")
            if row is None:
                cur.execute("INSERT INTO maintenance_resolutions VALUES (?,?,?,?,?,?)",
                            (resolution_id, finding_id, decision, actor, encoded, now))
        row = self.ledger._conn.execute("SELECT * FROM maintenance_resolutions WHERE id=?", (resolution_id,)).fetchone()
        return {**dict(row), "evidence": json.loads(row["evidence"])}

    @staticmethod
    def _budget_row(row, *, created=False):
        return {**dict(row), "metadata": json.loads(row["metadata"]), "created": created, "new": created,
                "call_allowed": created, "worst_cost_usd": row["worst_microusd"] / 1_000_000,
                "accounted_cost_usd": row["charged_microusd"] / 1_000_000,
                "actual_cost_usd": row["actual_microusd"] / 1_000_000 if row["actual_microusd"] is not None else None}

    def reserve_cost(self, reservation_id, *, repo, owner, worst_cost_usd, lane="priority",
                     cost_kind="api", metadata=None, now=None, require_existing=False):
        """Bind one reservation; recovery may validate but never create it."""
        now, cost, encoded = self._now(now), _money(worst_cost_usd), _json(metadata or {})
        if metadata and "settlement" in metadata:
            raise ValueError("reservation metadata cannot use the settlement key")
        if not reservation_id or not repo or not owner or lane not in ("priority", "fair") or cost_kind not in ("api", "subscription"):
            raise ValueError("invalid maintenance cost reservation")
        day = budget_date(now)
        with self._tx() as cur:
            old = cur.execute("SELECT * FROM maintenance_budget WHERE id=?", (reservation_id,)).fetchone()
            if old:
                if (old["repo"], old["owner"], old["lane"], old["cost_kind"], old["worst_microusd"]) != (repo, owner, lane, cost_kind, cost):
                    raise MaintenanceConflict("cost reservation identity changed")
                old_metadata = json.loads(old["metadata"])
                old_metadata.pop("settlement", None)
                if _json(old_metadata) != encoded:
                    raise MaintenanceConflict("cost reservation request metadata changed")
                return self._budget_row(old)
            if require_existing:
                raise MaintenanceConflict("original cost reservation is missing")
            totals = cur.execute("SELECT COALESCE(SUM(charged_microusd),0) total, "
                                 "COALESCE(SUM(CASE WHEN lane='priority' THEN charged_microusd ELSE 0 END),0) priority "
                                 "FROM maintenance_budget WHERE day=?", (day,)).fetchone()
            policy = cur.execute("SELECT * FROM maintenance_budget_policy WHERE day=?", (day,)).fetchone()
            if policy is None:
                cur.execute("INSERT INTO maintenance_budget_policy(day,limit_microusd,fair_microusd) VALUES (?,?,?)",
                            (day, self.limit, self.fair_reserve))
            elif (policy["limit_microusd"], policy["fair_microusd"]) != (self.limit, self.fair_reserve):
                raise MaintenanceConflict("daily budget policy is frozen after first reservation")
            if totals["total"] + cost > self.limit:
                raise BudgetExceeded("global daily maintenance budget exhausted")
            released = policy is not None and policy["released_at"] is not None
            if lane == "priority" and not released and totals["priority"] + cost > self.limit - self.fair_reserve:
                raise BudgetExceeded("priority budget exhausted; fair reserve remains protected")
            if lane == "fair" and totals["total"] - totals["priority"] + cost > self.fair_reserve:
                raise BudgetExceeded("fair rotation daily budget exhausted")
            cur.execute("INSERT INTO maintenance_budget VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (reservation_id, day, repo, owner, lane, cost_kind, cost, cost, None,
                         "reserved", None, encoded, now, None))
            row = cur.execute("SELECT * FROM maintenance_budget WHERE id=?", (reservation_id,)).fetchone()
            return self._budget_row(row, created=True)

    def release_fair_reserve(self, day=None, *, now=None, reason="selected fair queue exhausted"):
        """Release today's reserve after the caller exhausted eligible fair work.

        Persist the proof assertion and refuse while a selected fair item from
        this day is unfinished. Unknown is an attempted audit, not verification.
        """
        now = self._now(now)
        day = day or budget_date(now)
        if day != budget_date(now) or not reason:
            raise ValueError("fair reserve can only be released for the current Shanghai day with a reason")
        midnight = datetime.fromisoformat(day).replace(tzinfo=SHANGHAI).timestamp()
        with self._tx() as cur:
            pending = cur.execute(
                "SELECT i.unit FROM maintenance_items i JOIN maintenance_runs r ON r.id=i.run_id "
                "WHERE i.outcome IS NULL AND r.created_at>=? AND r.created_at<?", (midnight, midnight + 86400))
            if any(json.loads(row["unit"]).get("lane") == "fair" for row in pending):
                raise MaintenanceConflict("selected fair audit items are still unfinished")
            row = cur.execute("SELECT * FROM maintenance_budget_policy WHERE day=?", (day,)).fetchone()
            if row is None:
                cur.execute("INSERT INTO maintenance_budget_policy VALUES (?,?,?,?,?)",
                            (day, self.limit, self.fair_reserve, now, reason))
            elif (row["limit_microusd"], row["fair_microusd"]) != (self.limit, self.fair_reserve):
                raise MaintenanceConflict("daily budget policy differs")
            elif row["released_at"] is None:
                cur.execute("UPDATE maintenance_budget_policy SET released_at=?,release_reason=? WHERE day=?", (now, reason, day))
        return dict(self.ledger._conn.execute("SELECT * FROM maintenance_budget_policy WHERE day=?", (day,)).fetchone())

    def settle_cost(self, reservation_id, *, actual_cost_usd=None, outcome="completed", metadata=None):
        if outcome not in ("completed", "unknown", "failed"):
            raise ValueError("invalid cost settlement outcome")
        actual = None if actual_cost_usd is None else _money(actual_cost_usd)
        with self._tx() as cur:
            row = cur.execute("SELECT * FROM maintenance_budget WHERE id=?", (reservation_id,)).fetchone()
            if row is None:
                raise KeyError(reservation_id)
            if row["status"] == "settled":
                if row["actual_microusd"] != actual or row["outcome"] != outcome:
                    raise MaintenanceConflict("cost settlement is immutable")
                return self._budget_row(row)
            # Subscription accounting uses the explicit reservation allowance,
            # while any actual charge is reported separately. Unknown/failed
            # calls never refund an uncertain amount.
            charged = settlement_charge(row["worst_microusd"], actual, outcome=outcome,
                                        fixed_accounting=row["cost_kind"] == "subscription")
            combined = {**json.loads(row["metadata"]), "settlement": metadata or {}}
            cur.execute("UPDATE maintenance_budget SET charged_microusd=?,actual_microusd=?,status='settled',outcome=?,metadata=?,settled_at=? WHERE id=?",
                        (charged, actual, outcome, _json(combined), self._now(None), reservation_id))
            return self._budget_row(cur.execute("SELECT * FROM maintenance_budget WHERE id=?", (reservation_id,)).fetchone())

    def record_usage(self, usage_id, unit_id, *, kind, repo="", count=1, detail=None, now=None):
        if kind not in ("retrieved", "injected") or type(count) is not int or count < 1:
            raise ValueError("usage must distinguish positive retrieved/injected counts")
        encoded, now = _json(detail or {}), self._now(now)
        with self._tx(request=True) as cur:
            row = cur.execute("SELECT * FROM maintenance_usage WHERE id=?", (usage_id,)).fetchone()
            if row and (row["unit_id"], row["repo"], row["kind"], row["count"], row["detail"]) != (unit_id, repo, kind, count, encoded):
                raise MaintenanceConflict("usage event identity changed")
            if row is None:
                cur.execute("INSERT INTO maintenance_usage VALUES (?,?,?,?,?,?,?)", (usage_id, unit_id, repo, kind, count, encoded, now))

    def usage(self):
        result = {}
        for row in self.ledger._conn.execute("SELECT unit_id,kind,SUM(count) count FROM maintenance_usage GROUP BY unit_id,kind"):
            result.setdefault(row["unit_id"], {"retrieved": 0, "injected": 0})[row["kind"]] = row["count"]
        return result

    def report(self, *, units=None, now=None, policy_sha256=None, eligible_repos=None):
        now = self._now(now)
        day = budget_date(now)
        budget_rows = list(self.ledger._conn.execute("SELECT * FROM maintenance_budget WHERE day=?", (day,)))
        history = self.history()
        coverage = {"eligible": len(units) if units is not None else None, "outcomes": {},
                    "never_reviewed": 0, "stale_identity": 0, "oldest_review_age_seconds": None}
        ages = []
        for unit in units or []:
            row = history.get(unit["unit_id"])
            if not row:
                coverage["never_reviewed"] += 1
            elif not fresh_history(unit, row):
                coverage["stale_identity"] += 1
            else:
                coverage["outcomes"][row["outcome"]] = coverage["outcomes"].get(row["outcome"], 0) + 1
                ages.append(max(0, now - row["finished_at"]))
        coverage["oldest_review_age_seconds"] = max(ages) if ages else None
        coverage["attempted"] = sum(coverage["outcomes"].values())
        coverage["successful"] = sum(coverage["outcomes"].get(outcome, 0)
                                     for outcome in ("verified", "contradicted"))
        valid_dates, night_details = [], []
        nightly_runs = [run for run in self.runs() if run["kind"] == "nightly"]
        current = nightly_runs[-1] if nightly_runs else None
        policy_sha256 = policy_sha256 if policy_sha256 is not None else current["policy_sha256"] if current else None
        eligible_repos = sorted(set(eligible_repos)) if eligible_repos is not None else \
            current["detail"].get("eligible_public_repos", []) if current else []
        for run in nightly_runs:
            if run["kind"] != "nightly":
                continue
            eligible = run["detail"].get("eligible_public_repos", [])
            observed = {item["unit"]["repo"] for item in self.items(run["id"])
                        if item["outcome"] in ("verified", "contradicted") and item["finished_at"] is not None
                        and cycle_date(item["finished_at"]) == run["cycle_date"]}
            valid = run["status"] == "complete" and bool(eligible) and set(eligible) <= observed \
                and run["policy_sha256"] == policy_sha256 and sorted(set(eligible)) == eligible_repos \
                and not run["detail"].get("budget_limited", False)
            if valid:
                valid_dates.append(run["cycle_date"])
            night_details.append({"date": run["cycle_date"], "valid": valid,
                                  "eligible_repos": eligible, "real_review_repos": sorted(observed)})
        valid_set = set(valid_dates)
        expected = datetime.fromisoformat(cycle_date(now)).date()
        today_run = next((run for run in nightly_runs if run["cycle_date"] == expected.isoformat()), None)
        # The eighth night's work must be allowed to use the seven completed
        # trial nights. A running current cycle is not a completed failure or
        # a missed date and must not make readiness impossible during its audit.
        if today_run and today_run["status"] == "running" \
                and today_run["policy_sha256"] == policy_sha256 \
                and sorted(set(today_run["detail"].get("eligible_public_repos", []))) == eligible_repos:
            expected -= timedelta(days=1)
        readiness_through_date = expected.isoformat()
        consecutive = 0
        while expected.isoformat() in valid_set:
            consecutive += 1
            expected -= timedelta(days=1)
        total = sum(row["charged_microusd"] for row in budget_rows)
        policy = self.ledger._conn.execute("SELECT * FROM maintenance_budget_policy WHERE day=?", (day,)).fetchone()
        return {"day": day, "budget": {"limit_usd": self.limit / 1_000_000,
            "fair_reserve_usd": self.fair_reserve / 1_000_000, "accounted_usd": total / 1_000_000,
            "fair_reserve_released": bool(policy and policy["released_at"] is not None),
            "remaining_usd": max(0, self.limit - total) / 1_000_000,
            "actual_known_usd": sum(row["actual_microusd"] or 0 for row in budget_rows) / 1_000_000,
            "unknown_actual_calls": sum(row["actual_microusd"] is None for row in budget_rows),
            "reserved_calls": sum(row["status"] == "reserved" for row in budget_rows),
            "by_cost_kind": {kind: sum(row["charged_microusd"] for row in budget_rows if row["cost_kind"] == kind) / 1_000_000
                             for kind in ("api", "subscription")}},
            "coverage": coverage, "valid_nights": len(valid_set), "consecutive_valid_nights": consecutive,
            "readiness_through_date": readiness_through_date,
            "readiness_policy_sha256": policy_sha256, "readiness_eligible_repos": eligible_repos,
            "seven_valid_nights_ready": consecutive >= 7, "nights": night_details,
            "pending_requests": len(self.pending_requests())}
