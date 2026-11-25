# app/request_policy.py — request safety policy

<!-- verified-against: 2026-11-25 -->

This headless application module validates untrusted read-only requests for
the SDK, MCP transport, and reserved-run worker. It derives a new `TaskSpec`
from raw input at reservation and repeats validation in the child after
reading `request.json`; persisted input alone is not an authority.

`enforce_mcp_policy` permits only imported `READ_ONLY_KINDS`, forces
`post=False`, limits repositories to the configured allowlist, checks positive
PR/issue numbers, validates full expected-head SHAs, and strips unknown
params. A bounded `deterministic_signals` list applies only to quality review;
carried findings are validated for PR review. PR review accepts boolean
`include_score`. False and omission have the same idempotency fingerprint. `authorize_repo_path` verifies
both checkout origin identity and containment under allowed roots.

`enforce_strict_review_policy` accepts only PR review, forces eco mode, and
rejects explicit posting. `enforce_quality_review_policy` accepts only PR
quality, forces eco mode, and rejects posting. Neither path can create an
outward write. Policy errors are `PolicyError`; the module does not execute a
run, call a model, or access knowledge. It imports only task-specification
primitives at module load; repository identity and carried-finding helpers
are loaded inside the relevant validation path.

The old `infermatrix_copilot.mcp_policy` module re-exports these callables for
compatibility. Keep policy changes here so the application and transports
cannot diverge. `test_mcp.py`, `test_sdk_v1.py`, and `test_contract.py` cover
the read-only, snapshot, identity, and compatibility behavior.
