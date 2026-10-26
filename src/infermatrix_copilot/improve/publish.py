"""Proposal publication (design §9.2, §11.1 rules 1, 4 and 5).

The engine holds no GitHub token. A proposal leaves the engine as an
*action file* in an outbox directory that the maintainer routine's ``propose``
routine picks up (open the issue, update its labels and trailing comment,
close it) and answers with an *ack file*; the routine also observes each
proposal issue (human comments, ``maintainer: hold``, referencing pull
requests) and writes an *inbox file* the engine reads back. Writing an
action file is the cycle's only outward write, so it lives behind the
``improve.publish`` step (``risk="push"``, explicit post intent AND
``ALLOW_POST=1``); everything else here is bookkeeping in the ledger.

Rule 4: a proposal cites evidence only by record id and blob hash; every
excerpt is drawn verbatim from the blob it names (after redaction), so a
reader can recover it from the hash. The linter refuses anything else and a
refused proposal is never published.

Layout of the outbox (owned by the engine; the routine writes acks/inbox)::

    <outbox>/actions/<action id>.json   the engine's requests
    <outbox>/acks/<action id>.json      the routine's answers (consumed)
    <outbox>/inbox/<proposal id>.json   the routine's observations (rewritten)
"""

from __future__ import annotations

from ..persistence import atomic_write_bytes

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..trace_store import TraceStore, file_lock
from .forensics import STAGES
from .ledger import Ledger, LedgerError, Proposal, WorkflowLedger
from .lints import catalogue

PROTOCOL = "improve-outbox/1"
PROPOSAL_MARKER = "improve:proposal:v1"
PROPOSAL_LABEL = "improve:proposal"
EXCERPT_MAX_LINES = 20
EXCERPT_MAX_LINE_CHARS = 300
PENDING_TTL = 7 * 24 * 3600.0            # an unacknowledged action is re-planned after this
PUBLISHABLE = ("open", "experiment-registered", "supported", "neutral", "underpowered")
_BODY_MAX_CHARS = 60_000

# the same families the maintainer routine refuses to post, plus API keys and
# addresses: an excerpt is quoted from a trace, which may carry anything
_CREDENTIAL_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*"),      # a block cut before its END: everything after it
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{16,}"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
)
_REDACTED = "[redacted]"


class PublishError(RuntimeError):
    pass


def redact(text: str) -> str:
    for pattern in _CREDENTIAL_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text


def credential_left(text: str) -> bool:
    return any(p.search(text) for p in _CREDENTIAL_PATTERNS)


@dataclass
class Citation:
    record_id: str
    blob: str = ""                      # sha256:<hex> or "" for a record without text
    source: str = ""                    # which input/output the blob is (e.g. outputs.review)
    excerpt: list[str] = field(default_factory=list)


_BLOB_PREFERENCE = ("review", "reply", "report", "answer", "text")


def excerpt_for(store: TraceStore, record_id: str, *, max_lines: int = EXCERPT_MAX_LINES) -> Citation:
    """The citation of one record: its most telling blob (outputs first)
    and the first ``max_lines`` non-blank lines of that blob, redacted, each
    cut at 300 characters with a trailing ellipsis so the line is still a
    prefix of a blob line. Raises KeyError for an unknown record."""
    record = store.get(record_id)
    refs: list[tuple[str, str]] = []
    for section in ("outputs", "inputs"):
        pairs = list((record.get(section) or {}).items())
        pairs.sort(key=lambda kv: (_BLOB_PREFERENCE.index(kv[0]) if kv[0] in _BLOB_PREFERENCE else 99, kv[0]))
        refs.extend((f"{section}.{k}", str(v)) for k, v in pairs if k != "system")
    for name, ref in refs:
        try:
            text = store.blob(ref)
        except (KeyError, ValueError, OSError):
            continue
        return Citation(record_id, ref, name, _excerpt_lines(text, max_lines))
    return Citation(record_id)


def _excerpt_lines(text: str, max_lines: int) -> list[str]:
    out: list[str] = []
    for line in redact(text).splitlines():
        if not line.strip():
            continue
        if len(line) > EXCERPT_MAX_LINE_CHARS:
            line = line[:EXCERPT_MAX_LINE_CHARS] + "…"
        out.append(line)
        if len(out) >= max_lines:
            break
    return out


def verify_excerpt(store: TraceStore, citation: Citation) -> str:
    """"" when every excerpt line is (a prefix of) a line of the redacted
    blob the citation names; otherwise the problem."""
    if not citation.blob:
        return "" if not citation.excerpt else "excerpt without a blob"
    try:
        text = store.blob(citation.blob)
    except (KeyError, ValueError, OSError) as exc:
        return f"blob {citation.blob[:19]} unresolvable ({type(exc).__name__})"
    lines = [ln for ln in redact(text).splitlines() if ln.strip()]
    if len(citation.excerpt) > EXCERPT_MAX_LINES:
        return f"excerpt longer than {EXCERPT_MAX_LINES} lines"
    for line in citation.excerpt:
        needle = line[:-1] if line.endswith("…") else line
        if not any(candidate == line or candidate.startswith(needle) for candidate in lines):
            return "excerpt line is not a line of the cited blob"
        if credential_left(line):
            return "excerpt still matches a credential pattern"
    return ""


def cite(store: TraceStore, proposal: Proposal, *, max_records: int = 6) -> list[Citation]:
    """Citations for a proposal's evidence, unresolvable ids left out (the
    linter reports those separately)."""
    out = []
    for rid in proposal.evidence[:max_records]:
        try:
            out.append(excerpt_for(store, rid))
        except KeyError:
            continue
    return out


def lint_proposal(store: TraceStore, proposal: Proposal, citations: list[Citation]) -> list[str]:
    """Why a proposal may not be published (empty = publishable). Rule 4:
    every cited record resolves, every excerpt is recoverable from its blob
    and at least one citation carries a blob."""
    problems: list[str] = []
    claim = (proposal.claim or "").strip()
    if not claim:
        problems.append("empty claim")
    elif len(claim) > 400:
        problems.append("claim longer than 400 characters (one sentence)")
    if proposal.tier not in (1, 2):
        problems.append(f"tier must be 1 or 2, got {proposal.tier}")
    if proposal.tier == 1:
        if not proposal.lint:
            problems.append("a Tier 1 proposal names the lint that worsened")
        if len(proposal.evidence) < 3:
            problems.append("a Tier 1 proposal cites at least 3 representative records")
    if proposal.tier == 2:
        if proposal.stage not in STAGES or proposal.stage in ("S0", "S10"):
            problems.append(f"a Tier 2 proposal carries a stage label S1-S9, got {proposal.stage!r}")
        if proposal.loss <= 0:
            problems.append("a Tier 2 proposal quantifies its loss")
        if not proposal.evidence:
            problems.append("a Tier 2 proposal cites at least 1 record")
    for rid in proposal.evidence:
        try:
            store.get(rid)
        except KeyError:
            problems.append(f"unresolvable record id {rid}")
    if not citations:
        problems.append("no citation")
    if citations and not any(c.blob for c in citations):
        problems.append("no citation carries a blob hash")
    for c in citations:
        if c.record_id not in proposal.evidence:
            problems.append(f"citation {c.record_id} is not in the proposal's evidence")
        problem = verify_excerpt(store, c)
        if problem:
            problems.append(f"{c.record_id}: {problem}")
    if proposal.proxy and not (proposal.tier == 1 or proposal.stage):
        problems.append("a proxy proposal must say what it is about")
    return problems


# -- rendering ------------------------------------------------------------------------

STAGE_HINTS = {
    "S1": "evidence reach: tool scope, evidence packing caps, the files a pass may open",
    "S2": "candidate elicitation: lens prompts, second-round prompting, per-file focus",
    "S3": "reduction: the reducer/dedupe prompt and its keep/drop thresholds",
    "S4": "budget: llm_max_tokens, iteration caps, the contract's required fields",
    "S5": "assembly: rendering caps, ordering, anchor resolution",
    "S6": "calibration: the severity/verdict rubric in the review prompt",
    "S7": "planning depth: the planner prompt and its fallback behaviour",
    "S8": "phrasing: the output format the judge scores",
    "S9": "verification: the verification pass prompt and what it may approve",
}


def marker(proposal: Proposal) -> str:
    payload = {"proposal": proposal.id, "workflow": proposal.workflow, "tier": proposal.tier, "state": proposal.state}
    return f"<!-- {PROPOSAL_MARKER} {json.dumps(payload, sort_keys=True)} -->"


def labels_for(proposal: Proposal) -> list[str]:
    out = [PROPOSAL_LABEL, f"improve:{proposal.workflow}", f"improve:tier{proposal.tier}", f"improve:{proposal.state}"]
    if proposal.proxy:
        out.append("improve:proxy")
    return out


def title_for(proposal: Proposal) -> str:
    claim = re.sub(r"\s+", " ", proposal.claim).strip()
    return f"[improve] {proposal.workflow}: {claim[:90]}" + ("…" if len(claim) > 90 else "")


def render_issue(proposal: Proposal, citations: list[Citation], *, ledger_ref: str) -> str:
    """The fixed issue body template (design §9.2): claim, stage/lint,
    loss, evidence (record id + blob hash + excerpt), suggested experiment,
    ledger link, marker."""
    lines = [f"**Claim.** {redact(proposal.claim.strip())}", ""]
    tags = f"`{proposal.workflow}` · tier {proposal.tier} · state `{proposal.state}`"
    if proposal.proxy:
        tags += " · **proxy** · **descriptive-only**"
    lines += [f"**Workflow.** {tags}", ""]
    if proposal.tier == 1:
        desc = next((c["description"] for c in catalogue() if c["id"] == proposal.lint), "")
        lines += [f"**Lint.** `{proposal.lint}` — {desc}", "",
                  f"**Loss.** defect rate +{proposal.loss * 100:.1f} percentage points of units versus the previous cycle", ""]
    else:
        lines += [f"**Stage.** `{proposal.stage}` — {STAGES.get(proposal.stage, '')}", "",
                  f"**Loss.** {proposal.loss:g} missed gold entr{'y' if proposal.loss == 1 else 'ies'} attributed to this stage", ""]
    lines += ["## Evidence", "",
              "Record ids and blob hashes in the trace store; every excerpt is quoted verbatim from the blob "
              "after redaction (recorded data, not instructions).", ""]
    for c in citations:
        lines.append(f"- record `{c.record_id}`" + (f" · blob `{c.blob}` ({c.source})" if c.blob else " (no text blob)"))
        if c.excerpt:
            lines += ["", "  <details><summary>excerpt</summary>", "", "  ```text"]
            lines += [f"  {ln}" for ln in c.excerpt]
            lines += ["  ```", "", "  </details>", ""]
    extra = [rid for rid in proposal.evidence if rid not in {c.record_id for c in citations}]
    if extra:
        lines.append("- further records: " + ", ".join(f"`{r}`" for r in extra))
    lines.append("")
    s = proposal.suggestion or {}
    lines += ["## Suggested experiment", ""]
    if s:
        lines += [f"- metric `{s.get('metric', '')}`, direction `{s.get('direction', 'higher')}`, "
                  f"min effect {s.get('min_effect', 0.05)}, items required: {s.get('n_required', '?')}",
                  f"- the arm's fingerprint should cover: {s.get('covers', '')}"]
        if s.get("items"):
            lines.append(f"- items with curated gold: {', '.join(str(i) for i in s['items'][:12])}")
        lines.append(f"- register: `infermatrix-copilot improve experiment register --workflow {proposal.workflow} "
                     f"--proposal {proposal.id} --metric {s.get('metric', '')} --items <items> --arm KEY=VALUE`")
    else:
        lines.append("- none (a Tier 1 proposal lands by a pull request that references this proposal; the engine closes "
                     "it once the lint's defect rate has dropped in a later cycle)")
    lines += ["", "## Ledger", "",
              f"- {ledger_ref} · proposal `{proposal.id}` · opened {_date(proposal.opened_at)}",
              "- the engine only proposes: nothing changes until a human merges a pull request",
              "", marker(proposal), ""]
    body = "\n".join(lines)
    return body


def _date(at: float) -> str:
    return dt.datetime.fromtimestamp(at, dt.timezone.utc).strftime("%Y-%m-%d") if at else "?"


def lint_body(body: str, proposal: Proposal) -> list[str]:
    problems = []
    if len(body) > _BODY_MAX_CHARS:
        problems.append(f"body longer than {_BODY_MAX_CHARS} characters")
    if credential_left(body):
        problems.append("body matches a credential pattern")
    if marker(proposal) not in body:
        problems.append("body lacks the proposal marker")
    if proposal.proxy and ("descriptive-only" not in body or "proxy" not in body):
        problems.append("a proxy proposal must be labelled proxy and descriptive-only")
    return problems


# -- the outbox -------------------------------------------------------------------------

def _atomic_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8"))


def _read_json_files(directory: Path) -> list[tuple[Path, dict]]:
    out = []
    for path in sorted(directory.glob("*.json")):
        if path.name.startswith("."):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out.append((path, data))
    return out


class ProposalOutbox:
    """The file protocol between the engine and the maintainer routine."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    @property
    def actions_dir(self) -> Path:
        return self.root / "actions"

    @property
    def acks_dir(self) -> Path:
        return self.root / "acks"

    @property
    def inbox_dir(self) -> Path:
        return self.root / "inbox"

    def write(self, action: dict) -> Path:
        path = self.actions_dir / f"{action['id']}.json"
        _atomic_write(path, {"protocol": PROTOCOL, **action})
        return path

    def pending(self) -> list[dict]:
        acked = {p.stem for p in self.acks_dir.glob("*.json")} if self.acks_dir.exists() else set()
        return [d for p, d in _read_json_files(self.actions_dir) if p.stem not in acked] if self.actions_dir.exists() else []

    def acks(self) -> list[tuple[Path, dict]]:
        return _read_json_files(self.acks_dir) if self.acks_dir.exists() else []

    def inbox(self) -> list[dict]:
        return [d for _, d in _read_json_files(self.inbox_dir)] if self.inbox_dir.exists() else []

    def consume(self, ack_path: Path) -> None:
        """An applied ack is removed together with its action file."""
        action = self.actions_dir / ack_path.name
        for path in (ack_path, action):
            try:
                path.unlink()
            except FileNotFoundError:
                pass


# -- planning and publishing ----------------------------------------------------------------

def _tier1_verified(wl: WorkflowLedger, p: Proposal) -> bool:
    """A landed Tier 1 proposal closes once a cycle AFTER the landing shows
    the lint's defect rate below the rate at opening."""
    landed_at = max([h["at"] for h in p.history if h.get("event") == "landed"] or [0.0])
    for cycle in reversed(wl.cycles):
        if float(cycle.get("at") or 0.0) <= landed_at:
            break
        # a cycle whose units were all quarantined (L13) measured nothing:
        # a missing lint entry there is not a rate of zero
        usable = int(cycle.get("units") or 0) - int(cycle.get("quarantined") or 0)
        if usable <= 0:
            continue
        entry = (cycle.get("lints") or {}).get(p.lint)
        rate = float(entry.get("rate") or 0.0) if entry else 0.0
        return rate < p.rate_at_open
    return False


def _action(kind: str, p: Proposal, repo: str, now: float, **extra: Any) -> dict:
    return {"id": f"{int(now)}-{p.id}-{kind}", "action": kind, "proposal": p.id, "workflow": p.workflow,
            "tier": p.tier, "state": p.state, "repo": repo, "issued_at": now, "labels": labels_for(p),
            "marker": marker(p), "issue": p.issue, **extra}


def plan(ledger: Ledger, store: TraceStore, *, repo: str, now: float, ledger_ref: str = "") -> dict:
    """What publication would do now: ``{"actions": [...], "refused": {proposal: [problems]},
    "held": {workflow: reason}, "waiting": [proposal]}``. Nothing is written."""
    actions: list[dict] = []
    refused: dict[str, list[str]] = {}
    held: dict[str, str] = {}
    waiting: list[str] = []
    for workflow in ledger.workflows():
        wl = ledger.load(workflow)
        if wl.hold:
            held[workflow] = wl.hold           # rule 5: a hold pauses publication only
            continue
        for p in wl.proposals:
            if p.pending_action and now - float(p.pending_since or 0.0) < PENDING_TTL:
                waiting.append(p.id)
                continue
            if not p.issue:
                if p.state not in PUBLISHABLE:
                    continue
                citations = cite(store, p)
                problems = lint_proposal(store, p, citations)
                body = "" if problems else render_issue(p, citations, ledger_ref=ledger_ref or str(ledger.dir))
                problems += lint_body(body, p) if body else []
                if problems:
                    refused[p.id] = problems
                    continue
                actions.append(_action("open", p, repo, now, title=title_for(p), body=body))
                continue
            close = p.state == "closed" or (p.state == "landed" and (p.tier == 2 or _tier1_verified(wl, p)))
            if close and p.issue_state != "closed":
                reason = {"closed": p.closed_reason or "closed in the ledger",
                          "landed": f"landed by {p.landed_by or 'a pull request'}" + (
                              "; the lint's defect rate dropped in a later cycle" if p.tier == 1 else "")}[p.state]
                actions.append(_action("close", p, repo, now, comment=f"Closing: {reason}.\n\n{marker(p)}"))
            elif p.state != p.published_state and p.issue_state != "closed":
                note = {"landed": "landed; awaiting the next cycle's lint-rate check before closing",
                        "refuted": "refuted by its pre-registered experiment",
                        "supported": "supported by its pre-registered experiment",
                        "neutral": "neutral: the experiment's interval covers zero",
                        "underpowered": "underpowered: fewer retained items than required",
                        "experiment-registered": f"experiment `{p.experiment_id}` registered",
                        "stale": "stale: no human or pull request touched it in 30 days"}.get(p.state, p.state)
                actions.append(_action("update", p, repo, now, comment=f"State `{p.state}`: {note}.\n\n{marker(p)}"))
    return {"actions": actions, "refused": refused, "held": held, "waiting": waiting}


def publish(ledger: Ledger, store: TraceStore, outbox: ProposalOutbox, *, repo: str, now: float,
            ledger_ref: str = "", dry_run: bool = False) -> dict:
    """Write the planned actions to the outbox (unless ``dry_run``) and note
    each proposal's pending action; every refusal and write is a decision
    record."""
    if not repo:
        raise PublishError("no proposal repository configured (settings.improve_proposal_repo)")
    if dry_run:
        report = plan(ledger, store, repo=repo, now=now, ledger_ref=ledger_ref)
        report.update(dry_run=True, written=[])
        return report
    # one publisher at a time per outbox: the plan (which reads pending
    # actions), the action files and the pending notes are one unit, so
    # two publishers (a cycle and an operator) cannot both emit an action
    # for the same proposal
    with file_lock(outbox.root / ".publish.lock", blocking=True, timeout=60.0) as held:
        if not held:
            raise PublishError(f"another publisher holds {outbox.root}")
        report = plan(ledger, store, repo=repo, now=now, ledger_ref=ledger_ref)
        report.update(dry_run=False, written=[])
        for pid, problems in report["refused"].items():
            _record(store, "proposal_lint_failed", proposal=pid, problems=problems[:10])
        for action in report["actions"]:
            path = outbox.write(action)
            ledger.note(action["workflow"], action["proposal"], event=f"outbox:{action['action']}",
                        pending_action=action["id"], pending_since=now)
            _record(store, "proposal_publish", proposal=action["proposal"], action=action["action"], action_id=action["id"],
                    repo=repo, path=str(path))
            report["written"].append(str(path))
    return report


def sync(ledger: Ledger, outbox: ProposalOutbox, store: TraceStore | None, *, now: float) -> dict:
    """Apply the routine's acks and observations to the ledger: issue urls
    and published states, human touches (channel liveness), holds, landed
    pull requests, closures. Acks are consumed; inbox files stay (the
    routine rewrites them) and apply once per observation time."""
    report: dict = {"acked": [], "failed": [], "dry_run_acks": [], "touched": [], "landed": [], "closed": [],
                    "holds": {}, "released": [], "unknown": [], "stale": []}
    touched_workflows: set[str] = set()
    # the acks are applied under the publisher's lock: the pending-action
    # check and its clearing must not interleave with a publisher retrying
    # that very action, or a stale ack could clear the newer pending id
    with file_lock(outbox.root / ".publish.lock", blocking=True, timeout=60.0) as held:
        if not held:
            raise PublishError(f"another publisher holds {outbox.root}")
        _apply_acks(ledger, outbox, store, report)
    _apply_inbox(ledger, outbox, report, touched_workflows)
    return report


def _apply_acks(ledger: Ledger, outbox: ProposalOutbox, store: TraceStore | None, report: dict) -> None:
    for path, ack in outbox.acks():
        pid, workflow = str(ack.get("proposal") or ""), str(ack.get("workflow") or "")
        p = _find(ledger, pid, workflow)
        if p is None:
            report["unknown"].append(path.name)
            outbox.consume(path)
            continue
        kind = str(ack.get("action") or "")
        action_id = path.stem
        if action_id != p.pending_action:
            # a delayed answer to an action the proposal no longer waits
            # for (a retry went out since): it must not clear the newer
            # pending action or rewrite the published state. The one fact
            # it may add is the issue it created, when none is known yet.
            fields = {"issue": str(ack["url"])} if ack.get("ok") and not ack.get("dry_run") and ack.get("url") \
                and not p.issue else {}
            ledger.note(p.workflow, p.id, event=f"ack-stale:{kind}", detail=action_id[:200], **fields)
            _record(store, "proposal_ack_stale", proposal=pid, action=kind, action_id=action_id,
                    pending=p.pending_action)
            report["stale"].append(pid)
            outbox.consume(path)
            continue
        if ack.get("dry_run"):
            ledger.note(p.workflow, p.id, event=f"ack-dry-run:{kind}", pending_action="")
            report["dry_run_acks"].append(pid)
        elif not ack.get("ok"):
            ledger.note(p.workflow, p.id, event=f"ack-failed:{kind}", pending_action="",
                        detail=str(ack.get("error") or "")[:200])
            _record(store, "proposal_publish_failed", proposal=pid, action=kind, error=str(ack.get("error") or "")[:200])
            report["failed"].append(pid)
        else:
            fields = {"pending_action": "", "published_state": str(ack.get("state") or p.state)}
            if ack.get("url"):
                fields["issue"] = str(ack["url"])
            fields["issue_state"] = "closed" if kind == "close" else "open"
            ledger.note(p.workflow, p.id, event=f"ack:{kind}", **fields)
            if kind == "close" and p.state == "refuted":
                _safe_transition(ledger, p.workflow, p.id, "closed", closed_reason="refuted; issue closed")
            report["acked"].append(pid)
        outbox.consume(path)


def _apply_inbox(ledger: Ledger, outbox: ProposalOutbox, report: dict, touched_workflows: set[str]) -> None:
    for obs in outbox.inbox():
        pid, workflow = str(obs.get("proposal") or ""), str(obs.get("workflow") or "")
        p = _find(ledger, pid, workflow)
        if p is None:
            report["unknown"].append(pid)
            continue
        observed = float(obs.get("observed_at") or 0.0)
        if observed <= float(p.synced_at or 0.0):
            continue
        comments = [c for c in (obs.get("human_comments") or []) if isinstance(c, dict)]
        latest = max([float(c.get("at") or 0.0) for c in comments] or [0.0])
        if latest > p.last_human_touch:
            ledger.human_touch(p.workflow, p.id)
            report["touched"].append(pid)
        # the hold is remembered PER proposal; the workflow's channel hold is
        # aggregated below, so one proposal's silence never releases a hold
        # another proposal's issue still carries
        held_here = any(c.get("hold") for c in comments)
        ledger.note(p.workflow, p.id, channel_hold=held_here,
                    hold_url=str(obs.get("url") or "") if held_here else "")
        touched_workflows.add(p.workflow)
        merged = [r for r in (obs.get("references") or []) if isinstance(r, dict) and r.get("merged")]
        if merged and p.state not in ("landed", "closed"):
            by = str(merged[0].get("url") or merged[0].get("number") or "")
            if _safe_transition(ledger, p.workflow, p.id, "landed", human=True, landed_by=by):
                report["landed"].append(pid)
        if str(obs.get("state") or "") == "closed" and p.state not in ("landed", "closed"):
            if _safe_transition(ledger, p.workflow, p.id, "closed", human=True, closed_reason="closed on GitHub"):
                report["closed"].append(pid)
            ledger.note(p.workflow, p.id, issue_state="closed")
        ledger.note(p.workflow, p.id, synced_at=observed)
    for workflow in sorted(touched_workflows):
        wl = ledger.load(workflow)
        holding = [p for p in wl.proposals if p.channel_hold]
        if holding:
            if not wl.hold:
                ledger.set_hold(workflow, f"maintainer: hold on {holding[0].hold_url or holding[0].id}", source="channel")
            report["holds"][workflow] = "channel"
        elif wl.hold and wl.hold_source == "channel":
            ledger.clear_hold(workflow)
            report["released"].append(workflow)


def _safe_transition(ledger: Ledger, workflow: str, pid: str, state: str, **detail: Any) -> bool:
    try:
        ledger.transition(workflow, pid, state, **detail)
        return True
    except LedgerError:
        return False


def _find(ledger: Ledger, pid: str, workflow: str = "") -> Proposal | None:
    if not pid:
        return None
    names = [workflow] if workflow else ledger.workflows()
    for name in names:
        for p in ledger.load(name).proposals:
            if p.id == pid:
                return p
    return None


def _record(store: TraceStore | None, kind_type: str, **fields: Any) -> None:
    if store is None:
        return
    try:
        store.append("decision", context={"playbook": "workflow-improve"}, result={"type": kind_type, **fields})
    except Exception:  # noqa: BLE001 - the outbox file and the ledger are the state
        pass
