import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "plugins" / "infermatrix-copilot" / "skills" / "imupdate" / "SKILL.md"


def test_imupdate_skill_supports_three_input_modes() -> None:
    text = SKILL.read_text(encoding="utf-8")

    assert "/imupdate <path-or-repository> [target-tag-or-sha]" in text
    assert "### Local Git path" in text
    assert "### Repository name, alias, or URL" in text
    for alias in ("`vllm-omni`", "`vllmomni`", "`vllm omni`"):
        assert alias in text
    assert "temporary clone" in text
    assert "Never call that result `CLEAN`" in text
    assert "Never silently downgrade" in text

    assert "upstream.audited_sha" in text
    assert "tools/audit_vllm_omni_release.py" in text
    for argument in ("--from", "--to", "--repo", "--mode report-only"):
        assert argument in text
    assert "--mode enforce" in text
    assert "generate or rewrite owner rules automatically" in text


def test_installer_and_docs_expose_imupdate(monkeypatch, tmp_path) -> None:
    spec = importlib.util.spec_from_file_location("imupdate_installer", ROOT / "scripts/install_mcp.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    command = ["python", "managed-launcher.pyz", "--home", str(tmp_path / ".infermatrix-copilot"), "mcp"]
    for host, skills_root in (
        ("codex", ".agents/skills"),
        ("claude", ".claude/skills"),
        ("cursor", ".cursor/skills"),
        ("zcode", ".zcode/skills"),
    ):
        getattr(installer, "_install_" + host)(tmp_path, command, managed=True)
        installed = tmp_path / skills_root / "imupdate"
        text = (installed / "SKILL.md").read_text(encoding="utf-8")
        assert 'get_workflow(skill_name="imupdate", skill_protocol=1)' in text
        assert "name: imupdate" in text
        assert (installed / "agents/openai.yaml").is_file()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "/imupdate /path/to/vllm-omni" in readme
    assert "$imupdate vllm-omni" in readme
    assert "$imdesign" in readme
    assert "/imdesign 给 scheduler 加抢占开关" in readme
