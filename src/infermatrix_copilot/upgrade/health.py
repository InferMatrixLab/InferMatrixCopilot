"""Offline release health gate, independent of user credentials and doctor."""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


async def mcp_health() -> None:
    # Candidate health must not depend on the old installation's configuration,
    # active interpreter, or update metadata being readable.
    with tempfile.TemporaryDirectory(prefix="copilot-mcp-health-") as home:
        await _mcp_health_at_home(home)


async def _mcp_health_at_home(home: str) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {key: os.environ[key] for key in (
        "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COPILOT_RELEASE_ID",
        "COPILOT_RELEASE_ROOT", "COPILOT_RUNTIME_PYTHON",
    ) if key in os.environ}
    env.update(HOME=home, USERPROFILE=home, RUN_ROOT=str(Path(home) / "runs"),
               INFERMATRIX_COPILOT_HOME=home, ALLOW_PUSH="0", ALLOW_POST="0")
    server = StdioServerParameters(command=sys.executable,
                                  args=["-m", "infermatrix_copilot.thin_mcp_server"],
                                  env=env)
    async with stdio_client(server) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            if not {"review", "doc_read", "get_workflow", "get_update_status", "update_knowledge"} <= names:
                raise ValueError("release omits required MCP tools")
            result = await session.call_tool("get_workflow", {"skill_name": "imdesign", "skill_protocol": 1})
            content = json.loads(next(block.text for block in result.content if block.type == "text"))
            if result.isError or not content.get("content"):
                raise ValueError("packaged workflow cannot be read")
            if content.get("release_id") != os.environ.get("COPILOT_RELEASE_ID", "unmanaged"):
                raise ValueError("MCP workflow release identity mismatch")
            result = await session.call_tool("get_update_status", {})
            status = json.loads(next(block.text for block in result.content if block.type == "text"))
            if result.isError or "active_release" not in status:
                raise ValueError("packaged updater cannot report its status")
            result = await session.call_tool("update_knowledge", {})
            contribution = json.loads(next(block.text for block in result.content if block.type == "text"))
            if result.isError or not Path(contribution.get("knowledge_entry", "")).is_file():
                raise ValueError("packaged knowledge contribution instructions cannot be read")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Check a Copilot release without model or network calls")
    parser.add_argument("--expected-version", default="")
    parser.add_argument("--expected-release", default="")
    args = parser.parse_args(argv)
    try:
        from .. import __version__
        from ..sdk._resources import resource_dir
        from ..sdk.v1 import get_capabilities
        from ..knowledge_view import SUPPORTED_FORMATS, knowledge_format

        version = importlib.metadata.version("infermatrix-copilot")
        if version != __version__ or (args.expected_version and version != args.expected_version):
            raise ValueError("distribution version mismatch")
        if args.expected_release and args.expected_release != os.environ.get("COPILOT_RELEASE_ID"):
            raise ValueError("runtime release identity mismatch")
        for name in ("knowledge", "adapters", "playbooks", "skills"):
            resource_dir(name)
        if knowledge_format(resource_dir("knowledge")) not in SUPPORTED_FORMATS:
            raise ValueError("bundled knowledge format is unsupported")
        package = Path(__file__).resolve().parents[1]
        for path in ("host_skills/imdesign/SKILL.md",
                     "doc/knowledge/CONTRIBUTING.md", "trusted-keys.json", "plugin.json", "workflow-tools.json"):
            if not (package / "_runtime" / path).is_file():
                raise ValueError(f"release omits {path}")
        tools = package / "_runtime" / "tools"
        if not tools.is_dir() or not any(tools.glob("*.py")):
            raise ValueError("release omits packaged support tools")
        capabilities = get_capabilities()
        if capabilities.distribution_version != version:
            raise ValueError("SDK version mismatch")
        subprocess.run([sys.executable, "-m", "infermatrix_copilot", "--help"],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        asyncio.run(asyncio.wait_for(mcp_health(), timeout=30))
        print(json.dumps({"healthy": True, "version": version,
                          "release_id": os.environ.get("COPILOT_RELEASE_ID", "")}))
        return 0
    except Exception as exc:
        print(f"Release health check failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
