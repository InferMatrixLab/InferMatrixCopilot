# knowledge_trace/ — Knowledge-use trace, issue #442

<!-- verified-against: 2026-11-04 -->

Issue: https://github.com/InferMatrixLab/InferMatrixCopilot/issues/442

## Problem and outcome

Before this change, the Direct router returned owner pages, selection reasons, quick maps,
related prose, and a knowledge snapshot. Strict records selected pages and injected
skills. TraceStore records calls, decisions, and outcomes. These pieces do not
provide a unified account of **step → knowledge → check → evidence → decision**.

Provide a durable, queryable record for Direct and Strict and a deterministic
step-by-step report. It must answer which entries were selected, what content was
actually provided, why a rule was applicable, which evidence was checked, and what
decision followed. This records concise, reviewable justifications and observable
evidence, not private model chain-of-thought. A finding need not cite knowledge;
an assessed rule need not produce a finding.

## Existing owners

- `direct_routing.py`: repository-neutral bounded owner/model selection.
- `sdk/v1/direct.py`: review context, source head, resource revision, document
  excerpts, hashes, and context-bound reads.
- `thin_mcp_server.py`: Direct/Strict review, document reads, and completion tools.
- `engine/agent_runtime/`: Strict briefings, skills, knowledge tools, and dispatch.
- `trace_store.py`: redacted JSONL, content-addressed blobs, derived SQLite index.
- `run_trace.py`: per-run factual timeline; it can link to the knowledge trace.

The final serialized SDK response includes `DocumentRef.excerpt`, not just quick
maps. Capture all content actually returned. Capture Strict content after prompt
assembly and clipping, rather than equating retrieval with model input.

## Semantics

| State | Meaning | Observer |
|---|---|---|
| retrieved | Routing/search/briefing selected an entry | Copilot |
| provided | Exact content crossed a named output/input boundary | Boundary owner |
| assessed/applied | Agent explicitly connected a rule to a check or decision | Agent declaration, references validated |

Provided boundaries are `provider_response`, `host_context`, and `model_request`.
Returning content does not prove the host supplied it to a model. Supplying it
does not prove model attention or application. A quick map mentioning a rule ID
does not prove delivery of that rule's body. Unreported application stays unknown.

Assessment has independent dimensions:

- `applicability`: `applicable`, `not_applicable`, `unknown`.
- `verification`: `supported`, `contradicted`, `unresolved`.

An applicable rule with unavailable hardware is unresolved. A rule contradicted
by current source keeps the conflicting evidence. No assessment means
`not_assessed`, not `not_applicable`. Rules can be provided and assessed repeatedly
across steps; this is not a single linear state machine.

## Architecture and ownership

One `knowledge_trace/` package owns event semantics, identifiers, validation,
recording, query, and reporting. Direct and Strict adapters submit observations;
they do not implement separate schemas. Reuse TraceStore and its redaction and
blobs. JSONL and blobs are the source; SQLite and Markdown are derived views.
There is no additional service, message queue, model call, or retrieval pass.

Package ownership and public contract:

- `references.py`: knowledge identities, scoped document paths, content hashes,
  explicit rule-heading sections and conservative source ranges.
- `recorder.py`: `KnowledgeTrace.start_session`, `retrieve`, `provide`,
  `record_events`, `finish_session`, `report` and `query`; protocol validation,
  append ordering and writer serialization.
- `report.py`: deterministic event projection and `markdown(report)`; findings
  retain assessment/evidence links without inferring causation.
- `adaptive.py`: `packet_fragments(packet)` extracts exact delivered fragments
  from already metered provider packets; no source reads or permission changes.
- `cli.py`: model-free show/export/query commands, including atomic file export.
- `__init__.py`: public `KnowledgeTrace`, `KnowledgeTraceError`,
  `extract_rule_sections` and `markdown` exports.

Dependencies are stdlib plus the shared TraceStore owner. The package does not
import SDK, transports, planner or model runtime; those owners call downward
into this protocol. Existing router behavior and knowledge read budgets remain
intact.

## Data model

### Session

`trace_id`, `run_id`/`review_id`/`review_context_id`, canonical repository, mode,
source head or workspace fingerprint (or explicit unknown), knowledge snapshot,
resource revision, instrumentation capabilities, and lifecycle status.

`review_context_id` binds inputs and versions; `trace_id` identifies an execution
attempt. Within a DirectClient, repeating an identical plan request reuses its
trace, including after completion. Use a new `review_id` for a new attempt.
Strict creates a new trace for each execution/resume attempt and retains the run
ID for correlation. A trace ID is not an authorization token.

### KnowledgeRef

`knowledge_ref_id`, namespace (`general` or repository), document-relative path,
optional rule ID/section anchor, snapshot, document hash, and optional section
hash. Identity includes namespace, path, and version, not only `DIFF-1b`. Pages
without IDs remain page/section references; never infer a rule ID from prose.

### Exposure

`exposure_id`, knowledge reference, step, boundary, representation (search hit,
quick map, excerpt, full section), payload blob, included ranges, explicit offset
unit (`utf8_bytes` or `unicode_codepoints`), truncation, and delivery status.

Offsets refer to served visible text, not original disk lines after retired-rule
filtering. Unknown or disjoint ranges are empty with `range_status: unknown`, not
fabricated zero offsets. UTF-8 byte slices preserve their original source length
even when decoding introduces replacement characters. Keep document identity and
served-content hashes distinct from the redacted archive blob hash. Only explicit
rule headings attest rule-body delivery.

### EvidenceRef

Evidence ID, origin (`captured` or `host_reported`), kind (source/test/tool), source
head or workspace identity, relative path/range or command, output reference,
execution environment where available, and verification availability. References
from another source head are stale, not silently applicable.

### Assessment

Assessment ID, step, knowledge reference, exposure IDs, applicability,
verification, short rationale, concrete check, evidence IDs, decision/finding
IDs, reporter, and reference-validation result. Missing evidence is explicit.
Mechanical reference validity does not prove semantic correctness.

```json
{
  "assessment_id": "a-17",
  "step_id": "review.defaults",
  "knowledge_ref_id": "k-42",
  "exposure_ids": ["x-12"],
  "applicability": "applicable",
  "verification": "supported",
  "rationale": "The parameter permits zero; inspect default-value handling.",
  "check": "Verify guidance=0 reaches its consumer.",
  "evidence_ids": ["source-8", "test-3"],
  "decision_ids": ["finding-2"],
  "reported_by": "host"
}
```

## Event protocol and persistence

Add `knowledge_event` to the existing `trace/1` kind set. Its `result` contains
`schema: knowledge-use/1`, event type, trace/event IDs, producer ID, producer
sequence, parent IDs, and the event-specific payload. Preserve existing query
columns (`run_id`, `step`, `repo`, `rule_ids`); no initial index migration.

Event types:

- `session_started` / `session_finished`.
- `retrieval_completed`: mechanism, query, candidates, reasons, ranks, no hits.
- `content_provided`: actual content, representation, range, named boundary.
- `evidence_registered`.
- `assessment_recorded`.
- `gap_recorded`: failed reads, version mismatch, unavailable observation.

Cross-process recording must serialize session updates. Event IDs are idempotent;
same ID and same content retries return the previous result, conflicting content
is rejected. Producer sequences order local events; parent links express causal
order across producers. Wall-clock timestamps are not proof of causality.
Assessment/gap corrections append a same-type `supersedes` link rather than
rewriting old events. Observations and evidence remain immutable and cannot be
superseded; corrected evidence gets a new identity and can support a replacement
assessment. This prevents host corrections from hiding provider observations or
leaving assessments with dangling evidence references.

Store redacted payloads before records. Indexes and reports can be rebuilt. Old
readers/exporters must tolerate the added kind; do not count knowledge events as
model calls, outcomes, or training examples by accident.

## Direct integration

1. Start a session bound to repository, review context, and source head.
2. Record route/search candidates, selection reasons, fallbacks, unmatched scope.
3. Record all knowledge in the final response: quick maps, related prose,
   mandatory-guide excerpts, maps, and SDK document excerpts.
4. Return trace metadata, knowledge references, and exposure references.
5. Host optionally acknowledges exact content inserted into its context.
6. Host submits evidence and assessments in batches per step or at completion.
7. Completion returns separate review and knowledge-trace status.

Without a host acknowledgement, report "provider returned; host delivery unknown".
Shell/native file reads can be declared with hashes and tool references, but
remain host-reported when Copilot cannot observe them.

Traced MCP reads/searches accept an optional trace/context. Reads use the pinned
knowledge view; an unavailable old context fails explicitly instead of falling
through to the current tree. Untraced legacy calls retain their behavior.

SDK additions are optional/defaulted, with a capability flag and protocol version.
`KnowledgeTraceClient` exposes start, record, finish, and report. MCP tools are
`start_knowledge_trace`, `record_knowledge_usage`, `finish_knowledge_trace`, and
`get_knowledge_trace`. Review can auto-start a session. Existing clients need not
report assessments.

## Adaptive context integration

The existing `KnowledgeContextService` owns frozen views, cumulative budgets,
repository authorization, consent, feedback receipts and containment. Tracing
observes its final packet without repeating retrieval or changing those gates.
`documents` are metadata references and cannot attest delivery. Only complete
provider-rendered `<untrusted_data>` JSON blocks in `model_content` yield
exposures; verify `model_content_sha256` when supplied and treat malformed or
unframed content as a capture failure.

Preserve fragment `repo_id`, `source_pins` and context session ID in retrieval
metadata. Record only the budgeted fragment body, with its partial/truncated
state. Continuations and transformed/search fragments retain unknown source
ranges rather than inventing offsets. Empty/no-new-context packets provide no
new content; referenced sibling documents do not become exposures.

The context service may authorize cross-repository dependencies. This trace
protocol remains scoped to `general/` and one selected repository. Record an
explicit cross-repository observation gap and let the already authorized
context operation retain its original behavior, including in required mode.
Do not silently copy that other repository's content into this trace or use a
trace ID as a new grant.

Direct records the provider-return boundary. Strict queues each exact packet
fragment, then confirms it survives the final prompt/tool-response assembly
before recording a model-request boundary. JSON escaping of a tool result must
not turn a delivered fragment into an unexplained missing observation.

## Strict integration

Capture briefing/skill/related selection, then final model inputs after clipping.
Instrument `doc_search`, `doc_read`, and `doc_related`; associate actual tool
results with the model request that receives them. Add optional
`knowledge_assessments` to the output contract, with stable check/finding refs.

API mode can observe model requests. Harness mode records the supplied prompt
boundary and Copilot tools; external CLI-native file reads remain unknown unless
the host reports them. Do not parse arbitrary terminal output to invent usage.
Concurrent steps share a run session and retain qualified step IDs.

## Reports, query, and CLI

Deterministically project events into session identity, instrumentation coverage,
ordered steps, retrievals, exposures, assessments, evidence, findings, and gaps.
Support bounded/paginated event reads and repository/rule/run queries.

```bash
infermatrix-copilot knowledge-trace show <trace-id>
infermatrix-copilot knowledge-trace export <trace-id> --format json --out trace.json
infermatrix-copilot knowledge-trace export <trace-id> --format markdown --out KNOWLEDGE_TRACE.md
infermatrix-copilot knowledge-trace query --repo vllm-omni --rule DIFF-1b
```

| Step | Entry | Selection reason | Provided content | Assessment/evidence |
|---|---|---|---|---|
| Parameter defaults | DIFF-1b | guidance signal | Full section in model request | Applicable; source and zero-value test |
| Parameter defaults | Supporting guide | Source-path match | Truncated excerpt | Not assessed |
| Output contract | None | Search had no hit | — | Explicit knowledge gap |

Counts describe observation, not correctness or causation. "12 rules provided,
4 explicitly assessed" is valid. "Knowledge caused four findings" is not proven.
Historical records are reconstructed only where evidence permits, with unknown
fields retained; no fabricated backfill.

## Validation, scope, and failure behavior

Core validates page/rule/version/exposure references, repository scope, source
head consistency, event identity, and parent/evidence links. Readable knowledge
is shared `general/` plus exactly one selected repository. Reports do not disclose
another repository's runtime evidence.

`KNOWLEDGE_TRACE_MODE=off|best_effort|required`:

- `off`: no trace I/O; legacy execution behavior.
- `best_effort` (default): storage failure is visible as degraded telemetry but
  does not stop the task.
- `required`: failed critical recording stops subsequent controlled work and
  refuses an audit-complete result; it does not pretend to undo previous actions.

Review completion and trace completion are independent. Missing observation is
partial/unknown, never implicit proof that knowledge was unused.

`KNOWLEDGE_TRACE_ROOT` defaults to the Copilot runtime state directory. Keep all
artifacts outside the product knowledge tree, use relative public paths, reuse
secret redaction, and never automatically upload traces or publish comments.
Retention follows trace management; cleaned payloads are explicitly unavailable.
Content-addressed blobs deduplicate content; repeated observations remain events.
Bound event sizes/counts and report explicit limits rather than silently dropping.

## Delivery plan

1. Core protocol, references, persistence, deterministic reports, and CLI.
2. Strict briefing, document tools, actual request capture, and artifacts.
3. Direct SDK/MCP sessions, pinned reads, host receipts, backwards compatibility.
4. Assessments, evidence/finding links, completion diagnostics, and integration tests.

## Validation plan

Test retrieval without delivery/application; quick-map ID versus rule body; SDK
excerpt delivery without a doc read; prompt clipping; partial/multi-page reads;
snapshot activation mid-session; same rule ID in different repos; retries,
concurrency, and restart; stale-head evidence; successful checks without findings;
harness visibility gaps; required/best-effort/off modes; interrupted runs; index
rebuild; redaction; traversal and cross-repo rejection; and CLI JSON/Markdown export.
Run adjacent SDK, MCP, Strict, TraceStore, reporting, and CLI compatibility tests.

## Tradeoffs

Parsing only existing logs is cheaper but cannot reconstruct absent rule-to-check
links. A separate telemetry service provides scale at unnecessary operational
cost. Extending existing TraceStore with explicit producer observations preserves
the current ownership and permits incremental adoption. Remaining limits are
model attention, semantic evidence sufficiency, and unobserved native host reads;
the report must state these rather than infer them.

## Implemented validation ownership

`test/test_knowledge_trace.py` covers the shared protocol, references, immutable
observations, producer ordering, idempotency, index recovery, scopes, redaction
and event/report semantics. `test/test_knowledge_trace_cli.py` covers pagination
and complete JSON/Markdown exports. `test/test_knowledge_trace_direct.py` covers
SDK/MCP provider boundaries, pinned reads and host declarations.
`test/test_knowledge_trace_strict.py` covers final prompt clipping, assessed
checks, evidence identity and mode failure behavior. Adaptive capture and
authorization/budget compatibility are covered by
`test/test_knowledge_trace_adaptive.py` and
`test/test_knowledge_trace_adaptive_strict.py`. Adjacent SDK, MCP, agreement,
feedback and containment tests remain part of integration validation.
