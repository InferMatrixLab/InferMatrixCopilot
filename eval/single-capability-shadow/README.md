# Frozen shadow evaluation

Freeze the maintained helpers, installed products, inputs, private labels,
provider profiles and call allowances before dispatch. A changed helper requires
a new manifest; never update an original phase's copied helpers or reports.

## Init stage report version 2

`init_native_arm.py` emits `init-stage-report/2` on stdout and writes
`init-stage-artifacts/2` to the stage JSON file. These are evaluation formats;
public product SDK, CLI and durable workflow contracts are unchanged.

`gateway_attempts` counts recorded gateway observations, including failures
before native dispatch. The report's `gateway_attempts_scope` identifies which
roles are measured. Normal native workflows measure generation and judgment;
scripted correction measures only judge attempts and reports scripted generation
separately. Correction reports use `first_judge_attempts` and
`correction_judge_attempts`, including pre-dispatch refusals.

The old `native_calls`, `first_judge_calls` and `correction_judge_calls` counters
are no longer emitted by this helper. Frozen earlier reports retain their
original fields and hashes. Those historical counters must not be used as proof
of actual native dispatch: a refused fifth gateway attempt can coexist with
only four sent/completed calls.

The native call ledger and original native attempt facts remain authoritative
for dispatch/completion, admission and reconciliation. A packet or recorder row
alone is not evidence that a provider was invoked; absent facts remain unknown.
This report does not settle budgets, release allowances or authorize retries.
Model quality and publication eligibility remain separate checks.

## Whole-workflow deadlines

New quality admissions using the shared init driver must declare an explicit workflow_timeout_s that covers native_call_limits[case] * host_timeout_s +30 seconds. Each init phase (or standalone init quality stage) must additionally allow one host_timeout_s for preparation. A multi-phase outer stage must cover the sum of all serial phase timeouts plus30 seconds for its final receipt. These conservative serial bounds do not establish model quality, actual latency or account entitlement. The7200-second workflow/stage ceiling is unchanged: a longer scope must be split and frozen appropriately or use a separately reviewed protocol, never silently truncate work.

The existing Direct/Strict workflow policy remains. Original frozen helpers, manifests and recovery reads keep their original identities; new deadline admission checks do not redispatch or modify existing stages. A larger declared call allowance still needs its own accepted allocation; this check neither expands nor releases evaluation budgets.


## Worker startup provenance

For new protocols, `durable_worker.py` writes one immutable entry per startup in
`evaluation-workers/<startup_id>.json`. `evaluation-worker.json` remains the
immutable first-entry compatibility record; it does not identify the current
executor after queued adoption. Match startup entries to the SDK
`run_status.json` child PID when auditing an executing or completed run.

The SDK run lock and atomic execution claim own execution admission. Evaluation
startup metadata must not add a separate claim or prevent recovery of a queued
run whose first wrapper exited before the product started. Native-call ledgers
and original attempt receipts still govern model accounting and unknown outcomes.
This metadata change does not authorize a new paid run or expand an allowance.
Keep old frozen helpers and their startup files unchanged.
