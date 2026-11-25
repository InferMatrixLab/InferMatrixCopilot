# Recording Direct review knowledge usage

Retain returned `knowledge_trace.trace_id`. Pass it, selected `repo` and a stable
`step_id` to `doc_read`/`doc_search`; pass `trace_id` and `repo` to
`validate_direct_review`.
Forward the plan's `knowledge_usage` containment receipt unchanged where required.
When knowledge-usage tools exist, batch `record_knowledge_usage` for actual
exposures and explicitly checked rules once per meaningful step, before
completion closes the trace. Use
`content_provided` with `source_exposure_id` and `boundary="host_context"`;
record source/test evidence at the frozen head and actual validation environment,
then assessments with provided document/rule identity, applicability,
verification, rationale, check and evidence IDs. Example batch:

```json
[
  {"event_type":"content_provided","step_id":"review.source","exposure":{"source_exposure_id":"<returned exposure ID>","boundary":"host_context"}},
  {"event_type":"evidence_registered","step_id":"review.source","evidence":{"evidence_id":"source-check-1","kind":"source","path":"<repository-relative source path>","source_revision":"<pinned head SHA>","summary":"<actual observed behavior>"}},
  {"event_type":"assessment_recorded","step_id":"review.source","assessment":{"document_id":"<provided knowledge path>","rule_id":"<explicit rule ID>","exposure_ids":["<returned exposure ID>"],"applicability":"applicable","verification":"unresolved","rationale":"<why the rule applies>","check":"<check actually performed>","evidence_ids":["source-check-1"]}}
]
```

Omit `rule_id` when none is explicit. Submit with trace/repo and a step-unique
idempotency key. Reuse evidence; do not read or assess extra knowledge for telemetry.
Missing evidence stays unresolved; quick maps do not prove full rules were read,
and unassessed knowledge alone does not prove a source-review gap.
A terminal partial/blocked attempt calls `finish_knowledge_trace` with
`status="interrupted"` after validation; leave it running if continuing.
Successful validation closes it.
