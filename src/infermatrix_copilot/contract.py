"""The public cross-repo contract surface.

Everything a *consumer* of this copilot — a review bot, or any other host — is
allowed to import lives here, and nothing else is supported. The rule exists
because the alternative already caused version incidents: a downstream consumer
reaches into `thin_mcp_server` for four `_direct_*` privates through
`importlib`, so a rename inside a server module breaks a different repository
at runtime with no signal at build time.

Two things this module owns:

* **Versioning and the capability handshake.** A bot's preflight can ask what
  this copilot supports instead of discovering it from a failure.

* **The structured review result.** `get_review_result` used to return paged
  `RUN_REPORT.md` text, so a machine consumer had to scrape Markdown for the
  verdict and the findings — which meant a prose edit could silently change
  what a bot posted. `build_review_result` reads the facts from where the run
  already persisted them, and returns them typed.

Dependency direction: this module imports the copilot's data layers
(`run_status`, `run_trace`) and MUST NOT import either MCP server module. The
servers import *down* into this one. `test_contract.py` pins that.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import run_status as rs
from .direct_routing import (
    direct_completion_result,
    direct_execution_budget,
    direct_knowledge_routes,
    direct_mandatory_review_guides,
    direct_review_plan,
)
from .run_trace import RunTrace
from .contracts.models import (
    DIRECT_API_VERSION,
    KNOWLEDGE_API_VERSION,
    QUALITY_API_VERSION,
    SDK_API_VERSION,
    STRICT_API_VERSION,
)

__all__ = [
    "COMMENT_FIELDS",
    "DIRECT_API_VERSION",
    "KNOWLEDGE_API_VERSION",
    "QUALITY_API_VERSION",
    "SDK_API_VERSION",
    "STRICT_API_VERSION",
    "build_quality_result",
    "build_review_result",
    "capabilities",
    "direct_completion_result",
    "direct_execution_budget",
    "direct_knowledge_routes",
    "direct_mandatory_review_guides",
    "direct_review_plan",
    "build_findings",
    "finding_id",
    "sanitize_comments",
    "sanitize_dispositions",
    "unknown_run_result",
    "unknown_quality_result",
]
# The only comment keys that cross the boundary. A review comment accumulates
# internal bookkeeping on real runs (`_verified`, `_anchor_unverified`,
# `corroborated_by`); publishing those would leak pipeline internals into a bot's
# output and freeze them into a contract nobody meant to make. Allow-list, so a
# new internal key is excluded by default rather than by remembering to.
COMMENT_FIELDS: frozenset[str] = frozenset({
    "file", "line", "severity", "comment", "evidence", "suggestion",
})

# One record per candidate the review considered: where it was anchored, and
# what the review decided to do with it.
DISPOSITION_FIELDS: frozenset[str] = frozenset({
    "anchor", "disposition", "declared",
})

# One record per PUBLISHED finding, head-bound and identified, so a consumer
# can follow the same finding across heads instead of re-deciding what "the
# same finding" means from prose. The review bot's ready gate carries open
# blocker/major findings forward on this identity (RFC-pr-state-machine).
#
# Explicit carried-finding rechecks travel separately from this published set.
FINDING_FIELDS: tuple[str, ...] = (
    "finding_id", "severity", "anchor", "head_sha",
)

# Trace events the assembler surfaces as diagnostics.
_DIAGNOSTIC_EVENTS = ("review_plan", "capability_gap", "expected_head_mismatch",
                      "anchor_resolution", "diff_fallback")


def capabilities(*, max_strict_workers: int = 1,
                 supports_file_locking: bool = True) -> dict[str, Any]:
    """What this copilot can do, for a consumer's preflight.

    `max_strict_workers` is reported, not assumed: the run service drains its
    queue with `STRICT_MAX_WORKERS` workers (default 1), so a bot that fans out
    Strict requests should size its own admission to this rather than infer
    concurrency from the fact that `start` returns immediately. Runs on the
    same checkout+PR still execute one at a time whatever this says."""
    # Compatibility shim: new consumers import ``infermatrix_copilot.sdk.v1``;
    # existing MCP and cross-repo callers keep receiving the legacy dict shape,
    # now with the distribution/SDK/resource identity added.
    from .review.direct import get_capabilities

    return get_capabilities(
        max_strict_workers=max_strict_workers,
        supports_file_locking=supports_file_locking,
    ).to_dict()


def _read_json(path: Path) -> dict:
    """Parse `path`, or `{}` — a run that died early legitimately lacks these."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _state_updates(run_dir: Path) -> dict:
    """Every `state_updates` key published by a completed step, merged in step
    order — the same reconstruction `--resume` performs."""
    progress = _read_json(run_dir / "progress.json")
    merged: dict[str, Any] = {}
    for entry in (progress.get("completed") or {}).values():
        if isinstance(entry, dict):
            updates = (entry.get("outputs") or {}).get("state_updates")
            if isinstance(updates, dict):
                merged.update(updates)
    return merged


def finding_id(path: str, text: str) -> str:
    """A finding's identity: stable across heads, distinct across files.

    Deliberately excludes the line number. A finding survives an unrelated
    edit above it, and a consumer carrying blockers forward must not lose one
    because the diff moved it three lines down. Two findings in the same file
    whose text differs are different findings; the same text in another file
    is another finding.
    """
    normalized = " ".join(str(text or "").split()).casefold()
    digest = hashlib.sha256(f"{str(path or '').strip()}\0{normalized}".encode())
    return digest.hexdigest()[:16]


def _anchor_parts(comment: Mapping[str, Any]) -> dict[str, Any] | None:
    """``{path, line}`` for a published finding, or None for a fileless one."""
    path = str(comment.get("file") or "").strip()
    if not path:
        return None
    line = comment.get("line")
    return {"path": path, "line": int(line) if isinstance(line, int) else None}


def build_findings(comments: Any, head_sha: str) -> list[dict]:
    """The published findings, identified and bound to the head they were
    found on.

    One entry per published comment, in publication order. No join with the
    disposition audit: those records are keyed by anchor, and an anchor is
    not unique — a withheld candidate and a published one can share it, and
    anchor resolution can move a line after the records are written — so a
    join would mislabel findings rather than inform a consumer. Identity,
    severity and position are what a consumer carrying blockers forward
    needs, and all three are exact.

    Duplicate identities are kept, not collapsed: two findings the pipeline
    chose to publish separately are two findings, and silently dropping one
    could drop a blocker.
    """
    out: list[dict] = []
    for comment in comments if isinstance(comments, list) else []:
        if not isinstance(comment, Mapping):
            continue
        anchor = _anchor_parts(comment)
        out.append({
            "finding_id": finding_id(
                (anchor or {}).get("path") or "", comment.get("comment")
            ),
            # Post-demotion: `review_comments` is the finalized set, so this
            # is the severity the finding was actually published with.
            "severity": str(comment.get("severity") or "minor").casefold(),
            "anchor": anchor,
            "head_sha": str(head_sha or ""),
        })
    return out


def sanitize_dispositions(records: Any) -> list[dict]:
    """Project disposition records onto `DISPOSITION_FIELDS`.

    Same allow-list discipline as `sanitize_comments`: the withheld prose
    itself never crosses the boundary, only the anchor and what was decided.
    """
    out: list[dict] = []
    for record in records if isinstance(records, list) else []:
        if isinstance(record, dict):
            out.append({k: v for k, v in record.items()
                        if k in DISPOSITION_FIELDS})
    return out


def sanitize_comments(comments: Any) -> list[dict]:
    """Project review comments onto `COMMENT_FIELDS`, dropping everything else."""
    out: list[dict] = []
    for c in comments if isinstance(comments, list) else []:
        if isinstance(c, dict):
            out.append({k: v for k, v in c.items() if k in COMMENT_FIELDS})
    return out


def build_review_result(run_dir: Path | str) -> dict[str, Any]:
    """The machine-readable result of one review run.

    Reads only what the run already persisted, so it works for a finished run,
    a running one, and one that died before the review step — which returns
    state plus diagnostics rather than raising. A caller polling an unknown id
    is handled one level up (`state: "unknown"`), so that "lost" and "still
    running" stay distinguishable."""
    run_dir = Path(run_dir)
    status = rs.read_status(run_dir) or {}
    updates = _state_updates(run_dir)
    trace = RunTrace(run_dir / "run_trace.jsonl")

    diagnostics: dict[str, Any] = {}
    for name in _DIAGNOSTIC_EVENTS:
        events = [{k: v for k, v in e.items() if k not in ("ts", "kind")}
                  for e in trace.events(name)]
        if events:
            diagnostics[name] = events

    # which knowledge the run reviewed with (pinned at reservation)
    pin = _read_json(run_dir / "knowledge.json")
    if pin.get("snapshot"):
        diagnostics["knowledge_snapshot"] = str(pin["snapshot"])
        diagnostics["knowledge_tree_sha256"] = str(pin.get("tree_sha256") or "")

    metrics = _read_json(run_dir / "metrics.json")
    cost = metrics.get("cost") or {}
    if cost:
        diagnostics["cost"] = {
            "usd": cost.get("usd"), "minutes": cost.get("minutes"),
            "input_tokens": cost.get("input_tokens"),
            "output_tokens": cost.get("output_tokens"),
            **{key: cost[key] for key in (
                "source", "cost_partial", "usage_partial", "actual_usd", "actual_model_usd", "usd_is_estimate",
                "unpriced_calls", "llm_calls", "input_tokens_include_cache",
                "cache_read_tokens", "cache_creation_tokens", "legacy_usage_exceeds_records",
                "record_read_error_count", "conflicting_calls", "usage_capture_incomplete") if key in cost},
        }

    # A stale head is the one outcome a caller must not have to infer from
    # prose: it means "your snapshot is gone", not "the review found nothing".
    mismatch = (diagnostics.get("expected_head_mismatch") or [None])[0]

    reviewed_head = str(updates.get("pr_head_sha") or "")
    findings = build_findings(updates.get("review_comments"), reviewed_head)
    from .review.rechecks import checked_rechecks, validate_carried
    from .contracts.models import InvalidRequestError
    request = _read_json(run_dir / "request.json")
    params = request.get("params") or {}
    carried = (params.get("carried_findings") or []) if isinstance(params, dict) else []
    request_gaps = []
    if "params" in request:
        try:
            validate_carried(carried)
        except InvalidRequestError as exc:
            request_gaps.append(str(exc))
            carried = []
    else:
        carried = updates.get("review_carried_findings") or []
    rechecks, recheck_missing = checked_rechecks(
        carried,
        updates.get("review_finding_rechecks") or [], reviewed_head,
    )
    recheck_missing.extend(request_gaps)
    recheck_missing.extend(updates.get("review_recheck_missing") or [])

    return {
        "contract_version": STRICT_API_VERSION,
        "run_id": status.get("run_id") or run_dir.name,
        "state": status.get("state") or "unknown",
        "note": status.get("note") or "",
        "reviewed_head_sha": str(updates.get("pr_head_sha") or ""),
        "verdict": str(updates.get("review_verdict") or ""),
        "summary_markdown": str(updates.get("review_summary") or ""),
        "comments": sanitize_comments(updates.get("review_comments")),
        # Every candidate this review considered and what became of it. The
        # published `comments` above are the finalized set; this is the proof,
        # so a consumer never has to reconcile the summary's prose against the
        # list it was handed (#141).
        "finding_dispositions": sanitize_dispositions(
            updates.get("review_finding_dispositions")),
        # The same published set, one entry each, identified and bound to the
        # head it was found on, so a consumer can carry an open blocker to the
        # next head rather than re-deriving "the same finding" from prose.
        "findings": findings,
        "finding_rechecks": rechecks,
        "rechecks_complete": not recheck_missing,
        "recheck_missing": recheck_missing,
        "stale": bool(mismatch),
        "expected_head_sha": str((mismatch or {}).get("expected") or ""),
        "actual_head_sha": str((mismatch or {}).get("actual") or ""),
        "diagnostics": diagnostics,
    }


def _quality_reasons(value: Any) -> list[dict[str, Any]]:
    """Project persisted model output onto the public quality schema."""
    fields = ("criterion", "evidence", "path", "line")
    return [
        {key: item.get(key) for key in fields}
        for item in value if isinstance(item, dict)
    ] if isinstance(value, list) else []


def build_quality_result(run_dir: Path | str) -> dict[str, Any]:
    """Build the machine-readable result of a PR quality workflow."""
    run_dir = Path(run_dir)
    status = rs.read_status(run_dir) or {}
    updates = _state_updates(run_dir)
    trace = RunTrace(run_dir / "run_trace.jsonl")
    mismatch_events = [
        {key: value for key, value in event.items()
         if key not in ("ts", "kind")}
        for event in trace.events("expected_head_mismatch")
    ]
    mismatch = (mismatch_events or [None])[0]
    diagnostics: dict[str, Any] = {}
    if mismatch_events:
        diagnostics["expected_head_mismatch"] = mismatch_events
    assessments = [
        {key: value for key, value in event.items()
         if key not in ("ts", "kind")}
        for event in trace.events("quality_assessment")
    ]
    if assessments:
        diagnostics["quality_assessment"] = assessments
    return {
        "contract_version": QUALITY_API_VERSION,
        "run_id": status.get("run_id") or run_dir.name,
        "state": status.get("state") or "unknown",
        "note": status.get("note") or "",
        "reviewed_head_sha": str(updates.get("pr_head_sha") or ""),
        "verdict": str(updates.get("quality_verdict") or ""),
        "confidence": str(updates.get("quality_confidence") or ""),
        "summary": str(updates.get("quality_summary") or ""),
        "reasons": _quality_reasons(updates.get("quality_reasons")),
        "stale": bool(mismatch),
        "expected_head_sha": str((mismatch or {}).get("expected") or ""),
        "actual_head_sha": str((mismatch or {}).get("actual") or ""),
        "diagnostics": diagnostics,
    }


def unknown_run_result(run_id: str) -> dict[str, Any]:
    """The result for an id this server has never heard of.

    Explicit rather than an exception so a bot holding an id it can no longer
    match — a lost response, a restarted server — can tell "lost" from "still
    running" and decide whether to retry."""
    return {"contract_version": STRICT_API_VERSION, "run_id": run_id,
            "state": "unknown", "note": "", "reviewed_head_sha": "",
            "verdict": "", "summary_markdown": "", "comments": [],
            "finding_dispositions": [],
            "stale": False, "expected_head_sha": "", "actual_head_sha": "",
            "diagnostics": {}}


def unknown_quality_result(run_id: str) -> dict[str, Any]:
    """The typed quality result for an unknown run id."""
    return {
        "contract_version": QUALITY_API_VERSION,
        "run_id": run_id,
        "state": "unknown",
        "note": "",
        "reviewed_head_sha": "",
        "verdict": "",
        "confidence": "",
        "summary": "",
        "reasons": [],
        "stale": False,
        "expected_head_sha": "",
        "actual_head_sha": "",
        "diagnostics": {},
    }
