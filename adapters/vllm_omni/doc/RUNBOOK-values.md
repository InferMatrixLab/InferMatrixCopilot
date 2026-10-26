# RUNBOOK values — adapter zero (vllm-omni)

Current boundary (2026-10-14): the user retired the external rebase agent.
Copilot's shipped adapter runs its internal engine and uses its own knowledge
stores; no external checkout, EXT1 pin, parent data layer or external rollback
run is required. `REBASE_AGENT_ROOT` is deprecated and unset by default; an
existing explicit value is only available to custom legacy data imports.
The deployment comparisons and machine paths below are historical evidence,
not current preconditions. Keep internal checkout exclusion and bundled prompt
goldens. Never replay historical paid runs or alter their original ledgers to
satisfy a current gate.

The repo-specific companion to the neutral `doc/RUNBOOK-rebase.md`
template: placeholder resolutions, evidence log, owner decisions, and
run-day fill-in tables for THIS adapter's cutover. Machine-readable
facts stay in `manifest.yaml`; this file holds operational values and
history only.

## Placeholder resolutions

| Placeholder | Value |
|---|---|
| `<copilot-root>` | `/data/zhoutaichang/copilot/InferMatrixCopilot` |
| `<external-agent-root>` | `/data/zhoutaichang/rebase/vllm-omni-rebase-agent` |
| `<target-checkout>` | `/data/zhoutaichang/rebase/vllm-omni` (decision A — the deployed checkout) |
| `<target-repo-env-keys>` | `VLLM_OMNI_REPO` **and every** `REPO_PATHS` line (`{"vllm-omni": "<target-checkout>"}`); `.env` currently carries duplicates — last wins, fix all |
| `<rebase-branch>` | `dev/vllm-align` (= manifest `push.rebase_branch`) |
| `<lock_name>` | `omni` (manifest `rebase.lock_name`) |
| `<ext1-pin-shas>` | `0395bbe 353b0a6 8ffcc6c 634b002 71222e8 015344d` |
| `<orchestrator-entrypoint>` | `/data/zhoutaichang/rebase/.venv/bin/omni-rebase-orchestrator` (cwd = `<external-agent-root>`) |
| `<branch-override-env>` | `REBASE_BRANCH` |
| `<upstream-pin-env>` | `FORCE_VLLM_COMMIT` |
| `<frozen-upstream-sha>` | `ffd46bfab2128bb84146050e98b51a617c6575ab` |
| `<last-rebase-baseline>` | effective `LAST_REBASE_VLLM_COMMIT` from `<external-agent-root>/config.sh` (default `d4004455d2357985830af10e432709b42c820455`) — `grep -n LAST_REBASE_VLLM_COMMIT config.sh` on run day |
| `<knowledge-stores>` | `$AG/agent/store/debug_memory.db  $AG/agent/skills  $AG/rebase_logs/state.json` |
| `<parent-debug-db>` | `$AG/agent/store/debug_memory.db` (WAL-mode — snapshot via `scripts/knowledge_digest.py`, never bare `cp`) |
| `<parent-skills-dir>` | `$AG/agent/skills` |
| `<parent-state-json>` | `$AG/rebase_logs/state.json` |
| `archival_secret_allowlist` | (empty — no tracked secret-bearing files known; PR7 archival aborts if one appears) |
| `<gpu-device-set>` | `4,5,6,7` (decision B; contention fallback `4,5` — same set for BOTH runs) |
| CI pipelines | manifest `rebase.ci.pipeline: vllm-omni-release`, `rebase.ci.baseline_pipeline: vllm-omni-rebase`, `ci.org: vllm` |
| Push signoff | manifest `push.signoff`: tzhouam <tzhouam@connect.ust.hk> |

## EXT1 pin — evidence (DONE, 2026-08-01)

- Pinned SHA `0395bbe` = six commits (guard module + orchestrator hook
  + fail-closed root-anchored symlink-hostile atomic hygiene shield +
  baseline-under-lock ordering + errno-precise diagnostics + validated
  gitdir + no checkout fabrication).
- **Working-tree caveat (applies to §8 baselines):** the deployed
  checkout carries UNCOMMITTED owner changes (config.sh/§10 maps,
  config.py, debug_memory_store.py, test_watchdog.sh, skills, task
  scripts, nightly_rebase_cron.sh, …). The PR5 `shell_golden.json` was
  captured from this WORKING TREE (the deployed truth), so the golden
  describes live behavior, not the pinned commit's. Any comparison run
  from a fresh clone of the pinned SHA will differ from the deployed
  instance — baseline runs must use the deployed working tree as-is.

## PR6 preflight — evidence (2026-08-01, session-executed)

### No-live-consumer evidence (plan §1 re-verification: CONFIRMED)

1. **Scheduler scan**: root's crontab has exactly ONE entry — `0 16 * * *
   /rebase/nightly_local.sh` — a nightly ACCURACY/PERF CI over a third
   checkout (`/rebase/vllm-omni`), NOT the rebase orchestrator. Nothing
   schedules the orchestrator: `nightly_rebase_cron.sh` (designed for
   18:30 UTC) is present but NOT installed in any crontab/timer. No
   orchestrator process is running; the parent's `state.json` is the
   known Jul-25 mid-flight recording (`phase=local_testing`).
2. **Buildkite, 30 days** (`vllm/vllm-omni-rebase`, read-only API query):
   100 builds, ALL attributed — the pipeline's OWN daily schedules
   ("Scheduled build" on `main`, "Scheduled nightly build - Align" on
   `dev/vllm-align`), the owner's manual API builds (Jul 27–28 v0.26.0
   session), and webhook builds. ZERO unattributed; zero
   `imx_op_id`-stamped builds (expected — the copilot has never pushed).

### Preconditions resolved (with dates)

- **#2 tier config (2026-08-01)**: `AGENT_MODEL="claude-sonnet-5"` was
  untruthful on `api.deepseek.com` (silently served `deepseek-v4-flash`;
  the served-model guard refused). Fixed to `deepseek-v4-pro[1m]`;
  `doctor --probe` verifies the eco tier round-trips truthfully. `.env`
  backup: `.env.backup-20260801-155809`.
- **#3 v1 rollback path**: `REBASE_AGENT_ROOT` points at the canonical
  checkout in `.env` — satisfied. (A STALE sibling working copy exists
  at `copilot/vllm-omni-rebase-agent` with no EXT1, and the settings
  default points there — the env override is what makes v1 runs import
  the EXT1-guarded code.)
- **#4 target venv**: `VLLM_OMNI_VENV=/data/zhoutaichang/rebase/.venv`
  (the parent's `REBASE_VENV`).
- **#5 DRIFT_TRIAGE**: all decided — #1 flavors frozen, #4
  manifest-built slug set, #6 map flavors, #7 declared-vs-computed
  mapping.
- **Doctor**: fully green (deps/env/gh/repos/backends/playbooks/moa/
  probe). **Lock-leak scan**: both vllm-omni checkouts' `locks/omni.lock`
  FREE. **Mechanical rollback rehearsal (timed): 5 s** to
  (v1 playbook resolved via `--plan-only`) + (external orchestrator
  `--dry-run` exit 0); the FULL timed rehearsal is run-day Phase 2.

### Frozen-SHA candidates for the §8 comparison

| World | SHA | Branch |
|---|---|---|
| deployed target `rebase/vllm-omni` | `05d0926e` | dev/vllm-align |
| copilot target `copilot/vllm-omni` | `a61d5c0f` | main (NOT the validation world per decision A) |
| upstream `rebase/vllm` | `ffd46bfab2` | (detached — parent wheel pin) |
| external agent | `0395bbe` | main (= EXT1 pin) |

## Owner decisions

- **A. Validation target world — DECIDED (2026-08-01, per
  recommendation)**: both validation runs operate on the DEPLOYED
  checkout `/data/zhoutaichang/rebase/vllm-omni` (dev/vllm-align).
  Run-day Phase 1 repoints `<target-repo-env-keys>` for the window
  (timestamped `.env` backup first) and Phase 6 restores/supersedes.
  EXT1's per-checkout flock covers both runs on the same world — the
  mutual exclusion it was built for.
- **B. GPU window — DECIDED (owner, 2026-08-01): 03:00 CST start.**
  Clock facts (verified): cron runs on `/etc/localtime` = **UTC**, so
  the perf-CI crontab `0 16 * * *` fires at 16:00 UTC = **00:00 CST**
  (it seizes all GPUs and kills GPU compute procs after a 1-hour wait;
  its tail can run past 03:00 CST). Window: **03:00 → 23:30 CST**
  (= 19:00 → 15:30 UTC), hard stop 23:30 CST — nothing of ours may be
  on GPUs at midnight CST. Two window rules: (1) at 03:00 CST confirm
  `nvidia-smi` shows the nightly's tail is done before starting; (2) if
  ext + v3 don't both fit one window, run Phase 3 (ext) in day-1's
  window and Phase 4 (v3) in day-2's — the Phase-3 restore step already
  reseeds the world between them.
  **`<gpu-device-set>` = `4,5,6,7`** — GPU 0 carries a resident ~139 GB
  allocation (avoid); 4 visible GPUs let the `gpu_4_queue` jobs
  actually EXECUTE (not hw-skip) in BOTH runs, maximizing §8 signal.
  Fallback under run-day contention: drop to `4,5` (the deployed cron
  precedent) — but the SAME set for both runs is the invariant, never
  mixed.
- **C. Buildkite schedule race — DECIDED (owner, 2026-08-01): pause
  both schedules for the validation window.** Pipeline
  `vllm/vllm-omni-rebase` → Settings → Schedules: pause "Scheduled
  build" (main) and "Scheduled nightly build - Align" (dev/vllm-align)
  in Phase 0; re-enable in Phase 6. Scope note (precise, post-CI-W):
  the v3 run pushes `rebase-val-nat-<date>`, a branch the schedules
  never build, so the ownership refusal is NOT the main exposure here —
  the exposure is comparison stability: a scheduled main build landing
  between the ext and nat runs would CHANGE the pre-existing-failure
  baseline (`rebase.ci.baseline_pipeline` main-branch query) mid-
  comparison, plus GPU/queue capacity contention. Pausing removes both.

## Run-day fill-ins (write values here on the day)

| Item | Value |
|---|---|
| Run date (window: 03:00–23:30 CST per decision B) | ☐ |
| 03:00 CST GPU-idle check passed (nightly tail done) | ☐ |
| Buildkite schedules paused (decision C, who/when) | ☐ |
| Copilot SHA under test | ☐ |
| Target start SHA (`git -C $TGT rev-parse dev/vllm-align`) | ☐ |
| Effective `LAST_REBASE_VLLM_COMMIT` | ☐ |
| `.env` backup name (Phase 1) | ☐ |
| Knowledge snapshot dir (`backups/<ts>/`) | ☐ |
| Phase-1 snapshot digest (`knowledge_digest.py snapshot` output) | ☐ |
| Ext run open/close attestations recorded | ☐ |
| Rollback rehearsal time (Phase 2, < 30 min) | ☐ |
| Ext run: state=done timestamp + target HEAD | ☐ |
| Nat run: run dir + exit + target HEAD | ☐ |
| COMPARISON.md signed (owner, date) | ☐ |

## Staged-enablement seed baseline

Stage 1's deterministic-output diffs compare against the frozen
2026-08-01 report-only dry run over the live checkout: **54 jobs / 310
test changes, done/exit-0** (plan §5 PR5 row).
