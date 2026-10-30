#!/usr/bin/env python3
"""Build and sign a managed release from an immutable Git snapshot.

Only the wheel and a fully hashed dependency lock are distributed. The signing
secret is read from the environment and never written into the source snapshot.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile
import tomllib
from urllib.parse import urlparse
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from packaging.version import Version


def canonical_json(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(command: list[str], *, cwd: Path, capture: bool = False) -> bytes:
    # Public releases resolve against PyPI, never a developer's private index.
    env = os.environ.copy()
    for name in ("UV_INDEX", "UV_INDEX_URL", "UV_EXTRA_INDEX_URL", "UV_FIND_LINKS",
                 "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_FIND_LINKS"):
        env.pop(name, None)
    result = subprocess.run(command, cwd=cwd, env=env, check=True,
                            stdout=subprocess.PIPE if capture else None,
                            timeout=900)
    return result.stdout if capture else b""


def snapshot(source: Path, revision: str, destination: Path) -> tuple[str, int]:
    commit = run(["git", "rev-parse", "--verify", f"{revision}^{{commit}}"],
                 cwd=source, capture=True).decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("release commit must be a full SHA-1 Git commit")
    epoch = int(run(["git", "show", "-s", "--format=%ct", commit],
                    cwd=source, capture=True))
    archive = run(["git", "archive", "--format=tar", commit],
                  cwd=source, capture=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as members:
        for entry in members:
            path = PurePosixPath(entry.name)
            if (path.is_absolute() or ".." in path.parts
                    or not (entry.isfile() or entry.isdir())):
                raise ValueError(f"unsupported source archive member: {entry.name}")
            target = destination.joinpath(*path.parts)
            if entry.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with members.extractfile(entry) as original:
                    target.write_bytes(original.read())
                target.chmod(entry.mode & 0o777)
    return commit, epoch


def source_version(source: Path) -> str:
    """Read the single version source without importing application code."""
    text = (source / "src/infermatrix_copilot/__init__.py").read_text("utf-8")
    match = re.search(r'^__version__\s*=\s*[\'"]([^\'"]+)[\'"]', text, re.M)
    if match is None:
        raise ValueError("source has no literal __version__")
    version = str(Version(match.group(1)))
    if version != match.group(1):
        raise ValueError("__version__ must use normalized PEP 440 syntax")
    project = tomllib.loads((source / "pyproject.toml").read_text("utf-8"))["project"]
    if "version" in project and project["version"] != version:
        raise ValueError("project and module version disagree")
    return version


def normalize_plugins(source: Path, version: str) -> None:
    """Normalize the release copy only; leave the caller's working tree intact."""
    plugin = source / "plugins/infermatrix-copilot"
    for relative in (".codex-plugin/plugin.json", ".claude-plugin/plugin.json"):
        path = plugin / relative
        if path.is_file():
            value = json.loads(path.read_text("utf-8"))
            value["version"] = version
            path.write_text(json.dumps(value, indent=2) + "\n", "utf-8")


def load_signer(source: Path, key_id: str, encoded: str) -> Ed25519PrivateKey:
    try:
        private = Ed25519PrivateKey.from_private_bytes(
            base64.b64decode(encoded.strip(), validate=True))
        public = private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        trust = json.loads((source / "release/trusted-keys.json").read_text("utf-8"))
        trusted = base64.b64decode(trust["keys"][key_id], validate=True)
        if trust["schema"] != 1 or public != trusted:
            raise ValueError("signing key does not match the committed trust root")
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("release signing key is invalid or not trusted by this commit") from exc
    return private


def validate_wheel(wheel: Path, version: str) -> None:
    required = (
        "infermatrix_copilot/knowledge/AGENTS.md",
        "infermatrix_copilot/_runtime/playbooks/pr-review.yaml",
        "infermatrix_copilot/_runtime/adapters/vllm_omni/manifest.yaml",
        "infermatrix_copilot/_runtime/skills/code-quality-review/SKILL.md",
        "infermatrix_copilot/_runtime/host_skills/imreview/SKILL.md",
        "infermatrix_copilot/_runtime/doc/knowledge/CONTRIBUTING.md",
        "infermatrix_copilot/_runtime/tools/audit_vllm_omni_release.py",
        "infermatrix_copilot/_runtime/trusted-keys.json",
        "infermatrix_copilot/_runtime/workflow-tools.json",
    )
    with zipfile.ZipFile(wheel) as archive:
        missing = [name for name in required if name not in archive.namelist()]
        if missing:
            raise ValueError(f"release wheel omits runtime files: {missing}")
        metadata = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
        if len(metadata) != 1 or f"\nVersion: {version}\n" not in archive.read(
                metadata[0]).decode("utf-8"):
            raise ValueError("wheel version does not match release version")
        plugin = "infermatrix_copilot/_runtime/plugin.json"
        if plugin in archive.namelist():
            if json.loads(archive.read(plugin))["version"] != version:
                raise ValueError("packaged plugin version does not match release version")


def bundle(output: Path, files: dict[str, Path], epoch: int) -> None:
    """Create a minimal archive with deterministic metadata and no symlinks."""
    with output.open("wb") as stream:
        with gzip.GzipFile(filename="", mode="wb", fileobj=stream, mtime=epoch) as gz:
            with tarfile.open(fileobj=gz, mode="w") as archive:
                for name, path in sorted(files.items()):
                    data = path.read_bytes()
                    member = tarfile.TarInfo(name)
                    member.size = len(data)
                    member.mtime = epoch
                    member.mode = 0o644
                    archive.addfile(member, io.BytesIO(data))


def build(args: argparse.Namespace) -> dict:
    if args.sequence < 1:
        raise ValueError("sequence must be a positive monotonic release number")
    parsed = urlparse(args.artifact_base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("artifact base URL must be HTTPS without credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("artifact base URL must not contain a query or fragment")
    secret = os.environ.get(args.signing_key_env)
    if not secret:
        raise ValueError(f"set {args.signing_key_env} to a base64 raw Ed25519 private key")
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="copilot-release-") as temporary:
        source = Path(temporary) / "source"
        source.mkdir()
        commit, epoch = snapshot(Path(args.source).resolve(), args.commit, source)
        version = source_version(source)
        parsed_version = Version(version)
        if args.channel == "stable" and (parsed_version.is_prerelease or parsed_version.is_devrelease):
            raise ValueError("prerelease versions belong on the preview channel")
        signer = load_signer(source, args.key_id, secret)
        normalize_plugins(source, version)
        release_id = f"{version}-{commit[:12]}"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", release_id):
            raise ValueError("version cannot be represented by a safe release directory name")
        wheel_dir = Path(temporary) / "wheel"
        run([args.uv, "build", "--wheel", "--no-create-gitignore", "--out-dir",
             str(wheel_dir), "--default-index", "https://pypi.org/simple"], cwd=source)
        wheels = list(wheel_dir.glob("*.whl"))
        if len(wheels) != 1:
            raise ValueError("build must produce exactly one wheel")
        wheel = wheels[0]
        validate_wheel(wheel, version)
        lock = Path(temporary) / "requirements.txt"
        run([args.uv, "pip", "compile", "pyproject.toml", "--extra", "mcp",
             "--extra", "kb", "--extra", "updater", "--universal", "--generate-hashes",
             "--no-emit-package", "infermatrix-copilot", "--no-header", "--no-annotate",
             "--no-build", "--quiet", "--default-index", "https://pypi.org/simple",
             "--output-file", str(lock)], cwd=source)
        if "--hash=sha256:" not in lock.read_text("utf-8"):
            raise ValueError("dependency lock contains no artifact hashes")
        wheel_path = f"wheel/{wheel.name}"
        bundle_path = output / f"infermatrix-copilot-{release_id}.tar.gz"
        bundle(bundle_path, {wheel_path: wheel, "requirements.txt": lock}, epoch)
        published = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        project = tomllib.loads((source / "pyproject.toml").read_text("utf-8"))["project"]
        manifest = {
            "schema": 1, "release_id": release_id, "version": version,
            "commit": commit, "channel": args.channel, "sequence": args.sequence,
            "published_at": published, "requires_python": project["requires-python"],
            "min_launcher_version": 1, "runtime_schema": 1, "skill_protocol": 1,
            "knowledge_formats": [2],
            "artifacts": {
                "bundle": {"url": f"{args.artifact_base_url.rstrip('/')}/{bundle_path.name}",
                           "sha256": sha256(bundle_path), "size": bundle_path.stat().st_size},
                "wheel": {"path": wheel_path, "sha256": sha256(wheel)},
            },
        }
        envelope = {"manifest": manifest, "key_id": args.key_id,
                    "signature": base64.b64encode(signer.sign(canonical_json(manifest))).decode("ascii")}
        encoded = json.dumps(envelope, indent=2, ensure_ascii=False) + "\n"
        (output / f"copilot-{args.channel}.json").write_text(encoded, "utf-8")
        (output / "release.json").write_text(encoded, "utf-8")
        # Separate assets make a release inspectable without extracting the bundle.
        (output / wheel.name).write_bytes(wheel.read_bytes())
        (output / "requirements.txt").write_bytes(lock.read_bytes())
        return envelope


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--commit", default="HEAD", help="immutable source revision; dirty files are ignored")
    parser.add_argument("--output", default="dist/release")
    parser.add_argument("--channel", choices=("stable", "preview"), default="stable")
    parser.add_argument("--sequence", required=True, type=int)
    parser.add_argument("--artifact-base-url", required=True)
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--signing-key-env", default="COPILOT_RELEASE_SIGNING_KEY")
    parser.add_argument("--uv", default="uv")
    args = parser.parse_args()
    try:
        envelope = build(args)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"release build failed: {exc}\n")
    print(json.dumps({"release_id": envelope["manifest"]["release_id"],
                      "channel": args.channel, "output": str(Path(args.output).resolve())}))


if __name__ == "__main__":
    main()
