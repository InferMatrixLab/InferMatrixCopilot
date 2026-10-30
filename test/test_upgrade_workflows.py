import json
import os
from pathlib import Path, PureWindowsPath
import sys
from types import SimpleNamespace

import pytest

from infermatrix_copilot.upgrade import workflows


def test_workflow_is_release_local_and_returns_complete_references(tmp_path, monkeypatch):
    package = tmp_path / "installed-package"
    skill = package / "_runtime" / "host_skills" / "imdesign"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: imdesign\n---\nFull workflow.\n")
    (skill / "references" / "guide.md").write_text("Complete reference.")
    monkeypatch.setattr(workflows, "_PACKAGE", package)
    monkeypatch.setenv("COPILOT_RELEASE_ID", "pinned-release")
    result = workflows.get_workflow("imdesign")
    assert result["release_id"] == "pinned-release"
    assert result["content"].endswith("Full workflow.\n")
    assert Path(result["references"]["references/guide.md"]).read_text() == "Complete reference."


def test_workflow_reference_keys_use_forward_slashes_on_windows(tmp_path, monkeypatch):
    package = tmp_path / "installed-package"
    skill = package / "_runtime/host_skills/imdesign"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: imdesign\n---\nFull workflow.\n")
    reference = skill / "references/guide.md"
    reference.write_text("Complete reference.")
    monkeypatch.setattr(workflows, "_PACKAGE", package)
    relative_to = type(reference).relative_to

    def windows_relative_to(path, *args, **kwargs):
        relative = relative_to(path, *args, **kwargs)
        return PureWindowsPath(relative) if path == reference else relative

    # Exercise the public response using Windows relative-path semantics even
    # when the files themselves live on a POSIX test runner.
    with monkeypatch.context() as windows:
        windows.setattr(type(reference), "relative_to", windows_relative_to)
        result = workflows.get_workflow("imdesign")

    assert result["references"] == {"references/guide.md": str(reference.resolve())}


@pytest.mark.parametrize("name,protocol", [("../imdesign", 1), ("/imdesign", 1),
                                           ("imdesign", 2), ("imdesign", True)])
def test_invalid_protocol_and_paths_are_rejected(name, protocol):
    with pytest.raises(ValueError):
        workflows.get_workflow(name, protocol)


def test_managed_authoring_workflow_requires_separate_source_checkout(tmp_path, monkeypatch):
    package = tmp_path / "release" / "package"
    skill = package / "_runtime" / "host_skills" / "imupdate"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("Edit {{INFERMATRIX_COPILOT_ROOT}}/knowledge.")
    monkeypatch.setattr(workflows, "_PACKAGE", package)
    monkeypatch.setenv("COPILOT_RELEASE_ROOT", str(tmp_path / "release"))
    monkeypatch.delenv("COPILOT_AUTHORING_ROOT", raising=False)
    with pytest.raises(ValueError, match="COPILOT_AUTHORING_ROOT"):
        workflows.get_workflow("imupdate")
    authoring = tmp_path / "authoring"
    (authoring / "knowledge").mkdir(parents=True)
    (authoring / "tools").mkdir()
    (authoring / "pyproject.toml").touch()
    (authoring / "knowledge" / "AGENTS.md").touch()
    monkeypatch.setenv("COPILOT_AUTHORING_ROOT", str(authoring))
    result = workflows.get_workflow("imupdate")
    assert result["content"] == f"Edit {authoring}/knowledge."
    assert result["authoring_root"] == str(authoring)


def test_authoring_root_cannot_be_inside_managed_release(tmp_path, monkeypatch):
    (tmp_path / "knowledge").mkdir()
    (tmp_path / "tools").mkdir()
    (tmp_path / "pyproject.toml").touch()
    (tmp_path / "knowledge" / "AGENTS.md").touch()
    monkeypatch.setenv("COPILOT_RELEASE_ROOT", str(tmp_path))
    monkeypatch.setenv("COPILOT_AUTHORING_ROOT", str(tmp_path))
    with pytest.raises(ValueError, match="outside the immutable"):
        workflows._authoring_root()


def _authoring(path):
    (path / "knowledge").mkdir(parents=True)
    (path / "tools").mkdir()
    (path / "adapters").mkdir()
    (path / "pyproject.toml").touch()
    (path / "knowledge/AGENTS.md").touch()
    return path


def test_managed_workflow_uses_physical_cli_and_protects_skill_sources(tmp_path, monkeypatch):
    package = tmp_path / "package"
    skill = package / "_runtime/host_skills/imdesign"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: imdesign\n---\n\n# Design workflow\n\n"
        "Run ./infermatrix-copilot --help before continuing.\n"
    )
    monkeypatch.setattr(workflows, "_PACKAGE", package)
    monkeypatch.setenv("COPILOT_RELEASE_ROOT", "/managed/release")
    monkeypatch.setenv("COPILOT_RUNTIME_PYTHON", sys.executable)
    result = workflows.get_workflow("imdesign")
    assert result["cli_argv"] == [sys.executable, "-I", "-m", "infermatrix_copilot"]
    assert "./infermatrix-copilot" not in result["content"]
    assert "are immutable" in result["content"]
    assert "get_workflow('imupdate', 1)" in result["content"]


@pytest.mark.parametrize("windows_commands", [False, True])
def test_authoring_workflow_uses_release_runner_for_cli_and_audits(tmp_path, monkeypatch, windows_commands):
    authoring = _authoring(tmp_path / "authoring")
    monkeypatch.setenv("COPILOT_RELEASE_ROOT", str(tmp_path / "managed"))
    monkeypatch.setenv("COPILOT_RUNTIME_PYTHON", sys.executable)
    monkeypatch.setenv("COPILOT_AUTHORING_ROOT", str(authoring))
    if windows_commands:
        monkeypatch.setattr(workflows, "os", SimpleNamespace(name="nt", environ=os.environ))
    result = workflows.get_workflow("imkbinit")
    assert result["cli_argv"] == [sys.executable, "-I", "-m", workflows.__name__,
                                  "--workspace", str(authoring), "cli", "--"]
    assert "uvx --from" not in result["content"]
    assert "PYTHONPATH=src python tools/" not in result["content"]
    assert result["support_commands"]["audit_knowledge_depth"] in result["content"]
    update = workflows.get_workflow("imupdate")
    assert "python <infermatrix-root>/tools/" not in update["content"]
    assert update["support_commands"]["audit_vllm_omni_release"] in update["content"]
    assert update["support_commands"]["check_knowledge_tree"] in update["content"]


def test_windows_authoring_launcher_preserves_literal_paths(tmp_path, monkeypatch):
    package = tmp_path / "installed-package"
    skill = package / "_runtime/host_skills/imkbinit"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: imkbinit\n---\n\n# Init workflow\n\n"
        "## Launcher\n\nuvx --from old-checkout infermatrix-copilot\n\n"
        "## 1. Resolve the repository\n\nContinue.\n"
    )
    python = str(PureWindowsPath(r"C:\hostedtoolcache\Python\3.12\python.exe"))
    authoring = PureWindowsPath(r"C:\Users\O'Connor\authoring\1")
    monkeypatch.setattr(workflows, "_PACKAGE", package)
    monkeypatch.setattr(workflows, "_authoring_root", lambda: authoring)
    monkeypatch.setattr(workflows, "current_release", lambda: {
        "python": python, "release_id": "windows-release"})
    monkeypatch.setenv("COPILOT_RELEASE_ROOT", str(tmp_path / "managed"))
    # Replace only this module's OS facade; changing os.name globally would
    # make pathlib try to access a Windows filesystem on POSIX.
    monkeypatch.setattr(workflows, "os", SimpleNamespace(name="nt", environ=os.environ))

    result = workflows.get_workflow("imkbinit")

    assert result["cli_argv"] == [python, "-I", "-m", workflows.__name__,
                                  "--workspace", str(authoring), "cli", "--"]
    expected = ("& '" + python + "' '-I' '-m' '" + workflows.__name__
                + "' '--workspace' 'C:\\Users\\O''Connor\\authoring\\1' 'cli' '--'")
    assert f"```text\n{expected}\n```" in result["content"]
    assert "uvx --from" not in result["content"]
    assert "## 1. Resolve the repository\n\nContinue." in result["content"]


def test_support_runner_uses_bundled_tool_with_explicit_workspace(tmp_path, monkeypatch):
    authoring = _authoring(tmp_path / "authoring")
    package = tmp_path / "package"
    tools = package / "_runtime/tools"
    tools.mkdir(parents=True)
    output = tmp_path / "output.json"
    (tools / "audit_knowledge_depth.py").write_text(
        "import json, sys\nfrom pathlib import Path\n"
        f"Path({str(output)!r}).write_text(json.dumps({{'origin': 'release', 'argv': sys.argv, "
        "'root': 'explicit'}))\n")
    (authoring / "tools/audit_knowledge_depth.py").write_text("raise RuntimeError('old source code executed')")
    monkeypatch.setattr(workflows, "_PACKAGE", package)
    old_cwd = Path.cwd()
    assert workflows._run_tool("audit_knowledge_depth", ["--repo", "example"], authoring) == 0
    data = json.loads(output.read_text())
    assert data["origin"] == "release"
    assert data["argv"][-2:] == ["--root", str(authoring)]
    assert Path.cwd() == old_cwd


def test_support_validator_keeps_release_code_and_binds_authoring_data(tmp_path, monkeypatch):
    authoring = _authoring(tmp_path / "authoring")
    package = tmp_path / "package"
    tools = package / "knowledge/tools"
    tools.mkdir(parents=True)
    output = tmp_path / "output.json"
    (tools / "check_knowledge_tree.py").write_text(
        "import json\nfrom pathlib import Path\n"
        f"Path({str(output)!r}).write_text(json.dumps({{'origin': 'release', "
        "'knowledge': str(Path(__file__).resolve().parents[1])}))\n")
    monkeypatch.setattr(workflows, "_PACKAGE", package)
    assert workflows._run_tool("check_knowledge_tree", [], authoring) == 0
    assert json.loads(output.read_text()) == {"origin": "release", "knowledge": str(authoring / "knowledge")}


def test_support_cli_uses_workspace_data_without_mutating_caller_environment(tmp_path, monkeypatch):
    import subprocess

    authoring = _authoring(tmp_path / "authoring")
    observed = []
    original = os.environ.get("KNOWLEDGE_DIR")
    monkeypatch.setenv("KNOWLEDGE_ROOT", str(tmp_path / "unrelated-active-snapshot"))
    monkeypatch.setattr(subprocess, "call", lambda command, **kwargs:
                        observed.append((command, kwargs)) or 7)
    assert workflows.main(["--workspace", str(authoring), "cli", "--", "kb", "status"]) == 7
    assert observed[0][0][-4:] == ["cli", "--", "kb", "status"]
    assert observed[0][1]["env"]["KNOWLEDGE_DIR"] == str(authoring / "knowledge")
    assert observed[0][1]["env"]["ADAPTERS_DIR"] == str(authoring / "adapters")
    assert "KNOWLEDGE_ROOT" not in observed[0][1]["env"]
    assert observed[0][1]["cwd"] == authoring
    assert os.environ.get("KNOWLEDGE_DIR") == original
    assert os.environ["KNOWLEDGE_ROOT"] == str(tmp_path / "unrelated-active-snapshot")


def test_support_registry_binds_an_independent_repository_tool(tmp_path, monkeypatch):
    authoring = _authoring(tmp_path / "authoring")
    package = tmp_path / "package"
    runtime = package / "_runtime"
    (runtime / "tools").mkdir(parents=True)
    output = tmp_path / "arguments.json"
    (runtime / "tools" / "project_audit.py").write_text(
        "import json, sys\nfrom pathlib import Path\n"
        f"Path({str(output)!r}).write_text(json.dumps(sys.argv[1:]))\n")
    (runtime / "workflow-tools.json").write_text(json.dumps({"schema": 1, "tools": {
        "project_audit": {"path": "tools/project_audit.py", "workspace_options": {
            "--project": ".", "--manifest": "adapters/project/manifest.yaml"}}
    }}))
    monkeypatch.setattr(workflows, "_PACKAGE", package)
    assert workflows._run_tool("project_audit", [], authoring) == 0
    assert json.loads(output.read_text()) == [
        "--project", str(authoring), "--manifest", str(authoring / "adapters/project/manifest.yaml")]


@pytest.mark.parametrize("path", ["../outside.py", "/absolute.py", "tools/../../outside.py", "tools\\outside.py"])
def test_support_registry_refuses_escaping_code_paths(tmp_path, monkeypatch, path):
    runtime = tmp_path / "package/_runtime"
    runtime.mkdir(parents=True)
    (runtime / "workflow-tools.json").write_text(json.dumps({"schema": 1, "tools": {
        "project_audit": {"path": path}
    }}))
    monkeypatch.setattr(workflows, "_PACKAGE", runtime.parent)
    with pytest.raises(ValueError, match="registry is invalid"):
        workflows._tool_registry()
