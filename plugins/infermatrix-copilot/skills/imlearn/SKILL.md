---
name: imlearn
description: Distill reusable lessons from a completed local task and grade the project knowledge actually used, through InferMatrixCopilot. Use when the user invokes /imlearn or $imlearn, asks to learn from the current task, or another InferMatrixCopilot skill reaches its completion step.
---

# InferMatrix task learning

Use `$imlearn` in Codex or `/imlearn` in Claude Code and Cursor. FIRST call
`knowledge_agreement(action="status")`. Only `status="accepted"` permits
distillation, grading, uploads or extra learning analysis. Otherwise skip those
activities, offer the actual agreement and human signing command once, and
finish the requested task without repeated reminders. Never accept for the user.

After acceptance, work from the current host conversation and verified artifacts.
The host model extracts the lessons; no second model or raw transcript upload is
needed.

Follow [task completion learning](references/task-completion.md). Use it once
before the final task response, including a terminal handoff with an explicit
verification gap. For an on-demand invocation, reuse an existing task ID when
retrying the same evidence. Do not run it recursively after its own completion.

Knowledge publication requires both accepted human agreement and the configured
`local` or `draft_pr` mode with its push/post gates. Operator configuration is not
the user's signature. Report a compact outcome or contribution PR link; retain
blocked work for retry.
