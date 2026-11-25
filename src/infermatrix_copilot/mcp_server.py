"""MCP stdio transport over the headless durable RunService.

Protocol wiring belongs here. Run reservation, policy, execution, and polling
belong to app.run_service and are shared with the embedded SDK. The optional
MCP dependency is imported only while constructing the protocol server.
"""

from __future__ import annotations

import os
import signal
import subprocess  # compatibility: older tests/hosts patch this shared module
import sys
from collections.abc import Callable
from typing import Literal

from .config import Settings
from .mcp_policy import PolicyError
from .app.run_service import RunService as CopilotMCP


def _guard(fn: Callable[[], dict]) -> dict:
    """Run a tool body, converting a policy/validation refusal into a clean
    `{"error": …}` result rather than a protocol-level crash."""
    try:
        return fn()
    except (PolicyError, ValueError) as exc:
        return {"error": str(exc)}


def build_mcp(settings: Settings | None = None):
    """Build the FastMCP server with the V1 read-only tools bound to a
    `CopilotMCP`. Importing FastMCP here keeps the `mcp` dependency out of the
    core import path (it lives behind the `[mcp]` extra)."""
    from mcp.server.fastmcp import FastMCP

    core = CopilotMCP(settings)
    mcp = FastMCP(
        "infermatrix-copilot",
        instructions=(
            "Read-only repository copilot with curated knowledge. For a PR "
            "review, call start_review once, save its run_id, then poll "
            "get_result with that run_id until the state is terminal. If the "
            "user explicitly asks for the advanced/strong/high-performance "
            "model, pass mode='performance'; otherwise use mode='eco'. Return "
            "the complete paged report by following next_offset. Never start "
            "a second review merely because the first is still running. Use "
            "doc_search then doc_read for direct knowledge questions."
        ),
    )

    @mcp.tool()
    def start_review(pr: int, repo: str = "", review_depth: str = "",
                     mode: Literal["eco", "performance"] = "eco",
                     expected_head_sha: str = "", include_score: bool = False) -> dict:
        """Start a read-only review of PR `pr` in eco or performance mode.
        Returns {run_id}; poll get_result. `review_depth` optionally pins the
        adaptive depth (light|standard|full|auto; policy-validated).
        Set `include_score` when the user requests a numeric advisory score.
        `expected_head_sha` optionally pins the review to one snapshot: pass the
        full 40-hex head you observed and the run stops as stale rather than
        reviewing a different commit if the PR moved in between."""
        req: dict = {"kind": "pr_review",
                     "repo": repo or core.settings.default_repo,
                     "pr": pr, "mode": mode}
        if review_depth:
            req["params"] = {"review_depth": review_depth}
        if include_score:
            req.setdefault("params", {})["include_score"] = include_score
        if expected_head_sha:
            req["expected_head_sha"] = expected_head_sha
        return _guard(lambda: {"run_id": core.start(req)})

    @mcp.tool()
    def start_issue_answer(issue: int, repo: str = "") -> dict:
        """Draft a read-only answer to issue `issue` (never posted). Returns
        {run_id}; poll get_result."""
        return _guard(lambda: {"run_id": core.start(
            {"kind": "issue_answer", "repo": repo or core.settings.default_repo, "issue": issue})})

    @mcp.tool()
    def start_issue_triage(repo: str = "") -> dict:
        """Start a read-only triage of recent open issues. Returns {run_id};
        poll get_result."""
        return _guard(lambda: {"run_id": core.start(
            {"kind": "issue_filter", "repo": repo or core.settings.default_repo})})

    @mcp.tool()
    def get_capabilities() -> dict:
        """This copilot's contract version and capability flags. Call it once in
        your preflight: it is how a client detects an incompatible or
        unprotected deployment before a review fails on it, and
        `max_strict_workers` says how many reviews actually run at a time."""
        return _guard(core.capabilities)

    @mcp.tool()
    def get_result(run_id: str, offset: int = 0) -> dict:
        """Poll a run: {state, report?, report_path?, next_offset?}. `report` is
        size-capped; page with `offset` = the prior `next_offset`."""
        return _guard(lambda: core.get_result(run_id, offset))

    @mcp.tool()
    def get_status(run_id: str) -> dict:
        """Poll a run's progress: {status, progress?} (progress absent until the
        run is executing)."""
        return _guard(lambda: core.get_status(run_id))

    @mcp.tool()
    def list_playbooks() -> dict:
        """List the read-only task kinds and the playbooks backing them."""
        return _guard(core.list_playbooks)

    @mcp.tool()
    def doc_search(query: str, repo: str = "", limit: int = 40) -> dict:
        """Search curated general and repo-specific knowledge without a workflow."""
        return _guard(lambda: core.doc_search(query, repo, limit))

    @mcp.tool()
    def doc_read(path: str, repo: str = "", offset: int = 0) -> dict:
        """Read a Markdown document returned by doc_search; results are paged."""
        return _guard(lambda: core.doc_read(path, repo, offset))

    return mcp


def main() -> int:
    """Console-script entry (`infermatrix-copilot-mcp`): serve over stdio."""
    if os.name == "nt":
        # Codex/terminal hosts can emit a console Ctrl+C event while keeping the
        # stdio transport open.  anyio turns that inherited event into
        # KeyboardInterrupt and tears down the MCP server mid-run.  Stdio EOF
        # remains the authoritative, portable shutdown signal.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        mcp = build_mcp()
    except ImportError:
        sys.stderr.write(
            "infermatrix-copilot-mcp needs the MCP SDK. Install it with:\n"
            "    pip install 'infermatrix-copilot[mcp]'\n")
        return 1
    mcp.run()  # stdio transport by default
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
