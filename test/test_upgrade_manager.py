from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from infermatrix_copilot.upgrade import launcher
from infermatrix_copilot.upgrade.cli import main
from infermatrix_copilot.upgrade.manager import UpdateManager, python_path
from infermatrix_copilot.upgrade.releases import canonical_manifest
from infermatrix_copilot.upgrade.storage import UpgradeError, atomic_json, file_lock, read_json


@pytest.fixture
def installation(tmp_path, monkeypatch):
    root = tmp_path / "home"
    monkeypatch.setenv("INFERMATRIX_COPILOT_HOME", str(root))
    manager = UpdateManager(root, clock=lambda: 1000)
    old = seed(manager, "1.0.0-old", "1.0.0")
    atomic_json(root / "active.json", {"release_id": old["release_id"], "previous_release_id": ""})
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    atomic_json(root / "trusted-keys.json", {"schema": 1, "keys": {"test": base64.b64encode(public).decode()}})
    interpreter = python_path(root / "launcher/venv")
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"independent recovery interpreter")
    return manager, private


def seed(manager, identity, version):
    interpreter = python_path(manager.root / "releases" / identity / "venv").absolute()
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.write_bytes(b"test interpreter")
    metadata = {"release_id": identity, "version": version, "python": str(interpreter),
                "healthy": True, "bundle_sha256": "0" * 64}
    atomic_json(interpreter.parent.parent.parent / "installed.json", metadata)
    return metadata


def publish(manager, private, *, version="1.1.0", sequence=1, corrupt_bundle=False,
            key_id="test", trusted_keys=None):
    feed = manager.root.parent / "feed.json"
    artifact = manager.root.parent / f"bundle-{version}.tar.gz"
    wheel_output = io.BytesIO()
    package = Path(__file__).resolve().parents[1] / "src/infermatrix_copilot"
    with zipfile.ZipFile(wheel_output, "w") as archive:
        archive.writestr("infermatrix_copilot/__init__.py", (package / "__init__.py").read_bytes())
        for path in (package / "upgrade").glob("*.py"):
            archive.writestr(f"infermatrix_copilot/upgrade/{path.name}", path.read_bytes())
        archive.writestr("infermatrix_copilot/_runtime/trusted-keys.json",
                         json.dumps({"schema": 1, "keys": trusted_keys or {}}))
    wheel = wheel_output.getvalue()
    with tarfile.open(artifact, "w:gz") as archive:
        for name, data in (("wheel/infermatrix_copilot.whl", wheel),
                           ("requirements.txt", b"dependency==1 --hash=sha256:" + b"0" * 64)):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    manifest = {"schema": 1, "release_id": f"{version}-new", "version": version,
                "commit": "a" * 40, "channel": "stable", "sequence": sequence,
                "published_at": "2026-10-30T00:00:00Z", "requires_python": ">=3.11",
                "min_launcher_version": 1, "runtime_schema": 1, "skill_protocol": 1,
                "knowledge_formats": [2], "artifacts": {
                    "bundle": {"url": artifact.as_uri(), "size": artifact.stat().st_size,
                               "sha256": "f" * 64 if corrupt_bundle else hashlib.sha256(artifact.read_bytes()).hexdigest()},
                    "wheel": {"path": "wheel/infermatrix_copilot.whl", "sha256": hashlib.sha256(wheel).hexdigest()}}}
    envelope = {"manifest": manifest, "key_id": key_id,
                "signature": base64.b64encode(private.sign(canonical_manifest(manifest))).decode()}
    atomic_json(feed, envelope)
    manager.configure(feed_url=feed.as_uri())
    return envelope


def install_boundary(monkeypatch, manager, *, unhealthy=False):
    commands = []

    def command(argv, **kwargs):
        commands.append(argv)
        if argv[1] == "venv":
            interpreter = python_path(Path(argv[-1]))
            interpreter.parent.mkdir(parents=True)
            interpreter.write_bytes(b"candidate interpreter")

    def health(metadata):
        if unhealthy:
            raise UpgradeError("bad candidate")

    monkeypatch.setattr("infermatrix_copilot.upgrade.manager.run_command", command)
    monkeypatch.setattr("infermatrix_copilot.upgrade.manager.shutil.which", lambda name: "/tools/uv")
    monkeypatch.setattr(manager, "health", health)
    return commands


def test_signed_upgrade_is_atomic_and_retains_old_environment(installation, monkeypatch):
    manager, key = installation
    envelope = publish(manager, key)
    commands = install_boundary(monkeypatch, manager)
    assert manager.check(install=True)["state"] == "upgraded"
    assert manager.active()["release_id"] == "1.1.0-new"
    assert manager.active()["previous_release_id"] == "1.0.0-old"
    assert manager.active()["launcher_application"].startswith("updater-")
    assert manager.installed("1.0.0-old")["version"] == "1.0.0"
    assert manager.installed("1.1.0-new")["bundle_sha256"] == envelope["manifest"]["artifacts"]["bundle"]["sha256"]
    dependency_install = next(argv for argv in commands if "--require-hashes" in argv)
    assert "--only-binary" in dependency_install
    assert manager.check()["state"] == "current"


def test_check_does_not_install_or_activate(installation):
    manager, key = installation
    publish(manager, key)
    assert manager.check()["state"] == "available"
    assert manager.active()["release_id"] == "1.0.0-old"
    assert not (manager.root / "releases/1.1.0-new").exists()


def test_failed_health_quarantines_candidate_and_preserves_active(installation, monkeypatch):
    manager, key = installation
    publish(manager, key)
    install_boundary(monkeypatch, manager, unhealthy=True)
    with pytest.raises(UpgradeError, match="quarantined"):
        manager.check(install=True)
    assert manager.active()["release_id"] == "1.0.0-old"
    assert "1.1.0-new" in manager.status()["quarantined"]
    with pytest.raises(UpgradeError, match="quarantined"):
        manager.check(install=True)


def test_corrupt_bundle_never_runs_installer_or_changes_pointer(installation, monkeypatch):
    manager, key = installation
    publish(manager, key, corrupt_bundle=True)
    commands = install_boundary(monkeypatch, manager)
    with pytest.raises(UpgradeError, match="hash"):
        manager.check(install=True)
    assert commands == []
    assert manager.active()["release_id"] == "1.0.0-old"


def test_interrupted_install_retries_without_moving_venv(installation, monkeypatch):
    manager, key = installation
    publish(manager, key)
    incomplete = manager.root / "releases/1.1.0-new"
    incomplete.mkdir(parents=True)
    (incomplete / "half-installed").write_text("interrupted")
    install_boundary(monkeypatch, manager)
    manager.check(install=True)
    assert not (incomplete / "half-installed").exists()
    assert manager.installed("1.1.0-new")["python"] == str(python_path(incomplete / "venv"))


def test_offline_failure_is_retryable_with_backoff(installation):
    manager, key = installation
    manager.configure(feed_url=(manager.root.parent / "missing.json").as_uri())
    with pytest.raises(UpgradeError, match="download"):
        manager.check(install=True)
    status = manager.status()
    assert status["active_release"] == "1.0.0-old"
    assert status["next_check"] == 1060
    assert not manager.due()


def test_concurrent_updater_does_not_touch_state(installation):
    manager, key = installation
    with file_lock(manager.root / "locks/update.lock") as acquired:
        assert acquired
        assert manager.check(install=True) == {"state": "busy"}
    assert not (manager.root / "updates.json").exists()


def test_feed_cannot_roll_back_sequence_or_version(installation):
    manager, key = installation
    publish(manager, key, sequence=3)
    manager.check()
    publish(manager, key, version="1.2.0", sequence=2)
    with pytest.raises(UpgradeError, match="sequence"):
        manager.check()
    publish(manager, key, version="0.9.0", sequence=4)
    with pytest.raises(UpgradeError, match="increase"):
        manager.check(install=True)


def test_pinned_version_defers_auto_updates_and_unpin_restores_them(installation):
    manager, key = installation
    assert manager.pin("1.0.0")["state"] == "pinned"
    assert manager.check(install=True) == {"state": "pinned", "version": "1.0.0"}
    assert not manager.due()
    manager.configure(pinned_version="")
    assert manager.due()


def test_rollback_does_not_retry_bad_release(installation, monkeypatch):
    manager, key = installation
    publish(manager, key)
    install_boundary(monkeypatch, manager)
    manager.check(install=True)
    assert manager.rollback()["release_id"] == "1.0.0-old"
    with pytest.raises(UpgradeError, match="quarantined"):
        manager.check(install=True)


def test_status_reports_session_version_and_requires_restart(installation, monkeypatch):
    manager, key = installation
    publish(manager, key)
    install_boundary(monkeypatch, manager)
    monkeypatch.setenv("COPILOT_RELEASE_ID", "1.0.0-old")
    manager.check(install=True)
    status = manager.status()
    assert status["running_release"] == "1.0.0-old"
    assert status["active_release"] == "1.1.0-new"
    assert status["restart_required"] is True


def test_untrusted_release_never_downloaded(installation, monkeypatch):
    manager, key = installation
    atomic_json(manager.root / "trusted-keys.json", {"schema": 1, "keys": {}})
    monkeypatch.setattr(manager.fetcher, "fetch", lambda *args, **kwargs: pytest.fail("untrusted network call"))
    with pytest.raises(UpgradeError, match="trusted"):
        manager.check(install=True)


def test_upgrade_cli_works_without_loading_app_or_sdk(installation, capsys):
    manager, key = installation
    assert main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["active_version"] == "1.0.0"
    script = "import sys; from infermatrix_copilot.upgrade.cli import main; main(['status','--json']); assert 'infermatrix_copilot.app.core' not in sys.modules"
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_launcher_never_replays_user_process_failure(installation, monkeypatch):
    manager, key = installation
    monkeypatch.setattr(manager, "health", lambda metadata: None)
    monkeypatch.setattr(launcher, "_background", lambda manager: None)
    commands = []

    class Child:
        def __init__(self, command, **kwargs):
            commands.append(command)

        def wait(self, **kwargs):
            return 7

    monkeypatch.setattr(launcher.subprocess, "Popen", Child)
    assert launcher._application(manager, "cli", ["-p", "user task"]) == 7
    assert len(commands) == 1
    assert list((manager.root / "leases").glob("*.json")) == []


def test_launcher_rolls_back_before_handing_off_input(installation, monkeypatch):
    manager, key = installation
    seed(manager, "1.1.0-new", "1.1.0")
    manager._activate("1.1.0-new")

    def health(metadata):
        if metadata["release_id"] == "1.1.0-new":
            raise UpgradeError("failed startup")

    monkeypatch.setattr(manager, "health", health)
    monkeypatch.setattr(launcher, "_background", lambda manager: None)
    commands = []

    class Child:
        def __init__(self, command, **kwargs):
            commands.append((command, kwargs["env"]))

        def wait(self, **kwargs):
            return 0

    monkeypatch.setattr(launcher.subprocess, "Popen", Child)
    assert launcher._application(manager, "mcp", []) == 0
    assert len(commands) == 1
    assert commands[0][1]["COPILOT_RELEASE_ID"] == "1.0.0-old"


def test_managed_memory_writes_survive_release_changes(tmp_path, monkeypatch):
    from infermatrix_copilot.config import Settings
    from infermatrix_copilot.memory.paths import KnowledgePaths

    monkeypatch.setenv("COPILOT_RELEASE_ROOT", str(tmp_path / "immutable-release"))
    settings = Settings(_env_file=None, memory_db=tmp_path / "user/debug_memory.db")
    adapter = tmp_path / "immutable-release/adapters/example"
    paths = KnowledgePaths.resolve(settings, "example", adapter)
    assert paths.shared_write_db == tmp_path / "user/state/example/debug_memory.db"
    assert paths.rebase_backend_db == paths.shared_write_db
    assert paths.debug_read_layers[1] == adapter / "store/debug_memory.db"
    assert paths.repo_map_cache_dir == tmp_path / "user/state/example/repo_map"


def test_custom_managed_home_controls_configuration_and_state(tmp_path):
    env = dict(os.environ, INFERMATRIX_COPILOT_HOME=str(tmp_path))
    script = "from infermatrix_copilot.config import Settings,_USER_CONFIG; import json; s=Settings(_env_file=None); print(json.dumps([str(_USER_CONFIG),str(s.run_root),str(s.memory_db)]))"
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == [str(tmp_path / ".env"), str(tmp_path / "runs"), str(tmp_path / "debug_memory.db")]


def test_signed_key_rotation_and_rollback_share_one_activation(installation, monkeypatch):
    manager, old_key = installation
    new_key = Ed25519PrivateKey.generate()
    encoded = base64.b64encode(new_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    overlap = {**manager.trusted_keys(), "next": encoded}
    publish(manager, old_key, trusted_keys=overlap)
    install_boundary(monkeypatch, manager)
    manager.check(install=True)
    first = manager.active()
    assert first["trusted_keys"] == overlap
    assert first["previous"]["trusted_keys"].keys() == {"test"}
    publish(manager, new_key, version="1.2.0", sequence=2,
            key_id="next", trusted_keys={"next": encoded})
    manager.check(install=True)
    second = manager.active()
    assert second["trusted_keys"] == {"next": encoded}
    assert second["launcher_application"] != first["launcher_application"] or second["release_id"] != first["release_id"]
    assert "test" not in manager.trusted_keys()
    manager.rollback()
    restored = manager.active()
    assert restored["release_id"] == first["release_id"]
    assert restored["launcher_application"] == first["launcher_application"]
    assert restored["trusted_keys"] == first["trusted_keys"]


def test_interrupted_activation_cannot_partially_change_recovery_or_trust(installation, monkeypatch):
    manager, key = installation
    publish(manager, key)
    install_boundary(monkeypatch, manager)
    before = manager.active()
    original = __import__("infermatrix_copilot.upgrade.manager", fromlist=["atomic_json"]).atomic_json

    def interrupt(path, value):
        if path == manager.root / "active.json":
            raise OSError("simulated crash at activation commit")
        original(path, value)

    monkeypatch.setattr("infermatrix_copilot.upgrade.manager.atomic_json", interrupt)
    with pytest.raises(OSError, match="simulated crash"):
        manager.check(install=True)
    assert manager.active() == before
    assert not (manager.root / "launcher/active.json").exists()
    monkeypatch.setattr("infermatrix_copilot.upgrade.manager.atomic_json", original)
    assert manager.check(install=True)["state"] == "upgraded"


def test_recovery_candidate_must_run_in_independent_environment(installation, monkeypatch):
    manager, key = installation
    publish(manager, key)
    commands = install_boundary(monkeypatch, manager)
    base = __import__("infermatrix_copilot.upgrade.manager", fromlist=["run_command"]).run_command

    def fail_recovery(command, **kwargs):
        if any(item.endswith(".pyz") for item in command):
            assert command[0] == str(python_path(manager.root / "launcher/venv"))
            raise UpgradeError("incompatible recovery Python")
        base(command, **kwargs)

    monkeypatch.setattr("infermatrix_copilot.upgrade.manager.run_command", fail_recovery)
    with pytest.raises(UpgradeError, match="recovery launcher.*quarantined"):
        manager.check(install=True)
    assert manager.active()["release_id"] == "1.0.0-old"
    assert "1.1.0-new" in manager.status()["quarantined"]


def test_health_never_reads_real_managed_configuration(installation, monkeypatch):
    manager, key = installation
    (manager.root / ".env").write_text("STRICT_MAX_WORKERS=invalid-user-setting\n")
    captured = []
    monkeypatch.setattr("infermatrix_copilot.upgrade.manager.run_command",
                        lambda command, **kwargs: captured.append(kwargs))
    manager.health(manager.installed("1.0.0-old"))
    env = captured[0]["env"]
    assert env["INFERMATRIX_COPILOT_HOME"] != str(manager.root)
    assert env["COPILOT_RELEASE_ROOT"] == str(manager.root / "releases/1.0.0-old")
    assert "GITHUB_TOKEN" not in env and "OPENAI_API_KEY" not in env
