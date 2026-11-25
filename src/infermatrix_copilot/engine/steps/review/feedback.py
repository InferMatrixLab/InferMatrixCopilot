"""Compare independently selected findings with active, same-head inline threads."""
from __future__ import annotations

import json
import re
import subprocess

from .._common import gh, repo_path
from .anchor import normalize_path


_THREADS = """
query($owner:String!, $repo:String!, $pr:Int!) {
  repository(owner:$owner, name:$repo) { pullRequest(number:$pr) {
    headRefOid
    reviewThreads(last:50) { nodes {
      isResolved isOutdated path line diffSide
      comments(first:1) { nodes { body url } }
    } }
  } }
}
"""


def independent_context(text: str) -> str:
    """Older checkpoints stored reviewer discussion after the PR description."""
    sections = re.split(r"(?m)(^## (?:PR discussion \(do not repeat these concerns — build on or extend them\)"
                        r"|Review summaries|Inline review comments|Linked issue #[0-9]+:[^\n]*)$)", text)
    kept = [sections[0]]
    for header, content in zip(sections[1::2], sections[2::2]):
        if header.startswith("## Linked issue #"):
            kept.extend((header, content))
    return "".join(kept).rstrip()


def existing_threads(ctx, pr, head):
    """Index active RIGHT-side roots by path, line, and exact concern text."""
    if ctx.settings.pr_context_mode == "no_discussion":
        return {}, "disabled"
    if (not head or not pr or not repo_path(ctx) or ctx.settings.improve_shadow
            or ctx.settings.pr_context_source == "snapshot"):
        return {}, "unavailable"
    try:
        code, response = gh(["api", "graphql", "-f", f"query={_THREADS}", "-F", "owner={owner}",
                             "-F", "repo={repo}", "-F", f"pr={int(pr)}"], cwd=repo_path(ctx))
        if code:
            return {}, "unavailable"
        pull = json.loads(response)["data"]["repository"]["pullRequest"]
        if pull["headRefOid"] != head:
            return {}, "stale"
        threads = {}
        for node in pull["reviewThreads"]["nodes"]:
            if node["isResolved"] is not False or node["isOutdated"] is not False or node["diffSide"] != "RIGHT":
                continue
            roots = node["comments"]["nodes"]
            if not roots or type(node["line"]) is not int:
                continue
            if not isinstance(roots[0].get("body"), str):
                continue
            body = roots[0]["body"].strip()
            # The publisher formats severity and appends its evidence after the finding.
            formatted = re.match(r"^\*\*\[(?:blocker|major|minor|nit)\]\*\* ", body)
            if formatted:
                body = body[formatted.end():].partition("\n\nEvidence: ")[0]
            key = (node["path"], node["line"], body)
            threads[key] = roots[0]["url"]
        return threads, "checked"
    except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError):
        return {}, "unavailable"


def match_thread(finding, threads):
    line = finding.get("line")
    if type(line) is not int or finding.get("_anchor_unverified") or not str(finding.get("evidence") or "").strip():
        return None
    key = (normalize_path(finding.get("file")), line, str(finding.get("comment") or "").strip())
    return threads.get(key)
