# Current-main conflict resolution — 2026-10-20

Merged upstream `6a6d87fb0bab609552e6907c1f747f764a0211ff` into the framework branch.

## Resolution

- Keep the lazy SDK and neutral contracts; add the upstream RFC public exports.
- Keep one shared process supervisor for ZCode and preserve upstream typed model failures for both streaming and nonstreaming calls. Adapt the new upstream failure tests to mock the canonical runner.
- Package both the knowledge resources and upstream RFC docs/host skills.
- Move the upstream RFC client implementation into `rfc_service.client`; CLI and MCP use it directly, while `sdk.v1.rfc` re-exports the same classes. The existing boundary guard remains enforced.
- Preserve upstream RFC features and knowledge changes. No production release pin, deployment or model-call allowance changed.

## Validation of the merged tree

- ZCode streaming/spooling, provider, sandbox, SDK, MCP and ownership/registration suites: 162 passed.
- All RFC tests plus ownership, registration and shared process tests: 365 passed, 1 collection skip (Playwright unavailable). Includes shared RFC client identity and cold-import checks.
- ReviewBot paired consumer checks against the newly installed SDK: 145 passed (Direct/Strict, local/remote hosts, provider boundary, knowledge SDK and immutable test-pin selection).
- Wheel build and installation passed. Outside the checkout, confirmed shared RFC/Review exports and presence of RFC web resources, host skill, RFC guide, knowledge schema and knowledge docs. Third-party installed dependencies were reused; no source-path injection was used for this wheel probe or paired SDK.

Evidence: `output/single-capability-completion-20261012/conflict-resolution-20261020/` (local artifacts). Initial failures and the initial missing build-module attempt remain recorded separately; final XML files record the successful reruns.

These are affected-scope integration checks, not a new full paired-suite or real-model quality claim. Historical full-suite results retain their original snapshot scope. Browser acceptance, real-model comparisons, other native vendors and release/rollout gates remain follow-up work.

## Portable documentation references

Historical reports retain their original host-local artifact paths as inline code, rather than repository hyperlinks. These artifacts are not bundled in Git or available to a clean CI checkout; their paths record where the original evidence was retained, not a public download or a new verification result. Repository documents continue to use relative links. The broken model-generated `RevisionCache` link quoted in the Foundation report is preserved as text with its original target. The link checker is unchanged.
