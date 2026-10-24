# RUNBOOK — repo-rebase v3 cutover operations (repo-neutral template)

The operational companion to `doc/PLAN-rebase-merge.md` (§3.3 rollback,
§8 validation/staged enablement). Everything here is human-executed; the
plan doc holds the contracts, this holds the switches and checklists.

> **EXECUTED for adapter zero (vllm-omni), 2026-08-25** — owner-ordered
> PR6 promotion + PR7 retirement as ONE unit: `repo-rebase-v3` →
> **locked**; the delegating `repo-rebase` v2, `repo-rebase-native-v1`,
> the external-orchestrator step/monitor and the v1 env bridge are
> **deleted**. The rollback-to-v1 rehearsal path below no longer exists —
> rollback is now `git revert` of the cutover commit. Validation
> evidence: the live vLLM v0.28.0 campaign (plan §5.4; the owner waived
> the formal COMPARISON.md in favor of the campaign record).

**This file is REPO-NEUTRAL** — the process ANY adapter's cutover
follows. Every `<angle-bracket>` placeholder resolves in the adapter's
values file, `adapters/<repo>/doc/RUNBOOK-values.md`, which also holds
that adapter's evidence log (pin SHAs, preflight findings, owner
decisions, freeze tables, timings). Machine-readable repo facts live in
the adapter **manifest** — this doc names the manifest KEY, never a
value. Adapter zero's values file exists at that path under its
adapter directory.

Placeholders used throughout (all resolved in the values file):
`<copilot-root>` · `<external-agent-root>` · `<target-checkout>` ·
`<target-repo-env-keys>` · `<ext1-pin-shas>` ·
`<orchestrator-entrypoint>` · `<branch-override-env>` ·
`<upstream-pin-env>` · `<frozen-upstream-sha>` ·
`<last-rebase-baseline>` · `<knowledge-stores>` · `<gpu-device-set>` ·
`<parent-debug-db>` · `<parent-skills-dir>` · `<parent-state-json>` ·
`<phase1-snapshot-digest>` (recorded on the day) ·
`archival_secret_allowlist` (PR7; may be empty).

## Release campaign startup and resume

Choose the adapter's `upstream.target_branch` and freeze the upstream commit
before starting a campaign. The wheel picker fetches that branch explicitly,
including in scratch clones whose fetch configuration covers only the default
branch. A forced commit must be an ancestor of the fetched target branch;
a main-only commit cannot override a release target. Fetch errors stop the
run rather than using a stale tracking ref.

Record the run id printed at startup. Resume that exact campaign with its
saved identity asserted:

```bash
./infermatrix-copilot --resume <run-id> \
    --repo <name> --playbook repo-rebase-v3 \
    --task-param force_upstream_commit=<frozen-upstream-sha> \
    --task-param last_rebase_commit=<last-rebase-baseline>
```

`--repo`, `--playbook`, and `--task-param` are assertions on resume. A mismatch
blocks before execution and leaves the saved task unchanged. Bare `--resume`
still selects the latest saved task; use an explicit id when several campaigns
share the run directory. Changing the target calls for a new run rather than
reusing another target's checkpoints or completion signals.

Live module prompts take their target branch, upstream pin, assignment
baseline, and target-repository baseline ref from the current run. The
assignment baseline selects work; it does not prove the current tree's API
version or provide test evidence for a new target. The checkout SHA displayed
alongside the pin lets the agent check that its source and runtime agree.

Agent shells and test shells disable Bash startup-file loading so SSH session
metadata cannot shadow the selected runtime's executables. Harness completion
requires a fresh module signal, no typed session/audit refusal, and the required
plan decision from that module's current attempt. Plain completion prose does
not replace these checks.

## External checkout pin (EXT1-class guard)

Before cutover, the EXTERNAL orchestrator's canonical checkout gains a
startup flock guard so external and copilot runs can never share the
target checkout:

- **Canonical external checkout:** `<external-agent-root>`; **pinned
  SHA(s):** `<ext1-pin-shas>` — independently revertible with
  `git revert <ext1-pin-shas>` in that checkout.
- **Guard semantics:** the orchestrator flocks
  `<target-checkout>/locks/<lock_name>.lock` (manifest
  `rebase.lock_name`) after its dry-run exit and before resume
  detection; refuses (exit 3) when a copilot run holds it. Mutual
  exclusion is pinned both directions by
  `test/test_ext1_checkout_guard.py`.
- **Working-tree caveat:** whether the deployed external checkout
  carries uncommitted changes beyond the pin — and therefore whether
  goldens/baselines describe the WORKING TREE rather than the pinned
  commit — is recorded in the values file. Baseline runs for §8 must
  use the deployed truth as-is.

## Remote-CI wiring ops (v3_ci)

`rebase.v3_ci` is live-wired (plan §5 CI-W row, §6.30 divergences).
Operational notes:

- **remote_ci and full modes reach a REAL provider.** The step needs
  the provider token (`BUILDKITE_API_TOKEN` for `ci.provider:
  buildkite`), the adapter's `ci.org` + `rebase.ci.pipeline`
  (build-under-test) + `rebase.ci.baseline_pipeline` (pre-existing-
  failure baseline), and pushes ONLY the adapter-declared
  `push.rebase_branch` as `push.signoff`. `ALLOW_PUSH=1` still gates
  execution (dry-run is FORBIDDEN at phase 4, per the §2.2 matrix).
- **Knobs** (`.env`, neutral): `REBASE_CI_RETRIES`,
  `REBASE_CI_JOB_RETRY_MAX`, `REBASE_CI_POLL_SEC`,
  `REBASE_CI_TIMEOUT_SEC`, `REBASE_CI_SETTLE_SEC` (defaults are the
  parent's; see `config.py`).
- **Ownership safety (supervised-run relevant):** the run refuses to
  push/create while any UNOWNED build is active on the rebase branch —
  pause the CI provider's own schedules for that branch during
  validation (owner decision C in the values file) or expect `refused`
  terminals. Owned = this run's op-recorded builds + webhook artifacts
  of its own pushes; schedule/api builds at the same commit are adopted
  monitor-only.
- **Abort cleanup:** a cancelled/killed run cancels its own op-recorded
  active builds via the lifecycle finalizer; adopted builds are never
  touched. The rollback-inventory row "running CI → cancel op-recorded
  builds" is automatic on abort; manual cancellation stays available
  through the ledger (`<run_dir>/ci_ops/*.json`) + the provider UI.
- **Artifacts:** push WAL at `<run_dir>/push_wal/` (reverse-order
  rollback per the inventory below); build ops at `<run_dir>/ci_ops/`;
  CI job logs at `<run_dir>/ci_logs/`.

## PR6-class preconditions (gate — none may be skipped)

Host-specific findings and their resolutions are logged in the values
file; this is the neutral checklist:

1. **Timed rollback rehearsal** (< 30 min from decision to a running v1
   backend), owner attached — scripted as run-day Phase 2 below; time
   recorded in the values file.
2. **Truthful tier/backend config:** live agent runs need a backend
   that serves the model it claims (the served-model guard refuses
   silent substitution) or an explicit, recorded
   `MODEL_MISMATCH_POLICY=warn` decision. Never run the supervised
   validation on a silently-substituting backend.
3. **v1 rollback path points at the CANONICAL external checkout:**
   `REBASE_AGENT_ROOT` must resolve to `<external-agent-root>` (the
   `Settings.rebase_agent_root` default is a sibling working copy —
   verify, do not assume).
4. **Target venv configured:** the manifest `repo.venv` env var must
   point at the target repo's runtime venv; wheel installs and the
   local test loop BLOCK without it.
5. **DRIFT_TRIAGE has no undecided entries.**
6. **Validation branches** `rebase-val-ext-<date>` /
   `rebase-val-nat-<date>` created; knowledge snapshots taken
   (`backups/<ts>/`).
7. **Supervised v3 full run** (`ALLOW_PUSH=1`, human attached) per plan
   §8; comparison over the MANIFEST-BUILT slug set (DRIFT #4), module
   outcomes mapped through the golden's routing flavor (DRIFT #7).

## PR4d ops — knowledge migration + activation (POST-PR6-validation)

The machinery ships dormant (plan §5.4); Decision 6's sequencing is
unchanged — run this only after the PR6 gate passes:

1. `infermatrix-copilot migrate-knowledge --repo <name> --dry-run` —
   read the report (state dir `MIGRATION_REPORT.md`).
2. `infermatrix-copilot migrate-knowledge --repo <name>` — refuses while
   any run for the repo is live (knowledge run-lock) or the checkout
   flock is held; re-running after a crash redoes safely (journaled).
3. Review + commit the adapter `skills/` git diff (migrated seed skills
   are a deployment artifact, owner-committed).
4. Activate: add the repo to `IMX_KNOWLEDGE_RUNTIME` in `.env`
   (timestamped backup first). Activation is fail-closed: without the
   `MIGRATION_COMPLETE.json` marker the v3 prelude BLOCKS.
5. Remove the adapter's `rebase.knowledge` keys (read-compat retires) —
   HIGH-RISK manifest edit, owner-only.
6. §8 PR4d re-validation gate: 1 clean `report_only` (semantic diffs vs
   pre-PR4d baseline), 1 supervised `local_ci`, and the soak's second
   supervised FULL run — all post-activation.
7. Rollback: drop the repo from `IMX_KNOWLEDGE_RUNTIME` (legacy
   locations were never modified) and, if needed, restore
   `state/<repo>/backups/<ts>/` — rehearse once as part of PR4d
   acceptance (plan §8).

## Rollback inventory (reverse order of application)

| Surface | Rollback |
|---|---|
| Playbook flip (v3 → locked) | revert the PR6 promotion commit |
| `.env` armed block | restore the timestamped backup made at arming |
| Running CI builds | cancel op-recorded builds only (`ci_ops/` records; never a build the run cannot prove it created) |
| Pushed refs | reverse-order over the push WAL; `ABSENT`-recorded branches roll back by lease-protected deletion |
| Validation branches | deleted after the comparison freeze |
| Mutated target checkout | snapshot restore (attempt-scoped) or manual steps from the run's DIAGNOSTICS |
| Upstream | per-run scratch clone — discarded automatically by the run finalizer |
| Runtime knowledge | move `backups/<ts>/` back (non-destructive) |
| External pin | `git revert <ext1-pin-shas>` in `<external-agent-root>` |

**Abort criteria** (any one triggers rollback + investigation): §8
validation gate fails; > 25 % end-to-end wall-clock regression; any
unowned CI mutation observed; a lock leak (post-run lock-dir scan); any
ESCALATION during soak stages 1–2.

## Staged enablement thresholds (plan §8)

1. `report_only` nightly, unsupervised → promote after **3 consecutive
   clean runs** (exit 0, no lock leaks, no ESCALATION.md) with the
   deterministic outputs (manifest, drift scan, assignment preview)
   diffed clean against the frozen baseline (the seed baseline is
   recorded in the values file).
2. `local_ci`, unsupervised (covered by the recorded Decision-1 risk
   acceptance) → promote after **2 consecutive runs** with zero new
   DRIFT/parity items.
3. Push-capable `full`, unsupervised → after **2 supervised clean full
   runs** (the PR6 validation run counts as the first; the second is
   deliberately scheduled post-PR4d).

## Preflight evidence & owner decisions

Recorded PER ADAPTER in the values file: the no-live-consumer evidence,
preconditions resolved with dates, the frozen-SHA table, and the owner
decisions (A: unified validation target world; B: GPU window +
`<gpu-device-set>`; C: provider schedule pause).

## Run day — PR6 validation script (owner-attached)

Sequential, copy-paste blocks; every mutation has its rollback in the
inventory above. Stop at any ✗ — do not improvise past a failed check.
Shorthands (resolve each from the values file):

```bash
export COP=<copilot-root>
export AG=<external-agent-root>
export TGT=<target-checkout>            # decision A: the ONE target world
export UPSTREAM_SHA=<frozen-upstream-sha>
export VAL=$(date -u +%Y%m%d)           # validation-branch suffix
```

### Phase 0 — preflight (T-1 day is fine; ~15 min)

- [ ] Decisions **B** (window + `<gpu-device-set>`) and **C** (provider
      schedules paused) are settled; record them in the values file.
- [ ] `cd $COP && git status --short` → clean; `git log --oneline -1`
      recorded as the copilot SHA under test.
- [ ] `git -C $AG log --oneline -1` → still `<ext1-pin-shas>` head (+
      the deployed working tree per the caveat above — do NOT clean it).
- [ ] `./infermatrix-copilot doctor --probe` → fully green (truthful
      backend; `MODEL_MISMATCH_POLICY` stays `fail`).
- [ ] Locks free: the target checkout's
      `locks/<lock_name>.lock` acquirable; no stray lock dirs.
- [ ] No orchestrator running: `pgrep -af <orchestrator-entrypoint>` →
      empty.
- [ ] Freeze table (in the values file) filled with run-day values:

```bash
git -C $TGT rev-parse HEAD <rebase-branch>     # target start SHA
# last-rebase baseline: <last-rebase-baseline> lookup per values file
```

### Phase 1 — snapshots, branches, .env repoint (~10 min)

```bash
TS=$(date +%Y%m%d-%H%M%S)
# knowledge snapshot (restored before EACH run; non-destructive rollback).
# The debug DB is WAL-mode: a bare `cp -a` can miss committed WAL-only
# rows and re-attach a stale WAL on restore, so the DB goes through the
# sqlite-backup-based snapshot tool; plain files still copy. RECORD the
# printed digest in the values-file freeze table — it is the §8
# opening-identity reference both worlds are checked against.
mkdir -p $AG/backups/$TS
$COP/scripts/knowledge_digest.py snapshot \
    --db <parent-debug-db> --dest $AG/backups/$TS/debug_memory.db
cp -a <parent-skills-dir> $AG/backups/$TS/skills
# the SKILLS digest is Phase-5 gate evidence — record it AT SNAPSHOT
# time (recomputing later would attest a possibly-changed dir)
$COP/scripts/knowledge_digest.py digest --skills $AG/backups/$TS/skills
cp -a <parent-state-json> $AG/backups/$TS/state.json
# validation branches from the frozen start SHA (one per world)
git -C $TGT branch rebase-val-ext-$VAL <rebase-branch>
git -C $TGT branch rebase-val-nat-$VAL <rebase-branch>
# .env repoint (decision A) — timestamped backup FIRST
cd $COP && cp .env .env.backup-$TS
```

- [ ] Edit `$COP/.env`: point `<target-repo-env-keys>` at
      `<target-checkout>` (beware duplicate keys — last one wins, fix
      every occurrence). Leave `ALLOW_PUSH` at 0 (armed per-command in
      Phase 4 only).
- [ ] TEMPORARY manifest edit (HIGH-RISK section, owner-only, reverted
      in Phase 6): set `push.rebase_branch: rebase-val-nat-<VAL>` so
      the v3 run pushes the VALIDATION branch, never the real rebase
      branch. Do not commit.
- [ ] `./infermatrix-copilot doctor` re-run green against the target
      checkout.

### Phase 2 — timed FULL rollback rehearsal (gate item 1; target < 30 min)

Stopwatch starts at the words "roll back", ends when the v1 backend is
observably executing (prelude complete, phase 1 underway):

```bash
date -u +%T   # stopwatch start
cd $COP && ./infermatrix-copilot --playbook repo-rebase-native-v1 --yes \
    --task-param rebase_mode=full
# watch until the v1 prelude completes and phase-1 output appears, then
# Ctrl-C (this rehearses the SWITCH, not a full v1 run)
date -u +%T   # stopwatch stop — record in the values file
```

- [ ] Time recorded in the values file (must be < 30 min).
- [ ] Post-abort: locks free again; no stray processes.

### Phase 3 — external baseline run to phase=done (hours; GPU window per B)

The deployed external orchestrator, deployed working tree, validation
branch, frozen upstream. Its own flock on the target's
`locks/<lock_name>.lock` excludes the copilot for the duration:

```bash
git -C $TGT checkout rebase-val-ext-$VAL && git -C $TGT status --short  # clean
# START evidence, captured IMMEDIATELY before launch (the start head +
# opening digests are Phase-5 gate inputs). The attest→launch window is
# not lock-bridgeable for the ext world (its own EXT1 guard takes the
# flock at startup) — the compensating controls are decision C (provider
# schedules paused), the paused nightly, and the single-operator run-day
# protocol: NOTHING else may be launched between these lines and the
# orchestrator start. Record all three values in the values file.
git -C $TGT rev-parse HEAD          # <ext-start-head>, must == frozen SHA
$COP/scripts/knowledge_digest.py digest --db <parent-debug-db> \
    --skills <parent-skills-dir>
cd $AG && <branch-override-env>=rebase-val-ext-$VAL \
    <upstream-pin-env>=$UPSTREAM_SHA \
    CUDA_VISIBLE_DEVICES=<gpu-device-set> \
    <orchestrator-entrypoint> 2>&1 | tee rebase_logs/val-ext-$VAL.log
# closing attestation (the ext run's own writes are EXPECTED — this is
# attribution data, not a gate condition)
$COP/scripts/knowledge_digest.py digest --db <parent-debug-db> \
    --skills <parent-skills-dir>
```

- [ ] Runs to `phase=done` (check the orchestrator's state file). A
      mid-flight failure here is an EXTERNAL-side failure — investigate,
      do not paper over; the comparison needs a completed baseline.
- [ ] Archive the ext world into `$COP/validation/$VAL/ext/` (state
      file, its built test manifest, latest run dir,
      `git -C $TGT rev-parse HEAD` (= `<ext-post-run-head>`), both
      attestation outputs, and the per-slug results json extracted from
      the ext run's own records as `ext_results.json`
      ({slug: passed|failed} — Phase 5's `--ext-results` input).
- [ ] **Restore for the v3 run — WHILE HOLDING the checkout flock**
      (a "locks free" pre-check alone is a race): take
      `locks/<lock_name>.lock`, then
      `$COP/scripts/knowledge_digest.py restore
      --snapshot $AG/backups/$TS/debug_memory.db --target
      <parent-debug-db>` (removes stale WAL sidecars; printed digest must
      equal the Phase-1 snapshot digest), `cp -a` the skills/state.json
      snapshots back, release the flock. Target back to the frozen start
      (`git -C $TGT checkout rebase-val-nat-$VAL`, clean status); locks
      free; GPUs idle (`nvidia-smi`).

### Phase 4 — supervised v3 full run (human attached, ALLOW_PUSH armed per-command)

```bash
# START evidence for the nat world (same rule as Phase 3): record
# immediately before launch; nothing else may touch the world between
git -C $TGT rev-parse HEAD          # <nat-start-head>, must == frozen SHA
cd $COP && ALLOW_PUSH=1 CUDA_VISIBLE_DEVICES=<gpu-device-set> \
    ./infermatrix-copilot --playbook repo-rebase-v3 --yes \
    --task-param rebase_mode=full \
    --task-param last_rebase_commit=<last-rebase-baseline> \
    --task-param force_upstream_commit=$UPSTREAM_SHA \
    2>&1 | tee validation/$VAL/nat-run.log
```

Watch for (abort → rollback inventory, in order): any push to a branch
other than `rebase-val-nat-$VAL`; any CI mutation without `imx_op_id`
metadata in the trace; a `refused` terminal naming an unowned build
(check decision C's pause actually took); lock-leak or crash. A crash
is resumable (`--resume`) — the WAL/op-ledger recovery is exactly what
the CI-W review rounds hardened.

- [ ] Terminal: exit 0 (`done`) or exit 3 with explained substate —
      record `RUN_REPORT.md`, `metrics.json`, run dir path.
- [ ] Post-run scans: locks free; `push_wal/` all `pushed`/reconciled;
      `ci_ops/` all terminal; no builds left running on the provider.
- [ ] Archive the run dir into `$COP/validation/$VAL/nat/`.

### Phase 5 — comparison + sign-off (gate)

Per plan §8 + DRIFT #4/#7, over the MANIFEST-BUILT slug set. The
evidence assembly is scripted and FAIL-CLOSED — it stamps GATE-ELIGIBLE
from the artifact-carried checks (opening-snapshot identity, nat-run
no-drift, frozen SHAs, slug sets, wall-clock) and marks everything else
HUMAN JUDGMENT PENDING:

```bash
$COP/scripts/compare_validation.py \
    --ext-state $COP/validation/$VAL/ext/state.json \
    --ext-manifest $COP/validation/$VAL/ext/test_manifest.json \
    --nat-run $COP/validation/$VAL/nat/<run-dir> \
    --frozen-target <target-start-sha> --frozen-upstream $UPSTREAM_SHA \
    --snapshot-digest <phase1-snapshot-digest> \
    --snapshot-skills-digest <phase1-skills-digest> \
    --ext-open-digest <phase3-opening-db-digest> \
    --ext-open-skills-digest <phase3-opening-skills-digest> \
    --ext-start-head <ext-pre-run-target-head> \
    --nat-start-head <nat-pre-run-target-head> \
    --ext-head <ext-post-run-target-head> \
    --nat-head <nat-post-run-target-head> \
    --ext-results $COP/validation/$VAL/ext/ext_results.json \
    --routing-golden adapters/vllm_omni/rebase/shell_golden.json \
    --ext-wallclock-sec <ext-sec> --nat-wallclock-sec <nat-sec> \
    --out $COP/validation/$VAL/COMPARISON.md
```

Every flag is REQUIRED gate evidence (missing ⇒ GATE-ELIGIBLE: NO):
the snapshot digests come from the Phase-1 snapshot output, the ext
opening attestations from Phase 3's first `knowledge_digest.py digest`
invocation, the START heads from the `git rev-parse` each of Phase 3/4
records IMMEDIATELY BEFORE launching its run (they must equal the
frozen target SHA — record them in the values file at launch time),
and the post-run heads from each phase's archive step.

- [ ] GATE-ELIGIBLE: YES (any NO reason is an abort-criteria discussion,
      not something to hand-edit away).
- [ ] Slug sets identical (ext vs nat manifest builds).
- [ ] Per-module / per-slug outcomes equal-or-better (nat vs ext), ext
      module names mapped through the golden routing flavor; one re-run
      allowed per flaky divergence, divergences investigated via the v1
      backend — never averaged away.
- [ ] CI verdicts equal-or-better; push/commit shape identical (same
      files staged, same unstage exclusions, signed-off author).
- [ ] Wall-clock: nat ≤ 1.25 × ext end-to-end.
- [ ] `validation/$VAL/COMPARISON.md` written from the checklist above
      and **signed by the owner** (name + date in the file).

### Phase 6 — verdict

**PASS →** in one commit (the PR6 promotion): flip v3 → locked
`repo-rebase`, retire the delegating yaml, update `CLAUDE.md`'s locked-
playbook rule, revert the temporary `push.rebase_branch` edit; then arm
`.env` deliberately (new timestamped backup; `--dry-run` removal per
plan §9); unpause the provider schedules; delete the validation
branches after the comparison freeze; enter staged soak stage 1
(`report_only` nightly).

**FAIL / ABORT →** rollback inventory top-to-bottom (restore
`.env.backup-$TS`, revert the manifest edit, cancel op-recorded builds,
WAL-reverse pushed refs, restore knowledge snapshot, checkout restore),
then investigate via the v1 backend. The validation branches are the
evidence — keep them until the investigation closes.
