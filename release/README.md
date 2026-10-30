# Managed releases

The distribution version comes from `src/infermatrix_copilot/__init__.py`.
`scripts/build_copilot_release.py` reads an immutable Git commit, builds its wheel,
and resolves the `mcp`, `kb`, and `updater` extras into one universal, pinned
requirements file with SHA-256 hashes. Runtime dependencies must have binary
distributions on the target platform; installation does not execute dependency
source builds. The bundle contains exactly `requirements.txt` and
`wheel/<wheel-name>.whl`. It never contains a local checkout or `.env` file.
Host skill workflows, authoring documentation and tools are packaged in the wheel.
Plugin metadata is normalized to the distribution version inside the temporary
release snapshot; local plugin files are not rewritten.

## Provision the trust root before the first release

`trusted-keys.json` intentionally starts with an empty `keys` map. Automatic
installation remains unavailable until a maintainer provisions a production
signing key. Generate an Ed25519 key using the organization's secret-management
process; do not commit its private bytes.

1. Add the base64-encoded **32 raw public bytes** to `trusted-keys.json` under a
   stable key ID: `{"schema":1,"keys":{"release-2026":"BASE64_PUBLIC_BYTES"}}`.
2. Store the base64-encoded **32 raw private bytes** in the GitHub Actions secret
   `COPILOT_RELEASE_SIGNING_KEY`, and the key ID in repository variable
   `COPILOT_RELEASE_KEY_ID`.
3. Commit the public key before publishing or distributing a managed launcher.
   The builder rejects a signing key whose public half is absent from that
   committed snapshot.

Key rotation needs an overlap release that trusts both old and new public keys.
Distribute that release while still signing with the old key. After its health
check and activation, the updater adopts the complete trust map from the
authenticated wheel and stages its compatible recovery launcher. Then switch the
signing secret and ID. A subsequent release signed with the new key can omit the
old key to revoke it. Keep the overlap release available long enough for existing
installations to adopt the new trust root; users who skip that release must
explicitly install the overlap version or rerun the installer. Existing key IDs
cannot change their public bytes while still present. Rollback restores the
previous release's application, recovery launcher,
and trust snapshot together. A downloaded manifest never grants itself trust.
Releases requiring a newer launcher protocol (`min_launcher_version`) require an
installer migration first.

## Publish

Bump the single version source, commit, and push a matching `v<version>` tag.
The release workflow runs a clean-install startup check, publishes immutable
assets under that tag, then replaces the selected channel's signed JSON pointer.
Development/prerelease versions automatically select `preview`. A manual workflow
run can also publish the selected commit; an existing release is never overwritten.

Stable feed:
`https://github.com/JiusiServe/InferMatrixCopilot/releases/download/auto-update-stable/copilot-stable.json`

Preview feed:
`https://github.com/JiusiServe/InferMatrixCopilot/releases/download/auto-update-preview/copilot-preview.json`

The workflow run number is the signed monotonic `sequence`; keep this workflow
identity when moving publication automation, or initialize the replacement above
the largest sequence already published. Concurrent publication is serialized.
GitHub's current version download URLs and mutable feeds are never trusted without
signature and hash verification.

For a local build after provisioning a nonproduction key in a test repository:

```sh
python scripts/build_copilot_release.py \
  --source /path/to/repository --commit HEAD --sequence 1 --key-id test-key \
  --artifact-base-url https://example.test/releases/v0.2.0 --output /tmp/release
```

The private key is read from `COPILOT_RELEASE_SIGNING_KEY`; it is not a command-line
argument. Dirty and untracked files are deliberately ignored. Local changes must
be committed in the test repository to become part of a build.

## Wire format

The envelope is `{ "manifest": {...}, "key_id": "...", "signature": "..." }`.
The signature is Ed25519 over UTF-8 canonical JSON of **only** the manifest, using
`json.dumps(manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=False)`.
The signature and raw public keys use standard base64. The manifest schema is in
`manifest.schema.json`. Its `artifacts.bundle` binds the exact compressed byte
size and SHA-256; `artifacts.wheel` binds the relative wheel path and SHA-256 after
safe extraction. Python, launcher, runtime, workflow protocol, and knowledge format
compatibility must pass before activation.
