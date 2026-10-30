---
name: imupdate
description: Resolve an upstream repository from a local path, repository name, alias, or URL, then update InferMatrixCopilot's release-driven knowledge. Use when the user invokes /imupdate or $imupdate, asks to sync knowledge after an upstream release, compare vLLM-Omni tags or SHAs, or refresh the release baseline, model catalog, source maps, and manifests.
---

# InferMatrix update

```text
/imupdate <path-or-repository> [target-tag-or-sha]
```

The installed InferMatrixCopilot root is:

```text
{{INFERMATRIX_COPILOT_ROOT}}
```

If that placeholder was not replaced, locate or temporarily clone the
InferMatrixCopilot checkout containing `tools/audit_vllm_omni_release.py`.

## Select the input mode

### Local Git path

When the argument exists and is a Git checkout, use the deterministic audit.
Read `adapters/vllm_omni/release_baseline.yaml`; use
`upstream.audited_sha` as the old revision and the optional target or checkout
`HEAD` as the new revision. Refuse an accidental downgrade when the target is
older than the audited revision. Do not fetch or checkout unless explicitly
asked.

### Repository name, alias, or URL

When the argument is not a local path, use the host model to resolve it:

1. Treat `vllm-omni`, `vllmomni`, `vllm omni`, and
   `vllm-project/vllm-omni` as `https://github.com/vllm-project/vllm-omni`.
2. Check the configured repo map and current workspace for a matching checkout.
   If found, continue as the local-path mode.
3. Otherwise verify the canonical repository from authoritative GitHub
   metadata. Choose the supplied target, otherwise the newest release or tag
   that is not older than the audited revision, otherwise the default-branch
   HEAD; state that choice. Never silently downgrade because GitHub's "latest
   release" can exclude newer prereleases.
4. Prefer a temporary clone and run the deterministic audit. If repository
   access or cloning is unavailable, inspect the authoritative source with the
   host model, update only source-backed facts, and clearly report that machine
   audit/enforce validation was unavailable. Never call that result `CLEAN`.
5. Ask for clarification only when multiple plausible canonical repositories
   remain after resolution.

## Audit and update

1. Resolve old and new revisions to full SHAs.
2. Create a temporary JSON path and run the read-only audit in report mode when
   a local or temporary Git checkout is available:

   ```text
   python <infermatrix-root>/tools/audit_vllm_omni_release.py \
     --from <audited-sha> \
     --to <target-sha> \
     --repo <upstream-repo> \
     --mode report-only \
     --json-output <temporary-json>
   ```

3. Explain the registry, pipeline, deploy, path-routing, source, and pin drift.
   Read the relevant upstream diff before changing InferMatrixCopilot.
4. Update only facts proven by the report and source: the release baseline,
   model catalog, component source maps, pins, or adapter manifest. Never
   generate or rewrite owner rules automatically as part of the structural update. Send
   reusable semantic lessons through the separate task completion learning
   workflow and its reviewable knowledge contribution.
5. When machine audit is available, run the same command with `--mode enforce`,
   then run:

   ```text
   python knowledge/tools/check_knowledge_tree.py
   python knowledge/tools/check_wiki_lint.py
   python -m pytest test/test_release_audit.py test/test_adapters.py \
     test/test_routing.py test/test_knowledge_source.py \
     test/test_skills_scope.py test/test_capabilities.py -q
   ```

6. Delete temporary reports and clones. Return the canonical repository, input
   mode, old and new SHAs, structural deltas, files changed, validation
   results, and any unresolved or model-only findings.

The audit is evidence collection, not the updater. Codex performs the bounded
knowledge edits and must not commit, push, or open a PR unless asked.


## Task completion learning

Before your final task response, FIRST call `knowledge_agreement(action="status")`.
Only `status="accepted"` permits [task completion learning](../imlearn/references/task-completion.md)
to distill lessons, grade knowledge or upload feedback. Otherwise skip learning,
finish the requested task, and offer the actual agreement and human signing
command once without per-task reminders. Never sign for the user. Accepted
agreement and configured publication/write gates are both required; operator
configuration is not consent. This does not authorize publishing the task's code,
comments or RFC.
