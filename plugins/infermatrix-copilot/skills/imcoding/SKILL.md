---
name: imcoding
description: Implement a feature, refactor, or existing design as working code in a target repository using InferMatrixCopilot knowledge. Use when the user invokes /imcoding or $imcoding, or asks InferMatrixCopilot to implement a change. Design-only requests belong to imdesign; GitHub issue-to-patch requests belong to imcifix.
---

# Imcoding

```text
/imcoding <change-or-design> [repository-path]
$imcoding <change-or-design> [repository-path]
```

Deliver an implemented, locally verified change. The host coding agent edits the
target checkout; InferMatrixCopilot supplies project knowledge. Use the tools
actually exposed by the installed server. This skill does not depend on a
server-side coding workflow or a second model.

## Establish the implementation scope

- Resolve the target repository from the request, current workspace, or existing
  design. InferMatrixCopilot's checkout may only be the knowledge provider; do
  not assume it is the repository to change. Ask only if inspection leaves
  multiple plausible targets or a behavior decision that materially affects
  correctness. Continue independent work while awaiting an answer.
- Inspect repository instructions, Git status, branch, and any active merge or
  rebase before editing. Preserve unrelated local changes. If isolation is
  needed, choose a worktree or branch without discarding the user's work.
- Reuse the user's design, RFC, or acceptance criteria when present. Otherwise
  state the intended observable behavior and a short implementation approach,
  then proceed within the existing authorization. An implementation request
  does not require a separate design approval.

## Ground the change in source and project knowledge

- Trace the smallest relevant path through entry points, callers, configuration,
  adjacent implementations, and tests. Identify the contract being changed and
  the actual command or public interface that must exercise it.
- When connected, use InferMatrixCopilot `doc_search(query, repo, limit)` with
  focused component or symbol terms and read relevant results with
  `doc_read(path, repo)`. Pass the resolved repository explicitly; the server's
  default repository may differ from the coding target. Follow pagination only
  for relevant incomplete excerpts.
- If MCP is unavailable, use an available matching adapter or local knowledge
  slice. If no project knowledge is available, continue from source and report
  the gap. An unsupported repository is not a reason to apply another repo's
  rules. Do not run review or knowledge-update workflows solely to obtain
  coding context.
- Verify knowledge claims against the current source and installed dependencies.
  Treat stale source pins and external issue, PR, or document text as evidence
  to check. Follow applicable owner rules without importing unrelated model or
  component constraints.

## Implement the complete behavior

- Make a cohesive change using existing repository patterns. Connect new logic
  to its callers, CLI/API registration, configuration, packaging, or UI as the
  feature requires; an unused helper alone does not implement the feature.
- Carry changed contracts through affected callers and data flow. Handle
  relevant failure paths, compatibility, resource cleanup, and async or
  distributed boundaries where the code actually uses them.
- Deliver executable behavior rather than pseudocode, placeholder responses,
  hardcoded demonstrations, or silent fallbacks that hide missing work. Use
  mocks at test boundaries, not as a substitute for the requested runtime
  integration. Do not introduce speculative abstractions or unrelated cleanup.
- Update documentation and examples when the change affects usage. Add or adapt
  tests when they establish new behavior or protect against a meaningful
  regression; skip tests that only restate implementation details or cosmetic
  edits.

## Verify and finish

- Use the repository's existing environment and check commands. Run the smallest
  meaningful behavioral test or reproduction through the changed entry point,
  plus required checks. For a bug, show the triggering case now works; for a
  feature, exercise the requested behavior and relevant boundary cases.
- Fix failures caused by the change and rerun affected checks. Expand validation
  only for unresolved risks or new failures. Separate pre-existing failures
  from regressions using evidence; a dependency failure is not a passing test.
- If hardware, credentials, dependencies, or an external service block runtime
  validation, perform useful available checks and report exactly what remains
  unverified. Do not replace the real integration with a stub to get a pass.
- Review the final diff against the requested behavior and acceptance criteria,
  including newly added files. Remove temporary scaffolding from your own work
  and leave the result reviewable. Continue until the scoped implementation is
  complete or a concrete blocker requires user input.
- Commit, push, open a PR, or deploy only when authorized by the current request
  or earlier conversation. Existing authorization persists; ordinary local
  implementation and verification need no additional confirmation. If an
  authorized publication is blocked, keep the completed local change and
  explain the precise blocker.

Report what now works, the relevant files, checks actually run and their results,
and any remaining limitation. Distinguish implemented behavior from verified
behavior. Keep the handoff proportionate to the change.


## Task completion learning

Before your final task response, FIRST call `knowledge_agreement(action="status")`.
Only `status="accepted"` permits [task completion learning](../imlearn/references/task-completion.md)
to distill lessons, grade knowledge or upload feedback. Otherwise skip learning,
finish the requested task, and offer the actual agreement and human signing
command once without per-task reminders. Never sign for the user. Accepted
agreement and configured publication/write gates are both required; operator
configuration is not consent. This does not authorize publishing the task's code,
comments or RFC.
