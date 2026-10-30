import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_mcp.py"


def _load_installer():
    spec = importlib.util.spec_from_file_location("install_mcp", INSTALLER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_detected_agent_writes_generic_config(monkeypatch, tmp_path):
    installer = _load_installer()
    monkeypatch.setattr(installer, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(installer, "_detect_agents", lambda root: [])

    assert installer.main(["--development", "--config-root", str(tmp_path)]) == 0

    generated = tmp_path / "infermatrix-copilot.mcp.json"
    config = json.loads(generated.read_text(encoding="utf-8"))
    server = config["mcpServers"]["infermatrix-copilot"]
    assert server["command"] == "uvx"
    assert "infermatrix-copilot-mcp" in server["args"]
    strict_config = tmp_path / ".infermatrix-copilot" / ".env"
    assert strict_config.is_file()
    assert "REPO_FULL_NAMES=" in strict_config.read_text(encoding="utf-8")


def test_repo_path_is_saved_for_strict_without_overwriting_secret(
    monkeypatch, tmp_path
):
    installer = _load_installer()
    monkeypatch.setattr(installer, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(installer, "_detect_agents", lambda root: [])
    repo = tmp_path / "vllm-omni"
    (repo / ".git").mkdir(parents=True)
    strict_config = tmp_path / ".infermatrix-copilot" / ".env"
    strict_config.parent.mkdir()
    strict_config.write_text(
        "ANTHROPIC_API_KEY=keep-me\nREPO_PATHS={}\n",
        encoding="utf-8",
    )

    assert installer.main([
        "--development",
        "--config-root",
        str(tmp_path),
        "--repo-path",
        str(repo),
    ]) == 0

    text = strict_config.read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY=keep-me" in text
    assert "OPENAI_API_KEY=" in text
    assert "OPENAI_BASE_URL=" in text
    assert "LLM_PROVIDER=auto" in text
    repo_paths_line = next(
        line for line in text.splitlines()
        if line.startswith("REPO_PATHS=")
    )
    assert json.loads(repo_paths_line.partition("=")[2]) == {
        "vllm-omni": str(repo)
    }
    assert (
        'REPO_FULL_NAMES={"vllm-omni": "vllm-project/vllm-omni"}'
        in text
    )


def test_cursor_install_preserves_existing_config(tmp_path):
    installer = _load_installer()
    cursor_root = tmp_path / ".cursor"
    cursor_root.mkdir()
    config_path = cursor_root / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "other": "kept",
                "mcpServers": {"existing": {"command": "existing"}},
            }
        ),
        encoding="utf-8",
    )

    installer._install_cursor(tmp_path)

    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["other"] == "kept"
    assert config["mcpServers"]["existing"]["command"] == "existing"
    assert config["mcpServers"]["infermatrix-copilot"]["command"] == "uvx"
    assert list(cursor_root.glob("mcp.json.*.bak"))
    assert (cursor_root / "skills" / "imreview" / "SKILL.md").is_file()
    update_skill = cursor_root / "skills" / "imupdate" / "SKILL.md"
    assert update_skill.is_file()
    assert (cursor_root / "skills" / "imdesign" / "SKILL.md").is_file()
    kbinit_skill = (cursor_root / "skills" / "imkbinit" / "SKILL.md").read_text(encoding="utf-8")
    assert "{{INFERMATRIX_COPILOT_ROOT}}" not in kbinit_skill and str(ROOT) in kbinit_skill
    text = update_skill.read_text(encoding="utf-8")
    assert "{{INFERMATRIX_COPILOT_ROOT}}" not in text
    assert str(ROOT) in text


def test_zcode_install_preserves_existing_config(tmp_path):
    installer = _load_installer()
    config_path = tmp_path / ".zcode" / "cli" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "hooks": {"kept": True},
                "mcp": {
                    "other": "kept",
                    "servers": {"existing": {"command": "existing"}},
                },
            }
        ),
        encoding="utf-8",
    )

    installer._install_zcode(tmp_path)

    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["hooks"] == {"kept": True}
    assert config["mcp"]["other"] == "kept"
    assert config["mcp"]["servers"]["existing"]["command"] == "existing"
    server = config["mcp"]["servers"]["infermatrix-copilot"]
    assert server["type"] == "stdio"
    assert server["command"] == "uvx"
    assert "infermatrix-copilot-mcp" in server["args"]
    assert list(config_path.parent.glob("config.json.*.bak"))
    skills_root = tmp_path / ".zcode" / "skills"
    assert (skills_root / "imreview" / "SKILL.md").is_file()
    assert (skills_root / "imdesign" / "SKILL.md").is_file()
    assert (skills_root / "imkbinit" / "SKILL.md").is_file()
    update_skill = skills_root / "imupdate" / "SKILL.md"
    assert update_skill.is_file()
    text = update_skill.read_text(encoding="utf-8")
    assert "{{INFERMATRIX_COPILOT_ROOT}}" not in text
    assert str(ROOT) in text


def test_zcode_install_creates_config_when_absent(tmp_path):
    installer = _load_installer()

    installer._install_zcode(tmp_path)

    config_path = tmp_path / ".zcode" / "cli" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["mcp"]["servers"]["infermatrix-copilot"]["command"] == "uvx"
    assert not list(config_path.parent.glob("config.json.*.bak"))


def test_zcode_install_refuses_malformed_config(tmp_path):
    installer = _load_installer()
    config_path = tmp_path / ".zcode" / "cli" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(installer.InstallError):
        installer._install_zcode(tmp_path)

    assert config_path.read_text(encoding="utf-8") == "{not json"
    assert not list(config_path.parent.glob("config.json.*.bak"))
    assert not (tmp_path / ".zcode" / "skills").exists()


def test_zcode_install_refuses_non_object_servers(tmp_path):
    installer = _load_installer()
    config_path = tmp_path / ".zcode" / "cli" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps({"mcp": {"servers": ["not", "a", "dict"]}}),
        encoding="utf-8",
    )

    with pytest.raises(installer.InstallError):
        installer._install_zcode(tmp_path)

    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["mcp"]["servers"] == ["not", "a", "dict"]


def test_zcode_detected_by_config_dir(monkeypatch, tmp_path):
    installer = _load_installer()
    monkeypatch.setattr(installer.shutil, "which", lambda command: None)
    (tmp_path / ".zcode").mkdir()

    assert installer._zcode_installed(tmp_path)
    assert "zcode" in installer._detect_agents(tmp_path)


def test_zcode_not_detected_without_cli_or_dir(monkeypatch, tmp_path):
    installer = _load_installer()
    monkeypatch.setattr(installer.shutil, "which", lambda command: None)

    assert not installer._zcode_installed(tmp_path)
    assert "zcode" not in installer._detect_agents(tmp_path)


def test_codex_install_sets_timeout_and_installs_current_skill_location(
    monkeypatch, tmp_path
):
    installer = _load_installer()
    monkeypatch.delenv("CODEX_HOME", raising=False)
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text(
        "[mcp_servers.infermatrix-copilot]\n"
        'command = "uvx"\n'
        "startup_timeout_sec = 30\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(installer.shutil, "which", lambda command: command)
    monkeypatch.setattr(installer, "_run", lambda command, message: None)
    monkeypatch.setattr(installer, "_run_quiet", lambda command: None)

    installer._install_codex(tmp_path)

    text = config_path.read_text(encoding="utf-8")
    assert "startup_timeout_sec = 120" in text
    assert text.count("startup_timeout_") == 1
    assert (tmp_path / ".agents" / "skills" / "imreview" / "SKILL.md").is_file()
    assert (tmp_path / ".agents" / "skills" / "imdesign" / "SKILL.md").is_file()
    assert (tmp_path / ".agents" / "skills" / "imkbinit" / "SKILL.md").is_file()


def test_codex_timeout_supports_quoted_server_table(tmp_path):
    installer = _load_installer()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[mcp_servers."infermatrix-copilot"]\ncommand = "uvx"\n',
        encoding="utf-8",
    )

    installer._set_codex_startup_timeout(config_path)

    text = config_path.read_text(encoding="utf-8")
    assert "startup_timeout_sec = 120" in text


def test_codex_config_path_honors_codex_home(monkeypatch, tmp_path):
    installer = _load_installer()
    custom_home = tmp_path / "custom-codex"
    monkeypatch.setenv("CODEX_HOME", str(custom_home))

    assert installer._codex_config_path(tmp_path) == custom_home / "config.toml"


def test_codex_cli_path_prefers_desktop_environment(monkeypatch, tmp_path):
    installer = _load_installer()
    desktop_cli = tmp_path / "codex.exe"
    desktop_cli.touch()
    monkeypatch.setenv("CODEX_CLI_PATH", str(desktop_cli))
    monkeypatch.setattr(
        installer.shutil,
        "which",
        lambda command: "C:/Program Files/WindowsApps/codex.exe",
    )

    assert installer._codex_cli_path() == str(desktop_cli)


def test_codex_cli_path_falls_back_to_path(monkeypatch, tmp_path):
    installer = _load_installer()
    monkeypatch.setenv(
        "CODEX_CLI_PATH",
        str(tmp_path / "missing-codex.exe"),
    )
    monkeypatch.setattr(installer.shutil, "which", lambda command: "codex")

    assert installer._codex_cli_path() == "codex"


def test_installed_explainer_video_keeps_nested_templates(tmp_path):
    installer = _load_installer()
    destination = tmp_path / "skills"
    installer._install_skills(destination)

    skill = destination / "explainer-video"
    assert (skill / "SKILL.md").is_file()
    templates = skill / "templates"
    narration = json.loads((templates / "narration.json").read_text(encoding="utf-8"))
    assert narration["scenes"]
    for name in ["tts.py", "render.py", "stage/index.html", "stage/core.js", "stage/boot.js"]:
        assert (templates / name).is_file(), name
    for scene in narration["scenes"]:
        assert (templates / "stage" / "scenes" / f"{scene['id']}.js").is_file()


def _managed_command(tmp_path):
    return [str(tmp_path / "launcher-python"), str(tmp_path / "launcher.pyz"), "--home", str(tmp_path / ".infermatrix-copilot"), "mcp"]


def test_managed_install_is_default_and_registers_stable_launcher(monkeypatch, tmp_path):
    installer = _load_installer()
    command = _managed_command(tmp_path)
    monkeypatch.setattr(installer, "_bootstrap_managed", lambda root, args: command)
    assert installer.main(["--agent", "cursor", "--config-root", str(tmp_path)]) == 0
    config = json.loads((tmp_path / ".cursor" / "mcp.json").read_text())
    server = config["mcpServers"]["infermatrix-copilot"]
    assert [server["command"], *server["args"]] == command
    skill = (tmp_path / ".cursor" / "skills" / "imreview" / "SKILL.md").read_text()
    assert 'get_workflow(skill_name="imreview", skill_protocol=1)' in skill
    assert "Immediately update" not in skill
    manifest = json.loads((tmp_path / ".infermatrix-copilot" / "install.json").read_text())
    assert "cursor" in manifest["registrations"]
    assert manifest["skill_installs"]
    assert (tmp_path / ".infermatrix-copilot" / "bin" / "infermatrix-copilot").exists()


def test_dry_run_never_bootstraps_or_writes(monkeypatch, tmp_path):
    installer = _load_installer()
    def unexpected(*args):
        raise AssertionError("dry run attempted bootstrap")
    monkeypatch.setattr(installer, "_bootstrap_managed", unexpected)
    assert installer.main(["--dry-run", "--agent", "cursor", "--config-root", str(tmp_path)]) == 0
    assert not list(tmp_path.iterdir())


def test_managed_skill_adopts_exact_legacy_and_preserves_host_assets(tmp_path):
    installer = _load_installer()
    destination = tmp_path / "skills"
    home = tmp_path / "managed"
    installer._install_skills(destination)
    installer._install_skills(destination, managed=True, home=home)
    assert "get_workflow" in (destination / "imupdate" / "SKILL.md").read_text()
    assert (destination / "imupdate" / "agents" / "openai.yaml").is_file()
    assert "{{INFERMATRIX_COPILOT_ROOT}}" not in (destination / "imupdate" / "SKILL.md").read_text()


def test_managed_skills_refuse_local_edits_without_partial_overwrite(tmp_path):
    installer = _load_installer()
    destination = tmp_path / "skills"
    home = tmp_path / "managed"
    installer._install_skills(destination, managed=True, home=home)
    modified = destination / "imupdate" / "SKILL.md"
    modified.write_text("my local instructions")
    with pytest.raises(installer.InstallError, match="preserved"):
        installer._install_skills(destination, managed=True, home=home)
    assert modified.read_text() == "my local instructions"


def test_managed_skills_remove_only_unmodified_owned_obsolete_files(monkeypatch, tmp_path):
    installer = _load_installer()
    source = tmp_path / "source"
    (source / "demo").mkdir(parents=True)
    (source / "demo" / "SKILL.md").write_text("---\nname: demo\ndescription: demo\n---\nworkflow")
    obsolete = source / "demo" / "old.txt"
    obsolete.write_text("owned")
    monkeypatch.setattr(installer, "SKILLS_SOURCE", source)
    destination = tmp_path / "skills"
    home = tmp_path / "managed"
    installer._install_skills(destination, managed=True, home=home)
    (destination / "demo" / "unknown.txt").write_text("keep")
    obsolete.unlink()
    installer._install_skills(destination, managed=True, home=home)
    assert not (destination / "demo" / "old.txt").exists()
    assert (destination / "demo" / "unknown.txt").read_text() == "keep"


def test_managed_skills_do_not_follow_skill_directory_symlinks(tmp_path):
    installer = _load_installer()
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = tmp_path / "skills"
    destination.mkdir()
    (destination / "imreview").symlink_to(outside, target_is_directory=True)
    with pytest.raises(installer.InstallError, match="preserved"):
        installer._install_skills(destination, managed=True, home=tmp_path / "managed")
    assert not list(outside.iterdir())


def test_codex_upgrade_preserves_environment_nested_tables_and_unrelated_servers(monkeypatch, tmp_path):
    installer = _load_installer()
    monkeypatch.delenv("CODEX_HOME", raising=False)
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir()
    path.write_text(
        '# user comment\n[mcp_servers.infermatrix-copilot]\ncommand = "old"\n'
        'args = [\n "--old",\n "arg",\n]\nenabled = false\n'
        '[mcp_servers.infermatrix-copilot.env]\nAPI_KEY = "keep"\n'
        '[mcp_servers.other]\ncommand = "other"\n'
    )
    installer._install_codex(tmp_path, _managed_command(tmp_path))
    config = installer.tomllib.loads(path.read_text())
    entry = config["mcp_servers"]["infermatrix-copilot"]
    assert entry["env"] == {"API_KEY": "keep"}
    assert entry["enabled"] is False
    assert entry["command"] == _managed_command(tmp_path)[0]
    assert config["mcp_servers"]["other"] == {"command": "other"}
    assert "# user comment" in path.read_text()


@pytest.mark.parametrize("host,relative,keys", [
    ("cursor", ".cursor/mcp.json", ("mcpServers",)),
    ("claude", ".claude.json", ("mcpServers",)),
    ("zcode", ".zcode/cli/config.json", ("mcp", "servers")),
])
def test_host_migration_preserves_existing_server_configuration(tmp_path, host, relative, keys):
    installer = _load_installer()
    config = {"unrelated": "keep"}
    servers = config
    for key in keys:
        servers[key] = {}
        servers = servers[key]
    servers["infermatrix-copilot"] = {"command": "old", "env": {"TOKEN": "secret"}, "disabled": True}
    servers["other"] = {"command": "other"}
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config))
    getattr(installer, "_install_" + host)(tmp_path, _managed_command(tmp_path))
    updated = json.loads(path.read_text())
    assert updated["unrelated"] == "keep"
    for key in keys:
        updated = updated[key]
    assert updated["infermatrix-copilot"]["env"] == {"TOKEN": "secret"}
    assert updated["infermatrix-copilot"]["disabled"] is True
    assert updated["other"] == {"command": "other"}


def test_generic_managed_install_also_emits_managed_skills(monkeypatch, tmp_path):
    installer = _load_installer()
    monkeypatch.setattr(installer, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(installer, "_detect_agents", lambda root: [])
    monkeypatch.setattr(installer, "_bootstrap_managed", lambda root, args: _managed_command(tmp_path))
    assert installer.main(["--config-root", str(tmp_path)]) == 0
    assert "get_workflow" in (tmp_path / ".infermatrix-copilot" / "skills" / "imreview" / "SKILL.md").read_text()
    config = json.loads((tmp_path / "infermatrix-copilot.mcp.json").read_text())
    assert config["mcpServers"]["infermatrix-copilot"]["command"] == _managed_command(tmp_path)[0]


def test_cli_only_installs_owned_local_managed_wrapper(monkeypatch, tmp_path):
    installer = _load_installer()
    command = _managed_command(tmp_path)
    monkeypatch.setattr(installer, "_bootstrap_managed", lambda root, args: command)
    wrapper = tmp_path / "checkout" / "infermatrix-copilot"
    args = ["--cli-only", "--config-root", str(tmp_path), "--local-cli-wrapper", str(wrapper)]
    assert installer.main(args) == 0
    assert "launcher.pyz" in wrapper.read_text()
    assert 'cli "$@"' in wrapper.read_text()
    assert installer.main(args) == 0
    wrapper.write_text("custom local wrapper")
    assert installer.main(args) == 1
    assert wrapper.read_text() == "custom local wrapper"


def test_cli_wrapper_adopts_exact_legacy_template_and_updates_legacy_ownership(monkeypatch, tmp_path):
    installer = _load_installer()
    source = tmp_path / "checkout"
    source.mkdir()
    monkeypatch.setattr(installer, "PROJECT_ROOT", source)
    wrapper = source / "infermatrix-copilot"
    wrapper.write_text(f'#!/usr/bin/env bash\nexec "{source}/.venv/bin/infermatrix-copilot" "$@"\n')
    manifest = source / ".install-manifest"
    manifest.write_text(".venv\n./infermatrix-copilot\nother-owned-file\n")
    installer._write_cli_wrapper(tmp_path / "managed", _managed_command(tmp_path), wrapper)
    assert "launcher.pyz" in wrapper.read_text()
    assert manifest.read_text() == ".venv\nother-owned-file\n"


def test_cli_wrapper_preserves_unowned_existing_file(tmp_path):
    installer = _load_installer()
    wrapper = tmp_path / "infermatrix-copilot"
    wrapper.write_text("my CLI wrapper")
    with pytest.raises(installer.InstallError, match="preserved"):
        installer._write_cli_wrapper(tmp_path / "managed", _managed_command(tmp_path), wrapper)
    assert wrapper.read_text() == "my CLI wrapper"


def test_dry_run_does_not_write_local_cli_wrapper(monkeypatch, tmp_path):
    installer = _load_installer()
    wrapper = tmp_path / "checkout" / "infermatrix-copilot"
    assert installer.main(["--dry-run", "--cli-only", "--config-root", str(tmp_path), "--local-cli-wrapper", str(wrapper)]) == 0
    assert not list(tmp_path.iterdir())
