#!/usr/bin/env python3
"""Private-repository bootstrap for MCP hosts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILLS_SOURCE = PROJECT_ROOT / "plugins" / "infermatrix-copilot" / "skills"
SERVER_NAME = "infermatrix-copilot"
PACKAGE = (
    "infermatrix-copilot[mcp] @ "
    "git+https://github.com/JiusiServe/InferMatrixCopilot.git@main"
)
SERVER_COMMAND = ["uvx", "--from", PACKAGE, "infermatrix-copilot-mcp"]
CODEX_STARTUP_TIMEOUT_SEC = 120
STRICT_CONFIG_DIR = ".infermatrix-copilot"
STRICT_CONFIG_FILE = ".env"
DEFAULT_REPO_FULL_NAME = "vllm-project/vllm-omni"


class InstallError(RuntimeError):
    pass


def _strict_config_path(config_root: Path) -> Path:
    return config_root / STRICT_CONFIG_DIR / STRICT_CONFIG_FILE


def _validate_repo_path(repo_path: Optional[Path]) -> Optional[Path]:
    if repo_path is None:
        return None
    resolved = repo_path.expanduser().resolve()
    if not resolved.is_dir() or not (resolved / ".git").exists():
        raise InstallError(
            f"Strict repo path is not a Git checkout: {resolved}"
        )
    return resolved


def _ensure_strict_config(
    config_root: Path,
    repo_path: Optional[Path] = None,
) -> Path:
    """Create the stable per-user Strict config without overwriting secrets."""
    path = _strict_config_path(config_root)
    if path.exists():
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        values = {
            "LLM_PROVIDER": "auto",
            "ANTHROPIC_API_KEY": "",
            "ANTHROPIC_BASE_URL": "",
            "OPENAI_API_KEY": "",
            "OPENAI_BASE_URL": "",
            "AGENT_MODEL": "claude-sonnet-5",
            "OPENAI_MODEL": "gpt-5.6",
            "DEFAULT_REPO": "vllm-omni",
            "REPO_FULL_NAMES": json.dumps(
                {"vllm-omni": DEFAULT_REPO_FULL_NAME},
                ensure_ascii=False,
            ),
        }
        if repo_path is not None:
            values["REPO_PATHS"] = json.dumps(
                {"vllm-omni": str(repo_path)},
                ensure_ascii=False,
            )
        for key, value in values.items():
            replacement = f"{key}={value}"
            for index, line in enumerate(lines):
                if line.startswith(f"{key}="):
                    if key == "REPO_PATHS":
                        lines[index] = replacement
                    break
            else:
                lines.append(replacement)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    path.parent.mkdir(parents=True, exist_ok=True)
    repo_paths = (
        {"vllm-omni": str(repo_path)}
        if repo_path is not None
        else {}
    )
    path.write_text(
        "\n".join(
            [
                "# InferMatrixCopilot Strict runtime",
                "# Direct mode does not need these model settings.",
                "LLM_PROVIDER=auto",
                "ANTHROPIC_API_KEY=",
                "ANTHROPIC_BASE_URL=",
                "OPENAI_API_KEY=",
                "OPENAI_BASE_URL=",
                "AGENT_MODEL=claude-sonnet-5",
                "OPENAI_MODEL=gpt-5.6",
                f"REPO_PATHS={json.dumps(repo_paths, ensure_ascii=False)}",
                "DEFAULT_REPO=vllm-omni",
                "REPO_FULL_NAMES="
                + json.dumps(
                    {"vllm-omni": DEFAULT_REPO_FULL_NAME},
                    ensure_ascii=False,
                ),
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _run(command: Sequence[str], message: str) -> None:
    try:
        subprocess.run(command, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise InstallError(message) from exc


def _run_quiet(command: Sequence[str]) -> None:
    subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _install_legacy_skills(destination_root: Path) -> None:
    destination_root.mkdir(parents=True, exist_ok=True)
    for source in sorted(path for path in SKILLS_SOURCE.iterdir() if path.is_dir()):
        destination = destination_root / source.name
        shutil.copytree(source, destination, dirs_exist_ok=True)
        for path in destination.rglob("*"):
            if not path.is_file():
                continue
            content = path.read_text(encoding="utf-8")
            if "{{INFERMATRIX_COPILOT_ROOT}}" in content:
                path.write_text(
                    content.replace(
                        "{{INFERMATRIX_COPILOT_ROOT}}",
                        str(PROJECT_ROOT),
                    ),
                    encoding="utf-8",
                )


def _atomic_write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_install_manifest(home: Path) -> dict:
    path = home / "install.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(f"Invalid managed installation manifest: {path}") from exc
    if not isinstance(data, dict):
        raise InstallError(f"Invalid managed installation manifest: {path}")
    return data


def _record_install(home: Path, section: str, key: str, value: dict) -> None:
    manifest = _read_install_manifest(home)
    entries = manifest.setdefault(section, {})
    if not isinstance(entries, dict):
        raise InstallError(f"Invalid {section} in {home / 'install.json'}")
    entries[key] = value
    _atomic_write(home / "install.json", json.dumps(manifest, indent=2) + "\n")


def _skill_wrapper(source: str, name: str) -> str:
    # Retain host discovery metadata while fetching the instructions from the
    # exact release serving the current MCP connection.
    match = re.match(r"\A---\r?\n.*?\r?\n---(?:\r?\n|$)", source, re.DOTALL)
    if match is None:
        raise InstallError(f"Skill has no frontmatter: {name}")
    return (
        match.group(0).rstrip() + "\n\n"
        f"Call InferMatrixCopilot `get_workflow(skill_name=\"{name}\", "
        "skill_protocol=1)` before carrying out this skill. Follow the workflow "
        "returned by the connected server for the rest of this request.\n\n"
        "If the tool is unavailable or reports an incompatible protocol, "
        "ask the user to restart the host after updating InferMatrixCopilot. "
        "Do not substitute a workflow from a different release.\n"
    )


def _install_skills(
    destination_root: Path,
    *,
    managed: bool = False,
    home: Optional[Path] = None,
) -> None:
    if not managed:
        _install_legacy_skills(destination_root)
        return
    if home is None:
        raise InstallError("Managed skills need an installation home.")
    manifest = _read_install_manifest(home)
    skill_installs = manifest.get("skill_installs", {})
    if not isinstance(skill_installs, dict):
        raise InstallError("Invalid skill ownership manifest.")
    previous_install = skill_installs.get(str(destination_root), {})
    if not isinstance(previous_install, dict):
        raise InstallError("Invalid skill ownership record.")
    previous = previous_install.get("files", {})
    if not isinstance(previous, dict):
        raise InstallError("Invalid skill ownership file hashes.")
    for name in previous:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise InstallError(f"Unsafe skill ownership entry: {name}")
    desired: dict[str, bytes] = {}
    legacy: dict[str, bytes] = {}
    for source in sorted(SKILLS_SOURCE.rglob("*")):
        if not source.is_file() or source.is_symlink():
            continue
        relative = source.relative_to(SKILLS_SOURCE)
        if len(relative.parts) < 2:
            continue
        raw = source.read_bytes()
        legacy[str(relative)] = raw.replace(
            b"{{INFERMATRIX_COPILOT_ROOT}}", str(PROJECT_ROOT).encode("utf-8")
        )
        desired[str(relative)] = (
            _skill_wrapper(raw.decode("utf-8"), relative.parts[0]).encode("utf-8")
            if relative.name == "SKILL.md" else raw
        )
    conflicts = []
    for name, content in desired.items():
        path = destination_root / name
        if any(part.is_symlink() for part in (path, *path.parents) if part != destination_root and destination_root in part.parents) or (path.exists() and not path.is_file()):
            conflicts.append(str(path))
        elif path.exists():
            existing = path.read_bytes()
            if (
                existing != content
                and existing != legacy[name]
                and hashlib.sha256(existing).hexdigest() != previous.get(name)
            ):
                conflicts.append(str(path))
    if conflicts:
        raise InstallError(
            "Locally modified or unowned skill files were preserved. "
            "Move them aside or merge them before retrying: " + ", ".join(conflicts)
        )
    hashes = {}
    for name, content in desired.items():
        _atomic_write(destination_root / name, content)
        hashes[name] = hashlib.sha256(content).hexdigest()
    for name, expected in previous.items():
        if name in desired:
            continue
        relative = Path(name)
        obsolete = destination_root / relative
        if any(part.is_symlink() for part in (obsolete, *obsolete.parents) if part != destination_root and destination_root in part.parents):
            continue
        if obsolete.is_file():
            if hashlib.sha256(obsolete.read_bytes()).hexdigest() == expected:
                obsolete.unlink()
            else:
                hashes[name] = expected
                print(f"Preserved locally modified obsolete skill: {obsolete}", file=sys.stderr)
    _record_install(home, "skill_installs", str(destination_root), {"protocol": 1, "files": hashes})


def _bootstrap_managed(config_root: Path, args: argparse.Namespace) -> list[str]:
    # The installer itself is stdlib-only and runs under `uv run --no-project`.
    # Bootstrap creates its own environment before loading runtime dependencies.
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    try:
        from infermatrix_copilot.upgrade.bootstrap import install
        return install(
            source_root=PROJECT_ROOT,
            home=config_root / STRICT_CONFIG_DIR,
            channel=args.channel,
            public_key=args.public_key,
            authoring_root=PROJECT_ROOT,
        )
    except Exception as exc:
        raise InstallError(f"Managed runtime bootstrap failed: {exc}") from exc
    finally:
        sys.path.pop(0)


def _write_cli_wrapper(
    home: Path,
    command: Sequence[str],
    output: Optional[Path] = None,
) -> Path:
    import shlex

    wrapper = (output.expanduser().absolute() if output else
               home / "bin" / ("infermatrix-copilot.cmd" if os.name == "nt" else "infermatrix-copilot"))
    cli_command = [*command[:-1], "cli"]
    if os.name == "nt":
        content = "@echo off\r\n" + subprocess.list2cmdline(cli_command) + " %*\r\n"
    else:
        content = "#!/bin/sh\nexec " + shlex.join(cli_command) + ' "$@"\n'
    data = content.encode("utf-8")
    key = "cli" if output is None else "local_cli:" + str(wrapper)
    registrations = _read_install_manifest(home).get("registrations", {})
    if not isinstance(registrations, dict):
        raise InstallError("Invalid CLI ownership registrations.")
    previous = registrations.get(key, {})
    if not isinstance(previous, dict):
        raise InstallError("Invalid CLI wrapper ownership record.")
    legacy = set()
    local_checkout = (wrapper.name == "infermatrix-copilot" and wrapper.parent.exists()
                      and PROJECT_ROOT.exists() and wrapper.parent.samefile(PROJECT_ROOT))
    if local_checkout:
        for root in {PROJECT_ROOT, wrapper.parent}:
            legacy.add(f'#!/usr/bin/env bash\nexec "{root}/.venv/bin/infermatrix-copilot" "$@"\n'.encode())
    if wrapper.is_symlink() or (wrapper.exists() and not wrapper.is_file()):
        raise InstallError(f"Existing CLI wrapper was preserved: {wrapper}")
    if wrapper.exists():
        existing = wrapper.read_bytes()
        if (existing != data and existing not in legacy
                and hashlib.sha256(existing).hexdigest() != previous.get("sha256")):
            raise InstallError(f"Locally modified or unowned CLI wrapper was preserved: {wrapper}")
    _atomic_write(wrapper, data)
    if os.name != "nt":
        wrapper.chmod(0o755)
    _record_install(home, "registrations", key, {
        "path": str(wrapper), "command": cli_command, "sha256": hashlib.sha256(data).hexdigest(),
    })
    # A migrated repo-local wrapper belongs to the managed installer. Keep the
    # legacy uninstaller from removing it through its old artifact list.
    manifest = PROJECT_ROOT / ".install-manifest"
    if output is not None and local_checkout and manifest.is_file():
        lines = manifest.read_text(encoding="utf-8").splitlines()
        kept = [line for line in lines if line not in {"./infermatrix-copilot", "infermatrix-copilot"}]
        if kept != lines:
            _atomic_write(manifest, "\n".join(kept) + ("\n" if kept else ""))
    return wrapper


def _set_codex_startup_timeout(config_path: Path) -> None:
    """Give uvx enough time to create its environment on first startup."""
    if not config_path.is_file():
        raise InstallError(f"Codex config was not created: {config_path}")

    text = config_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    table = re.compile(
        rf'^\s*\[mcp_servers\.(?:{re.escape(SERVER_NAME)}|'
        rf'"{re.escape(SERVER_NAME)}"|\'{re.escape(SERVER_NAME)}\')\]\s*$'
    )
    start = next(
        (index for index, line in enumerate(lines) if table.match(line)),
        None,
    )
    if start is None:
        raise InstallError(
            f"Codex MCP config for {SERVER_NAME} was not created: {config_path}"
        )

    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].lstrip().startswith("[")
        ),
        len(lines),
    )
    setting = f"startup_timeout_sec = {CODEX_STARTUP_TIMEOUT_SEC}"
    timeout_lines = [
        index
        for index in range(start + 1, end)
        if re.match(r"^\s*startup_timeout_(?:sec|ms)\s*=", lines[index])
    ]
    if timeout_lines:
        lines[timeout_lines[0]] = setting
        for index in reversed(timeout_lines[1:]):
            del lines[index]
    else:
        lines.insert(end, setting)

    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _codex_config_path(config_root: Path) -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    return (
        Path(codex_home).expanduser()
        if codex_home
        else config_root / ".codex"
    ) / "config.toml"


def _codex_cli_path() -> Optional[str]:
    """Resolve the usable Codex CLI, including the Desktop-provided path."""
    desktop_cli = os.environ.get("CODEX_CLI_PATH")
    if desktop_cli:
        candidate = Path(desktop_cli).expanduser()
        if candidate.is_file():
            return str(candidate)
    return shutil.which("codex")


def _update_codex_config(config_path: Path, command: Sequence[str]) -> None:
    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise InstallError(f"Codex config is invalid and was not changed: {config_path}") from exc
    servers = parsed.get("mcp_servers", {})
    if not isinstance(servers, dict) or not isinstance(servers.get(SERVER_NAME, {}), dict):
        raise InstallError(f"Invalid Codex MCP configuration: {config_path}")
    lines = text.splitlines()
    table = re.compile(
        rf'^\s*\[mcp_servers\.(?:{re.escape(SERVER_NAME)}|'
        rf'"{re.escape(SERVER_NAME)}"|\'{re.escape(SERVER_NAME)}\')\]\s*(?:#.*)?$'
    )
    start = next((i for i, line in enumerate(lines) if table.match(line)), None)
    values = [
        "command = " + json.dumps(command[0]),
        "args = " + json.dumps(list(command[1:])),
        f"startup_timeout_sec = {CODEX_STARTUP_TIMEOUT_SEC}",
    ]
    if start is None:
        if SERVER_NAME in servers:
            raise InstallError(f"Unsupported Codex MCP table syntax; preserved: {config_path}")
        lines.extend(["", f"[mcp_servers.{SERVER_NAME}]", *values])
    else:
        end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
        kept = []
        i = start + 1
        while i < end:
            if re.match(r"^\s*(?:command|args|url|startup_timeout_(?:sec|ms))\s*=", lines[i]):
                assignment = lines[i]
                i += 1
                while True:
                    try:
                        tomllib.loads(assignment)
                        break
                    except tomllib.TOMLDecodeError:
                        if i >= end:
                            raise InstallError(f"Unsupported Codex MCP assignment; preserved: {config_path}")
                        assignment += "\n" + lines[i]
                        i += 1
            else:
                kept.append(lines[i])
                i += 1
        lines[start + 1:end] = [*values, *kept]
    updated = "\n".join(lines).lstrip("\n") + "\n"
    try:
        tomllib.loads(updated)
    except tomllib.TOMLDecodeError as exc:
        raise InstallError(f"Codex config could not be updated safely: {config_path}") from exc
    _atomic_write(config_path, updated)


def _install_codex(config_root: Path, command: Optional[Sequence[str]] = None, *, managed: bool = False) -> None:
    actual = list(command or SERVER_COMMAND)
    _update_codex_config(_codex_config_path(config_root), actual)
    _install_skills(config_root / ".agents" / "skills", managed=managed, home=config_root / STRICT_CONFIG_DIR)


def _install_claude(config_root: Path, command: Optional[Sequence[str]] = None, *, managed: bool = False) -> None:
    actual = list(command or SERVER_COMMAND)
    _update_json_server(config_root / ".claude.json", ("mcpServers",), actual, "Claude Code")
    _install_skills(config_root / ".claude" / "skills", managed=managed, home=config_root / STRICT_CONFIG_DIR)


def _update_json_server(config_path: Path, keys: Sequence[str], command: Sequence[str], host: str) -> None:
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InstallError(f"{host} config is invalid and was not changed: {config_path}") from exc
        if not isinstance(config, dict):
            raise InstallError(f"{host} config must be an object: {config_path}")
    else:
        config = {}
    servers = config
    for key in keys:
        servers = servers.setdefault(key, {})
        if not isinstance(servers, dict):
            raise InstallError(f"{host} {'.'.join(keys)} must be an object: {config_path}")
    existing = servers.setdefault(SERVER_NAME, {})
    if not isinstance(existing, dict):
        raise InstallError(f"{host} server entry must be an object: {config_path}")
    existing.update({"type": "stdio", "command": command[0], "args": list(command[1:])})
    existing.pop("url", None)
    if config_path.exists():
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        shutil.copy2(config_path, config_path.with_name(f"{config_path.name}.{timestamp}.bak"))
    _atomic_write(config_path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")


def _install_cursor(config_root: Path, command: Optional[Sequence[str]] = None, *, managed: bool = False) -> None:
    cursor_root = config_root / ".cursor"
    _update_json_server(cursor_root / "mcp.json", ("mcpServers",), list(command or SERVER_COMMAND), "Cursor")
    _install_skills(cursor_root / "skills", managed=managed, home=config_root / STRICT_CONFIG_DIR)


def _install_zcode(config_root: Path, command: Optional[Sequence[str]] = None, *, managed: bool = False) -> None:
    zcode_root = config_root / ".zcode"
    _update_json_server(zcode_root / "cli" / "config.json", ("mcp", "servers"), list(command or SERVER_COMMAND), "ZCode")
    _install_skills(zcode_root / "skills", managed=managed, home=config_root / STRICT_CONFIG_DIR)


def _zcode_installed(config_root: Path) -> bool:
    return bool(shutil.which("zcode")) or (config_root / ".zcode").exists()


def _cursor_installed(config_root: Path) -> bool:
    if shutil.which("cursor") or shutil.which("cursor-agent"):
        return True
    candidates = [
        config_root / ".cursor",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "cursor" / "Cursor.exe",
        Path("/Applications/Cursor.app"),
    ]
    return any(path.exists() for path in candidates)


def _detect_agents(config_root: Path) -> list[str]:
    agents = []
    if _codex_cli_path() or _codex_config_path(config_root).exists():
        agents.append("codex")
    if shutil.which("claude"):
        agents.append("claude")
    if _cursor_installed(config_root):
        agents.append("cursor")
    if _zcode_installed(config_root):
        agents.append("zcode")
    return agents


def _write_generic_config(output: Path, command: Optional[Sequence[str]] = None) -> None:
    _update_json_server(output, ("mcpServers",), list(command or SERVER_COMMAND), "Generic MCP")


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install InferMatrixCopilot.")
    parser.add_argument(
        "--agent",
        action="append",
        choices=("codex", "claude", "cursor", "zcode"),
        help="Override automatic Agent detection. May be repeated.",
    )
    parser.add_argument(
        "--config-root",
        type=Path,
        default=Path.home(),
        help="Home directory containing Agent configuration.",
    )
    parser.add_argument(
        "--repo-path",
        type=Path,
        help=(
            "Optional local vLLM-Omni Git checkout used by Strict mode. "
            "Saved in ~/.infermatrix-copilot/.env."
        ),
    )
    parser.add_argument("--development", action="store_true", help="Use the legacy uvx runtime and copied skills for development.")
    parser.add_argument("--cli-only", action="store_true", help="Install the managed CLI without registering MCP hosts.")
    parser.add_argument("--local-cli-wrapper", type=Path, help="Also create an owned CLI wrapper at this path.")
    parser.add_argument("--channel", choices=("stable", "preview"), default="stable")
    parser.add_argument("--public-key", type=Path, help="Trusted Ed25519 release keys in schema-1 JSON format.")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    config_root = args.config_root.expanduser().resolve()
    try:
        repo_path = _validate_repo_path(args.repo_path)
    except InstallError as exc:
        print(f"Installation incomplete: {exc}", file=sys.stderr)
        return 1
    agents = [] if args.cli_only else list(dict.fromkeys(args.agent or _detect_agents(config_root)))

    if args.dry_run:
        selected = "managed CLI" if args.cli_only else (", ".join(agents) if agents else "generic MCP config")
        print(f"Would install: {selected}")
        print(f"Config root: {config_root}")
        print(f"Strict config: {_strict_config_path(config_root)}")
        print("Mode: " + ("development" if args.development else f"managed ({args.channel})"))
        if args.local_cli_wrapper:
            print(f"Local CLI wrapper: {args.local_cli_wrapper.expanduser().absolute()}")
        return 0

    try:
        strict_config = _ensure_strict_config(config_root, repo_path)
        command = SERVER_COMMAND if args.development else _bootstrap_managed(config_root, args)
        if not args.development:
            wrapper = _write_cli_wrapper(config_root / STRICT_CONFIG_DIR, command)
            print(f"Managed CLI: {wrapper}")
            if args.local_cli_wrapper:
                local_wrapper = _write_cli_wrapper(config_root / STRICT_CONFIG_DIR, command, args.local_cli_wrapper)
                print(f"Local CLI wrapper: {local_wrapper}")
    except (InstallError, OSError) as exc:
        print(f"Installation incomplete: {exc}", file=sys.stderr)
        return 1
    if args.cli_only:
        return 0

    if not agents:
        output = PROJECT_ROOT / "infermatrix-copilot.mcp.json"
        skills_root = config_root / STRICT_CONFIG_DIR / "skills"
        try:
            _write_generic_config(output, command)
            _install_skills(skills_root, managed=not args.development, home=config_root / STRICT_CONFIG_DIR)
        except (InstallError, OSError) as exc:
            print(f"Installation incomplete: {exc}", file=sys.stderr)
            return 1
        print(f"No known Agent detected. MCP config written to:\n  {output}")
        print(f"Portable Skills:\n  {SKILLS_SOURCE if args.development else skills_root}")
        print(f"Strict config:\n  {strict_config}")
        return 0

    failures = []
    installers = {
        "codex": _install_codex,
        "claude": _install_claude,
        "cursor": _install_cursor,
        "zcode": _install_zcode,
    }
    for agent in agents:
        try:
            installers[agent](config_root, command, managed=not args.development)
            if not args.development:
                _record_install(config_root / STRICT_CONFIG_DIR, "registrations", agent, {"config_root": str(config_root), "command": list(command)})
            print(f"Installed for {agent}.")
        except (InstallError, OSError) as exc:
            failures.append(f"{agent}: {exc}")

    if failures:
        print("Installation incomplete:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    print(f"Strict config: {strict_config}")
    print("Set ANTHROPIC_API_KEY or OPENAI_API_KEY there before Strict mode.")
    if "codex" in agents:
        print(
            "Codex: restart, then run: $imreview <PR URL>, "
            "$imcoding <goal or design>, $imcifix <issue URL>, "
            "or $imupdate <repository>"
        )
    if any(agent != "codex" for agent in agents):
        print(
            "Claude/Cursor/ZCode: restart, then run: /imreview <PR URL>, "
            "/imcoding <goal or design>, /imcifix <issue URL>, "
            "or /imupdate <repository>"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
