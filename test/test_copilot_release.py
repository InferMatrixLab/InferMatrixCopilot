"""Release boundary tests: immutable inputs, trust root and signed payloads."""
import argparse
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tarfile
import zipfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "copilot_release_builder", ROOT / "scripts/build_copilot_release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def write(root: Path, relative: str, content: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, "utf-8")


@pytest.fixture
def release_repo(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    secret = key.private_bytes(serialization.Encoding.Raw,
                               serialization.PrivateFormat.Raw,
                               serialization.NoEncryption())
    monkeypatch.setenv("COPILOT_TEST_SIGNER", base64.b64encode(secret).decode())
    write(source, "src/infermatrix_copilot/__init__.py", '__version__ = "1.2.3"\n')
    write(source, "pyproject.toml", '[project]\nname="infermatrix-copilot"\n'
          'dynamic=["version"]\nrequires-python=">=3.11"\n')
    write(source, "release/trusted-keys.json", json.dumps({
        "schema": 1, "keys": {"test": base64.b64encode(public).decode()}}))
    write(source, "release/workflow-tools.json", json.dumps({
        "schema": 1, "tools": {"example_audit": {"path": "tools/example_audit.py"}}}))
    write(source, "plugins/infermatrix-copilot/.codex-plugin/plugin.json",
          '{"name":"infermatrix-copilot","version":"0.0.1"}')
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "-qm", "fixture"], check=True)
    # These must not alter the release snapshot or leak into artifacts.
    write(source, "src/infermatrix_copilot/__init__.py", '__version__ = "99.0.0"\n')
    write(source, ".env", "TEST_SECRET=must-not-ship\n")
    args = argparse.Namespace(
        source=str(source), commit="HEAD", output=str(tmp_path / "output"),
        channel="stable", sequence=42, key_id="test", uv="fake-uv",
        artifact_base_url="https://example.test/releases/v1.2.3",
        signing_key_env="COPILOT_TEST_SIGNER",
    )
    return source, key, args


def fake_uv(monkeypatch):
    """Replace the network/build boundary; exercise real Git, tar and signatures."""
    original = release.run
    seen = []

    def run(command, *, cwd, capture=False):
        if command[0] != "fake-uv":
            return original(command, cwd=cwd, capture=capture)
        seen.append(command)
        if command[1] == "build":
            output = Path(command[command.index("--out-dir") + 1])
            output.mkdir()
            with zipfile.ZipFile(output / "infermatrix_copilot-1.2.3-py3-none-any.whl", "w") as wheel:
                for name in (
                    "knowledge/AGENTS.md", "_runtime/playbooks/pr-review.yaml",
                    "_runtime/adapters/vllm_omni/manifest.yaml",
                    "_runtime/skills/code-quality-review/SKILL.md",
                    "_runtime/host_skills/imreview/SKILL.md",
                    "_runtime/doc/knowledge/CONTRIBUTING.md",
                    "_runtime/tools/audit_vllm_omni_release.py",
                ):
                    wheel.writestr("infermatrix_copilot/" + name, "fixture")
                wheel.writestr("infermatrix_copilot/_runtime/trusted-keys.json",
                               (cwd / "release/trusted-keys.json").read_bytes())
                wheel.writestr("infermatrix_copilot/_runtime/workflow-tools.json",
                               (cwd / "release/workflow-tools.json").read_bytes())
                wheel.writestr("infermatrix_copilot/_runtime/plugin.json",
                               (cwd / "plugins/infermatrix-copilot/.codex-plugin/plugin.json").read_bytes())
                wheel.writestr("infermatrix_copilot-1.2.3.dist-info/METADATA",
                               "Metadata-Version: 2.1\nName: infermatrix-copilot\nVersion: 1.2.3\n")
        else:
            output = Path(command[command.index("--output-file") + 1])
            output.write_text("example==1.0 --hash=sha256:" + "a" * 64 + "\n")
        return b""

    monkeypatch.setattr(release, "run", run)
    return seen


def test_signed_release_uses_only_committed_source(release_repo, monkeypatch):
    source, key, args = release_repo
    seen = fake_uv(monkeypatch)
    envelope = release.build(args)
    manifest = envelope["manifest"]
    key.public_key().verify(base64.b64decode(envelope["signature"]),
                            release.canonical_json(manifest))
    assert manifest["version"] == "1.2.3"
    assert manifest["release_id"] == "1.2.3-" + manifest["commit"][:12]
    assert manifest["sequence"] == 42
    output = Path(args.output)
    bundle = next(output.glob("*.tar.gz"))
    assert manifest["artifacts"]["bundle"]["sha256"] == hashlib.sha256(bundle.read_bytes()).hexdigest()
    assert manifest["artifacts"]["bundle"]["size"] == bundle.stat().st_size
    with tarfile.open(bundle) as archive:
        assert sorted(archive.getnames()) == ["requirements.txt", manifest["artifacts"]["wheel"]["path"]]
        wheel = archive.extractfile(manifest["artifacts"]["wheel"]["path"]).read()
        assert hashlib.sha256(wheel).hexdigest() == manifest["artifacts"]["wheel"]["sha256"]
        assert b"must-not-ship" not in wheel
    assert json.loads((output / "copilot-stable.json").read_text()) == envelope
    assert json.loads((source / "plugins/infermatrix-copilot/.codex-plugin/plugin.json").read_text())["version"] == "0.0.1"
    assert '"99.0.0"' in (source / "src/infermatrix_copilot/__init__.py").read_text()
    compile_command = seen[1]
    assert "--universal" in compile_command and "--generate-hashes" in compile_command
    assert [compile_command[index + 1] for index, part in enumerate(compile_command)
            if part == "--extra"] == ["mcp", "kb", "updater"]


def test_untrusted_signer_rejected_before_build(release_repo, monkeypatch):
    _, _, args = release_repo
    args.key_id = "unknown"
    seen = fake_uv(monkeypatch)
    with pytest.raises(ValueError, match="not trusted"):
        release.build(args)
    assert seen == []


@pytest.mark.parametrize("base", ["http://example.test", "https://secret:token@example.test", "https://example.test/?token=secret"])
def test_release_rejects_unsafe_download_location(release_repo, base):
    _, _, args = release_repo
    args.artifact_base_url = base
    with pytest.raises(ValueError, match="URL"):
        release.build(args)


def test_source_symlinks_rejected(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "outside").symlink_to("/etc/passwd")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "-qm", "fixture"], check=True)
    destination = tmp_path / "snapshot"
    destination.mkdir()
    with pytest.raises(ValueError, match="unsupported source archive member"):
        release.snapshot(source, "HEAD", destination)


def test_manifest_schema_matches_builder(release_repo, monkeypatch):
    jsonschema = pytest.importorskip("jsonschema")
    _, _, args = release_repo
    fake_uv(monkeypatch)
    envelope = release.build(args)
    schema = json.loads((ROOT / "release/manifest.schema.json").read_text())
    jsonschema.validate(envelope["manifest"], schema)


def test_publisher_refuses_wheel_without_workflow_registry(release_repo, monkeypatch, tmp_path):
    _, _, args = release_repo
    fake_uv(monkeypatch)
    release.build(args)
    original = next(Path(args.output).glob("*.whl"))
    broken = tmp_path / "without-registry.whl"
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(broken, "w") as target:
        for member in source.namelist():
            if not member.endswith("/_runtime/workflow-tools.json"):
                target.writestr(member, source.read(member))
    with pytest.raises(ValueError, match="workflow-tools.json"):
        release.validate_wheel(broken, "1.2.3")
