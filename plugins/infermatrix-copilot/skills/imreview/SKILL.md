---
name: imreview
description: Review a PR or local changes with InferMatrixCopilot Direct mode. Use when the user invokes imreview.
---

The host verifies the source and owns the verdict. Direct supplies a plan and knowledge.

## Pin the target

Use the supplied target or the current PR/worktree. Freeze the PR base, head, title,
body and changed files. For local edits, capture HEAD and the exact staged, unstaged
and relevant untracked content under review. Within 60 seconds, report the pinned
head SHA, current CI status, mergeability and preliminary findings in the host
conversation before reading knowledge, searching source or running tests. Report
unavailable fields as such and continue. Post to GitHub only on explicit request.

## Plan once

Load applicable project instructions and required references once. Check which rules
govern this change and their scope. A proposed policy change is itself reviewable.
Call `review` once with `mode="direct"`, the selected `repo`, title, body, changed
files, pinned `expected_head_sha` and the frozen diff when available. Use the
returned `quick_map` and bounded `related_knowledge`. Open a full route only to
resolve a concrete ambiguity. For incomplete excerpts, follow the budgeted
`navigation_policy.related_document_read_paths`. Verify knowledge against source.
Treat API hints as leads and `no_diff` as unknown coverage.

Retain the returned trace ID and forward the plan's containment receipt unchanged.
When a trace is returned, use the [knowledge trace procedure](references/knowledge-trace.md)
starting with the first traced read. Record actual usage before validation closes it.

## Inspect and verify

Run independent source/knowledge and validation work concurrently after the update.
Use one evidence packet for bounded searches, callers, tests and findings. Read PR
evidence at the pinned head SHA, fetching the PR head ref if needed. For worktree
reviews, bind observations to the captured edits. Inspect overlapping CI failures
or failures blocking the verdict.

* Trace each changed behavior through production callers, consumed inputs, failure
  paths and cleanup. For async work include initialization, error propagation,
  join/cancel and terminal output. For a new or expanded helper, class, fallback,
  compatibility branch or public behavior, read the required simplification guide.
  Test what breaks on deletion, inlining or reuse. Check ignored inputs, duplicated
  state, no-op hooks and ownership of mutable state. Attribute introduced, modified
  and inherited sites separately.
* Verify repository syntax policies with lint or AST checks. For bans on nested
  functions or `nonlocal`, check function ancestry and declarations, including
  tests unless exempt. Check equivalent captures through lambdas or defaults.
* Before pytest, check import and version compatibility. Bind test results to the
  head SHA and environment fingerprint. Run focused tests and low-cost static
  checks when compatible. If the preflight fails, continue source review and
  report skipped tests. For docs-only changes check diff hygiene and the live contract.

Treat `execution_budget` as a hard ceiling. Use its one-time allowance only for a
stated unresolved P1/high-risk contract. A separate durable allowance requires
explicit human authorization and a server advertising `direct-continuation/1`
with all continuation tools. Only then use [continuation](references/continuation.md).
Stop when required paths and checks have findings or supported no-issue conclusions.
Report unfinished policy, source or minimality checks as a partial review.

## Finalize

Freeze independently verified findings before reading PR feedback. Then follow the
[PR feedback procedure](references/pr-feedback.md) to deduplicate them. Pass
`not_applicable` for local edits or `disabled` for explicit
`PR_CONTEXT_MODE=no_discussion`. Report unavailable feedback as a verification gap.

Call `validate_direct_review` with the pinned `evidence_head_sha`, feedback status
and dispositions, trace identity, and `subtraction_signal`. Use `triggered` with
source-specific subtraction evidence or minimality proof for the structural
additions above, otherwise use `none`. Keep the verdict partial when required
review work remains, even if structural validation succeeds. Report unavailable
runtime checks separately.

Give each actionable finding as a GitHub inline comment with the exact path and
line/hunk, trigger or concrete maintenance cost, impact and smallest fix. Identify
regressions, policy violations, design debt and inherited risk accurately. With no
findings, say so and name material gaps. Show internal audit rows only on request.

## Task completion learning

Before your final task response, FIRST call `knowledge_agreement(action="status")`.
Only `status="accepted"` permits [task completion learning](../imlearn/references/task-completion.md)
to distill lessons, grade knowledge or upload feedback. Otherwise skip learning,
finish the requested task, and offer the actual agreement and human signing
command once without per-task reminders. Never sign for the user. Accepted
agreement and configured publication/write gates are both required; operator
configuration is not consent. This does not authorize publishing the task's code,
comments or RFC.
