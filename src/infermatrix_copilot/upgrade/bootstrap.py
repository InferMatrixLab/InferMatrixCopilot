"""Install a recovery launcher and the initial user-authorized application."""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.metadata
import json
import re
import urllib.parse
import shutil
import sys
import tempfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

from .manager import UpdateManager, python_path, run_command
from .storage import UpgradeError, atomic_json, atomic_write, file_lock, home_path, read_json, release_id


def _trust_file(path: Path) -> dict:
    return _validate_trust(read_json(path, {}))


def _validate_trust(data: dict) -> dict:
    if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("keys"), dict):
        raise UpgradeError("--public-key must point to a schema-1 trusted-keys.json file")
    try:
        for key, value in data["keys"].items():
            if not isinstance(key, str) or not key or len(base64.b64decode(value, validate=True)) != 32:
                raise ValueError("invalid public key")
    except (ValueError, TypeError) as exc:
        raise UpgradeError("invalid Ed25519 public key") from exc
    return data


def _launcher(home: Path, package: Path, uv: str) -> list[str]:
    launcher = home / "launcher"
    interpreter = python_path(launcher / "venv").absolute()
    readiness = launcher / "venv/.copilot-updater-dependencies.json"
    dependencies = {"schema": 1, "requirements": ["cryptography>=42", "packaging>=24"]}
    if not interpreter.is_file():
        readiness.unlink(missing_ok=True)
        run_command([uv, "venv", "--python", ">=3.11", str(launcher / "venv")])
    try:
        ready = read_json(readiness) == dependencies
    except UpgradeError:
        ready = False
    if not ready:
        run_command([uv, "pip", "install", "--python", str(interpreter),
                     "--only-binary", ":all:", *dependencies["requirements"]])
        atomic_json(readiness, dependencies)
    files = [("infermatrix_copilot/__init__.py", (package / "__init__.py").read_bytes())]
    files.extend((f"infermatrix_copilot/upgrade/{path.name}", path.read_bytes())
                 for path in sorted((package / "upgrade").glob("*.py")))
    digest = hashlib.sha256(b"".join(name.encode() + content for name, content in files)).hexdigest()[:16]
    application = launcher / f"updater-{digest}.pyz"
    if not application.exists():
        with tempfile.TemporaryDirectory(prefix="copilot-launcher-") as temporary:
            target = Path(temporary) / "launcher.pyz"
            with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("__main__.py", "from infermatrix_copilot.upgrade.launcher import main\nraise SystemExit(main())\n")
                for name, content in files:
                    archive.writestr(name, content)
            atomic_write(application, target.read_bytes())
    # Stable tiny entry reads a versioned recovery zipapp. Existing processes
    # retain their immutable zipapp even when an installer updates the pointer.
    atomic_json(launcher / "active.json", {"application": application.name})
    entry = launcher / "entry.py"
    atomic_write(entry, b'''import json, os, pathlib, sys
base = pathlib.Path(__file__).absolute().parent
activation = base.parent / "active.json"
state = json.loads(activation.read_text(encoding="utf-8")) if activation.exists() else {}
if not isinstance(state, dict):
    raise SystemExit("Invalid application activation pointer")
name = state.get("launcher_application")
if name is None:
    name = json.loads((base / "active.json").read_text(encoding="utf-8"))["application"]
if not isinstance(name, str) or pathlib.Path(name).name != name or not name.startswith("updater-") or not name.endswith(".pyz"):
    raise SystemExit("Invalid recovery launcher pointer")
os.execv(sys.executable, [sys.executable, "-I", str(base / name), *sys.argv[1:]])
''')
    return [str(interpreter), str(entry), "--home", str(home), "mcp"]


def _version(wheel: Path) -> str:
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(names) != 1:
                raise ValueError("expected one metadata document")
            metadata = BytesParser().parsebytes(archive.read(names[0]))
        name = metadata.get_all("Name", [])
        versions = metadata.get_all("Version", [])
        if (len(name) != 1 or re.sub(r"[-_.]+", "-", name[0]).lower() != "infermatrix-copilot"
                or len(versions) != 1 or not versions[0] or not versions[0][0].isdigit()):
            raise ValueError("missing or mismatched distribution identity")
        # Bootstrap remains stdlib-only. Enforce a safe directory component here;
        # the isolated runtime health gate validates its exact installed version.
        version = release_id(versions[0])
        return version
    except (OSError, ValueError, zipfile.BadZipFile, UpgradeError) as exc:
        raise UpgradeError("invalid initial wheel metadata") from exc


def install(source_root: Path | None, home: Path, channel: str = "stable",
            public_key: Path | None = None, authoring_root: Path | None = None,
            *, distribution_spec: str = "") -> list[str]:
    home = home_path(home)
    if channel not in {"stable", "preview"}:
        raise UpgradeError("invalid release channel")
    uv = shutil.which("uv")
    if not uv:
        raise UpgradeError("install uv before installing managed Copilot")
    package = Path(__file__).resolve().parents[1]
    if source_root is not None:
        source_root = source_root.expanduser().absolute()
        package = source_root / "src/infermatrix_copilot"
    trust_path = public_key or (source_root / "release/trusted-keys.json" if source_root else package / "_runtime/trusted-keys.json")
    trust = _trust_file(trust_path)
    manager = UpdateManager(home)
    with file_lock(home / "locks/update.lock", timeout=600) as held:
        if not held:
            raise UpgradeError("another installation or updater is busy")
        current_trust = (_trust_file(home / "trusted-keys.json")
                         if (home / "trusted-keys.json").exists() else {"schema": 1, "keys": {}})
        active = manager.active()
        authoritative = (dict(_validate_trust({"schema": 1, "keys": active["trusted_keys"]})["keys"])
                         if active and "trusted_keys" in active else dict(current_trust["keys"]))
        for key, value in trust["keys"].items():
            for previous_keys in (current_trust["keys"], authoritative):
                if key in previous_keys and previous_keys[key] != value:
                    raise UpgradeError("trusted signing key identity changed; use a new key identifier")
        current_trust["keys"].update(trust["keys"])
        authoritative.update(trust["keys"])
        command = _launcher(home, package, uv)
        application = read_json(home / "launcher/active.json")["application"]
        if active:
            # Explicit reinstallation can repair/update recovery independently
            # of the active application, including a newer min-launcher gate.
            run_command([command[0], "-I", str(home / "launcher" / application),
                         "--home", str(home), "upgrade", "status", "--json"], timeout=30)
            updated = {**active, "trusted_keys": authoritative, "launcher_application": application}
            # Never resurrect a key retired by a signed release merely because
            # the legacy installer trust file still contains it.
            atomic_json(home / "trusted-keys.json", current_trust)
            if updated != active:
                atomic_json(home / "active.json", updated)
        else:
            atomic_json(home / "trusted-keys.json", current_trust)
            with tempfile.TemporaryDirectory(prefix="copilot-bootstrap-") as temporary:
                temporary = Path(temporary)
                if source_root:
                    run_command([uv, "build", "--wheel", "--out-dir", str(temporary), str(source_root)])
                    wheels = list(temporary.glob("*.whl"))
                    if len(wheels) != 1:
                        raise UpgradeError("initial wheel build produced no unique artifact")
                    wheel = wheels[0]
                    version = _version(wheel)
                    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
                    spec = f"{wheel}[mcp,kb,updater]"
                else:
                    if not distribution_spec:
                        raise UpgradeError("an immutable distribution source is required for bootstrap")
                    version = importlib.metadata.version("infermatrix-copilot")
                    digest = hashlib.sha256(distribution_spec.encode()).hexdigest()
                    spec = distribution_spec
                identity = release_id(f"bootstrap-{version}-{digest[:16]}")
                directory = home / "releases" / identity
                if directory.exists():
                    shutil.rmtree(directory)
                directory.mkdir(parents=True)
                run_command([uv, "venv", "--python", ">=3.11", str(directory / "venv")])
                interpreter = python_path(directory / "venv").absolute()
                run_command([uv, "pip", "install", "--python", str(interpreter), spec])
                metadata = {"release_id": identity, "version": version, "python": str(interpreter),
                            "bundle_sha256": digest, "bootstrap": True, "healthy": True,
                            "launcher_application": read_json(home / "launcher/active.json")["application"]}
                manager.health(metadata)
                atomic_json(directory / "installed.json", metadata)
                manager._activate(identity)
        registry = read_json(home / "install.json", {})
        registry.update(schema=1, launcher_command=command)
        atomic_json(home / "install.json", registry)
    values = {"channel": channel}
    authoring_root = authoring_root or source_root
    if authoring_root:
        values["authoring_root"] = str(authoring_root.expanduser().absolute())
    manager.configure(**values)
    return command


def install_distribution(home: Path) -> list[str]:
    """Portable plugin bootstrap pins the exact Git commit resolved by uvx."""
    distribution = importlib.metadata.distribution("infermatrix-copilot")
    try:
        direct = json.loads(distribution.read_text("direct_url.json") or "{}")
    except (TypeError, ValueError) as exc:
        raise UpgradeError("invalid initial distribution provenance") from exc
    if not isinstance(direct, dict):
        raise UpgradeError("invalid initial distribution provenance")
    vcs = direct.get("vcs_info", {})
    if not isinstance(vcs, dict):
        raise UpgradeError("invalid initial Git distribution provenance")
    commit = vcs.get("commit_id", "")
    if vcs.get("vcs") == "git" and isinstance(commit, str) and re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        url = direct.get("url", "")
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname != "github.com"
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise UpgradeError("unsupported initial Git distribution; use the checkout installer")
        spec = f"infermatrix-copilot[mcp,kb,updater] @ git+{url}@{vcs['commit_id']}"
    elif direct.get("archive_info") and urllib.parse.urlsplit(direct.get("url", "")).path.endswith(".whl"):
        archive = direct["archive_info"]
        if not isinstance(archive, dict) or not isinstance(archive.get("hashes", {}), dict):
            raise UpgradeError("invalid initial wheel provenance")
        digest = archive.get("hashes", {}).get("sha256", "")
        legacy_hash = archive.get("hash", "")
        if not digest and isinstance(legacy_hash, str) and legacy_hash.startswith("sha256="):
            digest = legacy_hash.removeprefix("sha256=")
        parsed = urllib.parse.urlsplit(direct["url"])
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)):
            raise UpgradeError("initial wheel requires HTTPS and its recorded SHA-256 hash")
        url = urllib.parse.urlunsplit(parsed._replace(fragment="sha256=" + digest.lower()))
        spec = f"infermatrix-copilot[mcp,kb,updater] @ {url}"
    elif not direct:
        spec = f"infermatrix-copilot[mcp,kb,updater]=={distribution.version}"
    else:
        raise UpgradeError("development installation detected; run install-mcp.sh for a managed installation")
    return install(None, home, distribution_spec=spec)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Install managed InferMatrixCopilot")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--home", type=Path, default=home_path())
    parser.add_argument("--channel", choices=("stable", "preview"), default="stable")
    parser.add_argument("--public-key", type=Path)
    parser.add_argument("--authoring-root", type=Path)
    args = parser.parse_args(argv)
    try:
        command = install(args.source, args.home, args.channel, args.public_key, args.authoring_root)
        print(json.dumps({"launcher_command": command}))
        return 0
    except UpgradeError as exc:
        print(f"Installation incomplete: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
