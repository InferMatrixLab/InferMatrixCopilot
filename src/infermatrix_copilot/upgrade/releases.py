"""Bounded release transport, signature verification, and safe bundle extraction."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

from . import LAUNCHER_VERSION, RUNTIME_SCHEMA, SKILL_PROTOCOL
from .storage import UpgradeError, release_id

MAX_METADATA = 1024 * 1024
MAX_BUNDLE = 256 * 1024 * 1024
MAX_EXTRACTED_SIZE = 512 * 1024 * 1024
MAX_MEMBERS = 1000


def _validate_url(url: str, *, local: bool = True) -> urllib.parse.SplitResult:
    try:
        if not isinstance(url, str):
            raise ValueError("invalid URL")
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme not in ({"https", "file"} if local else {"https"})
                or parsed.username or parsed.password
                or (parsed.scheme == "https" and not parsed.hostname)
                or (parsed.scheme == "file" and parsed.netloc not in {"", "localhost"})):
            raise ValueError("invalid URL")
        parsed.port  # validates malformed/non-numeric ports, without echoing the URL
        return parsed
    except (TypeError, ValueError) as exc:
        raise UpgradeError("release URLs must use HTTPS or an explicitly configured local file without credentials") from exc


def canonical_manifest(manifest: dict) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        _validate_url(newurl, local=False)
        target = super().redirect_request(request, fp, code, msg, headers, newurl)
        if target and urllib.parse.urlsplit(request.full_url).netloc != urllib.parse.urlsplit(newurl).netloc:
            target.remove_header("Authorization")
        return target


class Fetcher:
    """GitHub auth is used only for GitHub; never load application .env keys."""

    def __init__(self, *, timeout: float = 15.0):
        self.timeout = timeout
        self.opener = urllib.request.build_opener(_Redirect())

    def _token(self) -> str:
        token = os.environ.get("COPILOT_UPDATE_TOKEN") or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if token:
            return token
        if shutil.which("gh"):
            try:
                result = subprocess.run(["gh", "auth", "token"], capture_output=True,
                                        text=True, timeout=5, check=False)
                if result.returncode == 0:
                    return result.stdout.strip()
            except (OSError, subprocess.SubprocessError):
                pass
        return ""

    def fetch(self, url: str, *, limit: int = MAX_METADATA) -> bytes:
        parsed = _validate_url(url)
        if type(limit) is not int or limit <= 0:
            raise UpgradeError("release download size limit must be positive")
        # Browser asset URLs do not support bearer auth on private repositories.
        # Resolve their asset API URL before download, keeping credentials scoped.
        token = (self._token() if parsed.hostname in {"github.com", "api.github.com"}
                 and parsed.port in {None, 443} else "")
        match = re.fullmatch(r"/([^/]+)/([^/]+)/releases/download/([^/]+)/([^/]+)", parsed.path)
        accept = "application/octet-stream"
        if parsed.hostname == "github.com" and match and token:
            owner, repo, tag, asset = match.groups()
            metadata = self.fetch(f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}")
            try:
                assets = json.loads(metadata)["assets"]
                url = next(item["url"] for item in assets if item["name"] == urllib.parse.unquote(asset))
            except (ValueError, KeyError, TypeError, StopIteration) as exc:
                raise UpgradeError("release asset is unavailable") from exc
            asset_url = _validate_url(url, local=False)
            if asset_url.hostname != "api.github.com" or asset_url.port not in {None, 443}:
                raise UpgradeError("invalid GitHub asset API URL")
        elif parsed.hostname == "api.github.com":
            accept = "application/vnd.github+json" if "/releases/tags/" in parsed.path else accept
        headers = {"User-Agent": "InferMatrixCopilot-Updater/1", "Accept": accept}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with self.opener.open(urllib.request.Request(url, headers=headers), timeout=self.timeout) as response:
                if response.headers.get("Content-Length") and int(response.headers["Content-Length"]) > limit:
                    raise UpgradeError("release download exceeds its size limit")
                data = response.read(limit + 1)
                if len(data) > limit:
                    raise UpgradeError("release download exceeds its size limit")
                return data
        except (OSError, ValueError, urllib.error.URLError) as exc:
            # URLs and headers can carry credentials; do not echo them in logs.
            raise UpgradeError("release download failed; check connectivity and GitHub access") from exc


def verify_envelope(envelope: dict, keys: dict, *, channel: str, sequence: int = 0) -> dict:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.version import InvalidVersion, Version

    try:
        manifest = envelope["manifest"]
        key = keys[envelope["key_id"]]
        signature = base64.b64decode(envelope["signature"], validate=True)
        Ed25519PublicKey.from_public_bytes(base64.b64decode(key, validate=True)).verify(
            signature, canonical_manifest(manifest))
    except (KeyError, TypeError, ValueError, InvalidSignature) as exc:
        raise UpgradeError("release signature is invalid or its signing key is not trusted") from exc
    try:
        if type(manifest["schema"]) is not int or manifest["schema"] != 1 or manifest["channel"] != channel:
            raise ValueError("schema or channel mismatch")
        release_id(manifest["release_id"])
        version = Version(manifest["version"])
        if channel == "stable" and (version.is_prerelease or version.is_devrelease):
            raise ValueError("stable channel contains a prerelease")
        if not re.fullmatch(r"[0-9a-f]{40}", manifest["commit"]):
            raise ValueError("commit is not immutable")
        if type(manifest["sequence"]) is not int or manifest["sequence"] < sequence:
            raise ValueError("release feed sequence went backwards")
        if (type(manifest["min_launcher_version"]) is not int
                or not 1 <= manifest["min_launcher_version"] <= LAUNCHER_VERSION):
            raise ValueError("release needs a newer recovery launcher; rerun the installer")
        if (type(manifest["runtime_schema"]) is not int
                or type(manifest["skill_protocol"]) is not int
                or manifest["runtime_schema"] != RUNTIME_SCHEMA
                or manifest["skill_protocol"] != SKILL_PROTOCOL):
            raise ValueError("release requires a maintenance migration")
        if (manifest["knowledge_formats"] != [2]
                or any(type(item) is not int for item in manifest["knowledge_formats"])):
            raise ValueError("release has incompatible knowledge formats")
        if not isinstance(manifest["requires_python"], str) or not manifest["requires_python"].strip():
            raise ValueError("missing Python requirement")
        SpecifierSet(manifest["requires_python"])
        artifact = manifest["artifacts"]["bundle"]
        wheel = manifest["artifacts"]["wheel"]
        for item in (artifact, wheel):
            if not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
                raise ValueError("invalid artifact digest")
        if type(artifact["size"]) is not int or not 0 < artifact["size"] <= MAX_BUNDLE:
            raise ValueError("invalid bundle size")
        _validate_url(artifact["url"])
        path = PurePosixPath(wheel["path"])
        if (path.is_absolute() or ".." in path.parts or "\\" in wheel["path"]
                or ":" in wheel["path"] or len(path.parts) != 2
                or path.parts[0] != "wheel" or path.suffix != ".whl"):
            raise ValueError("invalid wheel path")
    except (KeyError, TypeError, ValueError, InvalidSpecifier, InvalidVersion) as exc:
        raise UpgradeError(f"release manifest is incompatible: {exc}") from exc
    return manifest


def extract_bundle(archive: Path, destination: Path) -> None:
    """Extract regular files only; reject links, escapes and decompression bombs."""
    size = 0
    count = 0
    seen = set()
    try:
        # Release staging owns a fresh directory. Refusing existing contents
        # also prevents a pre-existing symlink from redirecting file writes.
        if destination.is_symlink() or (destination.exists() and any(destination.iterdir())):
            raise UpgradeError("release bundle destination must be empty")
        destination.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive, "r:gz") as bundle:
            for member in bundle:
                path = PurePosixPath(member.name)
                canonical = path.as_posix().rstrip("/")
                count += 1
                size += member.size
                if (not path.parts or path.is_absolute() or ".." in path.parts
                        or "\\" in member.name or ":" in member.name
                        or canonical in seen or count > MAX_MEMBERS
                        or member.size < 0 or size > MAX_EXTRACTED_SIZE
                        or not (member.isfile() or member.isdir())):
                    raise UpgradeError("unsafe release bundle")
                seen.add(canonical)
                target = destination.joinpath(*path.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.extractfile(member) as source, target.open("xb") as output:
                        shutil.copyfileobj(source, output)
    except (tarfile.TarError, OSError) as exc:
        raise UpgradeError("cannot unpack release bundle") from exc


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
