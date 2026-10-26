"""L4 plan review on the copilot LLM client — the production backend for
the `request_plan_review` tool (parent `agent/tools/plan_review.py`,
rebuilt on the injected Anthropic-compatible client instead of raw HTTP).

Contract (parent-shaped): read the exact plan files, ask the reviewer
model for a verdict, write `<plan_id>.review.json` / `.md` beside the
plan, return `{"verdict", "summary", ...}` — or `{"error": ...}` when the
review could not run (the agent retries; after its bounded rounds the
prompt's soft-fallback lets it proceed, parent parity)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..structured_output import decode_json_object

_REVIEW_PROMPT = """You are a strict senior reviewer for an automated \
repository-rebase agent. Review the plan below for correctness, missing \
steps, and risk. Respond with ONLY a JSON object:
{{"verdict": "approve" | "revise", "summary": "<one paragraph>",
  "concerns": ["<concern>", ...]}}

## Plan to review
plan_id: {plan_id}
kind: {kind}

### Plan JSON
{plan_json}

### Plan markdown
{plan_md}
"""


def _parse_review(text: str) -> dict | None:
    try:
        data = decode_json_object(text, policy="greedy_object")
    except ValueError:
        return None
    if data.get("verdict") not in ("approve", "revise"):
        return None
    return data


def review_plan(client: Any, model: str, *, plan_json_path: str,
                plan_md_path: str = "", kind: str = "rebase",
                max_tokens: int = 2000) -> dict:
    """Run one plan review (SYNC — tool backends dispatch synchronously
    from inside the agent loop, so this takes a sync Anthropic-compatible
    client). Never raises — every failure is an `{"error": ...}` result
    the agent can see and retry on."""
    json_path = Path(plan_json_path)
    if not json_path.exists():
        return {"error": f"Plan JSON not found: {plan_json_path}"}
    md_path = Path(plan_md_path) if plan_md_path \
        else json_path.with_suffix(".md")
    if not md_path.exists():
        return {"error": f"Plan markdown not found: {md_path} (pass "
                         "explicit plan_md_path or ensure .md exists "
                         "alongside .json)"}
    try:
        plan_json = json_path.read_text(encoding="utf-8")
        json.loads(plan_json)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return {"error": f"Failed to read/parse plan JSON: {exc}"}
    plan_md = md_path.read_text(encoding="utf-8")

    prompt = _REVIEW_PROMPT.format(plan_id=json_path.stem, kind=kind,
                                   plan_json=plan_json[:20000],
                                   plan_md=plan_md[:20000])
    try:
        response = client.messages.create(
            model=model, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}])
        text = "".join(getattr(b, "text", "") or "" for b in response.content)
    except Exception as exc:  # noqa: BLE001 - review failure is a result
        return {"error": f"Plan review call failed: {exc}"}

    review = _parse_review(text)
    if review is None:
        return {"error": "Reviewer returned no parseable verdict JSON — "
                         "retry request_plan_review"}
    out_json = json_path.parent / f"{json_path.stem}.review.json"
    out_md = json_path.parent / f"{json_path.stem}.review.md"
    try:
        out_json.write_text(json.dumps(review, indent=1), encoding="utf-8")
        out_md.write_text(
            f"# Plan review — {json_path.stem}\n\n"
            f"**Verdict:** {review['verdict']}\n\n"
            f"{review.get('summary', '')}\n\n"
            + "".join(f"- {c}\n" for c in review.get("concerns") or []),
            encoding="utf-8")
    except OSError as exc:
        return {"error": f"Could not write review files: {exc}"}
    return {"verdict": review["verdict"],
            "summary": review.get("summary", ""),
            "concerns": review.get("concerns") or [],
            "review_json": str(out_json), "review_md": str(out_md)}
