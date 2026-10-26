# Capability ownership audit

This records source paths inspected in the paired working trees on 2026-10-12.
Paths below are relative to each package's source root. It complements the
[contract matrix](single-capability-contract-matrix.md), function-level JSON
ledgers in the completion output directory, and full paired test results.
It does not establish model-quality parity or authorize rollout.

## Host policy compilation and dispatch/recovery have one path (2026-10-14)

`providers.codex_policy` owns the native catalog/tool/effort rules; the profile
factory selects and freezes it. `sandbox_profile_for_config` owns model/auth/
control/read composition; duplicated location branches are removed. The existing
Codex command/result methods delegate policy arguments and frozen inputs to the
same backend. Control facts retain native nulls, active layer precedence and
requirements provenance. `restore_completion_transport` reads original proofs
and worker results, with dispatch explicitly disabled. SDK/CLI/MCP/model callers
retain their canonical entry points and domain account authority. Current
installed and actual native local/SSH coverage is bound to the 714 wheel in
`provider-policy-binding-20261014/requirements-fixed`. Managed/account/other-
backend acceptance and global capability certification remain separate gates.

## Native configuration control belongs to the existing adapter (2026-10-14)

`CodexTransport.configuration_probe/get_configuration_result` own native
protocol requests and sanitized facts; `HarnessTransport.version_probe` owns
the shared version diagnostic. The neutral supervisor/input channel and generic
sandbox retain execution and recovery ownership. The worker treats the control
as an opaque host-selected command; it contains no Codex-specific branch.
Installed and actual fixed-wheel Docker/SSH controls verify this path, original
receipt recovery, nullable-state preservation and a separate positive startup
witness. Application policy-to-paid-dispatch integration remains open; the new
control alone is not native tool/account/model certification. See
`native-policy-composition-20261014` for the patch, retained failures and proof.

## Frozen sandbox inputs have one owner (2026-10-14)

`sandbox.inputs` owns path/capacity constraints, immutable byte staging and
projected-input verification. Container envelope/worker and SSH request/agent
call that canonical implementation. Backends own physical read-only mounts;
original request bindings own recovery identity. Provider tool/catalog policies
and credentials remain outside this neutral module. There is no previous input
body to delete; this closes a missing framework connection. Default old wire
formats remain unchanged. Source ownership guards, installed contracts and real
Docker/SSH conformance pass against the exact ae1 artifact; details and review
patch are in `sandbox-frozen-inputs-20261014`. Native-control certification and
the remaining full-plan acceptance gates are still open.

## Native tool-policy scope verified on pinned Codex (2026-10-14)

Zero-vendor-call namespace controls now exercise the actual Personal-Agent
Codex 0.162.1 binary, with isolated filesystem/network, synthetic configuration
and a loopback response fixture. Model/inference metadata is preserved; only
candidate tool-selection fields differ. Clean configuration exposes no tools,
and the native router rejects a forced `exec_command` without its side effect.
This is infrastructure evidence, not model-quality or authentication parity.

Managed MCP remains a real exception: the candidate launches an inherited
managed server and exposes two resource tools. Setting that server's CLI
`enabled=false` works unless management explicitly sets `enabled=true`; that
setting wins. A sealed configuration directory suppresses both startup and tool
exposure. The 0.162.1 tagged loader source independently confirms that legacy
managed layers append after session flags. The earlier 0.159.2 investigation
must not be treated as current-version certification.

Native app-server `initialize` + `config/read` controls return the effective
managed server configuration without starting that server or a model thread.
The shared supervisor/input channel owns this control lifecycle; it is stopped
after its response, so its -9/absolute outcome is deliberate control cleanup,
not an unknown paid model result. Production integration is still open. The
next adapter slice must bind frozen catalog/configuration inputs and the
requested policy through the generic sandbox interface, preserve account/model
behavior, and reject unverified managed/cloud scopes before model dispatch.
It must not add a second vendor launcher or claim global `builtin_tools_off`.

Evidence, exact binary/source/script/report hashes and retained fresh runtimes:
`native-tool-policy-closure-20261014/evidence.json`. All probe/copy handles are
terminal; original paid/shadow runs, native login cache and retained environments
are untouched. Vendor model calls are zero; production provider code is unchanged.

## Current candidate: explicit command lifetime policy (2026-10-14)

The last known command-lifetime compatibility difference is fixed in the same
neutral process supervisor. Legacy text commands release background children
only after leader exit and stdout/stderr EOF. Inherited pipes remain subject to
the original deadline; timeout and cancellation still terminate owned work.
Provider, test, broker and external-resource lifetimes retain owned cleanup.
The observation thread has one stop operation; no second process/repair loop was
introduced. Registered tools, schemas, output shapes, locale/newline/raw-timeout
behavior and domain acceptance policies remain unchanged.

The isolated correction was built and exercised before applying it. It is now
applied to the working tree, and every production Python file and runtime
resource matches that tested snapshot. Current artifact location retains the
name `background-policy-preview-20261014`; `applied-preview-source-bridge.json`
records its subsequent working-tree binding. Copilot wheel SHA-256:
`737e66326be4df252b186b79167f897cd8941ae9e78ed5b76f4fddd86e72e7db`; ReviewBot:
`9399980b86403b3d83682ba06bce56da9512145ba1a13b127b9584e4f520e23b`.

Applied source caller/compatibility/cleanup checks, isolated installed caller
checks, installed Direct/Strict SDK and publication-preparation main flows, SDK
smoke and post-test runtime/resource integrity pass. The exact wheel also passed
Personal-Agent namespace, worker and fixture-broker checks with zero vendor
model calls. Fresh paired source framework/boundary validation passed with unchanged source
and test inputs, no failures/errors/skips. Packaged resource/CLI cases and final
post-resource integrity passed on this same artifact. All handles are terminal.

The prior 3e8 candidate's full source suites terminated with no failures/errors,
unchanged source/test inputs and three Copilot environment skips (none in Bot).
Its raw source verifier remains false; exact-wheel resource/native coverage
remains separately recorded. All of that candidate's installed groups and
post-test frozen-source integrity passed. Source advanced only after its full
source suite ended; the remaining installed checks used immutable copies and
installed wheels, never the advanced worktree. Historical reports retain their
original artifact identities. These full-suite results do not transfer to the
737 artifact; current selected checks and final full integration are distinct.

Review patch, base hashes, source/artifact bridge and scoped evidence:
`background-command-policy-cutover-20261014`. Two production Python files change
by +23 lines / +155 AST nodes; no new production files and no Bot changes.
The regression tests also exercise all six migrated command entry points.
Framework and installed check selectors include them for subsequent runs.
Native parity, remaining deep recovery/accounting, the independently admitted
frozen quality comparison and immutable release/retention remain full-scope
gates. No original paid/shadow execution, production pin, commit, push, release
or deployment was changed. Counts are not a release gate.

## External rebase dependency retirement (2026-10-14)

The user retired the external rebase agent. Internal execution already used
Copilot's engine; this slice removes the shipped adapter's external debug-store
and skills declarations, automatic sibling checkout discovery, EXT1 discovery
and integration suite, and live parent-template comparison. `CheckoutLock`,
bundled prompt goldens and internal workflow checks remain. The deprecated
`REBASE_AGENT_ROOT` accepts explicit paths for existing custom data imports and
defaults to `None`; it cannot add a dependency to the shipped adapter. Read-only
legacy import/schema tests remain and no external model executor is retained.

This slice changes three production Python files by zero net lines and +30 AST
nodes (one settings validator; two documentation updates), plus the adapter and
tests. Relative to the preserved pre-completion working-tree archives, current
Copilot production changes +27 files / +5,261 lines / +39,683 AST nodes;
ReviewBot +1 file / +14 lines / +352 AST nodes. Relative to the original upstream
commits, Copilot changes +60 files / +8,327 lines / +59,874 AST nodes; ReviewBot
zero files / −1,420 lines / −3,345 AST nodes. Baselines differ because the
completion archive already includes the staged framework refactor. Counts do
not replace semantic ownership and behavior verification. Both measurements and
the incremental review patch are under `external-rebase-retirement-20261014`.

The same read-only audit found two remaining framework gaps: live rebase prompt
references to missing memory/kill scripts, and `testing.runner`'s independent
setup/test process supervision. They are not covered by the earlier provider-only
AST guard or an exact-body duplicate scan. Their inspected source hashes and
lifecycle sites are in `remaining-framework-audit.json`; the completion plan
specifies the migration and deletion work. Until those callers delegate, process
supervision cannot be claimed to have a single implementation across all callers.

The subsequent `internal-rebase-process-cutover-20261014` slice migrates these
callers. The canonical body is now in neutral `process_execution`, with shared
PID/start-time discovery and termination in `process_tree`. Provider and testing
legacy modules alias those bodies. Testing setup/test execution and improvement,
worker and SSH callers use the neutral mechanism; independent testing timers,
snapshot/wait/kill loops are deleted. Root ancestry, immutable leader birth,
reused-PID rejection, detached descendants and the bounded discovery window
remain. Procfs inspection replaces subprocess inspection on Linux. Stopping is
signalled before external cleanup so a blocked cleanup cannot admit late broker
work. Combined log bytes and separate setup/descendant grace policies are explicit.

The live prompt now uses registered memory/test tool examples validated against
the shipped schemas. Historical templates and golden request bytes are unchanged.
Ownership guards verify module identity, neutral dependencies and deletion of
test-runner lifecycle primitives. Existing test/cleanup, provider, accounting,
completion and sandbox caller suites pass. Paired framework, installed groups,
post-test integrity and exact-wheel namespace/broker evidence pass. Both full
source suites have no failures/errors; three Copilot environment skips retain
raw status with exact-wheel coverage. All validation handles are terminal. This slice changes eight production
Python files (two new) by +20 lines / +444 AST nodes. Counts are not release gates.

## Historical candidate: auxiliary command caller cutover (2026-10-14)
Current follow-up: the explicit background command observation reproduces a
legacy compatibility difference: successful commands with redirected children
must retain those children, and inherited pipes must drain under the command
deadline after their parent exits. This is not covered by the foreground pass.
`background-policy-proposal/proposal.patch` adds a named command exit policy to
the same supervisor; provider/test owned lifetimes remain unchanged. The isolated
proposal passes its regression/control cases. It remains unapplied while the
current full-source and installed checks finish; no installed or release claim
is made for that patch. Apply it after verifying the retained base hashes.


Shadow unit entry points, rebase shell/import tools, generic shell/search tools
and wheel-install commands delegate to neutral `run_command` → `run_process`.
Their independent invocation bodies are deleted. Chat repository search also
delegates to the tool search owner; literal/regex policy, VCS/vendor exclusions,
output bounds and diagnostic return/raise behavior remain explicit. The shared
text adapter preserves native shell selection, locale/UTF-8 policy, universal
newlines, raw timeout bytes and nonzero status. Actual caller tests verify
recorded detached-child cleanup without vendor/model calls.

Source caller/contract/boundary checks pass. Final source, installed and framework
validation is running against frozen inputs in `*-auxiliary-process-final-20261014`.
The initially started broader checks were stopped when additional callers were
found; their incomplete artifacts are retained and never counted as passing.
Incremental patch, before hashes, remaining host-I/O inventory and reasons:
`auxiliary-process-cutover-20261014`. Production delta: seven Python files,
zero new files, +18 lines / +85 AST nodes; ReviewBot unchanged. The testing-runner
change removes only an extra EOF blank line. Counts are not a release gate.

Current Copilot wheel SHA-256: `3e8d9fe4b46cd80ab141fcafd5d2979ba44297d6364f5a8367ec7f0e655896e6`.
ReviewBot SHA-256: `9399980b86403b3d83682ba06bce56da9512145ba1a13b127b9584e4f520e23b`.
Prior validation remains bound to its original candidate. Native control fixtures
have a zero-vendor-call request and run in a new retained environment. No original
paid/shadow run, production pin, commit, push, release or deployment is changed.

## Neutral contracts and application composition

`contracts.models` owns shared DTO definitions. `sdk.v1.models` re-exports those
definitions, preserving class identity rather than recreating types. Knowledge
validation imports its result DTO from the neutral module. The SDK cold-import,
old wire/state and import-boundary fixtures pass in the current source suites;
legacy wire fixtures also pass from installed wheels.

`app.workflow_execution` composes execution policy and supplies it to
`engine.executor`; improvement authorization/accounting lives in
`improve.execution`. Cold imports of the executor do not load improvement or
knowledge domains. Step effects load when the registry is assembled, not merely
when its facade is imported. The executor's trace/1 output projection retains
legacy artifact field names, but makes no knowledge/review acceptance decision.

`source_fingerprint.source_tree_hash` owns executable-tree hashing. Tracing calls
it directly; `improve.artifacts.tree_hash` delegates while retaining its public
error type. The old domain-owned hashing body was removed. The neutral owner is
protected from candidate mutation and included in the init-policy fingerprint.

`kb_service.quality_review.review_call` binds policy, frozen-input and dispatch
identities into trace context before calling the gateway. Existing native archive
and trace writers persist those identities; the final quality receipt shares
them. Crash tests prove the binding survives process loss and the original
checkpoint refuses replacement dispatch. This does not yet implement outcome
reconciliation. Applied verification: 93 tests in
`prepared-boundary-and-recovery/applied-tests.xml`; candidate-14 wheels predate
these changes.

## Provider mechanisms and host integration

`providers.registry` owns descriptors. Buffered and streamed CLI wrappers and
ZCode spooling use `providers.process.run_process`. DeepSeek's SDK now runs in a
worker under that supervisor; its independent Timer was removed. Readiness and
authentication probes use `HarnessTransport.auth_probe`. Native version probes
use the public SDK's `provider_version`, which delegates to
`HarnessTransport.version_probe`; both use one bounded `_probe` mechanism.
ReviewBot's `doctor` delegates its vendor diagnostic to this SDK capability;
its host-local `_version` remains only for Git. Provider version checks preserve
the configured command prefix, output precedence, deadline and credential
filtering. Applied-source verification passed 19 provider diagnostic tests and
41 bot CLI tests; candidate 12 wheels predate this migration. Session capability
preflight is defined once in `HarnessTransport.validate_session`.

`providers.completion.complete_native` owns native completion accounting and
archive handling. `providers.run_harness_session` owns session accounting and
capture; both normal workflow steps and the rebase thread delegate. Domain budget
policies and ledgers remain separate. Partial reported usage is retained even
without an archive; absent usage is not evidence of a free call.

Structured sessions now settle through each adapter's canonical usage decoder;
their public `usage` metadata retains the legacy native shape. Buffered decoding
separates envelope parsing from success validation so multiline failure receipts
retain usage on nonzero exit, timeout and native rejection. Claude and Cursor
completion also feed native receipts to `complete_native` before response or
tool-policy validation. Their recovery snapshots delegate to the normal usage
decoders; they do not contain independent billing rules.

`AgentSessionRequest` now exposes an optional native receipt observer. The shared
session boundary composes it on a copy of the request, captures receipts before
calling external observers, and recovers usage through the adapter's existing
snapshot decoder when no outcome usage is available. All five harness adapters
forward these receipts. Refused outcomes settle as failed; reported partial usage
is retained. Session and completion traces can now record known billing facts
without fabricating an accepted reply. Offline tests cover trace failure and
observer interruption across all adapters, plus refused outcomes and persisted
trace usage. These checks do not establish hard-crash receipt durability, native
tool-policy enforcement, or final installed-artifact conformance.

ReviewBot's triage and owner routing use SDK sessions. Its old Codex/Cursor files
retain configuration and prompt-delegation surfaces, with review/repair methods
removed. `knowledge_model.py` and `knowledge_curation_cycle.py` are deleted.
The original-bot deletion ledger records the removed function definitions.

`host_client.HostClient` owns ReviewBot's JSON-line exchange. Direct's `host` and
Strict's `_exchange` delegate. Strict retains generation/capability policy and
typed error translation; `_spawn` and `_roundtrip` are removed. Host closure
does not own the independently durable provider worker.

## Knowledge rules, validation and workflows

Structured-output extraction is owned by `structured_output.decode_json_object`.
Improvement verdicts and rebase plan reviews now delegate with the legacy
`greedy_object` policy; worktree decision loading delegates its fallback with
`trailing_object`, retaining ordinary full-document JSON parsing first. The three
local regex/extraction bodies are removed. Error translation and verdict schema
checks stay with callers. Frozen characterization covers 13 inputs per caller,
including multiple objects, fences, malformed prefixes and scalar documents.

| Capability | Owner and inspected callers | Deliberate policy boundary |
| --- | --- | --- |
| IDs, heading syntax and capacity | `knowledge_service.format`; `common`, `lifecycle`, `ops`, tree/wiki validators | Different heading levels and append/lifecycle rendering remain explicit. |
| Rule identity and collisions | `lifecycle.rule_heading_ids`, `repeated_rule_id`, `first_taken_rule_id`; `proposals`, `apply`, `ops` | Apply checks occupied/introduced/tombstoned IDs appropriate to its operation. |
| Validator execution | `validation.validate_workspace`; `apply`, reconciliation and `validate_snapshot` | Snapshot export differs from workspace access; both execute the same validator sequence. |
| Portable validators | `knowledge_service.validators`; thin `knowledge/tools` entry scripts and packaged resources | CLI diagnostics/exit behavior remain compatible. Installed-resource cases passed in the wheel suite. |
| Curation and bounded repair | `knowledge_service.curation` / `drafting`; public `KnowledgeCurator.curate`, bot distiller | Append-only byte policy and lifecycle footer/index/source updates remain distinct. |
| Intake evidence | `knowledge_service.evidence`; bot intake/evidence adapters and Copilot source adapters | Hosts retain authorization, cursor and export-recovery state. |
| Git object decoding | `git_objects`; knowledge source/snapshot readers | Callers supply their Git runner/error policy. |
| Judgment dispatch and receipts | `kb_service.quality_review`; screening, modules, foundation, depth, discovery, aggregate history and maintenance source audit | Rubrics and pass/fail/unsure interpretation remain stage-specific. |
| Archived native receipt validation | `quality_review.verify_archived_receipt`; foundation native approval replay and discovery live/cached checks | Stages retain evidence-scope and acceptance checks. Foundation no longer invokes a discovery-stage method; discovery retains a delegating wrapper. Role, blob, native-attempt and reasoning-level checks have one body. |
| Task/checkpoint mechanics | `DispatchJournal`, `checkpoint_review`, `drain_review_tasks` and bounded-attempt primitives | Stages select coverage work and construct correction feedback; pending unknown calls cannot authorize replay. |

The shared format rules are consumed through compatibility aliases where old
imports must remain stable. An alias is not a second parser. Tree checks, wiki
lint, lifecycle checks and semantic judgments retain separate results and roles;
consolidation does not make structural validity equivalent to semantic approval.

Seven inspected rubric constants match the original baseline exactly. This is
consistent with preserved policies, but does not replace the planned shadow
evaluation of acceptance, corrections and findings.

## Review computation and publication

`sdk.v1.ReviewRuntime` delegates durable reservations to the run service and
supports both execution modes; `StrictRuntime` is an alias. Local/remote bot
workers use the provider request/result flow instead of vendor runners.
The shared result contracts retain mode-specific evidence and diagnostics.
Business keys, execution binding, persisted run IDs and receipt reconciliation
remain distinct from computation completion.

The shared idempotency reader now treats unreadable/invalid bindings as requiring
reconciliation. Reservation retains a known run ID when its directory or status
is missing, rather than creating another run. An interrupted reservation can be
rearmed only with an explicit null child marker and its matching original
request. Retention rereads bindings under their key lock and preserves missing,
damaged and interrupted outcomes. Known terminal entries still use the existing
retention window. This corrects an older reaper behavior that explicitly allowed
a fresh run after the original directory disappeared. Offline subprocess tests
kill a claimed execution and verify same-key retry does not dispatch again,
including when the status file is subsequently missing.

`review.diff.parse_diff` owns diff interpretation; planning, coverage and anchors
are projections. The planner's `_unquote` copy is removed. Quoted paths and
observable clipping are tested; no parser can infer an entirely omitted upstream
file or hunk solely from the supplied patch.

`review.rechecks`, `review.dispositions` and `review.publication_policy` own the
shared semantic checks. Bot publication projection delegates evidence validation;
standalone publication calls the same policy. Bot final-head reads use the shared
head predicate at delivery boundaries. Hosts retain GitHub identity normalization,
last head reads, existing-publication reconciliation and delivery receipts.

## Evidence limits and remaining gates

### Native tool-policy follow-up

Shared transport ownership does not itself prove that all entry points enforce
their requested tool policy. The current source audit finds these differences:

| Adapter / entry point | Current mechanism | Remaining evidence or change |
| --- | --- | --- |
| Codex completion | Default completion rejects before dispatch; explicit `ContainerCompletionProfile` delegates to the durable container executor and canonical event/usage decoder | The container profile disables connector startup, permits contained local tools and rejects tool-using verdicts; it does not establish `builtin_tools_off`. Native 0.162.1 synthetic stale-cache and forced-401 refresh pass on Personal-Agent. Real-account parity, durable credential profiles, application profile selection and the original compatibility failures remain open. |
| Cursor completion | Ask mode, scratch directory, post-call tool-event audit | Ask mode and filesystem isolation do not establish tool absence. Verify native preventive controls and inherited configuration. |
| DeepSeek completion | Shared composition omits bash/editor plugin registration; shared event audit rejects tool activity | Pinned 0.1.0rc6 native probe exposes zero tools and rejects an injected bash call; agent control retains both native tools. Inherited configuration variation and current-wheel integration remain open. |
| Claude completion / bridged session | Empty built-in selection (`--tools ""`), strict MCP configuration, hooks disabled through native settings; enumerated denylist removed | Native 2.1.296 probes verify empty completion tools, explicit bridge preservation, forced-Bash rejection, inherited hook suppression and user-configured model preservation. Managed customization, other native versions and installed current-wheel coverage remain open. |
| ZCode completion | Built-in denylist and isolated project config; oversized prompts retain `Read` for an attachment | Remove or replace the oversized-input exception with a verified input transport. Installed 0.16.9 help advertises prompt arguments/attachments and an app server, but no prompt-file/stdin option. Do not assume the app-server policy equals CLI policy. |
| Structured read-only sessions | `json_session.run_readonly_json` delegates to adapter command/event methods | These are read-only agent sessions, not verified tool-free completions. Audit triage/routing requirements explicitly rather than treating the two contracts as interchangeable. |

Strict/quality readiness and the shared agent runner now reuse adapter completion
preflight. The runner checks its actual repair client even for mixed-provider
investigations. This prevents late discovery of an unsupported repair operation;
it does not close the native-control gaps above. Preserve separate evidence for
source-level policy construction, native tool exposure, hostile tool responses,
inherited configuration and installed distribution behavior.

The later frozen-3 candidate records Copilot 4,490 passed/20 skipped and
ReviewBot 2,692 passed; installed wheels have 387 passing checks plus release SDK
smoke. Foundation and depth installed supplements add 51 and 48 passing checks.
See the candidate record for exact artifact identities and limitations. These
full source suites predate the deployment-drain amendment, which has separate
focused, deployment and installed-SDK evidence; final integration must include
that amendment. Two source skips were separately exercised successfully from
wheels; 18 environment-dependent cases remain unverified.

The exact-body scan and selected deletion ledgers support this audit but cannot
prove behavioral uniqueness by themselves. Any newly discovered bypass must be
added to the inventory and migrated with its old body removed. Release acceptance
still requires the frozen model-quality comparison, actual immutable published
pair and pins, and the target deployment's live retention/drain evidence.
# Recovery follow-up — 2026-10-13

ReviewBot `HostClient.call` owns wire-value validation for both review modes.
`review.provider_run.require_resolved_review` owns the host decision whether a
provider outcome can end an attempt; Direct polling/conversion and Strict
polling/conversion delegate to it. The old Direct interrupted-to-backend-failure
branch is removed. Strict unknown-state conversion is guarded before failure
classification, including remote adapters inheriting the same conversion.
The coordinator preserves pending outcomes instead of advancing fallback.

Evidence: `host-unresolved-recovery-evidence.json` in the completion output root
binds source and reports. Reopened-ledger local/remote tests preserve original
identity and occupied capacity. This closes the inspected malformed-envelope and
unresolved-state classification paths, not live reconciliation or final artifact
validation.


## Codex terminal-result and billing follow-up — 2026-10-13

Codex follow-up: sessions and contained completion now share
`CodexTransport._require_completed`, including missing/failed terminal events
and unfinished later turns. Both capture native events before rejecting nonzero
process exits; the shared session accounting boundary preserves reported usage.
The completion-only terminal check body was removed. Top-level provider/auth
base URL overrides cannot attest subscription billing. Source evidence:
`codex-terminal-and-billing.json` (143 focused tests); installed-artifact
validation remains pending for these changes.

## Claude terminal-result and usage follow-up — 2026-10-13

Claude session errors now fail explicitly; only native max-turn termination remains
a truncated outcome. A conflicting max-turn stop reason cannot hide another error.
Model identity selection uses the canonical `SessionUsage.normalized_cost`; malformed
or incomplete per-model costs leave identity unknown without discarding valid token
usage or total cost. Native receipts reach the shared accounting path before failure.

The final offline adapter/accounting/JSON-session suite passed 116 tests with no
failures, errors or skips. `claude-terminal-accounting-evidence.json` in the completion
output root binds source and report hashes. Candidate-5 predates these changes; this
evidence does not close final installed-artifact, native-policy or quality gates.

## Remote response ambiguity — 2026-10-13

Local host and remote Strict now delegate operation-value validation to
`host_client.validate_host_value`. Remote adapter validation remains at its public
boundary and calls the same function. The removed inline run-ID check no longer
owns an independent predicate.

`WorkerUncertain` is both a protocol error and an unavailable outcome: routing
keeps the original reservation pending, while RPC does not immediately replay
malformed responses. Missing success values, invalid envelopes and unknown error
classes cannot establish failure. Direct and auxiliary jobs share start identity,
poll state, progress and result validation in `_run_job`; malformed values no
longer escape as incidental exceptions that could permit fallback.

The final worker/host/recovery suite passed 213 tests with no failures, errors
or skips. The initial run exposed a removed remote-adapter check; restoring
delegation fixed it. `remote-ambiguity-evidence.json` binds final source/report
hashes. Offline fixtures do not establish live outcome reconciliation; candidate-5
predates this slice and final paired-artifact validation remains open.

## ZCode native generation verification — 2026-10-13

The native `workspace/generateText` probe now succeeds using an isolated personal
provider-registry fixture and loopback fake endpoint. It sends the complete
180,010-byte prompt with no advertised tools and returns model identity plus
reported usage. An injected Bash response is returned as tool-call data without
execution or a second model request; a production adapter must reject that result
and retain its usage. No paid model request was made.

Inherited-configuration testing found a real limitation: a configured MCP process
starts even with no advertised model tools, and a project-level `mcp.enabled=false`
override did not suppress startup. Empty `mcpServers` in session creation does not
override inherited configuration in the inspected native implementation. Also,
app-server account providers rely on host-supplied account state/runtime headers;
native CLI authentication parity is not established. Do not switch production
completion to this path until both boundaries are resolved.

`zcode-appserver-research/native-generation-evidence.json` binds native/report
hashes and the positive, hostile and inherited-configuration observations. All
probe processes are terminal; current production and candidate-6 remain unchanged.


## ZCode MCP configuration resolution — 2026-10-13

Follow-up native probes resolve the earlier inherited-MCP override observation.
The native schema accepts `mcp.servers` but ignores `mcp.enabled`; its feature
switch is `features.mcp`. The existing production
`ZCodeTransport._write_oneshot_config` already writes the correct switch.

A probe invoking that actual helper, with user configuration outside the scratch
workspace, completes a 180,010-byte prompt through the native app-server against
a loopback fake provider: one request, no advertised tools, usage 10 input / 2
output, and no inherited MCP marker. A matching positive control changes only
`features.mcp` to true and observes the inherited process marker. A separate
explicit-feature override probe also suppresses startup. All probes are terminal;
no paid calls, production edits or user-configuration changes occurred.

`zcode-appserver-research/native-configuration-resolution.json` under the completion
output root binds all three reports, the native binary and the production helper's
source hash. It supersedes the unresolved-MCP conclusion in the earlier evidence
without rewriting the original reports. The master plan now reflects this result.

Remaining scope: account authentication parity, hooks/plugins/managed startup
controls, protocol deadlines/cancellation and adapter integration. The production
CLI's oversized-prompt Read exception is unchanged. Do not count these probes as
full tool-policy certification or a completed transport migration.


## ZCode inherited hook boundary — 2026-10-13

Offline native CLI probing found a startup boundary not covered by the previous
MCP checks. An inherited user `SessionStart` process executes with the production
completion configuration. Adding project `hooks.enabled=false` does not suppress
it: the native `vat` hook-root resolver enables hooks when any source enables
hooks. Both terminal probes observed the marker and two loopback fake provider
requests; neither probe timed out. They establish hook execution, not successful
model completion or tool-free behavior.

The attempted production setting and its configuration assertion were removed
because native evidence disproved the fix. The provider source hash again matches
the earlier candidate-6 source. The focused 30-test ZCode run passed while testing
the attempted setting, which demonstrates why argument/configuration tests alone
cannot certify native policy; do not treat it as proof of hook suppression.

Native source also shows that `ZCODE_DATA_BASE_DIR` selects the credential file,
while the fallback cipher key incorporates `os.homedir`. Moving the process home
without a verified credential strategy risks breaking existing login. App-server
mode does not initialize the standalone credential source. Do not silently replace
account authentication or copy credentials into model-readable scratch space.

`zcode-appserver-research/native-hook-boundary-evidence.json` binds both reports,
the native binary, restored production source and relevant source excerpts. All
processes are terminal. No paid calls or user configuration changes occurred.
Next work must establish a supported startup boundary with account compatibility;
a disabled project-hook flag is now ruled out by native evidence.


## Rejected quality-reply accounting — 2026-10-13

The shared `quality_review.review_call` previously discarded known native facts
when an arrived reply failed archive or evidence verification. It now retains
the canonical native receipt and marks the call completed, while the quality
outcome remains unavailable. Missing responses retain unknown outcome and no
invented receipt. Stage rubrics, fixed budget charges and retry policy are
unchanged; the shared runner does not dispatch a replacement.

Regression tests cover missing archive, mismatched digest, evidence rejection
and a lost response. The runner plus depth, maintenance, shared budgeting,
foundation, history and discovery integration passed 284 tests, zero failures,
errors or skips. `quality-rejected-receipt-evidence.json` binds source and report
hashes. This adds seven production lines and 33 AST nodes versus candidate-6.
The final paired wheels/full integration must include this change; historical
candidate-6 results do not cover it.

The current exact-body audit scanned 453 production files and 2,217 functions
spanning at least 13 source lines, excluding docstrings, with zero duplicate AST
bodies. `current-exact-function-body-audit.json` binds every source hash. This
limited scan does not prove semantic uniqueness or close the ownership ledger.


## Completion finalization recovery — 2026-10-13

`providers.completion._completion_boundary` now owns dispatch metadata on every
completion exception, including recording, archive-finalization and budget-
settlement errors. The previous two assignments at the terminal raise path are
removed. HarnessLLM, improvement judges and ModelGateway already delegate to
this completion owner. Post-dispatch infrastructure errors cannot authorize a
generator fallback, including callbacks rethrowing stale exception metadata.
Known replies remain completed facts; unknown native outcomes stay unknown.

Pacing finalization now retains an already returned reply and usage on failure;
it cannot authorize fallback either. Ordinary provider-error policy remains
caller-owned. Fault injection covers all three finalization boundaries after
both completed and lost native responses, plus pacing journal failure.

The final focused integration passed 163 tests, zero failures/errors/skips,
including quality receipts, shared budgets, pacing, generator fallback, model
dispatch, subscription and improvement budgets. `completion-finalization-evidence.json`
binds source/report hashes and the inspected callers. This slice changes
production by +23 lines / +99 AST nodes versus candidate-6. These source
results do not cover installed artifacts; candidate-6 predates this change and
the preceding quality-receipt change. No paid calls were made.

## Git-tree parser follow-up

A later caller audit found four independent `ls-tree -z` metadata parsers still
outside `git_objects.tree_entries`: discovery `_tracked`, reconciliation
`_validate_target`, pinned observer `_entry`, and improvement `freeze_review`.
All four now delegate and their local decoding bodies are removed. The parser
has an explicit tolerant lookup policy for the pinned observer; malformed
nonempty replies still cannot populate its confirmed-missing cache. Discovery
mode selection, reconciliation content/mode checks, and replay size/credential/
symlink restrictions remain domain policies.

The affected suites pass 201 checks, including real-Git executable-byte replay
and symlink refusal. `git-tree-owner-consolidation.json` under the completion
output root binds the five production files, four callers and two reports.
Compared with candidate-7 this slice adds four production lines and removes
three AST statements. It requires new installed-artifact validation and does
not close the complete ownership ledger by itself.

## Maintenance quality receipt and settlement boundary

`maintenance_audit.BudgetGateway` retains domain reservation/cache ownership,
with `reserved_call` and `MaintenanceStore.settle_cost` owning finalization and
idempotent settlement. It now passes known API cost to settlement before any
post-reply rejection, preserving actual overspend. Native trace/hash metadata
survives its durable cache and is interpreted by the existing shared quality
receipt implementation; the gateway does not create a second quality rubric.
Legacy cache shapes remain readable. The 112 passing focused checks and exact
source bindings are recorded in `maintenance-receipt-accounting-evidence.json`.
This source postdates candidate-7 and needs subsequent artifact validation.

## Complete public SDK export and bot import inventory

Candidate-8 now has a complete inventory of the callable public SDK exports:
87 function/class exports resolve to 83 canonical definitions across 19 source
files. Each exported object is identical to its defining module's object; each
unwrapped implementation file matches the frozen source hash. The four pairs
are the intended Review/Strict runtime, configuration, handle and poll aliases.
The audit imports the installed wheel under isolated Python and denies process
launch/network-connect audit events; it does not execute workflows.

The matching bot inventory scans all 127 frozen production Python modules:
81 named SDK imports and five SDK-module attribute accesses across 27 consuming
modules reference 52 public exports. Every referenced export resolves through
the installed inventory, including constants. These observations cover the
public import surface, not all dynamically dispatched instance-method calls or
proof that every business loop has a single implementation.

Reports: candidate-8 `public-sdk-owner-inventory.json` and
`bot-sdk-consumer-inventory.json`, hash-bound in `paired-artifacts.json`.
Reproducers in the completion output root: `audit_sdk_owners.py` and
`audit_bot_sdk_imports.py`. No product code or wheel inputs changed.

## Remaining agent-runtime composition boundary

Candidate-9's `agent-runtime-composition-gap.json` identifies residual domain
policy below application composition: `engine/agent_runtime/runner.py` imports
`improve.shadow.harden_scope`, selects it using `improve_shadow`, and suppresses
skill touches using that domain flag. `engine/agent_runtime/knowledge.py` uses
the same flag to select run-local caches and symlink read fencing. Existing
Executor boundary checks do not establish closure for these agent mechanisms.

Move scope transformation, skill metadata-write permission and repo-map
storage/read restrictions into injected execution policy selected by application
composition. Preserve protections for ensemble lens names and direct domain
callers. Replace the source-string assertion in `test_improve_p0b.py` with a
behavior test showing no production metadata writes. Cover run-local caches,
symlink rejection and concurrent per-run policy isolation. Keep the canonical
shadow restrictions in the improvement domain; do not remove their enforcement.

Implementation remains open. Candidate-9 suites are running against frozen
inputs, so this audit changed no production or test source.

### Agent-runtime shadow policy migration applied

The residual shadow-policy slice described above is now applied and passes 91
focused primary-worktree tests. `engine/agent_policy.py` defines neutral policy
fields; `improve/execution.py` selects shadow restrictions and injects them via
StepContext, including the direct `improve/drivers.py` worker. The common agent
runner and knowledge helper consume the policy without improvement imports or
settings branches. Rebase context replacement retains injected resources.
Evidence includes actual inside/outside tool reads, symlink/cache protection,
no skill-usage writes, concurrent policy isolation, direct-worker composition
and nested-context propagation. This closes the identified shadow-policy slice;
it does not claim final paired-artifact or full semantic ownership closure.

## Native archive finalization and validation owner

Structural native-attempt validation has moved from the quality receipt verifier
to `TraceStore.validate_native_attempt`; the former body is deleted and its
caller delegates. Quality-specific role and reply acceptance remain in
`quality_review.verify_archived_receipt`. The new offline recovery command uses
that same structural validator and the existing `NativeCallArchive.finish` to
finalize a unique original successful journal record. It contains no model loop,
trace append, index repair or domain settlement. CLI subprocess crash/recovery
and corruption coverage passes 59 focused checks. Installed coverage is selected
for the next wheel build; checkpoint and budget reconciliation are not closed.

## Checkpoint identity ownership follow-up

`quality_review.checkpoint_review` snapshots and persists the original input
binding before dispatch; history, intake, depth and block-review callers share
that implementation. It no longer merges a mutable caller binding into the
completed result. Existing unresolved checkpoints without binding metadata
remain unresolved. New corruption and process-loss tests plus affected domain
suites pass 192 checks. Evidence: `checkpoint-binding-evidence.json`. This is
input retention and mutation protection, not completed domain reconciliation.

## Maintenance settlement arithmetic and recovery

Maintenance's inline completed/unknown/subscription charge calculation has
been removed. `budgeting.settlement_charge` is now its canonical amount-rule
owner, preserving integer domain units; the maintenance ledger retains atomic
reservation and immutable settlement authority. Its cached-reply path now passes
known original API costs into that ledger instead of losing them on recovery.
Six interrupted-settlement cases cover API/subscription and overrun/refund/
unknown cost; the affected integration passes 188 checks. Evidence is
`maintenance-recovery-cost-evidence.json`. These changes postdate candidate 15;
its still-running installed tests cannot validate them.

## Maintenance replay reservation authority

Cached-reply reuse now calls the existing ledger reservation owner with
`require_existing=True`. That mode validates original binding and refuses
creation; the gateway's direct SQL read is removed. Owner, lane, cost kind and
allowance drift tests cover pending and settled originals. The same owner keeps
transactional mutation and immutable settlement authority. Relevant suites pass
151 checks; candidate-15 wheels predate this follow-up.

## Init account identity remains with its budget owner

`init_budget.Budget` owns reservation ID allocation, status and accounted amount
inside the existing reserve/finalize lifecycle. `checkpoint_budget` supplies the
init record's additive call map; execution resume retains and validates that
map. Native trace context links to the original reservation. No new model loop
or separate budget arithmetic is introduced. Legacy aggregate totals remain
conservative. Nine focused and 110 affected conformance checks pass, with
expected overlap. Discovery journal migration and recovered-call settlement
remain explicit unfinished work.

## Discovery budget persistence adapter migrated

Discovery's journal now passes original call entries to `init_budget.Budget`.
The separate resume JSON/total-handling body is replaced by delegation to
`_discovery_budget`, which owns the discovery persistence shape and uses the
shared budget's entry validation. Spent/reserved aggregates and batch identity
remain compatible; additive IDs are never invented for old calls. Record-backed
and discovery-backed budgets share allocation, trace linkage and settlement
lifetime. Final suites pass 128 conformance and 17 focused checks (overlapping).
Actual recovered-call settlement still requires original accounting policy and
verified outcome binding; this slice does not claim that completion.

## Init cost reconciliation ownership

`init_recovery.reconcile_cost` composes original trace/archive validation with
original record or discovery-account lookup. `Budget.reconcile` owns the monetary
transition and immutable receipt; the CLI only translates arguments/results.
The quality runner binds fixed accounting at dispatch, while generator calls
use actual accounting. Native billing consistency is checked by the same
`TraceStore.validate_native_attempt` used for quality receipts and archive
finalization. Discovery v2 is now the sole budget-total authority, with a stage
record mirror and compatible conservative legacy migration. No provider is
invoked during recovery. The affected 233 checks pass; installed coverage and
workflow result reuse remain open.

## Aggregate quality result recovery

`quality_review.recover_quality_reply` reconstructs a bound native reply using
canonical JSON parsing, native archive validation and quality input hashing.
PR-history's `aggregate_review_result` supplies its existing schema and result
contract for both ordinary execution and recovery; its inline formatter is
removed. `init_recovery` composes checkpoint and domain budget persistence, and
the CLI delegates. Original runtime bytes are bound through the neutral package
fingerprint owner without importing improvement code. Tests pass for the
registered aggregate policy; other quality policies and installed validation
remain open. This is not universal stage recovery or model-quality acceptance.

### Depth facet result recovery (2026-10-13)

- Canonical owner: `kb_service.depth_judge.facet_review_contract`,
  `validate_facet_review`, `facet_review_result`; normal `review_facets` and
  `init_recovery._review_result` delegate to the same rules and normalization.
- Caller binding: `init_knowledge_depth` persists `facet_review_binding` before
  dispatch. The shared recovery core still verifies original runtime, input,
  attempt, native archive and accounting identities; it never dispatches.
- Removed bodies: nested facet-result validation and successful native result
  formatting in `review_facets` moved into these owners, with no parallel
  recovery implementation. Stage-specific acceptance remains explicit.
- Evidence: `depth-review-recovery-final.xml`, 124 passing source tests. Installed
  validation and exact source/AST evidence are recorded under the output slice
  `installed-init-depth-recovery` / `depth-review-recovery-evidence.json` when
  terminal; earlier wheel candidates do not cover this change.

### Record-owned init block results (2026-10-13)

`gate.validate_block_review` and `gate.block_review_result` now own the block
schema and conversion for normal `judge_block` and original-result recovery.
The nested validator and inline result-construction bodies are removed from
`judge_block`. `init_support.judge` retains block identity and the requested
reviewer with its existing input digest before dispatch. `init_recovery` selects
this policy by the archived quality scope, so a PR-history advisory block cannot
be confused with the aggregate review. Checkpoint lookup, archive verification,
accounting and immutable persistence remain the same shared implementation.
The parallel foundation task journal is not covered by this record adapter.

### Parallel task archive identity and foundation replay checks (2026-10-13)

- `_attempt_scope` is the shared owner of `knowledge_dispatch` native context for
  both `checkpoint_review` and `DispatchJournal`/`drain_review_tasks`. Generators
  and judges in a task retain the same original dispatch/attempt; quality policy
  and judgment call sequencing remain separate. No provider invocation or
  recovery dispatch loop was added.
- `gate.block_review_verdict` owns validated block acceptance for live result
  formatting and foundation native approval replay. Foundation's hard-coded
  three-dimension approval body is removed. Its duplicate provider/model/error
  and reply-hash comparisons are removed because `verify_archived_receipt`
  already enforces those facts. Foundation-specific prompt/evidence/section and
  artifact binding remains in the foundation adapter.
- `task-native-binding.xml`: 96 passing source tests, including process loss in
  a parallel generator, no second dispatch, concurrent task isolation and
  generator/judge archive agreement. Expanded shared-replay tests are recorded
  separately. Legacy archives without the generic task context are not assigned
  fabricated identities. Full unfinished foundation-task reconstruction remains
  an open application adapter, not something this context addition claims to do.

### Original parallel request persistence (2026-10-13)

`quality_review.DispatchJournal` owns optional request envelopes alongside the
existing dispatch intents. It saves both through the existing checkpoint callback,
normalizes requests to their durable JSON representation, validates their retained
binding/digest, preserves them after settlement, and refuses mutation or missing
new-request evidence before clearing an intent. `original_request` is read-only
and returns a detached copy. Legacy callers without this facility retain their
original identities; missing old requests are not fabricated.

Foundation supplies its domain snapshot through `_request_context` and
`_request_snapshot`: shared rendering context is stored once by content hash,
while each task retains its original job and result sequence. `original_job_request`
checks that frozen data against its stage identity, runtime and original dispatch.
The existing worker pool and result/application loops are unchanged. This fills
a previously missing persistence capability; no independent request-recovery
loop or alternate acceptance policy was added. Reconstructing and settling the
original multi-call result is still pending, as is full native profile binding.


### Archived task replay follow-up (2026-10-13)

Native reply reconstruction now has one implementation in
`kb_service.quality_review.recover_native_reply`; single-judgment recovery and
`OriginalTaskReplay` delegate to it. The replay adapter supplies archived replies
to the existing foundation `_worker`, preserving its stage-specific validation
and rendering rather than introducing a second computation loop. It cannot
make model calls or write budget accounts. Completion elapsed metadata now has
one normalization point shared by live results and persisted receipts.

Foundation request snapshots also retain generator admission mode, prices and
init settings. These are original execution facts, not new authorization.
Application integration is still open: complete native-attempt inventory,
original-account settlement and result/intent settlement must precede usable
offline foundation reconciliation. Pure replay and tests do not close that row.


### Foundation offline reconciliation integration (2026-10-13)

`reconcile-foundation` delegates to `init_recovery.reconcile_foundation`. The
application reconstructs the original owner/coverage context and pinned local
observer from the retained request; replay uses the normal worker, accounting
uses `_settle_cost`/`Budget.reconcile`, and result/intent settlement uses
`DispatchJournal`. No separate correction loop or replacement dispatch was
introduced. Crash/repeat settlement passed the 159-test source run before
command reconstruction was added; the expanded command run remains pending.
Old snapshots missing execution or validation context refuse reconstruction.

Feature discovery's `_run_tasks` still uses the shared journal without its
request snapshot facility. Its unresolved tasks cannot yet reconstruct the
original multi-call work, so the all-stage recovery requirement remains open.
Foundation progress must not be used to close that distinct caller.


Discovery live worker callbacks now delegate to `_execute_task` using explicit
persisted policies. Extraction/repair callback bodies and per-review dispatch
lambdas were removed; their existing policy methods remain canonical. Scheduling
and settlement still belong to the coordinator. This supplies a single execution
entry point for the pending recovery adapter, without claiming that adapter is
already connected. The 100-test policy and 24-test consolidation follow-ups pass.
