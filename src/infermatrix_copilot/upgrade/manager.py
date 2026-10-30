"""Transactional upgrades into immutable environments, outside the running app."""

from __future__ import annotations

import json
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import zipfile
from pathlib import Path

from .releases import Fetcher, extract_bundle, sha256, verify_envelope
from .storage import UpgradeError, atomic_json, atomic_write, file_lock, home_path, read_json, release_id

REPOSITORY = "JiusiServe/InferMatrixCopilot"
DEFAULTS = {"auto_update": True, "channel": "stable", "check_interval": 21600,
            "pinned_version": ""}


def validate_policy(policy: dict) -> dict:
    if (type(policy["auto_update"]) is not bool or policy["channel"] not in {"stable", "preview"}
            or type(policy["check_interval"]) is not int or policy["check_interval"] < 60
            or not isinstance(policy["pinned_version"], str)
            or any(not isinstance(policy.get(key, ""), str) for key in ("feed_url", "authoring_root"))):
        raise UpgradeError("invalid automatic upgrade policy")
    return policy


def python_path(directory: Path) -> Path:
    # Do not resolve a venv's python symlink: its lexical path selects the venv.
    return directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def run_command(command: list[str], *, env: dict | None = None, cwd: Path | None = None,
                timeout: float = 600) -> None:
    try:
        result = subprocess.run(command, env=env, cwd=cwd, capture_output=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpgradeError("release preparation command could not complete") from exc
    if result.returncode:
        # pip and build tools can include private index credentials in output.
        raise UpgradeError("release preparation failed; the previous release remains available")


class UpdateManager:
    def __init__(self, root: Path | str | None = None, *, fetcher=None, clock=time.time):
        self.root = home_path(root)
        self.fetcher = fetcher or Fetcher()
        self.clock = clock

    def policy(self) -> dict:
        try:
            raw = tomllib.loads((self.root / "updates.toml").read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError) as exc:
            raise UpgradeError("invalid updates.toml") from exc
        policy = {**DEFAULTS, **raw}
        return validate_policy(policy)

    def configure(self, **values) -> dict:
        allowed = {"auto_update", "channel", "check_interval", "pinned_version", "feed_url", "authoring_root"}
        if set(values) - allowed:
            raise UpgradeError("unknown upgrade setting")
        with file_lock(self.root / "locks/update.lock") as held:
            if not held:
                raise UpgradeError("another updater is busy")
            policy = validate_policy({**self.policy(), **values})
            lines = []
            for key, value in policy.items():
                if not isinstance(value, (str, bool, int)) or key not in allowed:
                    raise UpgradeError("invalid upgrade setting")
                literal = str(value).lower() if isinstance(value, bool) else json.dumps(value)
                lines.append(f"{key} = {literal}")
            atomic_write(self.root / "updates.toml", ("\n".join(lines) + "\n").encode())
        return policy

    def active(self) -> dict | None:
        active = read_json(self.root / "active.json")
        if active is not None:
            if not isinstance(active, dict):
                raise UpgradeError("invalid active release pointer")
            release_id(active.get("release_id"))
        return active

    def installed(self, identity: str) -> dict:
        identity = release_id(identity)
        metadata = read_json(self.root / "releases" / identity / "installed.json")
        if not isinstance(metadata, dict) or metadata.get("release_id") != identity or not metadata.get("healthy"):
            raise UpgradeError("release is incomplete or has not passed its health check")
        expected = python_path(self.root / "releases" / identity / "venv").absolute()
        if metadata.get("python") != str(expected) or not expected.is_file():
            raise UpgradeError("release interpreter is missing")
        return metadata

    def trusted_keys(self) -> dict:
        active = self.active()
        if active is not None and "trusted_keys" in active:
            keys = active["trusted_keys"]
        else:
            keys = read_json(self.root / "trusted-keys.json", {}).get("keys", {})
        if not isinstance(keys, dict):
            raise UpgradeError("invalid release trust store")
        return keys

    def status(self) -> dict:
        policy = self.policy()
        active = self.active()
        state = read_json(self.root / "updates.json", {})
        running = os.environ.get("COPILOT_RELEASE_ID", "")
        active_id = active["release_id"] if active else ""
        installed = self.installed(active_id) if active_id else {}
        return {"managed": active is not None, "running_release": running,
                "active_release": active_id, "active_version": installed.get("version", ""),
                "previous_release": (active or {}).get("previous_release_id", ""),
                "staged_release": state.get("staged_release", ""),
                "available_version": state.get("available_version", ""),
                "restart_required": bool(running and active_id and running != active_id),
                "auto_update": policy["auto_update"], "channel": policy["channel"],
                "pinned_version": policy["pinned_version"],
                "last_checked": state.get("last_checked"), "last_error": state.get("last_error", ""),
                "next_check": state.get("next_check"), "quarantined": state.get("quarantined", {}),
                "trusted_keys_configured": bool(self.trusted_keys())}

    def due(self) -> bool:
        policy = self.policy()
        state = read_json(self.root / "updates.json", {})
        return (policy["auto_update"] and not policy["pinned_version"]
                and self.clock() >= state.get("next_check", 0))

    def _feed(self, policy: dict, version: str = "") -> str:
        if version:
            from packaging.version import InvalidVersion, Version

            try:
                version = str(Version(version))
            except InvalidVersion as exc:
                raise UpgradeError("invalid pinned version") from exc
            tag = f"v{version}"
        else:
            tag = f"auto-update-{policy['channel']}"
        return policy.get("feed_url") or (
            f"https://github.com/{REPOSITORY}/releases/download/{tag}/copilot-{policy['channel']}.json")

    def check(self, *, install: bool = False, version: str = "", automatic: bool = False) -> dict:
        with file_lock(self.root / "locks/update.lock", blocking=False) as held:
            if not held:
                return {"state": "busy"}
            policy = self.policy()
            if automatic and not self.due():
                return {"state": "deferred"}
            if policy["pinned_version"] and not version:
                return {"state": "pinned", "version": policy["pinned_version"]}
            state = read_json(self.root / "updates.json", {})
            state["last_checked"] = self.clock()
            try:
                keys = self.trusted_keys()
                if not keys:
                    raise UpgradeError("no trusted release signing key; rerun installer with --public-key")
                try:
                    envelope = json.loads(self.fetcher.fetch(self._feed(policy, version)))
                except (ValueError, TypeError) as exc:
                    raise UpgradeError("invalid release envelope") from exc
                channel = policy["channel"]
                highwater = state.get("sequences", {}).get(channel, 0)
                manifest = verify_envelope(envelope, keys, channel=channel,
                                           sequence=0 if version else highwater)
                if version and manifest["version"] != version:
                    raise UpgradeError("published release does not match the requested version")
                state["available_version"] = manifest["version"]
                state.setdefault("sequences", {})[channel] = max(highwater, manifest["sequence"])
                active = self.active()
                identity = manifest["release_id"]
                if active and active["release_id"] == identity:
                    if self.installed(identity).get("bundle_sha256") != manifest["artifacts"]["bundle"]["sha256"]:
                        raise UpgradeError("published release identity changed its contents")
                    result = {"state": "current", "release_id": identity}
                elif identity in state.get("quarantined", {}):
                    raise UpgradeError("release is quarantined after a previous failure")
                else:
                    if active and not version:
                        from packaging.version import Version

                        if Version(manifest["version"]) <= Version(self.installed(active["release_id"])["version"]):
                            raise UpgradeError("automatic upgrade would not increase the installed version")
                    if not install:
                        result = {"state": "available", "release_id": identity, "version": manifest["version"]}
                    else:
                        state["staged_release"] = identity
                        atomic_json(self.root / "updates.json", state)
                        try:
                            self.prepare(envelope)
                        except UpgradeError:
                            # A download or authentication failure should be retryable.
                            # A candidate which actually failed its health test is quarantined by prepare().
                            state.update(read_json(self.root / "updates.json", state))
                            raise
                        self._activate(identity)
                        state["staged_release"] = ""
                        result = {"state": "upgraded", "release_id": identity, "version": manifest["version"]}
                state.update(last_error="", failures=0,
                             next_check=self.clock() + policy["check_interval"])
                atomic_json(self.root / "updates.json", state)
                return result
            except UpgradeError as exc:
                failures = state.get("failures", 0) + 1
                state.update(last_error=str(exc), failures=failures,
                             next_check=self.clock() + min(policy["check_interval"], 60 * 2 ** min(failures - 1, 8)))
                atomic_json(self.root / "updates.json", state)
                raise

    def prepare(self, envelope: dict) -> dict:
        manifest = envelope["manifest"]
        identity = release_id(manifest["release_id"])
        directory = self.root / "releases" / identity
        if (directory / "installed.json").exists():
            metadata = self.installed(identity)
            if metadata.get("bundle_sha256") != manifest["artifacts"]["bundle"]["sha256"]:
                raise UpgradeError("published release identity changed its contents")
            return metadata
        # An incomplete directory has never been activated. Venvs are built at
        # their final physical path because moving them breaks script shebangs.
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)
        artifact = manifest["artifacts"]["bundle"]
        data = self.fetcher.fetch(artifact["url"], limit=artifact["size"])
        if len(data) != artifact["size"]:
            raise UpgradeError("release bundle size does not match its signed manifest")
        with tempfile.TemporaryDirectory(prefix="copilot-bundle-") as scratch:
            scratch = Path(scratch)
            archive = scratch / "bundle.tar.gz"
            archive.write_bytes(data)
            if sha256(archive) != artifact["sha256"]:
                raise UpgradeError("release bundle hash does not match its signed manifest")
            extract_bundle(archive, directory / "bundle")
        wheel_info = manifest["artifacts"]["wheel"]
        wheel = directory / "bundle" / wheel_info["path"]
        requirements = directory / "bundle/requirements.txt"
        if not wheel.is_file() or sha256(wheel) != wheel_info["sha256"] or not requirements.is_file():
            raise UpgradeError("release wheel or dependency lock does not match the signed bundle")
        uv = shutil.which("uv")
        if not uv:
            raise UpgradeError("uv is required to prepare a release; rerun the installer")
        run_command([uv, "venv", "--python", manifest["requires_python"], str(directory / "venv")])
        interpreter = python_path(directory / "venv").absolute()
        run_command([uv, "pip", "install", "--python", str(interpreter), "--require-hashes",
                     "--only-binary", ":all:", "-r", str(requirements)])
        run_command([uv, "pip", "install", "--python", str(interpreter), "--no-deps", str(wheel)])
        metadata = {"release_id": identity, "version": manifest["version"], "commit": manifest["commit"],
                    "python": str(interpreter), "bundle_sha256": artifact["sha256"],
                    "healthy": True, "installed_at": self.clock(), "bootstrap": False}
        try:
            self.health(metadata)
        except UpgradeError:
            state = read_json(self.root / "updates.json", {})
            state.setdefault("quarantined", {})[identity] = "candidate failed offline health checks"
            atomic_json(self.root / "updates.json", state)
            raise UpgradeError("candidate failed its health check and was quarantined")
        try:
            metadata["launcher_application"] = self._stage_launcher(wheel)
            with zipfile.ZipFile(wheel) as archive:
                trust = json.loads(archive.read("infermatrix_copilot/_runtime/trusted-keys.json"))
            # Signed wheels authorize trust rotation. Existing key IDs remain
            # immutable; publishers introduce a new ID for new key material.
            if trust.get("keys"):
                from .bootstrap import _trust_file

                atomic_json(directory / "trusted-keys.json", trust)
                _trust_file(directory / "trusted-keys.json")
                for key, value in trust["keys"].items():
                    if key in self.trusted_keys() and self.trusted_keys()[key] != value:
                        raise UpgradeError("release changed an existing signing key identity")
        except (UpgradeError, ValueError, KeyError, zipfile.BadZipFile) as exc:
            state = read_json(self.root / "updates.json", {})
            state.setdefault("quarantined", {})[identity] = "candidate recovery launcher or trust store failed validation"
            atomic_json(self.root / "updates.json", state)
            raise UpgradeError("candidate recovery launcher failed validation and was quarantined") from exc
        atomic_json(directory / "manifest.json", envelope)
        atomic_json(directory / "installed.json", metadata)
        return metadata

    def _stage_launcher(self, wheel: Path) -> str:
        """Stage recovery code from the signed wheel, leaving the old zip intact."""
        with zipfile.ZipFile(wheel) as source:
            names = [name for name in source.namelist()
                     if name == "infermatrix_copilot/__init__.py"
                     or (name.startswith("infermatrix_copilot/upgrade/") and name.endswith(".py"))]
            if "infermatrix_copilot/upgrade/launcher.py" not in names:
                raise UpgradeError("release omits recovery launcher code")
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target:
                target.writestr("__main__.py", "from infermatrix_copilot.upgrade.launcher import main\nraise SystemExit(main())\n")
                for name in sorted(names):
                    target.writestr(name, source.read(name))
        data = output.getvalue()
        name = f"updater-{hashlib.sha256(data).hexdigest()[:16]}.pyz"
        application = self.root / "launcher" / name
        atomic_write(application, data)
        interpreter = python_path(self.root / "launcher/venv")
        if not interpreter.is_file():
            raise UpgradeError("recovery environment is missing; rerun the installer")
        # A candidate app may require a newer Python than the recovery
        # environment. Prove its updater can run independently before switching.
        run_command([str(interpreter), "-I", str(application), "--home", str(self.root),
                     "upgrade", "status", "--json"], timeout=30)
        return name

    def health(self, metadata: dict) -> None:
        with tempfile.TemporaryDirectory(prefix="copilot-health-") as scratch:
            # Keep platform process essentials, not application credentials/config.
            env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP") if key in os.environ}
            env.update(HOME=scratch, USERPROFILE=scratch, RUN_ROOT=str(Path(scratch) / "runs"),
                       COPILOT_RELEASE_ID=metadata["release_id"],
                       COPILOT_RELEASE_ROOT=str(self.root / "releases" / metadata["release_id"]),
                       COPILOT_RUNTIME_PYTHON=metadata["python"],
                       INFERMATRIX_COPILOT_HOME=str(Path(scratch) / ".infermatrix-copilot"),
                       ALLOW_PUSH="0", ALLOW_POST="0")
            run_command([metadata["python"], "-m", "infermatrix_copilot.upgrade.health",
                         "--expected-version", metadata["version"], "--expected-release", metadata["release_id"]],
                        env=env, cwd=Path(scratch), timeout=60)

    def _activate(self, identity: str) -> None:
        active = self.active()
        if active and active["release_id"] == identity:
            return
        snapshot = self._activation_snapshot(identity)
        if active:
            previous = self._activation_snapshot(active["release_id"])
            # Preserve the exact trust/recovery selected at that activation,
            # including installer-added keys and any authenticated revocations.
            for key in ("launcher_application", "trusted_keys"):
                if key in active:
                    previous[key] = active[key]
            snapshot["previous"] = previous
        snapshot["previous_release_id"] = active["release_id"] if active else ""
        # One authoritative commit point selects application, recovery code and
        # verification keys. A crash cannot publish only part of an activation.
        atomic_json(self.root / "active.json", snapshot)

    def _activation_snapshot(self, identity: str) -> dict:
        metadata = self.installed(identity)
        snapshot = {"release_id": identity, "trusted_keys": self.trusted_keys()}
        application = metadata.get("launcher_application")
        if application:
            if (Path(application).name != application or not application.startswith("updater-")
                    or not (self.root / "launcher" / application).is_file()):
                raise UpgradeError("release recovery launcher is missing")
            snapshot["launcher_application"] = application
        trust = read_json(self.root / "releases" / identity / "trusted-keys.json")
        if trust and trust.get("keys"):
            snapshot["trusted_keys"] = trust["keys"]
        return snapshot

    def rollback(self, *, failed: str = "") -> dict:
        with file_lock(self.root / "locks/update.lock") as held:
            if not held:
                raise UpgradeError("another updater is busy")
            active = self.active()
            if active and failed and active["release_id"] != failed:
                return {"state": "superseded", "release_id": active["release_id"]}
            if not active or not active.get("previous_release_id"):
                raise UpgradeError("no previous healthy release is available")
            previous = active["previous_release_id"]
            self.installed(previous)
            snapshot = active.get("previous") or self._activation_snapshot(previous)
            if snapshot.get("release_id") != previous:
                raise UpgradeError("invalid previous activation snapshot")
            snapshot = {**snapshot, "previous_release_id": ""}
            state = read_json(self.root / "updates.json", {})
            state.setdefault("quarantined", {})[active["release_id"]] = "rolled back"
            atomic_json(self.root / "updates.json", state)
            # No toggling: a failed release must not become the next rollback target.
            atomic_json(self.root / "active.json", snapshot)
            return {"state": "rolled_back", "release_id": previous}

    def pin(self, version: str) -> dict:
        from packaging.version import InvalidVersion, Version

        try:
            version = str(Version(version))
        except InvalidVersion as exc:
            raise UpgradeError("invalid pinned version") from exc
        with file_lock(self.root / "locks/update.lock") as held:
            if not held:
                raise UpgradeError("another updater is busy")
            active = self.active()
            if active and self.installed(active["release_id"])["version"] == version:
                identity = active["release_id"]
            else:
                matches = [path for path in (self.root / "releases").glob("*/installed.json")
                           if read_json(path, {}).get("version") == version]
                if not matches:
                    raise UpgradeError("version is not installed; run upgrade --version VERSION first")
                identity = matches[0].parent.name
                if identity in read_json(self.root / "updates.json", {}).get("quarantined", {}):
                    raise UpgradeError("cannot pin a quarantined release")
                self._activate(identity)
            policy = self.policy()
            policy["pinned_version"] = version
            lines = [f"{key} = {str(value).lower() if isinstance(value, bool) else json.dumps(value)}"
                     for key, value in policy.items()]
            atomic_write(self.root / "updates.toml", ("\n".join(lines) + "\n").encode())
        return {"state": "pinned", "release_id": identity, "version": version}
