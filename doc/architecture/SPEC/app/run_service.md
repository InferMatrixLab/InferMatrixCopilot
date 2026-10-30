# app/run_service.py —— 规范

<!-- verified-against: 2026-10-30 -->

`RunService` is the durable application boundary shared by the embedded
Strict SDK and MCP transport. It owns policy-checked reserve/start, a queue
drained by `STRICT_MAX_WORKERS` workers (default 1), isolated child launch, startup/orphan reconciliation,
readiness, bounded status/result polling, and repository-scoped knowledge
reads. Construction and polling require no CLI or MCP module import.

Knowledge reads resolve repository slices from the same frozen view's registry,
with legacy adapters retained as fallback. Search/read always pass the view's
manifest verifier. Child launch forwards the host-only
`ALLOWED_KNOWLEDGE_REPOSITORIES`; tool input cannot grant cross-repository access.
Strict adaptive document tools share the run's `KnowledgeContextService` ledger;
legacy standalone document endpoints retain their per-call read windows.

Reservation and run ID containment go directly through `RunReservation`,
not the workflow `Copilot` facade. `Copilot` remains for playbook readiness
and workflow execution; it does not mediate the service's reserve or poll
paths.

Reservations return `(run_id, created)` and enqueue only newly created runs.
The worker launches an isolated subprocess with posting and pushing disabled,
then reconciles the persisted status. A run can survive host restart; closing
a service only removes its liveness token and must not cancel a durable child.
The child rechecks repository/path/read-only policy against the same explicit
roots as the parent. Polling an unknown valid run ID reports `unknown`;
malformed or escaping IDs fail validation. Structured results and capped
report pages remain available after terminal status.

`mcp_server.CopilotMCP` remains a compatibility alias. The MCP server owns
tool registration and protocol error projection, while the SDK owns typed
request/result projection. Neither transport owns the run queue.

## 2026-09-28 Knowledge snapshot pinning
When `reserve_strict_review` / `reserve_quality_review` CREATE a reservation,
they record the current `KnowledgeView` in `<run>/knowledge.json`: snapshot id,
manifest tree hash, and resolved real paths (never the `active` symlink). An
idempotent retry does not rewrite it. `_launch` sets the child's
`KNOWLEDGE_DIR` and `KNOWLEDGE_ROOT` from that record, so a run that executes
later (queued, or relaunched after a restart) still reviews with the knowledge
active at reservation. If the pinned snapshot was pruned, the run is marked
`failed`; it never silently switches knowledge. Without a snapshot root the
run is pinned to this server's effective `knowledge_dir` (reported as `packaged`,
or `unverified` for a custom directory), with `KNOWLEDGE_ROOT` removed from the
child's environment. Runs reserved before pinning
existed have no record and use the process default.
Containment repository resolution and the issued scope receipt use this same
effective view, including a custom `knowledge_dir`; packaged bytes cannot stand
in for the custom bytes that the child consumes.

## 2026-09-30 Concurrent workers
`Settings.strict_max_workers` (`STRICT_MAX_WORKERS`, 1–32, default 1; embedded
hosts set it through `StrictRuntimeConfig.max_workers`) starts that many worker
threads, and `capabilities()` reports the same number as `max_strict_workers`.
Each worker launches one child at a time, so at most N children run at once.

`RunQueue` never hands out two runs with the same **conflict key** at once:
the checkout (resolved through the same `RepositoryContextResolver` execution
uses: frozen path, `REPO_PATHS`, then the adapter manifest) plus the PR number (`None` for issue tasks, which work in
the live checkout). Runs on one PR head share one PR-time worktree, and harness
sessions write their tool-bridge config into their working directory (cursor's
`.cursor/mcp.json` has one fixed path per tree), so overlapping them would bind
one run's agent to the other run's tool scope and trace. A run whose key is busy
stays queued in order without holding a worker; workers take the oldest run
whose key is free, so one busy PR cannot stall other PRs. The key is computed
from the persisted `request.json` at enqueue; an unreadable request shares the
key `("?", None)`. A failed launch marks the run `failed` and always frees its
key. `_reap()` is single-flight per service (a non-blocking lock): a worker that
finishes while another is sweeping skips its sweep.

The key cannot see a runtime fallback: a PR run whose worktree could not be
materialized degrades to the live checkout, where issue tasks and other
degraded runs also work. That shared directory is guarded where the hazard
is — the cursor transport serializes bridge-carrying sessions in a shared
checkout with an `flock` in its git dir (`providers/cursor.md`), across
services and the CLI as well. Not covered: two agent sessions *inside one run*
(ensemble lenses, per-comment verification, bounded by
`strict_backend_concurrency`) still share their run's own tree; that predates
this change and is independent of the worker count.


## 2026-10-08 Signed containment

When containment is explicitly enabled, reservation issues a private provider receipt for the pinned repository/shared snapshot scope into `knowledge.json`. It identifies conservative publication dependencies; it does not claim every scoped unit was injected. Launch rechecks the receipt and forwards only explicit maintenance configuration to the child. Old reservations without issuance fail closed until reassessed.

## Managed release launches (2026-10-30)

`_launch` selects the interpreter from the reservation's `release.json` and
forwards its runtime identity. It preserves all explicit child settings, scope
receipts, knowledge pins and platform process-isolation behavior. A missing
pinned runtime fails the run; activation never rewrites queued or live workers.
Existing runs without a pin retain their legacy interpreter selection.
