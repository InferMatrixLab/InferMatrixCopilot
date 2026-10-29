# Learn from completed tasks

Optional task learning and knowledge feedback start disabled. Normal knowledge
reading remains available. Before doing extra learning work, installed skills
check `knowledge_agreement(action="status")`; only `status="accepted"` enables
distillation, grading or uploads. Otherwise the host finishes the requested task
and can offer the actual agreement and signing command once, without repeating
the reminder on every task.

After acceptance, the host uses its current conversation and verified results to
distill reusable positive and negative lessons. It can give `+1` or `-1` to
knowledge that actually influenced the task, with a reason and outcome evidence.
This uses the host's current model without a separate extraction backend, but
still consumes extra tokens, execution time and possibly paid model/API quota.
Costs depend on the host, model and account; no free allowance is promised.

The accepted workflow is part of the `im*` skill contracts and MCP instructions.
It runs for a completed task or terminal handoff, including an honest validation
gap. It does not run for intermediate progress or status checks. Installation
uses the same skill files in Codex, Claude Code and Cursor; it does not assume a
host supports a completion hook. Unrelated chats that never activate a Copilot
workflow are outside this trigger's scope.

Run it on demand with `$imlearn` in Codex or `/imlearn` in Claude Code and Cursor.
The host selects bounded lesson summaries and relevant evidence; it does not
upload the raw transcript. No reusable lesson is also a valid result.

## Read, acknowledge and revoke

Configure the intended publication and collector destinations first, then inspect
the exact terms and configuration:

```sh
infermatrix-copilot learning agreement show
infermatrix-copilot learning agreement status
```

To enable the optional workflow, the person must run this command in an
interactive terminal, read the displayed terms and destinations, enter their
name, and type exactly `I AGREE`:

```sh
infermatrix-copilot learning agreement accept
```

Pipes and acceptance flags are refused. An agent must never run acceptance on
the person's behalf or manufacture their name, confirmation or saved record.
MCP exposes only `knowledge_agreement(action="status"|"show"|"revoke")`; the SDK's
`KnowledgeAgreementClient` exposes only `status()`, `document()` and `revoke()`.
Operator configuration and permission to publish task code are not this named
acknowledgement. The record is a product acknowledgement for the local operating
system user, not independently verified identity or a claim about legal effect.

Acceptance binds the disclosure version/hash and the displayed publication mode,
repository/base and collector URL. Changed terms or destinations produce
`renewal_required`; a fresh human acknowledgement is required. Missing, revoked
or unavailable acceptance keeps learning and feedback disabled. Checking status
or showing the document does not sign or create an acceptance record.

```sh
infermatrix-copilot learning agreement revoke
```

Revocation prevents future learning, grading and uploads after the next gate
check. An operation already in flight may complete. Revocation does not delete
stored local lessons or ratings, retract a PR, or remove data already received
by a collector. Use the relevant local-storage, GitHub or collector administration
process to remove earlier data. Task status and explicit local feedback reports
remain readable.

## Privacy and storage

Lessons, reasons, evidence references and task identifiers can contain personal,
proprietary or confidential information. The host selects bounded summaries and
evidence rather than complete transcripts or command logs. Automated checks reject
recognizable credential and chat-export forms but cannot guarantee complete
redaction; the person and host must select appropriate material.

Private local runtime state retains selected lessons, evidence, delivery metadata,
ratings, queues and the named acknowledgement. If publication is enabled, rules
and their selected provenance reach the configured GitHub repository: publicly for
a public repository, or to authorized collaborators/operators for a private one.
If collection is enabled, its operators and authorized readers can access the
uploaded feedback. Review each destination's access and retention policies before
accepting. Credentials are deployment configuration, not consent or feedback data.

## Configure a knowledge workspace

Set configuration on the machine that runs the MCP server or CLI:

```dotenv
KNOWLEDGE_LEARNING_STATE_DIR=/path/to/private-learning-state
KNOWLEDGE_LEARNING_WORKSPACE=/path/to/dedicated/InferMatrixCopilot-checkout
KNOWLEDGE_LEARNING_REPOSITORY=JiusiServe/InferMatrixCopilot
KNOWLEDGE_LEARNING_BASE=main
KNOWLEDGE_LEARNING_PUBLICATION=local
```

Use a dedicated writable knowledge checkout. The task's target repository and
the knowledge contribution repository are separate identities. The service uses
the configured destination; task input does not select credentials or a remote
destination. Keep local state, paths and secrets outside committed knowledge.

`local` is the default for knowledge contributions. Feedback is also local unless
the separate team feedback destination below is configured. To authorize automatic
knowledge draft PRs after human acknowledgement, configure `draft_pr` and the
existing push/post gates:

```dotenv
KNOWLEDGE_LEARNING_PUBLICATION=draft_pr
ALLOW_PUSH=1
ALLOW_POST=1
```

Current human acknowledgement and that persistent configuration together permit
knowledge publication without a new approval for each task. Configuration alone
does not enable learning or uploads. It does not authorize publishing the task's code,
comments or RFC. Knowledge PRs are never merged by this completion step. A
validation, credential or publication blocker leaves the prepared work available
for inspection and retry using the same task ID.

## Learning and feedback contracts

The host calls `knowledge_learning` with `action="prepare"`, a stable `task_id`,
`repo`, frozen `source_pin`, and bounded `lessons`. Preparation returns the owner
catalog and proposal contract. The host drafts executable rules, then calls
`action="submit"` with the original `packet_digest` and `proposals`. Call
`action="status"` with the same task ID to inspect the result or reconcile a
retry. Existing owner rules are reused; empty or redundant lessons do not need
an empty PR.
Repository slices are checked at the frozen Git base before a nonempty task is
retained. Correct an invalid repository using the same task ID; the old invalid
preparation is recoverable when it has no proposals or application and its
worktree remains pristine. Unexpected workspace edits are preserved for inspection.
Slow publication of one task does not hold the shared learning database's write
lock or block unrelated task completion.

Each lesson has `outcome` (`positive` or `negative`), `summary`, a bounded
`evidence` reference list, and optional `title` and repository-relative
`changed_paths`. The source pin is a full lowercase Git SHA. Proposals have the
shape `{"rules": [...]}` and follow the returned owner-rule schema.

With accepted agreement, knowledge retrieval returns a `knowledge_feedback` receipt containing
`delivery_id`, `task_id` and exact `targets`. The host grades only targets it
actually used, preserving their identities, through
`knowledge_feedback(task_id, delivery_id, ratings)`. A positive grade requires
evidence that the advice helped; a negative grade requires a concrete harmful or
incorrect effect. Merely retrieved or untested knowledge gets no grade. Feedback
does not automatically change a rule's meaning or lifecycle.
Without agreement, retrieval still returns its source content and feedback
metadata reports `status="agreement_required"` with no targets or delivery ID;
there is nothing to grade.

Each rating supplies `target_id`, integer `score` (`-1` or `1`), `used: true`,
`reason`, and bounded `evidence`. Use the exact target and task identities from
the receipt. Pass the task ID to retrieval when supported to keep deliveries
from the same task together.
Use a globally unique task identity, such as the host chat UUID with a scoped
task suffix. Preserve it when moving between machines or retrying. Short local
counters such as `task-1` can collide in a shared team ledger. The collector
does not add a machine prefix because that would count the same task twice.

Prefer a rule target when its complete section was delivered. Use a document
target for page-level guidance; avoid rating the same effect at both levels.

SDK plans normalize repository aliases before issuing feedback identities.
For an explicit task ID, standalone reads reuse that task's prior repository
context when it is unambiguous. When reading a `general/...` page before any
plan, pass `DirectClient.read_document(..., task_id=..., repository="vllm-omni")`
or use the issued review context. Explicit repository/context is also required
when the task has multiple repository contexts. This preserves the same target
identity across plan and follow-up reads. Historical receipts and votes retain
their original immutable identities.

If adaptive feedback issuance is temporarily unavailable, source content still
returns with `feedback_storage_unavailable`. Retry the same session/request after
storage recovers: the service repairs the receipt for the originally delivered
fragments without consuming more context budget or including additional rules.

Lessons become owner-scoped rule proposals, with the existing validators and
publication checks. Local chat evidence stays in private task state; product
knowledge receives reusable rules and appropriate evidence references. Do not
store raw chats, credentials, unrelated personal details or command-log dumps in
the tree or PR. Feedback reports are available on demand through
`knowledge_feedback_report(repo="...", limit=100)`; normal task handoffs show a
compact outcome and a PR link or blocker.

## Collect team feedback

Run one collector for a shared team ledger. It uses the standard-library HTTP
server and durable SQLite state; no model backend or MCP dependency is required.
The service state must be on a persistent local disk outside the knowledge tree.
Use one service instance per state directory; horizontally distributed database
coordination is outside this service's contract.

Set `KNOWLEDGE_FEEDBACK_SERVER_TOKEN` to an operator-provisioned secret of at
least 32 characters in the collector process environment, then start it:

```sh
infermatrix-feedback --host 127.0.0.1 --port 8788 --state-dir /srv/copilot/feedback
```

`KNOWLEDGE_FEEDBACK_SERVER_STATE_DIR` can supply the state directory instead.
For remote clients, put the loopback listener behind an HTTPS reverse proxy that
preserves the Authorization header and `/v1/feedback/` routes. The listener
provides HTTP, not TLS. Provision the matching token only to trusted Copilot
provider runtimes; it authorizes both ingestion and aggregate reports. It is not
an end-user receipt signing key or a token to include in a tool call.

Configure each provider runtime (the MCP/CLI/SDK process, not its chat input):

```dotenv
KNOWLEDGE_FEEDBACK_URL=https://feedback.example.org
KNOWLEDGE_FEEDBACK_TOKEN=<operator-provisioned-secret>
KNOWLEDGE_FEEDBACK_TIMEOUT_SECONDS=5
```

HTTP is accepted only for loopback development, for example
`http://127.0.0.1:8788`. Client redirects are rejected rather than forwarding
credentials to a new destination. Keep the token in private runtime configuration.
The knowledge PR publication mode and push/post gates do not configure feedback
transport. This separate destination permits automatic upload only with a current
human acknowledgement that includes its actual URL. Changing the URL requires
renewal before uploading or reading the central report.

Recording a grade commits the local rating and an upload queue entry together,
then attempts upload after the transaction. Network, authentication or uncertain
response failures leave the grade durable and pending. A returned sync status
distinguishes local acceptance from a confirmed central acknowledgment. Retry
with `learning sync` or `knowledge_feedback_sync`; status inspection does not
make a network request. Existing local ratings can also be uploaded by a sync
after team collection and the corresponding agreement are current. Revoked or
missing agreement leaves queued grades unsent. No background daemon is installed: bounded
upload attempts run on recording and explicit sync.

The collector accepts the existing `knowledge-feedback/1` receipt, its exact
rating identities and a minimal agreement attestation. It validates receipt/target hashes, task and repository
bindings, and rated-target eligibility before counting a grade. Provider
authentication establishes issuance trust; hash checks alone do not prove that
a provider delivered the content or that an agent's observation is correct.
The attestation contains the protocol/purpose, disclosure version/hash,
configuration hash, acceptance ID and time. It excludes the person's name and
the full local acknowledgement. It is a claim from the authenticated trusted
provider, not independent proof of the human's identity or signature.

The shared ledger preserves one immutable vote per `(task_id, target_id)` across
all machines and deliveries. Exact retries succeed without increasing counts;
conflicting ratings are rejected. Rule content or upstream-pin changes produce
a different target version. Unrelated page edits can retain an unchanged rule's
identity, while preserving the original delivery provenance.

Only receipt metadata and selected ratings are uploaded: hashes, relative
knowledge paths, task identity, reason and outcome evidence. Full source pages,
chat history, local machine paths and credentials do not belong in feedback.
The transport rejects recognizable credentials and conversation-export forms;
the host must still select and redact other private information.

The HTTP surface is:

| Request | Purpose |
| --- | --- |
| `POST /v1/feedback/events` | Authenticated `{ "receipt": {...}, "ratings": [...], "agreement": {...} }` ingestion |
| `GET /v1/feedback/report?repo=vllm-omni&limit=100` | Authenticated exact-version aggregate |
| `GET /healthz` | Minimal health status without feedback data |

Aggregates use the same positive, negative, net, mean and distinct-task counts
as local reports. `scope="auto"` (the default) uses the central service when
configured, otherwise the local ledger. Select `scope="local"` or
`scope="central"` explicitly when needed. A central-service failure is an error;
it never silently substitutes local counts for a team report. Querying a report
does not upload pending grades.
Central requests require current agreement; `--scope local` remains available
without it and makes no collector request.

## CLI

The CLI delegates to the same services and accepts JSON files or stdin:

```sh
infermatrix-copilot learning prepare --input task.json
infermatrix-copilot learning submit --input proposals.json
infermatrix-copilot learning feedback --input feedback.json
infermatrix-copilot learning status TASK_ID
infermatrix-copilot learning report --repo vllm-omni --limit 100
infermatrix-copilot learning report --repo vllm-omni --scope central
infermatrix-copilot learning report --repo vllm-omni --scope local
infermatrix-copilot learning sync --limit 100
infermatrix-copilot learning sync-status
```

For prepare, the object has `task_id`, `repository` (or `repo`), `source_pin` and
`lessons`. Submit has `task_id`, `packet_digest` and `proposals`. Feedback has
`task_id`, `delivery_id` and `ratings`. Use the service's returned lesson,
proposal and rating schemas. `--input -` reads a JSON object from stdin; no
credential or transcript argument is accepted. Reuse the task ID, packet digest
and delivery identities when retrying.
