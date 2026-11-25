"""Ground Strict review assessments in immutable source and raw run records."""

from __future__ import annotations

import asyncio
import io
import tokenize
from functools import lru_cache
from pathlib import Path
import re
import subprocess
import tempfile

from ....tools import ToolDef
from .._common import gh, git, repo_path
from ..pr import fetch
from .repo_tools import _safe_rel_path
from .utils import _review_verdict


WEIGHTS = {"scope": 15, "correctness": 25, "validation": 20, "claim_evidence": 30, "simplicity": 10}
CLAIMS = """list of {claim, status, reason, kind, records}. Check each acceptance claim and every
required output separately even when there are no review findings. kind=source|runtime.
For verified source behavior cite records [{path,quote}] with an exact decisive source line.
For observed runtime behavior cite a raw CI record {job_id,quote} from read_review_ci, or a
committed raw output {path,quote,run_sha,revision_quote} whose revision_quote line identifies the
producing revision. Check real outputs (including media when required), workload/config and both
sides of comparisons; an attachment URL, hash, screenshot of metrics or a green badge alone is
insufficient. For a comparison set comparison=true and mark each reference role=baseline|head.
If repository acceptance explicitly requires the missing artifact, cite acceptance_path and
acceptance_quote from the governing policy at the base; otherwise report the verification gap.
For a missing tracked artifact use status=missing and {path,requirement_path,requirement_quote}
with the complete affirmative governing clause, including qualifiers and the exact artifact path. For an identified CI
job whose log cannot be retrieved use status=inaccessible with {job_id}. For a wrong-run record use status=mismatched and cite its revision.
Use unverified for checks you could not perform, including unsupported media or links.
Use a single not_applicable row with rationale only when there is no applicable claim."""
RATINGS = """list of {dimension,rating,reason,path,quote}. Assess scope, correctness, validation,
claim_evidence and simplicity exactly once, even on a clean review. Rate 3 = matching evidence
throughout, 2 = bounded gap, 1 = material gap with some support, 0 = no usable support or a
material contract violation. Cite a decisive line in the frozen source. Use N/A only if outside
scope; use unverified for incomplete review. Explain each judgment, required artifacts, and
contributor vs reviewer checks. Never calculate a total. If a trusted repo rule zeroes simplicity
for an introduced explanatory comment, cite policy_path, policy_quote at the base and quote
the added comment at path; check exceptions and scope. Set comment_penalty=true only then."""


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _sha(value):
    return bool(re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", _text(value)))


class ReviewRecords:
    """Resolve model citations against this review's repository and pinned revisions."""

    def __init__(self, repo: Path | None, head: str, base: str, live: bool):
        self.repo, self.head, self.base, self.live = repo, head, base, live
        self.job_revision = lru_cache(maxsize=None)(self._job_revision)
        self.job_line = lru_cache(maxsize=None)(self._job_line)

    def _job_revision(self, job_id):
        if not self.repo or not self.live or type(job_id) is not int or job_id < 1:
            return ""
        try:
            rc, sha = gh(["api", f"repos/{{owner}}/{{repo}}/actions/jobs/{job_id}",
                          "--jq", ".head_sha"], cwd=self.repo)
            return sha.strip() if not rc and _sha(sha) else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    def _job_line(self, job_id, needle):
        if not self.job_revision(job_id) or not needle:
            return "unverified", ""
        with tempfile.TemporaryFile() as log:
            try:
                run = subprocess.run(
                    ["gh", "api", f"repos/{{owner}}/{{repo}}/actions/jobs/{job_id}/logs"],
                    cwd=self.repo, stdout=log, stderr=subprocess.PIPE, timeout=120)
            except (OSError, subprocess.SubprocessError):
                return "unverified", ""
            if run.returncode:
                error = run.stderr.lower()
                expired = b"http 404" in error or b"http 410" in error or b"log has expired" in error
                return ("inaccessible" if expired else "unverified"), ""
            log.seek(0)
            for raw in log:
                line = raw.decode("utf-8", errors="replace").strip()
                if needle in line:
                    return "verified", line
            return "unverified", ""

    def log_tool(self):
        def read_review_ci(job_id: int, contains: str):
            if type(job_id) is not int or not _text(contains):
                return "Supply the job id and a nonempty literal search string."
            revision = self.job_revision(job_id)
            _, line = self.job_line(job_id, contains)
            return f"Job {job_id} at {revision}: {line}" if line else "No matching raw log line."
        return ToolDef("read_review_ci", "Read a matching raw Actions job log line and its producing commit.",
                       {"type": "object", "properties": {"job_id": {"type": "integer"},
                        "contains": {"type": "string"}}, "required": ["job_id", "contains"]},
                       read_review_ci)

    def tracked(self, sha, path, quote=None):
        if not self.repo or not _sha(sha) or not _text(path) or "\0" in path:
            return None
        try:
            path = _safe_rel_path(path)
            rc, out = git(self.repo, "--literal-pathspecs", "ls-tree", sha, "--", path)
            if rc:
                return None
            if not out:
                return False
            if not out.startswith(("100644 blob ", "100755 blob ")):
                return None
            if quote is None:
                return True
            if not _text(quote) or "\0" in quote:
                return None
            lines = quote.splitlines()
            if len(lines) == 1:
                rc, _ = git(self.repo, "--literal-pathspecs", "grep", "-I", "-q", "-F", "-e", quote,
                            sha, "--", path)
                return rc == 0 if rc in (0, 1) else None
            rc, text = git(self.repo, "--literal-pathspecs", "grep", "-I", "-h", "-F", "-A",
                           str(len(lines) - 1), "-e", lines[0], sha, "--", path)
            return rc == 0 and quote in text
        except (ValueError, OSError, subprocess.SubprocessError):
            return None

    def governing_clause(self, sha, path, statement):
        if not _text(statement) or self.tracked(sha, path) is not True:
            return False
        try:
            rc, source = git(self.repo, "show", f"{sha}:{_safe_rel_path(path)}")
        except (ValueError, OSError, subprocess.SubprocessError):
            return False
        return not rc and _unconditional_clause(source, statement)

    def requires_artifact(self, sha, path, statement, artifact):
        statement = _text(statement)
        return _requires_path(statement, artifact) and self.governing_clause(sha, path, statement)

    def match(self, record, kind):
        if not isinstance(record, dict):
            return "unverified"
        path, quote = record.get("path"), record.get("quote")
        expected = self.base if record.get("role") == "baseline" else self.head
        if not _sha(expected):
            return "unverified"
        if kind not in ("source", "runtime"):
            return "unverified"
        if "job_id" in record:
            if kind != "runtime" or type(record["job_id"]) is not int or record["job_id"] < 1:
                return "unverified"
            actual = self.job_revision(record["job_id"])
            if _sha(actual) and actual != expected:
                return "mismatched"
            if actual != expected:
                return "unverified"
            status, _ = self.job_line(record["job_id"], _text(quote) or " ")
            if not _text(quote) and status == "verified":
                return "unverified"
            return status
        if self.tracked(expected, path, quote) is not True or not _text(quote):
            return "unverified"
        if kind == "source":
            return "verified"
        actual, line = record.get("run_sha"), record.get("revision_quote")
        if _sha(actual) and actual in _text(line) and self.tracked(expected, path, line):
            return "verified" if actual == expected else "mismatched"
        return "unverified"


def assess_claims(proposals, records):
    rows = []
    for row in proposals if isinstance(proposals, list) else []:
        if not isinstance(row, dict) or not _text(row.get("claim")) or not _text(row.get("reason")):
            rows.append({"claim": "Claim verification", "status": "unverified", "reason": "Malformed claim assessment."})
            continue
        status, refs = row.get("status"), row.get("records")
        refs = refs if isinstance(refs, list) else []
        failed_paths = []
        if "comparison" in row and type(row["comparison"]) is not bool:
            status = "unverified"
        elif any(not isinstance(ref, dict) or ref.get("role", "head") not in ("head", "baseline")
                 for ref in refs):
            status = "unverified"
        elif status == "missing":
            failed_paths = [ref["path"] for ref in refs if (
                isinstance(ref, dict) and _text(ref.get("path"))
                and records.requires_artifact(records.head, ref.get("requirement_path"),
                                              ref.get("requirement_quote"), ref["path"])
                and records.tracked(records.head, ref["path"]) is False
            )]
            status = "missing" if failed_paths else "unverified"
        elif status in ("verified", "mismatched", "inaccessible"):
            checks = [records.match(ref, row.get("kind")) for ref in refs]
            failed_paths = [_text(ref.get("path")) for ref, check in zip(refs, checks)
                            if isinstance(ref, dict) and check in ("inaccessible", "mismatched")]
            roles = {_text(r.get("role", "head")) for r in refs if isinstance(r, dict)}
            if "mismatched" in checks:
                status = "mismatched"
            elif "inaccessible" in checks:
                status = "inaccessible"
            elif (status != "verified" or not checks or "head" not in roles
                  or row.get("comparison") is True and "baseline" not in roles
                  or any(c != "verified" for c in checks)):
                status = "unverified"
        elif status != "not_applicable":
            status = "unverified"
        result = {"claim": row["claim"], "reason": row["reason"], "status": status,
                  "records": refs}
        if status == "unverified" and row.get("status") != status:
            result["reason"] = "The cited evidence could not be confirmed at the reviewed revision."
        policy = _text(row.get("acceptance_path"))
        statement = _text(row.get("acceptance_quote"))
        if (status in ("missing", "inaccessible", "mismatched") and statement
                and any(_policy_path(policy, path, records)
                        and records.requires_artifact(records.base, policy, statement, path)
                        for path in failed_paths if path)):
            result.update(acceptance_path=policy, acceptance_quote=statement)
        if result not in rows:
            rows.append(result)
    applicable = [r for r in rows if r["status"] != "not_applicable"]
    return applicable or rows[:1] or [{"claim": "Claim verification", "status": "unverified",
                                        "reason": "The investigation returned no claim assessment."}]


def score_review(proposals, claims, records, *, gap="", diff=""):
    dimensions = {}
    points = applicable_weight = 0
    for key, weight in WEIGHTS.items():
        candidates = []
        for row in proposals if isinstance(proposals, list) else []:
            if not isinstance(row, dict) or row.get("dimension") != key or not _text(row.get("reason")):
                continue
            rating = row.get("rating")
            if not (type(rating) is int and 0 <= rating <= 3 or rating == "N/A"):
                continue
            if records.tracked(records.head, row.get("path"), row.get("quote")) is not True or not _text(row.get("quote")):
                continue
            if key == "simplicity" and row.get("comment_penalty") is True:
                rule = _text(row.get("policy_quote"))
                if not (_comment_penalty_rule(rule, row["path"])
                        and _policy_path(row.get("policy_path"), row["path"], records)
                        and records.governing_clause(records.base, row.get("policy_path"), rule)
                        and _introduced_comment(records, diff, row["path"], row["quote"])):
                    continue
                rating = 0
            candidates.append({"rating": rating, "reason": row["reason"], "path": row["path"], "quote": row["quote"]})
        assessed = [r for r in candidates if type(r["rating"]) is int]
        dimension = min(assessed, key=lambda r: r["rating"]) if assessed else next(iter(candidates), {})
        statuses = {r["status"] for r in claims}
        if key == "claim_evidence":
            if statuses.intersection({"missing", "mismatched", "inaccessible"}):
                dimension = {"rating": 0, "reason": "Required artifact missing, inaccessible, or tied to a different revision."}
            elif statuses == {"not_applicable"}:
                dimension = {"rating": "N/A", "reason": claims[0]["reason"]}
            elif dimension.get("rating") == "N/A":
                dimension = {}
        if not dimension:
            dimension = {"rating": "unverified", "reason": "No supported assessment."}
            gap = gap or f"{key} is unassessed."
        dimensions[key] = {**dimension, "weight": weight}
        if type(dimension["rating"]) is int:
            applicable_weight += weight
            points += weight * dimension["rating"]
    if any(row["status"] == "unverified" for row in claims):
        gap = gap or "Claim verification is incomplete."
    if not applicable_weight:
        gap = gap or "No applicable dimensions."
    denominator = 3 * applicable_weight
    return {"total": None if gap else (200 * points + denominator) // (2 * denominator),
            "reason": gap, "dimensions": dimensions, "applicable_weight": applicable_weight,
            "reviewed_head_sha": records.head, "base_sha": records.base}


def _requires_path(statement, path):
    if not _text(path):
        return False
    # Accept a small grammar of explicit requirements. A substring of a
    # negation, alternative, example, or similarly named path is insufficient.
    clause = re.sub(r"^(?:[-*+] |[0-9]+[.)] )", "", statement.strip()).strip()
    artifact = rf"(?:`{re.escape(path)}`|{re.escape(path)})"
    subject = r"(?:we|you|(?:all |every |each |a |the )?(?:passing )?(?:reviews?|pull requests?|PRs?|changes?|submissions?|contributors?))"
    verb = r"(?:requires?|(?:must|shall) (?:include|produce|attach|provide|retain))"
    return bool(re.fullmatch(rf"(?i:(?:{subject} {verb}|must (?:include|produce|attach|provide|retain)) (?:the )?){artifact}[.]?", clause)
                or re.fullmatch(rf"{artifact}(?i: (?:is required|must be (?:included|present|provided)))[.]?", clause))


def _comment_penalty_rule(statement, target):
    rule = " ".join(statement.replace("**", "").removeprefix("- ").split())
    if "byte-preserved historical evidence" in rule.casefold():
        # The scoped rule covers maintained code. A file copied into a campaign
        # archive can have a source extension without being maintained code.
        if target.partition("/")[0] not in {"src", "csrc", "test", "tests", "scripts", "config"}:
            return False
    return bool(re.fullmatch(
        r"simplicity is 0/3 if the diff adds or rewrites (?:any|an) explanatory code comment"
        r"(?: in maintained source, tests, scripts, or config)?\."
        r"(?: Apply this check to changed source, tests, scripts, and config\.)?"
        r"(?: License headers(?:, shebangs, tool directives, and required docstrings| and directives) are exempt\.)?"
        r"(?: Treat byte-preserved historical evidence as data\.)?"
        r"(?: Express (?:the underlying )?behavior through code, tests, or relevant docs\.)?", rule, re.I))


def _unconditional_clause(source, statement):
    """Ignore quoted examples and qualifications outside the model's excerpt."""
    headings = {}
    fence = ""
    lines = source.splitlines()
    qualifier = re.compile(r"\b(if|unless|when|except(?:ions?)?|only|optional|example[s]?|"
                           r"sample[s]?|obsolete|legacy|deprecated|draft|recommend(?:ed)?)\b", re.I)
    clause = [line.strip() for line in statement.splitlines()]
    clause[0] = clause[0].removeprefix("- ")
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            mark = stripped[:3]
            if not fence or fence == mark:
                fence = "" if fence else mark
            continue
        if fence:
            continue
        heading = re.match(r"^(#{1,6}) (.+)", stripped)
        if heading:
            level = len(heading[1])
            headings = {k: v for k, v in headings.items() if k < level}
            headings[level] = heading[2]
            continue
        if stripped.removeprefix("- ") != clause[0] or any(qualifier.search(h) for h in headings.values()):
            continue
        end = index + len(clause)
        if [s.strip() for s in lines[index + 1:end]] != clause[1:]:
            continue
        # List-wide qualifiers may precede earlier items in the same block.
        previous_block = "\n".join(lines[:index]).rstrip().rsplit("\n\n", 1)[-1]
        if any(s.rstrip().endswith(":") for s in previous_block.splitlines()):
            continue
        if index and lines[index - 1].strip() and not re.match(r"^(?:#|[-*+] |[0-9]+[.)] )", lines[index - 1].strip()):
            if not lines[index - 1].rstrip().endswith((".", "!", "?")):
                continue
        if end < len(lines) and (re.match(r"^(?:except|unless|if|when|only|provided|but|or)\b", lines[end].strip(), re.I)
                                 or lines[end].startswith((" ", "\t")) and lines[end].strip()):
            continue
        return True
    return False


def _policy_path(path, target, records):
    path = _text(path)
    if Path(path).name in ("AGENTS.md", "CLAUDE.md"):
        return "/" not in path or target.startswith(str(Path(path).parent) + "/")
    if path.lower() in {"contributing.md", ".github/contributing.md", "docs/contributing.md"}:
        return True
    match = re.fullmatch(r"((?:\.agents|\.claude)/skills/(?:review-pr|imreview))/(.+)", path)
    if not match:
        return False
    folder, relative = match.groups()
    return relative == "SKILL.md" or records.tracked(records.base, folder + "/SKILL.md", f"({relative})") is True


def _explanatory_comment(line):
    text = line.strip()
    match = re.search(r"(?:^|\s)(#|//|/\*|--|\*)\s*(.*)$", text)
    if not match:
        return False
    return not re.match(r"(?:!|SPDX|copyright|coding[:=]|noqa|type:|fmt:|ruff:|pylint:|"
                        r"pragma|nosec|shellcheck|eslint|prettier|include|define|if(?:def|ndef)?\b|endif\b)",
                        match[2], re.I)


def _introduced_comment(records, diff, path, quote):
    if not _explanatory_comment(quote) or not _added_line(diff, path, quote):
        return False
    if not path.endswith((".py", ".pyi")):
        return Path(path).suffix.lower() not in (".md", ".rst", ".txt")
    try:
        code, source = git(records.repo, "show", f"{records.head}:{path}")
        if code:
            return False
        return any(token.type == tokenize.COMMENT and token.line.strip() == quote.strip()
                   for token in tokenize.generate_tokens(io.StringIO(source).readline))
    except (OSError, subprocess.SubprocessError, tokenize.TokenError, IndentationError):
        return False


def _added_line(diff, path, quote):
    current = ""
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            current = ""
        if line.startswith("+++ b/"):
            current = line[6:]
        elif current == path and line.startswith("+") and line[1:].strip() == quote.strip():
            return True
    return False


async def score_freshness(ctx):
    head = _text(ctx.state.get("pr_head_sha"))
    if not _sha(head):
        return "The reviewed head is unknown."
    if ctx.state.get("review_observed_stale_head"):
        return "The PR head moved."
    if getattr(ctx.settings, "pr_context_source", "live") == "snapshot":
        return ""
    repo = repo_path(ctx)
    pr = (ctx.state.get("task_spec") or {}).get("pr")
    if not repo or not pr:
        return "The current PR head could not be confirmed."
    try:
        _, current, _ = await asyncio.to_thread(fetch._resolve_pr_head, repo, pr)
    except (OSError, subprocess.SubprocessError):
        current = ""
    if not _sha(current):
        return "The current PR head could not be confirmed."
    if current != head:
        ctx.trace.record("expected_head_mismatch", pr=pr, expected=head, actual=current, step="agent.review_diff")
        return "The PR head moved."
    return ""


async def apply_review_evidence(ctx, output, updates, *, partial=False, records=None):
    records = records or ReviewRecords(repo_path(ctx), ctx.state.get("pr_head_sha", ""),
                    ctx.state.get("pr_base_sha", ""), getattr(ctx.settings, "pr_context_source", "live") != "snapshot")
    claims = await asyncio.to_thread(assess_claims, output.get("claim_checks"), records)
    updates["review_claim_evidence"] = claims
    text = ["Claim evidence"]
    for row in claims:
        refs = [str(r.get("path") or f"Actions job {r['job_id']}") for r in row.get("records", [])
                if isinstance(r, dict) and (_text(r.get("path")) or type(r.get("job_id")) is int)]
        citations = " (" + ", ".join(refs) + ")" if refs else ""
        text.append(f"- {row['status']} · {row['claim']} — {row['reason']}{citations}")
    if (ctx.state.get("task_spec", {}).get("params") or {}).get("include_score") is True:
        gap = "Part of the investigation was incomplete." if partial or output.get("_assessment_incomplete") else ""
        score = await asyncio.to_thread(score_review, output.get("dimension_ratings"), claims, records,
                                        gap=gap, diff=ctx.state.get("diff_text", ""))
        freshness = await score_freshness(ctx)
        if freshness:
            score.update(total=None, reason=freshness)
        updates["review_score"] = score
        label = f"{score['total']}/100" if score["total"] is not None else f"withheld ({score['reason']})"
        text += ["", f"Advisory score · {label} (applicable weight {score['applicable_weight']}/100)",
                 f"Base {records.base or 'unknown'} · head {records.head or 'unknown'}"]
        for key, entry in score["dimensions"].items():
            rating = entry["rating"]
            value = f"{rating}/3" if type(rating) is int else rating
            source = f" ({entry['path']})" if entry.get("path") else ""
            text.append(f"- {key} · {value} · {entry['reason']}{source}")
    if any("acceptance_quote" in row for row in claims):
        updates["review_verdict"] = _review_verdict([{"severity": "blocker"}], ctx.state.get("pr_state", ""))
        text += [f"- Required by {r['acceptance_path']}: {r['acceptance_quote']}"
                 for r in claims if "acceptance_quote" in r]
    for field in ("review_text", "review_summary"):
        body = str(updates.get(field) or "").rstrip()
        verdict = updates.get("review_verdict")
        if verdict:
            body = re.sub(r"(?:^|\n)\*\*Verdict:\*\* [^\n]*$", "", body).rstrip()
        sections = [body, "\n".join(text)]
        if verdict:
            sections.append(f"**Verdict:** {verdict}")
        updates[field] = "\n\n".join(part for part in sections if part)
