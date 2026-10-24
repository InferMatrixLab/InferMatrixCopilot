# Capability contracts and retained policies

This matrix maps the completion refactor to existing behavior fixtures. Source
paths are relative to Copilot `src/infermatrix_copilot`, except bot paths. A test
listed here is a coverage pointer, not a claim that the final full suite passed.

| Capability / callers | Canonical mechanism | Retained policy and persistence | Principal fixtures |
| --- | --- | --- | --- |
| Domain DTOs, SDK exports and workflow executor | `contracts.models`; application composition in `app.workflow_execution` | SDK exports preserve class identity and old serialized forms. The application injects domain policies; the executor owns registered-step execution. | `test_capability_ownership`, `test_step_registration_boundary`, `test_sdk_v1` |
| Copilot source intake and bot knowledge intake/evidence | `knowledge_service.evidence` (`parse_bugfix_record`, identity, changed paths and diff excerpts) | Hosts retain polling authorization, cursor/export recovery and delivery receipts. Callers pass their documented excerpt/path bounds. Bot imports only the public SDK exports. | `test_shared_knowledge_evidence`, bot `test_knowledge_intake`, `test_knowledge_distiller` |
| Knowledge application, reconciliation and snapshot validation | `knowledge_service.validation.validate_workspace` / `validate_snapshot`; packaged canonical tree/wiki validators | Snapshot export and workspace access differ; structural, lifecycle and semantic outcomes remain distinct. Wrappers preserve command and exit behavior. | `test_knowledge_validation`, `test_knowledge_wheel_resources`, `test_kb_reconcile_reviewed` |
| Git source/governed readers, pinned claims, discovery, reconciliation and review replay | `git_objects.tree_entries`, `batch_blobs`, `raw_changes`, `tree_texts` | Callers retain transport and mode/content policies. Pinned lookup alone tolerates malformed records; nonempty malformed replies cannot be cached as confirmed absence. Replay retains credential, size and symlink checks. | `test_git_objects`, `test_kb_pinned_observer_cache`, `test_feature_discovery_index`, `test_kb_reconcile_reviewed`, `test_replay_capture_pr_review` |
| CLI buffered and streamed provider calls, JSON sessions, ZCode | `process_execution.run_process` | Pipe/spool readers differ; ZCode retains anonymous regular files. Protocol parsing and model permissions stay in adapters. Explicit legacy injected runners are compatibility seams. | `test_provider_process`, `test_zcode_stream_spool`, `test_provider_codex`, `test_provider_cursor`, `test_provider_claude_code`, `test_provider_zcode`, `test_native_call_archive` |
| Shadow unit entry points and rebase shell/import tools | `process_execution.run_command` → `run_process` | Thin text/exception conversion preserves locale, universal newlines, raw timeout output, command environments, exit codes and tool output shapes. Domain admission and durable review worker lifetime remain separate policies. | `test_auxiliary_processes`, `test_improve_p4`, `test_engine_core`, `test_capability_ownership` |
| Copilot read-only JSON sessions and bot agent environments | `execution_environment.readonly_environment`, SDK export | Publisher identity is stripped at both boundaries. Subscription allowlists remain distinct policies and run before this filter if composed. | `test_execution_environment`, bot `test_codex_runner`, `test_worker_git` |
| Direct and Strict SDK subprocess hosts | bot `host_client.HostClient` | Direct/Strict error translation, admission and capability policy differ. Host generation invalidates prior handshake. Unknown replies do not trigger transport replay. Durable workers outlive the host. | bot `test_host_client`, `test_provider_run_recovery`, `test_strict_execution_binding`, `test_review_operational_capacity`, `test_e2e_provider_review` |
| Review planning, snippet anchors, coverage seeds and raw excerpts | `review.diff.parse_diff` and projections | Planning counts old/new rename paths; publication uses new-side lines. Missing whole files/hunks remain undetectable. Headerless excerpts inform planning but cannot prove anchors. | `test_parsed_diff`, `test_review_planner`, `test_review_anchor`, `test_publish_diff_inventory`, `test_agent_ensemble` |
| Direct decoding, feedback publication, standalone Strict publication | `review.dispositions`, `review.publication_policy`, `review.rechecks` | Wire normalization preserves legacy policy. GitHub normalization/safety, final head reads and receipts stay with hosts. Candidate dispositions and feedback dispositions have different schemas. | `test_review_publication_policy`, `test_sdk_v1`, `test_pr_steps`, bot `test_pipeline`, `test_publication_boundary`, `test_pr_findings`, `test_strict_backend` |
| Skeleton/rule-deepening screening (`init_stages._screen`) | `init_support.judge` → `gate.judge_block` → `quality_review.review_call` | Fail is dropped; unsure/unjudged remain advisory. Block dimension sets, evidence and page capacity are domain rules. Existing verdict records retain their fields; quality receipt is additive. | `test_kb_init_skeleton`, `test_kb_init_deepen`, `test_kb_intake_gate` |
| Module cards (`init_modules._review_card`) | Same block judgment path | Independent source review; one rejected card cannot halt other modules. Module record formatting remains an adapter. | `test_kb_init_modules`, `test_kb_module_source_evidence` |
| Foundation (`init_knowledge`, `init_knowledge_parallel`) | Shared block judgment, `quality_review.drain_review_tasks` and `DispatchJournal`; `drafting.AttemptPolicy` | Only pass is accepted. Same-owner/page/facet/version correction history, signed result validation and deterministic output ordering remain policies. Serial and parallel paths share the coordinator. Existing job state adds dispatch intents; unresolved owners cannot enter another correction. | `test_kb_init_knowledge`, `test_kb_foundation_parallel`, `test_kb_foundation_feedback`, `test_kb_foundation_publication`, `test_kb_foundation_local_privacy` |
| Facet depth (`depth_judge`, `init_knowledge_depth`) | `review_call`, `checkpoint_review`, `AttemptPolicy` | Strict requires a complete valid facet packet. Lightweight accepts independent valid facet decisions, charges logical rounds, and retains unknown outcomes. Coverage scans choose the next feature/evidence; they are application workflows, not another model dispatcher. Same-evidence and total-round ceilings remain distinct policies. | `test_kb_depth_evidence`, `test_kb_native_depth_approvals`, `test_kb_init_deepen`, `test_kb_depth_campaign_lightweight` |
| Feature discovery (`init_feature_discovery`) | `review_call`, `native_receipt`, `drain_review_tasks`, `DispatchJournal`, `drafting.run_attempts` | Candidate selection, relation validity, catalog boundary audits, evidence splitting, correction feedback and archive trust remain stage policy. Drafts are checkpointed before judgment follow-ups. Existing task/review/draft keys remain. | `test_kb_feature_discovery`, `test_kb_feature_discovery_stage`, `test_kb_feature_discovery_integration`, `test_kb_discovery_contract_units` |
| PR-history aggregate review (`init_history._review`) | `review_call`, `checkpoint_review` | Complete aggregate diff, exact base/head, evidence and owner routes; only approve permits publication. Existing approved records still reuse their exact identity. New aggregate intent/result state lives inside the existing history record. | `test_kb_init_history`, `test_quality_review_runner` |
| Maintenance source audit (`maintenance_audit.audit_unit`) | `quality_review.review_call`; domain `BudgetGateway` uses `reserved_call` and `MaintenanceStore.settle_cost` | Independent source assessments and verified/contradicted/unknown outcomes remain maintenance policy. The domain owns daily quotas/cache provenance; known overspend survives reply rejection and new caches retain native trace identity. Legacy caches replay without invented identity or a second paid dispatch. | `test_kb_maintenance_flow`, `test_kb_maintenance_store`, `test_quality_review_runner` |
| Curation/lifecycle repair and discovery extraction correction | `knowledge_service.drafting.run_attempts` | Public curation wrapper retains its 0..2 repair contract; discovery allows initial extraction plus three corrections. Different renderers and application contracts remain. | `test_sdk_knowledge_v1`, `test_knowledge_ops_l1`, `test_kb_feature_discovery`, `test_kb_intake_gate` |
| Native accounting and quality-call recovery | `providers.completion.complete_native`, `quality_review.review_call`, existing budget journals | Known pre-dispatch failures can use configured fallback. A dispatched call without a completed native response cannot silently select another paid backend. Init's fixed judge-account charge remains a convention; native receipts separately retain actual/unknown cost. | `test_kb_generator_fallback`, `test_native_call_archive`, `test_quality_review_runner`, `test_kb_unlimited_subscription` |

## Agent execution policy composition

`engine.agent_policy.AgentExecutionPolicy` carries per-step scope, metadata-write
and repo-map cache/read behavior without improvement dependencies. Application
composition selects `improve.execution.shadow_agent_policy`; the direct
improvement review worker injects the same policy. Ensemble calls retain the
context, and nested rebase contexts use dataclass replacement. Existing policy
semantics remain: shadow scope is read-only and fenced, skill usage is not
written, and repo-map caches stay in the run directory. The policy is not stored
in durable state. Source coverage is in `test_agent_runtime`,
`test_improve_p0b`, `test_workflow_execution`, `test_capability_ownership` and
`test_moa`; candidate-10's installed policy suite covers the runtime paths.

## Sandbox caller contracts and lifecycle requirements

The Docker executor, installed worker and explicit Codex completion profile are
implemented and have scoped tests; default workflow migration and authentication
are not certified. The inspected completion entry points converge on three direct
callers of `complete_native`; the separate read-only session API must not acquire
a second provider loop during this work.

| Entry point | Existing contract to preserve | Sandbox migration obligation |
| --- | --- | --- |
| `improve.judges.run_judge` | Gold-match and paired evaluation consume supplied records. The original Codex path used read-only execution and discarded verdicts containing tool activity; judge-call reservations remain domain-owned. | Preserve verdict rejection unless policy is explicitly changed. Containment prevents host access but does not make a tool-using verdict acceptable or blind. |
| `providers.harness_llm.HarnessLLM.create` | Shared completion for classification, reduction, output repair and other LLM-shaped callers. Explicit caller tools are rejected; model selection remains the configured harness model. | Keep the public completion contract. Do not infer permission for an agent loop from `tools=None` or globally relax repair/classification because another caller allows evidence reads. |
| `kb_service.models.ModelGateway._call_json` | Generation and quality judgment share dispatch, receipts and accounting; only eligible pre-dispatch generator failures may fall back. | Select policy with the trusted role/workflow configuration, preserving frozen evidence, output validation and unknown-outcome handling. A failed container connection does not prove the model never started. |
| `knowledge_service.curation.KnowledgeCurator.curate` | A disposable snapshot is passed to `run_readonly_json`; bounded correction and application validation remain canonical curation logic. | Mount the snapshot rather than the live workspace. Keep the existing source-unchanged check and repair semantics; do not move them into Docker code. |
| `sdk.v1.ReviewRuntime.run_session` | Shared bounded read-only classification/candidate session used by host callers. Durable reviews use reservation APIs separately. | Preserve schema, resume, deadlines and result shape. Resolve the execution profile before dispatch; do not reinterpret this as the no-tools completion contract. |

`process_execution.run_process` currently owns a local process group. A Docker
client process is not the container's process group. A container implementation
must persist a unique container identity before model dispatch, explicitly stop
and remove that container on cancellation/timeout, and verify termination.
Daemon disconnect or uncertain cleanup must retain that identity for
reconciliation; neither `--rm` nor killing the local client establishes that the
model stopped. Reuse shared supervision without introducing a separate vendor
execution loop. Cover creation/start races, lost acknowledgments, host crashes,
cleanup failure and once-per-call accounting in the lifecycle tests.

Environment evidence on 2026-10-13: UID 2005, no effective capabilities, user
namespaces disabled, no Docker/Podman/runc/containerd/nerdctl executable found,
and no conventional Docker or Podman system socket. No host settings changed.
Runtime validation requires a provisioned environment; the question identifying
that environment is pending. This evidence does not certify an alternative
sandbox or change the two unresolved Codex compatibility tests.

## Compatibility and deliberate fixes

Old SDK exports and old persisted rows remain covered by SDK and bot recovery
fixtures. No deployment pin or existing run location/profile was changed. New
quality receipts add policy/version/input identities without reinterpreting old
receipts. Tree checks, wiki lint, lifecycle validation, semantic review and
signed-snapshot containment remain distinct checks.

The completion pass intentionally fixes unbounded Strict host I/O, buffered
descendant cleanup, quoted Git path decoding, malformed/excess hunk anchoring,
standalone candidate-proof revalidation and fallback after unknown dispatch.
These are correctness/recovery fixes; no quality rubric was retuned. Stage
coverage loops and host business retry loops remain with their application
owners, using the canonical execution and validation mechanisms.


## Sandbox execution and profile admission follow-up (2026-10-14)

The earlier environment question is resolved by the user's Personal-Agent
selection. A separate private daemon and pinned worker validated local and SSH
execution, canonical output/cancellation and legacy observation. Production
hosts and original paid executions were not changed.

| Contract | Canonical owner / migrated callers | Current evidence / limits |
| --- | --- | --- |
| Backend independent of execution location | `sandbox.base`, backend registry, same local factory from SSH agent | Source/installed backend registration and actual local/SSH zero-model execution pass; flat Docker/profile wrappers delegate. |
| Native authentication control | `providers.base.auth_probe`, Codex status interpreter | Local/SSH native `login status` controls pass on exact admission wheel; no model invocation or USD inference. |
| Nonsecret context and original admission | `sandbox.context`, provider `subscription_admission`; ModelGateway/completion/agent delegate | Config/store/release/artifact/source/snapshot/expiry bind the next dispatch; deterministic positive and refusal paths pass. Unsupported identity stays unknown. |
| Original credential authority | `codex_credentials.login_snapshot`, ephemeral worker channel | Original cache unchanged; evidence scan and isolation pass. Native verification/refresh/revocation parity remain separate. |
| Trace and budget separation | `trace_store.auth_probe` and existing native accounting | Control journal failure cannot admit; model cost remains unknown. Domain quotas/journals retain authority. |

Reports are in `subscription-admission-20261014`, paired framework report in
`subscription-admission-framework-20261014`, artifacts in
`installed-subscription-admission-20261014`. These bind this candidate, not the
historical full-source report or final native/model-quality/release acceptance.
