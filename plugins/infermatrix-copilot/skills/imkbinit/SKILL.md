---
name: imkbinit
description: Bootstrap a repository's InferMatrixCopilot knowledge base with `kb init`, one reviewed stage at a time (skeleton, feature-discovery, modules, knowledge, deepen, pr-history, harvest-calibration). Use when the user invokes /imkbinit or $imkbinit, asks to initialise or onboard a repository's knowledge base, or wants the next kb init stage or an existing knowledge base widened or deepened.
---

# InferMatrix knowledge-base init

```text
/imkbinit <repository> [--publish]
$imkbinit <repository> [--publish]
```

The installed InferMatrixCopilot root is:

```text
{{INFERMATRIX_COPILOT_ROOT}}
```

If that placeholder was not replaced, locate the InferMatrixCopilot checkout
containing `src/infermatrix_copilot/kb_service/init_stages.py`.

## Launcher

The MCP install only registers an isolated `uvx` server, so the CLI may not be
on `PATH`. Call it `<imc>` below and resolve it once:

1. `infermatrix-copilot`, if `command -v infermatrix-copilot` finds it (a full
   `install.sh` install).
2. Otherwise run it from the InferMatrixCopilot root above, so the CLI and
   the knowledge tree come from the same checkout:

   ```text
   uvx --from "<infermatrix-root>[kb]" infermatrix-copilot
   ```

Check it once with `<imc> kb --help`. If neither works, stop and say how to
install it (`bash install.sh` in the InferMatrixCopilot checkout).

This skill only drives the `kb init` CLI and reports what it did. The CLI owns
every decision: what to generate, the checks, the budget, publishing. Never
edit the generated pages, adapters, or stage records yourself.

The user's explicit instructions for batch scope, models, publication and merging
override the default one-stage stopping boundary below. Reuse authorization
already given; CLI checks and publication gates still apply.

## 1. Resolve the repository

`kb init` takes the knowledge repository name: the `repos/<name>` in an
adapter's `knowledge.repo_subdir`. Match the user's alias, `owner/name` or URL
against `repo.full_name` in `adapters/*/manifest.yaml`. With no match, stop: a
repository needs its own adapter PR (with a `knowledge_lifecycle.init` block)
before `kb init` can run. If that block has no `seeds`, mention that
`<imc> kb init <repo> --suggest-seeds` lists candidates (it only
prints them, and makes no model call).

## 2. Find the next stage

Stages run in this order: `skeleton` → `feature-discovery` → `modules` → `knowledge` → `deepen` →
`pr-history` → `harvest-calibration`. The record of each stage is
`<state-dir>/init/<repo>/<stage>.json`, where the state dir is `--state-dir`,
else `$KB_STATE_DIR`, else `~/.infermatrix-copilot/kb`.

The [new-repository template](../../../../doc/architecture/templates/kb-init-new-repository.yaml)
sets `init.feature_discovery_required: true`; configure its scope and documentation
paths for the repository, retaining the adapter's existing publication permissions.
Existing adapters keep discovery optional until explicitly enabled or started.
When the flag is false and no discovery record exists, omit `feature-discovery`
from next-stage selection. Once a discovery batch starts, finish it before
later stages. Its preview must have `discovery.done: true`; publishing requires
the catalog PR to be merged. Existing batches must not silently change their
frozen feature denominator.

Which stages count as done depends on the mode of this invocation:

- **Publishing** (the user asked for PRs): a stage is done only when its
  record's `pr.number` is set and `gh pr view <number> --json state`, run
  inside the InferMatrixCopilot checkout (the knowledge repository), says
  `MERGED`. Dry-run records don't count, so a previewed stage is published
  next rather than skipped.
- **Dry run** (the default): a stage is also done when its record exists with
  `status: dry_run`, so stages can be previewed end to end before any PR.
  A semantic-depth record with `status: partial` or `depth.target_met: false`
  is unfinished. Visiting every feature does not establish semantic completion.

The next stage is the first one that isn't done. If an earlier stage's PR is
open, stop and say it's waiting for the owner's review. If a record says
`blocked`, report its `problems` and stop.

For an explicit breadth rerun of an already merged KB with no local skeleton
record, use `--stage modules --from-existing`. Merge its PR before running
`--stage knowledge --from-existing`; no skeleton record is manufactured.
For a knowledge-only rerun with no local skeleton/modules records, use
`--stage knowledge --from-existing`. The CLI
checks the merged index and every owner route. Existing stage records still
keep their review and merge gates; never manufacture records to skip them.
An enabled or started feature-discovery batch must still finish and its
catalog PR must merge before publishing modules or knowledge.
When using `eval/knowledge-depth/run_depth_campaign.py` after discovery,
pass `--discovery-record` with the original completed record. Its byte hash,
merged catalog and report are checked before any worker dispatch; each worker
receives the exact original JSON, retaining external archive paths. A baseline
with a discovery report requires this handoff even for older adapters. Never
rewrite a prior status or create a substitute record to satisfy the gate.

For implementation knowledge in an existing KB, use the independent stage
`knowledge-deepen` (`kb deepen <repo>` is its `--from-existing` alias).
It is distinct from the ordinary `deepen` stage that generates hot-module rules.
`kb widen <repo>` similarly selects `knowledge --from-existing`.
Use a new state directory when the knowledge baseline or source pin changes;
retain prior native records and accepted blocks as audit evidence.
If a previous merged batch's publication branch still exists, set
`KB_INIT_BRANCH_SUFFIX=<batch-slug>` before starting the new batch. It appends
the slug to the default stage branch without replacing old branches. Use
1–40 lowercase letters, digits or hyphens, with no leading or trailing hyphen.
The suffix is part of the checkpoint identity; retain it when resuming or
publishing that batch, including a publication waiting for PR confirmation.

## 3. Run it

Dry run is the default:

```text
<imc> kb init <repo> --stage <next> --dry-run
```

When the user selects Zcode on its OAuth coding subscription, set
`KB_GENERATOR=zcode:GLM-5.3` and pass `--subscription-generator` explicitly.
The CLI pins and checks the served model. It rejects custom API providers in
this mode; default API spend-cap checks remain in place. Report generator USD
as unreported and subscription fees outside stage USD accounting, alongside
token usage and the judge's accounted spend. Never claim a measured zero cost.

When the user explicitly selects uncapped subscription extraction,
`feature-discovery`, `modules`, `knowledge`, `knowledge-deepen`, `kb widen`
and `kb deepen` accept `--unlimited-subscription`:

```text
ZCODE_REASONING_LEVEL=low KB_GENERATOR=zcode:GLM-5.3 KB_JUDGE=codex:gpt-6.1-sol:medium <imc> kb deepen <repo> --pin <source-sha> --subscription-generator --unlimited-subscription --dry-run
```

The generator must be Zcode GLM-5.3 and the independent judge must be Codex;
both must be authenticated subscription roles. Other model protocols fail
before authentication or dispatch. This mode conflicts with `--budget-usd`, removes the stage's fixed
accounting ceiling, and preserves call, served-model and token receipts. It does
not authorize API spending or fallback to another provider; unavailable
subscription roles stop with their reason. Actual USD remains unknown when the
provider does not report it. Keep the requested source pin and reviewed policy.
For breadth reruns, explicitly set the same `KB_GENERATOR` and `KB_JUDGE`
roles and pass this flag to both `modules --from-existing` and
`knowledge --from-existing`; `--subscription-generator` alone preserves the
normal stage ceiling for judge accounting. The unlimited mode has a distinct
checkpoint identity; old runs with no flag keep their existing identity.

Only publish when the user explicitly asks (`--publish`, "open the PR").
Publishing needs `ALLOW_PUSH=1`, `ALLOW_POST=1` and
`KB_INIT_GIT_AUTHOR='Name <email>'` in the environment. Tell the user which of
these are missing instead of setting them. Then run the command without
`--dry-run`. A private upstream always runs as a dry run.

What each stage opens:

- `skeleton`: the map, the doc invariants and the seeds.
- `feature-discovery`: derive a feature baseline from existing document bodies,
  then inspect all declared source/test regions for additional capabilities.
  Zcode GLM-5.3 extracts and independent Codex reviews with shared concurrency 13.
  `KB_DISCOVERY_GENERATOR`, `KB_DISCOVERY_JUDGE` and `KB_DISCOVERY_CONCURRENCY`
  override the stage configuration; discovery rejects same-family review.
  `KB_DISCOVERY_PACKET_CHARS` groups complete indexed chunks (24000–192000,
  default 24000); `KB_DISCOVERY_START_INTERVAL_S` sets the initial Zcode spacing
  (1–60 seconds, default 15) while native rate-limit cooldown stays enabled.
  `ZCODE_REASONING_LEVEL=low|high|max` controls the effective native reasoning;
  a Zcode role's effort suffix does not. These settings are frozen in the batch
  and report, with reasoning also recorded in both trace and attempt archives.
  Changed settings or old checkpoints without them require a fresh state
  directory; retain the previous archive. Larger packets do not prove all
  features were found.
  Before promoting implemented new IDs, run the supplemental independent
  catalog boundary audit against every formal feature's ID, title and owner.
  Keep primary reviews and native receipts; unsupported or unresolved duplicate
  titles/aliases stay unknown without another generator repair loop. Unpublished
  old previews reuse their scan and primary reviews to acquire this audit.
  Before publication, jointly consolidate the actually admitted new proposals
  against one another with counterpart implementation and consumer excerpts.
  Retain original extraction and boundary proofs; unresolved duplicates stay
  unknown. This is part of aggregation and independent review in phase 1.
  Exhausted content repairs remain exhausted on `--retry-unfinished`.
  `--from-existing` checks merged skeleton routes, while `--retry-unfinished`
  resumes supported work. `--budget-usd` controls cumulative accounting;
  `--unlimited-subscription` authenticates both roles and removes that ceiling.
  Publish only the adapter's feature catalog and compact report under `eval/`.
  Unknown candidates and failed reads remain visible; a completed scan is not
  proof that every repository capability was found. Full native traces remain
  outside Git. Merge the catalog PR before breadth or deepen uses its frozen IDs.
- `modules`: one map card for every unrouted module.
- `knowledge`: evidence-backed architecture, API contracts, configuration,
  design tradeoffs, feature relationships and validation for each source owner.
  It visits owners even when they already have rules, labels inferred rationale,
  and reports missing facets and unread files. It does not change lifecycle flags.
  Use the repository's existing README, architecture/design, API and configuration
  docs via `init.doc_globs`, plus each feature's declared docs. Owner selection ranks
  the complete matched document list before applying prompt limits, with repository
  README fallback. Check documented behavior against pinned code and record mismatches
  or unimplemented designs; link upstream detail rather than copying whole documents.
  Older init chains without a required coverage policy may omit this stage; once started, its PR must merge
  before later stages run. New onboarding includes it.
  For full coverage, prepare the adapter's `knowledge-coverage.yaml`: enumerate
  product features from source and docs, give each feature explicit production
  entry points, and declare all first-party production roots, languages and
  exclusions (including frontend, SDK and desktop clients when requested).
  The targets are every inventoried feature and at least 85% of core files.
  Verified source-contract records count as structural interface/dependency
  knowledge; they do not prove complete behavior or test coverage. Bare links,
  rules, routes and files offered to a model do not count. A required policy
  blocks subsequent stages until both targets pass, including partial merged
  drafts. It also prevents skipping knowledge and rejects results for an older
  policy hash. Without a policy, report only owner/facet coverage and do not claim
  feature/core completion. Save audit reports under eval or local state.
  The stage emits `feature`, `entry_points` and `source_globs` from the reviewed
  policy on explanatory feature pages, preserving prose and source proofs.
  These hints let Direct and Agent review retrieve relevant knowledge automatically;
  they do not count toward structural or behavioral coverage.
- Independent `knowledge-deepen`: fill a worklist of feature × semantic facet,
  preserving existing approved blocks and their proofs. Retrieve facet-specific
  implementation and associated tests across the full reviewed source scope;
  use project/owner docs as context and pinned code to resolve disagreements.
  Retries address the recorded refusal or missing evidence rather than resending
  the same slices. Omitted, invalid, rejected and unjudged facets stay unknown.
  Optional `semantic_depth: {per_facet_gt: 0.90}` in the coverage policy requires
  every facet's recognized feature ratio to be strictly greater than 0.90,
  and every feature to have recognized depth. For 79 features this means at
  least 72 recognized items in each of seven dimensions, with denominator 553.
  An overall average cannot replace per-row acceptance. Keep breadth and all
  first-party production-file coverage separate.
  Recognition has two admitted bases: `supported` is source-backed positive
  knowledge; `verified_absent` is a replayable deterministic absence certificate
  plus the same three-criterion independent Codex approval. Existing blocks
  default to `supported`. Absence keeps a visible capability/test gap, never
  claims tests passed or that an implementation is verified. A partial search,
  parse/read error or unresolved association remains `unknown`; never shrink
  the denominator or manufacture absence. Report all three categories separately.
  The tracked test inventory includes every policy-supported first-party code
  suffix, even when a feature uses only one source language. Unsupported syntax
  or association checkers remain unknown; cross-language tests cannot be omitted
  to certify absence (`static-test-association-v2`).
  The target gate and processing the full gap worklist are distinct: retain
  `target_met`, `all_resolved` and each slot's reason. An unmet required target
  leaves a resumable partial preview and blocks publication. Reaching the
  threshold does not mean every gap is filled; continue the authorized worklist
  and report genuine remaining blockers.
- `deepen`: code rules for the hot modules, plus the adapter flip to
  `enabled: true, mode: shadow`.
- `pr-history`: reads the latest 1,000 merged upstream PRs as of the upstream
  pin, oldest first, with one extraction call per PR through `KB_GENERATOR`.
  `--pr-count N` overrides `init.pr_history_count` (default 1000). Executable
  rules go to their nearest component or offered model owner; model-specific
  contracts stay with that model. PRs yielding no upgrade are recorded and
  make no commit. One draft PR contains one commit per accepted upstream PR.
  The PR body shows counts and the first 20 upgrades; all upstream references
  remain in the commit trailers. Codex reviews its complete base-to-head diff,
  prior owner rules, routes and pinned source evidence;
  only approval of that exact head makes it ready. `KB_INIT_REVIEWER` may
  pin a Codex model/effort (`codex:model[:effort]`, default
  `codex:gpt-6-sol:medium`); it never changes reviewer provider. A failed review leaves
  the PR draft. Dry runs also perform the complete Codex review and write a
  `COMMITS.json` beside the preview. Raw PR evidence is transient.
- `harvest-calibration`: turns the merged review into
  `adapters/<adapter>/kb-calibration/cases/*.json`. It refuses to open a PR
  with fewer than five bad cases from the owner or from mutations.

`auto_merge` still needs `kb calibrate` and the shadow period. The skill never
flips it.

If the CLI or its run report blocks the stage, report the reason and stop.
Never work around it by hand.

For `pr-history`, a blocked record is a resumable checkpoint. When the user
requests a retry, rerun the same stage with the same pin/window/backend;
completed PRs and prepared commits are reused. The stage accounts cumulative
spend on its frozen knowledge baseline; unrelated main merges do not restart
the batch. Changing the baseline requires a new state directory.
`--budget-usd N` may raise the ceiling to continue after a budget stop.
Prepared publication retries also require the original immutable inputs.
It does not publish an incomplete window. Existing init runs with no history
record remain harvestable, and may add this phase after their earlier stages
have merged. Once a history record exists it must finish before harvest.

## 4. Report

Before publishing a completed knowledge batch, audit the complete candidate
checkout containing the baseline plus all accepted changes (a partial dry-run
delta is insufficient). Preserve its pinned source proof and native generator/
judge receipts, then run:

```text
PYTHONPATH=src python tools/audit_knowledge_depth.py --repo <repo> --upstream <pinned-source-checkout> --pin <source-sha> --approval-report <native-approval-report.json> --require-approvals
PYTHONPATH=src python tools/audit_review_retrieval.py --repo <repo> --report <eval-report.json>
```

Repeat `--approval-report` for earlier and current native approval batches when
needed. Validate source spans and approval bindings, rather than treating a
structurally valid marker or a visited worklist as approval. The required semantic policy uses
one target predicate in init, audit and publication.

The retrieval audit checks every catalog feature using description plus entry
points, and separately measures paths-only ambiguity. Retrieval remains bounded
to two documents and 6,000 content characters. Distinguish all `available_facets`
from actually delivered `included_facets`, `not_injected_facets`, and their bases;
read the remaining relevant facets through the existing bounded doc tools.
Lightweight depth also reads existing indexed project documents in each source
file's ancestor directories up to the nearest component guide, including
adjacent SKILL, README, design and reference guides. Generic owner documents
are the fallback when local text is unavailable. Documents share the existing
8 KB budget, favor real source filename windows, and retain exact pinned ranges
in the final packet. Documented manual operations
and observable expected results may support an explicitly unexecuted manual
validation entry; unrelated sibling components and invented tests cannot.
Keep saved native inputs and cumulative correction counts unchanged on resume.
`verified_gaps` describe absent evidence, not tests passed; missing facets remain
unknown. Save reports outside `knowledge/` and report retrieval separately from
breadth/depth and actual PR quality. This audit does not establish bug recall.

The delivery order is breadth review, targeted deepening, source/native-proof
audit, retrieval acceptance, independent full-change review, CI, then an authorized
merge. Report checks that could not run as unverified. Never label threshold
acceptance as complete gap closure when unknown slots remain.

Read the stage record and the PR body (`PR_BODY.md` under `pr.dry_run_dir` for
a dry run, otherwise the opened PR). Summarise:

- pin and status, plus the PR link or dry-run directory
- coverage: module and PR-weighted, routed and rule-bearing, before and after
- the judge table: pass, unsure, unjudged, and the rules stripped as fail
- dropped rules and their reasons
- the "needs human edit" checklist
- spend against the budget, and any `unfinished` units

## 5. Default stopping boundary

Without explicit authorization for a broader batch or merge, stop after one stage. The owner reviews and merges its PR before the next
stage can run. Never run two stages in one invocation, and never merge a PR.

### Foundation concurrency (2026-10-06)

Explicit `--stage knowledge --unlimited-subscription` shares at most 13 worker
slots across extraction and all existing independent section judgments. Set
`KB_KNOWLEDGE_CONCURRENCY` to 1–13 before starting; it is frozen in the input
identity. Native Zcode starts share a default 5-second pacing interval;
`KB_KNOWLEDGE_START_INTERVAL_S` may override it with a finite positive value
up to 60 seconds and is also bound to the stage identity. The CLI coordinator alone saves real worker checkpoints and appends
approved sections in deterministic order. Resume the same command/state to reuse
completed native approvals; keep their external trace archives. Finite-budget
runs retain the previous serial flow. New discovery features preferentially read
the frozen report’s exact complete source spans; unread gaps cannot be cited.

Completed unpublished caches and prepared-publication resumes also replay the
original native proofs before any write or model call. Missing or changed
archives reject reuse without changing the genuine record; restore the original
archive to continue. Changed frozen inputs require a new batch. Published
historical records remain unchanged.


## Task completion learning

Before your final task response, FIRST call `knowledge_agreement(action="status")`.
Only `status="accepted"` permits [task completion learning](../imlearn/references/task-completion.md)
to distill lessons, grade knowledge or upload feedback. Otherwise skip learning,
finish the requested task, and offer the actual agreement and human signing
command once without per-task reminders. Never sign for the user. Accepted
agreement and configured publication/write gates are both required; operator
configuration is not consent. This does not authorize publishing the task's code,
comments or RFC.
