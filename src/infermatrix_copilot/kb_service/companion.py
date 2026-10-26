"""Companion PRs: update references outside knowledge/ before a retirement lands.

When a knowledge change retires, replaces or purges rules that files outside
``knowledge/`` still cite (skills, plugins, adapters, docs, playbooks), the
gate sends it to people with "references outside knowledge/ need a companion
PR". Instead of stopping there, the service drafts that companion PR itself:
each citing file is rewritten (a replaced rule's ID becomes its replacement;
a list or table entry citing a retired rule is dropped; any other mention of
it is removed). The companion is opened ONLY as a draft labelled
``kb:companion`` through the restricted ``open_companion_pr`` path; people
review, ready and merge it. The knowledge change waits as
``companion_pending`` (its rules stay active meanwhile, so nothing ever cites
a retired rule), and is rebuilt on current main once the companion merged.
"""

from __future__ import annotations

import re

COMPANION_REASON = "references outside knowledge/ need a companion PR"
COMPANION_DIRS = ("skills/", "plugins/", "adapters/", "doc/", "playbooks/")


def needs_companion(decision) -> bool:
    """The change is otherwise fine: its only problem is external citations."""
    return (decision.status == "human" and len(decision.reasons) == 1
            and decision.reasons[0].startswith(COMPANION_REASON) and bool(decision.l1.external_refs))


def rewrite(text: str, rule_id: str, replacement: str | None) -> str:
    token = re.compile(rf"(?<![\w-]){re.escape(rule_id)}(?![\w-])")
    if replacement:
        return token.sub(replacement, text)
    out = []
    for line in text.splitlines(keepends=True):
        if token.search(line):
            if line.lstrip().startswith(("- ", "* ", "|")):
                continue  # a list or table entry about the retired rule
            indent = line[:len(line) - len(line.lstrip(" \t"))]  # YAML nesting must survive
            line = indent + re.sub(r"[ \t]{2,}", " ", token.sub("", line[len(indent):]))
        out.append(line)
    return "".join(out)


def companion_files(external: dict[str, str], refs, operations) -> dict[str, str]:
    replacements = {op.rule_id: op.new_rule_id for op in operations if op.kind == "replace" and op.new_rule_id}
    files: dict[str, str] = {}
    for rule_id, path in refs:
        if not path.startswith(COMPANION_DIRS):
            continue  # never outside the companion whitelist
        text = files.get(path, external[path])
        files[path] = rewrite(text, rule_id, replacements.get(rule_id))
    return {path: text for path, text in files.items() if text != external[path]}


def prepare_companion(companion_id: str, changeset_id: str, operations, decision,
                      external: dict[str, str], base_sha: str) -> dict | None:
    """Freeze deterministic companion edits with the completed gate result."""
    files = companion_files(external, decision.l1.external_refs, operations)
    if not files:
        return None
    return {"id": companion_id,
            "artifact": {"base_sha": base_sha, "files": files, "deleted": [], "evidence": []},
            "detail": {"for_changeset": changeset_id, "base_sha": base_sha,
                       "refs": [list(r) for r in decision.l1.external_refs]}}


def publish_companion(rt, lifecycle, companion_id: str) -> str:
    changeset = rt.ledger.changeset(companion_id)
    if changeset["status"] != "companion_staged":
        return changeset["status"]
    if rt.outbox is None or not lifecycle.auto_merge or not lifecycle.publishes:
        rt.ledger.update_changeset(companion_id, status="shadow_recorded")
        return "shadow_recorded"
    data = rt.load_changeset_files(companion_id)
    from .merge import issue_once

    issue_once(rt, lifecycle.repo, changeset, "open_companion_pr", {
        "changeset_id": companion_id, "base_sha": data["base_sha"],
        "branch": f"kb/{lifecycle.repo}/{companion_id}", "files": data["files"], "deleted": [],
        "title": (f"knowledge({lifecycle.repo}): update references for {changeset['detail']['for_changeset']}"
                  if changeset["detail"].get("for_changeset")
                  else f"adapter({lifecycle.repo}): update the release baseline "
                       f"({changeset['detail'].get('baseline_for', '')})"),
        "body": (("Companion to a knowledge change that retires or replaces rules cited here "
                  f"(`{changeset['detail']['for_changeset']}`). The knowledge change is rebuilt once this merges.")
                 if changeset["detail"].get("for_changeset")
                 else "The committed adapter baseline lags the audited upstream release (reconciliation "
                      "findings of the release audit); this updates its upstream pin and inventories.")
                + " Drafted by the Copilot knowledge service; review, mark ready and merge it by hand.",
    })
    rt.ledger.update_changeset(companion_id, status="pr_requested")
    return "pr_requested"
