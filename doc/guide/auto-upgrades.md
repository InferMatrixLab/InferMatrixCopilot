# Automatic upgrades

The normal installer creates a managed installation. MCP hosts and the CLI use
an independent launcher under `~/.infermatrix-copilot`, and each application
release has its own immutable environment. A running session continues with the
release it started with; a verified update becomes available to future sessions.

## Install or migrate

From a trusted source checkout:

```bash
./install-mcp.sh
# Select a host explicitly when detection is insufficient:
./install-mcp.sh --agent codex --agent cursor
# Install just the managed CLI:
bash install.sh
```

On Windows, run `install.cmd`. The installer prints the managed CLI path. Add
its `bin` directory to your PATH if you want to invoke `infermatrix-copilot`
without its full path. `bash install.sh` also creates an owned
`./infermatrix-copilot` wrapper for existing checkout-based commands. The
installer does not change PATH. Locally modified wrappers are preserved.

Useful installer options:

| Option | Behavior |
| --- | --- |
| `--dry-run` | Report the selected hosts and paths without downloads or writes. |
| `--config-root PATH` | Use `PATH/.infermatrix-copilot` and host directories under `PATH`; an explicit `CODEX_HOME` still selects Codex configuration. |
| `--channel stable` | Follow compatible stable releases (the default). |
| `--channel preview` | Follow the preview release feed. |
| `--public-key PATH` | Merge a schema-1 JSON map of trusted Ed25519 public keys. |
| `--repo-path PATH` | Record the checkout used by Strict mode. |
| `--local-cli-wrapper PATH` | Also create an owned CLI wrapper at the specified path. |
| `--development` | Retain the legacy `uvx` runtime and full copied skills instead of managed upgrades. |

`install.sh --development` retains the editable checkout CLI installer.
`install.sh --uninstall` removes only artifacts recorded by that legacy
installer; it leaves managed installations and host registrations in place.

Existing MCP entries are updated in place. Environment variables, host settings,
other servers, project scopes, and model credentials are preserved. JSON host
configuration receives a backup before changes. Restart the host after migration
so that it reloads the server registration and installed skills. Re-running the
installer also repairs or updates the independently tested recovery launcher
without replacing the active application release.

Managed skills retain their discovery metadata and assets, but load their
instructions with `get_workflow(skill_name, skill_protocol=1)` from the connected
server. This keeps the instructions aligned with the session's runtime. Updating
repository knowledge through `/imupdate` remains a separate operation and uses
the writable authoring checkout rather than the immutable application release.

The installer records file ownership and content hashes in `install.json`.
It adopts a legacy copied skill only when the contents exactly match the known
source. It refuses to overwrite unowned or locally changed files and reports
those paths. Move customized files aside or merge them before retrying. Unknown
files are preserved; obsolete owned files are removed only if unmodified.

## Updating and recovery

The launcher checks for a release in the background when an update check is due,
including on launch and during long running sessions. The default interval is six
hours. Edit `check_interval` (seconds, at least 60) in
`~/.infermatrix-copilot/updates.toml` to change it. The same file records the
selected channel and automatic update policy.
Offline startup uses the last verified release. Candidates are signature and
hash verified, installed in isolation, and self-tested before activation. A
shared update lock prevents multiple hosts from installing the same candidate
concurrently. Failed candidates leave the working release available.

Use the managed CLI to inspect and control upgrades:

```bash
infermatrix-copilot upgrade status
infermatrix-copilot upgrade --check
infermatrix-copilot upgrade
infermatrix-copilot upgrade rollback
infermatrix-copilot upgrade pin <version>
infermatrix-copilot upgrade unpin
infermatrix-copilot upgrade --channel preview
infermatrix-copilot upgrade --auto off
infermatrix-copilot upgrade --auto on
```

Pinned runs record the physical interpreter and release identity. Resuming a run
uses that original release, and operations against the same checkout and PR
share an execution lock across server versions. An application startup failure
can roll activation back; the launcher never replays an interrupted user task.
Releases are retained automatically so live and resumable runs keep their
original runtime. This implementation does not automatically remove old
application releases.

## Release publishing

Automatic upgrades require a published signed release feed and a trusted public
key. Source-checkout installation provides the initial trusted baseline; it does
not establish trust in an unsigned future download. Keep the signing private key
in release CI secrets and distribute the public key through the trusted
installer or `--public-key`. The key file uses this format, with each value a
base64 encoding of a 32-byte Ed25519 public key:

```json
{
  "schema": 1,
  "keys": {"publisher-2026": "BASE64_ENCODED_32_BYTE_PUBLIC_KEY"}
}
```

Trust maps are merged while preserving existing keys. A different key must use a
new identifier. The checked-in trust map starts empty; release operators must
configure their public key before automatic downloads can be trusted.

A release contains the wheel and bundled runtime resources, skill workflows,
knowledge, adapters, playbooks, and required support tools. Its manifest records
immutable source identity, artifact hashes, compatibility requirements, and the
release channel. Release CI must build and validate these artifacts before
publishing the signed feed. Releases requiring an incompatible state migration
must not be activated automatically.
