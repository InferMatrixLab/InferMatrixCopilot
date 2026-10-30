"""Read release-local host workflows for stable installed skill entrypoints."""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path, PurePosixPath

from .runtime import current_release

SKILL_PROTOCOL = 1
_AUTHORING = frozenset({"imupdate", "imkbinit"})
_PACKAGE = Path(__file__).resolve().parents[1]
_SOURCE = _PACKAGE.parent.parent


def _relative_path(value: str, *, allow_root: bool = False) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("invalid workflow tool data path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or (not path.parts and not allow_root):
        raise ValueError("workflow tool data path escapes its root")
    return path


def _tool_registry() -> dict[str, dict]:
    """Load release-owned tool bindings; repository-specific policy is data."""
    candidates = (_PACKAGE / "_runtime" / "workflow-tools.json",
                  _SOURCE / "release" / "workflow-tools.json")
    for path in candidates:
        if not path.is_file():
            continue
        try:
            registry = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(registry, dict) or type(registry.get("schema")) is not int
                    or registry["schema"] != 1 or not isinstance(registry.get("tools"), dict)
                    or not registry["tools"]):
                raise ValueError("invalid registry schema")
            for name, tool in registry["tools"].items():
                if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) or not isinstance(tool, dict):
                    raise ValueError("invalid tool identity")
                if _relative_path(tool["path"]).suffix != ".py":
                    raise ValueError("workflow tool must be a Python script")
                replacements = tool.get("replace_commands", [])
                if not isinstance(replacements, list) or any(not isinstance(item, str) or not item for item in replacements):
                    raise ValueError("invalid workflow command bindings")
                options = tool.get("workspace_options", {})
                if not isinstance(options, dict):
                    raise ValueError("invalid workspace options")
                for flag, relative in options.items():
                    if not re.fullmatch(r"--[a-z][a-z0-9-]*", flag):
                        raise ValueError("invalid workspace option flag")
                    _relative_path(relative, allow_root=True)
                if type(tool.get("bind_workspace_file", False)) is not bool:
                    raise ValueError("invalid workspace file binding")
            return registry["tools"]
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise ValueError("release workflow tool registry is invalid") from exc
    raise FileNotFoundError("release workflow tool registry is missing; reinstall InferMatrixCopilot")


def _command(argv: list[str]) -> str:
    if os.name == "nt":
        return "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in argv)
    return shlex.join(argv)


def _skills_root() -> Path:
    # A wheel's instructions and runtime must come from the same release,
    # even when the working directory happens to contain a source checkout.
    candidates = (_PACKAGE / "_runtime" / "host_skills",
                  _SOURCE / "plugins" / "infermatrix-copilot" / "skills")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    raise FileNotFoundError("release host workflows are missing; reinstall InferMatrixCopilot")


def _authoring_root(value: str | Path | None = None) -> Path:
    selected = value or os.environ.get("COPILOT_AUTHORING_ROOT", "")
    if selected:
        root = Path(selected).expanduser().resolve()
    elif not os.environ.get("COPILOT_RELEASE_ROOT"):
        root = _SOURCE
    else:
        raise ValueError(
            "this workflow edits the Copilot knowledge workspace; set "
            "COPILOT_AUTHORING_ROOT to a writable InferMatrixCopilot source "
            "checkout and restart the MCP host")
    if (not (root / "pyproject.toml").is_file()
            or not (root / "knowledge" / "AGENTS.md").is_file()
            or not (root / "tools").is_dir()):
        raise ValueError(f"COPILOT_AUTHORING_ROOT is not an InferMatrixCopilot source checkout: {root}")
    managed = os.environ.get("COPILOT_RELEASE_ROOT")
    if managed:
        state = Path(os.environ.get("INFERMATRIX_COPILOT_HOME")
                     or Path.home() / ".infermatrix-copilot").expanduser().resolve()
        if (root.is_relative_to(Path(managed).expanduser().resolve())
                or root.is_relative_to(state / "releases")):
            raise ValueError("COPILOT_AUTHORING_ROOT must be outside the immutable managed release")
    return root


def get_workflow(skill_name: str, skill_protocol: int = 1) -> dict:
    """Return complete workflow instructions plus their physical references."""
    if isinstance(skill_protocol, bool) or skill_protocol != SKILL_PROTOCOL:
        raise ValueError(
            f"unsupported skill protocol {skill_protocol!r}; expected {SKILL_PROTOCOL}; "
            "rerun the installer and reload host skills")
    if not isinstance(skill_name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", skill_name):
        raise ValueError("invalid workflow skill name")
    skills = _skills_root()
    directory = (skills / skill_name).resolve()
    if directory.parent != skills:
        raise ValueError("workflow path escapes the release")
    path = directory / "SKILL.md"
    if not path.is_file():
        raise FileNotFoundError(f"unknown workflow skill: {skill_name}")
    authoring = _authoring_root() if skill_name in _AUTHORING else None
    runtime = (_PACKAGE / "_runtime" if (_PACKAGE / "_runtime").is_dir()
               else _SOURCE)
    workspace = authoring or runtime
    content = path.read_text(encoding="utf-8").replace(
        "{{INFERMATRIX_COPILOT_ROOT}}", str(workspace))
    python = current_release()["python"]
    managed = bool(os.environ.get("COPILOT_RELEASE_ROOT"))
    cli_argv = [python, *(["-I"] if managed else []), "-m", "infermatrix_copilot"]
    support_argv = [python, *(["-I"] if managed else []), "-m", __name__]
    if authoring:
        support_argv.extend(["--workspace", str(authoring)])
        cli_argv = [*support_argv, "cli", "--"]
    cli_command = _command(cli_argv)
    content = content.replace("./infermatrix-copilot", cli_command)
    tools = _tool_registry()
    support_commands = {name: _command([*support_argv, "tool", name, "--"])
                        for name in tools}
    if authoring:
        # Execute the release's code against the explicit authoring workspace;
        # `uvx --from <checkout>` would silently switch to that checkout's code.
        if skill_name == "imkbinit":
            content = re.sub(
                r"## Launcher\n.*?(?=\n## 1\. Resolve)",
                "## Launcher\n\nUse this release-pinned command as `<imc>`:\n\n"
                f"```text\n{cli_command}\n```\n\n"
                "The runner selects the authoring workspace's adapters and knowledge "
                "while keeping the installed release's CLI and model workflows. "
                "Check it with `<imc> kb --help`.\n",
                content, flags=re.DOTALL)
        for name, tool in tools.items():
            for command in tool.get("replace_commands", []):
                content = content.replace(command, support_commands[name])
    binding = (
        "## Release execution\n\n"
        f"This workflow belongs to release `{current_release()['release_id']}`. "
        f"Use `{cli_command}` for CLI invocations, including commands shown in "
        "the references. Runtime resources and the returned skill/reference paths "
        "are immutable. Knowledge or workflow edits belong in the separate "
        "`COPILOT_AUTHORING_ROOT` checkout; request `get_workflow('imupdate', 1)` "
        "before the rebase consolidation stage.\n\n"
    )
    content = content.replace("\n# ", "\n" + binding + "# ", 1)
    # Explicit reference paths work for tools that don't inherit SKILL.md's
    # directory as their cwd. Keep release-local references with the workflow.
    references = {str(item.relative_to(directory)): str(item.resolve())
                  for item in directory.rglob("*")
                  if item.is_file() and item.name != "SKILL.md"}
    return {
        "skill_name": skill_name,
        "skill_protocol": SKILL_PROTOCOL,
        "release_id": current_release()["release_id"],
        "content": content,
        "path": str(path),
        "skill_root": str(directory),
        "runtime_root": str(runtime),
        "authoring_root": str(authoring) if authoring else None,
        "cli_argv": cli_argv,
        "support_commands": support_commands,
        "references": references,
    }


def _run_tool(name: str, arguments: list[str], workspace: Path) -> int:
    """Execute bundled bytes, selecting only data from the authoring checkout."""
    import runpy

    saved_argv, saved_path = sys.argv, list(sys.path)
    try:
        tool = _tool_registry().get(name)
        if tool is None:
            raise ValueError("unknown workflow support tool")
        relative = _relative_path(tool["path"])
        candidates = (_PACKAGE / "_runtime" / relative,
                      _PACKAGE / relative, _SOURCE / relative)
        code = next((item for item in candidates if item.is_file()), None)
        if code is None:
            raise FileNotFoundError(f"bundled workflow tool is missing: {name}")
        if tool.get("bind_workspace_file", False):
            # These validators bind their data roots to __file__. Supply the
            # explicit workspace path while compiling the release's bytes.
            sys.argv = [str(code), *arguments]
            namespace = {"__name__": "__main__", "__package__": None,
                         "__file__": str(workspace / relative)}
            exec(compile(code.read_bytes(), str(code), "exec"), namespace)
        else:
            for flag, value in tool.get("workspace_options", {}).items():
                if flag not in arguments and not any(item.startswith(flag + "=") for item in arguments):
                    arguments.extend([flag, str(workspace / _relative_path(value, allow_root=True))])
            sys.argv = [str(code), *arguments]
            runpy.run_path(str(code), run_name="__main__")
        return 0
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = saved_argv
        sys.path[:] = saved_path


def main(argv: list[str] | None = None) -> int:
    """Launch bundled support code with an explicit child environment.

    The managed interpreter uses ``-I`` so a checkout's ``src`` cannot replace
    its installed application. The caller's process environment is untouched.
    """
    import argparse
    import subprocess

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("operation", choices=("cli", "tool"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    arguments = list(args.arguments)
    if arguments and arguments[0] == "--":
        arguments.pop(0)
    workspace = _authoring_root(args.workspace) if args.workspace or args.operation == "tool" else None
    if args.operation == "tool" and (not arguments or arguments[0] not in _tool_registry()):
        parser.error("select a bundled authoring tool")
    if args.worker:
        if args.operation == "cli":
            from ..cli import main as cli_main

            return cli_main(arguments)
        name, arguments = arguments[0], arguments[1:]
        if arguments and arguments[0] == "--":
            arguments.pop(0)
        return _run_tool(name, arguments, workspace)
    env = dict(os.environ)
    if workspace:
        env.pop("KNOWLEDGE_ROOT", None)
        env.update(COPILOT_AUTHORING_ROOT=str(workspace),
                   ADAPTERS_DIR=str(workspace / "adapters"),
                   KNOWLEDGE_DIR=str(workspace / "knowledge"))
    managed = bool(os.environ.get("COPILOT_RELEASE_ROOT"))
    command = [sys.executable, *(["-I"] if managed else []), "-m",
               "infermatrix_copilot.upgrade.workflows", "--worker"]
    if workspace:
        command.extend(["--workspace", str(workspace)])
    command.extend([args.operation, "--", *arguments])
    return subprocess.call(command, env=env, cwd=workspace)


if __name__ == "__main__":
    raise SystemExit(main())
