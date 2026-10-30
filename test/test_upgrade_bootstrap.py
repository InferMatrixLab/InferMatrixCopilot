"""Bootstrap exercises real artifacts and persistence with process I/O isolated."""

from __future__ import annotations

import base64
import importlib.util
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from infermatrix_copilot.upgrade import bootstrap
from infermatrix_copilot.upgrade.manager import UpdateManager, python_path
from infermatrix_copilot.upgrade.storage import UpgradeError, atomic_json, read_json


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source checkout"
    package = root / "src/infermatrix_copilot"
    (package / "upgrade").mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "2.3.4"\n')
    (package / "upgrade/__init__.py").write_text('"""Recovery package."""\n')
    (package / "upgrade/launcher.py").write_text('def main():\n    print("launcher fixture")\n    return 0\n')
    (root / "release").mkdir()
    atomic_json(root / "release/trusted-keys.json", {
        "schema": 1, "keys": {"publisher": base64.b64encode(b"p" * 32).decode()},
    })
    wheel = root / "infermatrix_copilot-2.3.4-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("infermatrix_copilot/__init__.py", '__version__ = "2.3.4"\n')
        archive.writestr("infermatrix_copilot-2.3.4.dist-info/METADATA", (
            "Metadata-Version: 2.3\nName: infermatrix-copilot\nVersion: 2.3.4\n"
        ))
        archive.writestr("infermatrix_copilot-2.3.4.dist-info/WHEEL", (
            "Wheel-Version: 1.0\nGenerator: bootstrap-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ))
        archive.writestr("infermatrix_copilot-2.3.4.dist-info/RECORD", "")
    return SimpleNamespace(root=root, package=package, wheel=wheel)


@pytest.fixture
def processes(monkeypatch, source):
    calls = []
    behavior = SimpleNamespace(calls=calls, fail=None, real_run=subprocess.run)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda command: "/fixture/uv" if command == "uv" else None)

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if behavior.fail and behavior.fail(command):
            return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"fixture failure")
        if command[:2] == ["/fixture/uv", "build"]:
            outdir = Path(command[command.index("--out-dir") + 1])
            shutil.copy2(source.wheel, outdir / source.wheel.name)
        elif command[:2] == ["/fixture/uv", "venv"]:
            interpreter = python_path(Path(command[-1]))
            interpreter.parent.mkdir(parents=True)
            interpreter.write_text("interpreter fixture")
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", run)
    return behavior


def test_source_install_builds_isolated_release_and_real_recovery_zipapp(source, processes, tmp_path):
    home = tmp_path / "managed home"
    command = bootstrap.install(source.root, home, channel="preview", authoring_root=source.root)
    assert command == [str(python_path(home / "launcher/venv")), str(home / "launcher/entry.py"), "--home", str(home), "mcp"]
    assert '[sys.executable, "-I",' in (home / "launcher/entry.py").read_text()
    manager = UpdateManager(home)
    metadata = manager.installed(manager.active()["release_id"])
    assert metadata["version"] == "2.3.4"
    assert metadata["release_id"].startswith("bootstrap-2.3.4-")
    assert manager.policy()["channel"] == "preview"
    assert manager.policy()["authoring_root"] == str(source.root)
    pointer = read_json(home / "launcher/active.json")
    application = home / "launcher" / pointer["application"]
    assert metadata["launcher_application"] == pointer["application"]
    with zipfile.ZipFile(application) as archive:
        assert archive.read("infermatrix_copilot/__init__.py") == (source.package / "__init__.py").read_bytes()
        assert "__main__.py" in archive.namelist()
    result = processes.real_run([sys.executable, str(application)], capture_output=True, check=True)
    assert result.stdout.strip() == b"launcher fixture"
    health = next((command, kwargs) for command, kwargs in processes.calls if "--expected-version" in command)
    assert health[0][health[0].index("--expected-version") + 1] == "2.3.4"
    assert health[1]["env"]["COPILOT_RELEASE_ID"] == metadata["release_id"]
    assert health[1]["env"]["ALLOW_PUSH"] == "0"


def test_reinstallation_preserves_active_release_and_existing_trust_and_registry(source, processes, tmp_path):
    home = tmp_path / "managed"
    bootstrap.install(source.root, home)
    first = read_json(home / "active.json")
    trust = read_json(home / "trusted-keys.json")
    trust["keys"]["enterprise"] = base64.b64encode(b"e" * 32).decode()
    atomic_json(home / "trusted-keys.json", trust)
    registry = read_json(home / "install.json")
    registry["registrations"] = {"cursor": {"config_root": "keep"}}
    atomic_json(home / "install.json", registry)
    processes.calls.clear()
    bootstrap.install(source.root, home)
    assert read_json(home / "active.json") == first
    assert read_json(home / "trusted-keys.json")["keys"] == trust["keys"]
    assert read_json(home / "install.json")["registrations"] == registry["registrations"]
    assert not any(command[:2] == ["/fixture/uv", "build"] for command, _ in processes.calls)
    assert not any("--expected-version" in command for command, _ in processes.calls)


def test_incomplete_candidate_fails_without_activating_and_can_retry(source, processes, tmp_path):
    home = tmp_path / "managed"
    processes.fail = lambda command: "--expected-version" in command
    with pytest.raises(UpgradeError, match="previous release"):
        bootstrap.install(source.root, home)
    assert not (home / "active.json").exists()
    assert not list((home / "releases").glob("*/installed.json"))
    processes.fail = None
    bootstrap.install(source.root, home)
    assert UpdateManager(home).installed(UpdateManager(home).active()["release_id"])["healthy"]


def test_launcher_dependency_failure_is_repaired_before_retry_publishes_launcher(source, processes, tmp_path):
    home = tmp_path / "managed"
    processes.fail = lambda command: "cryptography>=42" in command
    with pytest.raises(UpgradeError):
        bootstrap.install(source.root, home)
    assert python_path(home / "launcher/venv").is_file()
    assert not (home / "launcher/active.json").exists()
    processes.fail = None
    processes.calls.clear()
    bootstrap.install(source.root, home)
    assert any("cryptography>=42" in command for command, _ in processes.calls)


def test_failed_reinstallation_preserves_previously_active_application(source, processes, tmp_path):
    home = tmp_path / "managed"
    bootstrap.install(source.root, home)
    active = (home / "active.json").read_bytes()
    # Simulate an interrupted dependency installation in the recovery environment.
    python_path(home / "launcher/venv").unlink()
    processes.fail = lambda command: "cryptography>=42" in command
    with pytest.raises(UpgradeError):
        bootstrap.install(source.root, home)
    assert (home / "active.json").read_bytes() == active


def test_installer_bootstrap_interface_uses_real_bootstrap_and_managed_registration(monkeypatch, source, processes, tmp_path):
    spec = importlib.util.spec_from_file_location("bootstrap_installer_test", ROOT / "scripts/install_mcp.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    # Wrapper metadata comes from the actual host skill sources; application
    # bootstrap comes from a separate source root so its version can differ.
    monkeypatch.setattr(installer, "PROJECT_ROOT", source.root)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    config_root = tmp_path / "config"
    assert installer.main(["--agent", "cursor", "--config-root", str(config_root)]) == 0
    home = config_root / ".infermatrix-copilot"
    server = json.loads((config_root / ".cursor/mcp.json").read_text())["mcpServers"]["infermatrix-copilot"]
    assert [server["command"], *server["args"]] == read_json(home / "install.json")["launcher_command"]
    assert "get_workflow" in (config_root / ".cursor/skills/imcoding/SKILL.md").read_text()
    assert UpdateManager(home).status()["active_version"] == "2.3.4"
    assert read_json(home / "install.json")["registrations"]["cursor"]


@pytest.mark.parametrize("metadata", [
    "Metadata-Version: 2.3\nName: infermatrix-copilot\n",
    "Metadata-Version: 2.3\nName: infermatrix-copilot\nVersion: ../../outside\n",
    "Metadata-Version: 2.3\nName: another-project\nVersion: 2.3.4\n",
])
def test_initial_wheel_rejects_invalid_package_metadata(tmp_path, metadata):
    wheel = tmp_path / "invalid.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("infermatrix_copilot-2.3.4.dist-info/METADATA", metadata)
    with pytest.raises(UpgradeError, match="metadata"):
        bootstrap._version(wheel)


@pytest.mark.parametrize("value", [[], {"schema": 1, "keys": []}, {"schema": 1, "keys": {"bad": "short"}}])
def test_invalid_trust_store_is_rejected_before_writes(source, processes, tmp_path, value):
    path = tmp_path / "invalid-trust.json"
    atomic_json(path, value)
    home = tmp_path / "managed"
    with pytest.raises(UpgradeError):
        bootstrap.install(source.root, home, public_key=path)
    assert not home.exists()


def _mock_distribution(monkeypatch, direct):
    class Distribution:
        version = "2.3.4"

        def read_text(self, name):
            assert name == "direct_url.json"
            return json.dumps(direct)

    monkeypatch.setattr(bootstrap.importlib.metadata, "distribution", lambda name: Distribution())


def test_distribution_bootstrap_pins_exact_git_commit(monkeypatch, tmp_path):
    commit = "a" * 40
    _mock_distribution(monkeypatch, {"url": "https://github.com/example/copilot.git", "vcs_info": {"vcs": "git", "commit_id": commit}})
    captured = {}

    def install(source_root, home, **kwargs):
        captured.update(source_root=source_root, home=home, **kwargs)
        return ["launcher"]

    monkeypatch.setattr(bootstrap, "install", install)
    assert bootstrap.install_distribution(tmp_path) == ["launcher"]
    assert captured["distribution_spec"].endswith("@" + commit)
    assert captured["source_root"] is None


@pytest.mark.parametrize("direct", [
    {"url": "https://github.com/example/copilot.git", "vcs_info": {"vcs": "git", "commit_id": "z" * 40}},
    {"url": "https://example.com/copilot.whl", "archive_info": {"hashes": {"md5": "a" * 32}}},
    {"url": "http://example.com/copilot.whl", "archive_info": {"hashes": {"sha256": "a" * 64}}},
])
def test_distribution_bootstrap_rejects_mutable_or_invalid_provenance(monkeypatch, tmp_path, direct):
    _mock_distribution(monkeypatch, direct)
    with pytest.raises(UpgradeError):
        bootstrap.install_distribution(tmp_path)


def test_distribution_bootstrap_pins_wheel_archive_hash(monkeypatch, tmp_path):
    digest = "A" * 64
    _mock_distribution(monkeypatch, {"url": "https://example.com/copilot.whl", "archive_info": {"hashes": {"sha256": digest}}})
    captured = {}

    def install(source_root, home, **kwargs):
        captured.update(kwargs)
        return ["launcher"]

    monkeypatch.setattr(bootstrap, "install", install)
    bootstrap.install_distribution(tmp_path)
    assert captured["distribution_spec"].endswith("#sha256=" + digest.lower())


def test_trust_key_identifier_collision_preserves_existing_trust_and_active(source, processes, tmp_path):
    home = tmp_path / "managed"
    bootstrap.install(source.root, home)
    trust = (home / "trusted-keys.json").read_bytes()
    active = (home / "active.json").read_bytes()
    replacement = tmp_path / "replacement.json"
    atomic_json(replacement, {"schema": 1, "keys": {"publisher": base64.b64encode(b"x" * 32).decode()}})
    with pytest.raises(UpgradeError, match="identifier"):
        bootstrap.install(source.root, home, public_key=replacement)
    assert (home / "trusted-keys.json").read_bytes() == trust
    assert (home / "active.json").read_bytes() == active


def test_stable_entry_uses_authoritative_activation_recovery(source, processes, tmp_path):
    home = tmp_path / "managed"
    bootstrap.install(source.root, home)
    application = home / "launcher/updater-authoritative.pyz"
    with zipfile.ZipFile(application, "w") as archive:
        archive.writestr("__main__.py", 'print("authoritative activation")\n')
    active = read_json(home / "active.json")
    active["launcher_application"] = application.name
    atomic_json(home / "active.json", active)
    result = processes.real_run([sys.executable, str(home / "launcher/entry.py")], capture_output=True, check=True)
    assert result.stdout.strip() == b"authoritative activation"


def test_stable_entry_supports_legacy_recovery_pointer(source, processes, tmp_path):
    home = tmp_path / "managed"
    bootstrap.install(source.root, home)
    active = read_json(home / "active.json")
    active.pop("launcher_application", None)
    atomic_json(home / "active.json", active)
    result = processes.real_run([sys.executable, str(home / "launcher/entry.py")], capture_output=True, check=True)
    assert result.stdout.strip() == b"launcher fixture"


def test_existing_bootstrap_merges_explicit_trust_into_activation_snapshot(source, processes, tmp_path):
    home = tmp_path / "managed"
    bootstrap.install(source.root, home)
    active = read_json(home / "active.json")
    added = base64.b64encode(b"a" * 32).decode()
    trust = tmp_path / "additional-trust.json"
    atomic_json(trust, {"schema": 1, "keys": {"additional": added}})
    bootstrap.install(source.root, home, public_key=trust)
    updated = read_json(home / "active.json")
    assert updated["release_id"] == active["release_id"]
    assert updated["launcher_application"] == active["launcher_application"]
    assert updated["trusted_keys"] == {**active["trusted_keys"], "additional": added}


def test_reinstall_does_not_resurrect_keys_retired_by_signed_release(source, processes, tmp_path):
    home = tmp_path / "managed"
    bootstrap.install(source.root, home)
    active = read_json(home / "active.json")
    rotated = {"rotated": base64.b64encode(b"r" * 32).decode()}
    active["trusted_keys"] = rotated
    atomic_json(home / "active.json", active)
    # An empty source trust map has no new installer-authorized additions.
    atomic_json(source.root / "release/trusted-keys.json", {"schema": 1, "keys": {}})
    bootstrap.install(source.root, home)
    assert read_json(home / "active.json")["trusted_keys"] == rotated
    assert "publisher" in read_json(home / "trusted-keys.json")["keys"]


def test_reinstallation_updates_recovery_without_replacing_application(source, processes, tmp_path):
    home = tmp_path / "managed"
    bootstrap.install(source.root, home)
    before = read_json(home / "active.json")
    (source.package / "upgrade/launcher.py").write_text('def main():\n    print("repaired recovery")\n    return 0\n')
    bootstrap.install(source.root, home)
    after = read_json(home / "active.json")
    assert after["release_id"] == before["release_id"]
    assert after["trusted_keys"] == before["trusted_keys"]
    assert after["launcher_application"] != before["launcher_application"]
    result = processes.real_run([sys.executable, str(home / "launcher/entry.py")], capture_output=True, check=True)
    assert result.stdout.strip() == b"repaired recovery"


def test_recovery_repair_health_failure_preserves_authoritative_activation(source, processes, tmp_path):
    home = tmp_path / "managed"
    bootstrap.install(source.root, home)
    before = (home / "active.json").read_bytes()
    (source.package / "upgrade/launcher.py").write_text('def main():\n    return 1\n')
    processes.fail = lambda command: "-I" in command and "upgrade" in command
    with pytest.raises(UpgradeError):
        bootstrap.install(source.root, home)
    assert (home / "active.json").read_bytes() == before
    result = processes.real_run([sys.executable, str(home / "launcher/entry.py")], capture_output=True, check=True)
    assert result.stdout.strip() == b"launcher fixture"
