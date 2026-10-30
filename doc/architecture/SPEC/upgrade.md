# upgrade/ — managed application releases

<!-- verified-against: 2026-10-30 -->

`Application distribution and recovery boundary · refactor-status: ok`

## Responsibility and public contract

Install once and prepare compatible, authenticated application updates in the
background. New sessions select the active immutable release; running sessions,
workers and resumed runs retain their original interpreter and resources.
The embedded SDK never starts an updater or changes its host environment.

`infermatrix-copilot upgrade` provides status, availability checks, installation,
rollback, pin/unpin, channel selection and automatic-update controls. The stable
launcher independently exposes those controls when the application cannot start.
See [the usage guide](../../guide/auto-upgrades.md) and
[release publication](../../../release/README.md).

## Modules

| Module | Contract |
| --- | --- |
| `storage.py` | Atomic, fsynced writes; validated release identities; OS-released POSIX/Windows locks. Only lock contention is retried. |
| `releases.py` | Bounded HTTPS/local transport; origin-scoped GitHub credentials; Ed25519 manifest verification; compatibility and monotonic feed checks; link-free, bounded archive extraction. |
| `manager.py` | Validated policy, isolated candidate preparation and health, quarantine/backoff, atomic activation snapshots, authenticated trust rotation and rollback. |
| `bootstrap.py` | User-authorized initial installation and recovery repair; independent launcher environment; immutable versioned recovery zipapps; explicit trust provisioning. |
| `launcher.py` | Select and re-exec authoritative recovery code before input; probe startup; supervise exactly one user process; schedule optional background checks; never replay a submitted task. |
| `cli.py` | Model-free control commands with an explicit optional managed home. Status is read-only. |
| `health.py` | Credential-free, isolated metadata/resource, CLI and actual MCP initialization/workflow/status checks. No model calls or repository tasks. |
| `runtime.py` | Durable per-run release pins, original-runtime delegation, and worker-held cross-process checkout/PR execution exclusion. |
| `workflows.py` | Read-only protocol-1 workflow resolution; installed code and support scripts operate against a separately selected writable authoring workspace. Tool bindings come from release metadata. |

## Invariants

- `active.json` is the single activation commit point for the application,
  recovery zipapp and public trust map. Its previous snapshot is restored as a
  unit. Files referenced by either snapshot are prepared before publication.
- Candidates use a fresh environment at its final physical path; moving a venv
  or resolving its Python symlink would invalidate interpreter identity.
- Signed bundle bytes, size and wheel digest are checked before installation.
  Dependencies use a hashed universal lock and binary distributions. Application
  and independent recovery health must pass before activation.
- Candidate trust updates are authenticated by the existing trust root. New key
  material uses a new key ID; signed releases may retire old IDs. An unsigned
  feed or downloaded manifest cannot establish its own trust.
- Failed candidates retain the working release. Startup recovery happens before
  task input; an application's task exit status never triggers automatic replay.
- Configuration, learned state and authoring workspaces remain outside immutable
  release directories. Managed memory writes use stable per-repository state.
- Existing sessions are never hot-reloaded. Releases requiring another runtime
  schema, skill protocol, knowledge format or launcher protocol need maintenance.
- Protocol-1 host skill wrappers fetch full instructions from their session's
  MCP runtime. Migration of previously cached full skills requires a host reload.
- Release directories are retained; no garbage collector removes a live,
  queued or resumable runtime implicitly.

## Dependencies and boundaries

Storage, transport and bootstrap use the standard library. Signature verification
and version constraints import updater extras only at their execution boundary.
The recovery zipapp is independent of application dependencies. Repository-specific
support-tool bindings belong in `release/workflow-tools.json`, not the core.

Updater code does not publish releases, create repository changes, migrate
knowledge formats or manage a caller's editable/SDK environment. The release
builder and CI are separate publisher entrypoints. Production publication needs
a provisioned public trust root and CI signing secret.

## Validation

`test_upgrade_{manager,releases,bootstrap,runtime,workflows}.py`,
`test_copilot_release.py`, installer tests, and existing CLI/MCP/SDK, environment
purity, repository-neutrality and knowledge-path tests exercise these contracts.
Real clean-wheel, signed local-feed, recovery repair and rollback smoke checks
use temporary homes; Windows lock/launcher tests are also registered in CI.
