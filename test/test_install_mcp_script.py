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

    assert installer.main(["--config-root", str(tmp_path)]) == 0

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
