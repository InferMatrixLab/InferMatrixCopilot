"""Untrusted release metadata, transport redirects, and archive boundaries."""

import base64
import copy
import errno
import io
import json
import tarfile
import urllib.request

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from infermatrix_copilot.upgrade import releases
from infermatrix_copilot.upgrade.storage import UpgradeError, file_lock


@pytest.fixture
def signed_release():
    signer = Ed25519PrivateKey.generate()
    key = base64.b64encode(signer.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    manifest = {
        "schema": 1, "release_id": "1.2.0-abcdef012345", "version": "1.2.0",
        "commit": "a" * 40, "channel": "stable", "sequence": 12,
        "requires_python": ">=3.11", "min_launcher_version": 1,
        "runtime_schema": 1, "skill_protocol": 1, "knowledge_formats": [2],
        "artifacts": {
            "bundle": {"url": "https://example.org/release.tar.gz", "sha256": "b" * 64, "size": 123},
            "wheel": {"path": "wheel/copilot-1.2.0-py3-none-any.whl", "sha256": "c" * 64},
        },
    }

    def sign(data=None):
        chosen = copy.deepcopy(manifest if data is None else data)
        return {"key_id": "release-key", "manifest": chosen,
                "signature": base64.b64encode(signer.sign(releases.canonical_manifest(chosen))).decode()}

    return manifest, {"release-key": key}, sign


def test_ed25519_verified_manifest_is_returned(signed_release):
    manifest, keys, sign = signed_release
    assert releases.verify_envelope(sign(), keys, channel="stable", sequence=12) == manifest


@pytest.mark.parametrize("attack", ["unknown-key", "wrong-key", "tamper", "short-signature", "invalid-base64"])
def test_signature_attacks_are_rejected(signed_release, attack):
    _manifest, keys, sign = signed_release
    envelope = sign()
    if attack == "unknown-key":
        envelope["key_id"] = "unknown"
    elif attack == "wrong-key":
        keys["release-key"] = base64.b64encode(Ed25519PrivateKey.generate().public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw)).decode()
    elif attack == "tamper":
        envelope["manifest"]["artifacts"]["bundle"]["url"] = "https://attacker.example/payload"
    elif attack == "short-signature":
        envelope["signature"] = base64.b64encode(b"short").decode()
    else:
        envelope["signature"] = "!not-base64!"
    with pytest.raises(UpgradeError, match="signature"):
        releases.verify_envelope(envelope, keys, channel="stable")


@pytest.mark.parametrize("field,value", [
    ("schema", 2), ("schema", True), ("release_id", "../outside"),
    ("version", "invalid"), ("version", "1.3.0rc1"), ("version", "1.3.0.dev1"),
    ("commit", "main"), ("sequence", 11), ("sequence", True),
    ("min_launcher_version", 2), ("min_launcher_version", -1),
    ("runtime_schema", 2), ("runtime_schema", True), ("skill_protocol", 2),
    ("skill_protocol", True), ("knowledge_formats", [3]), ("knowledge_formats", [2.0]),
    ("requires_python", "not a specifier"), ("requires_python", ""),
    ("channel", "preview"),
])
def test_signed_incompatible_manifest_fails_before_install(signed_release, field, value):
    manifest, keys, sign = signed_release
    manifest[field] = value
    with pytest.raises(UpgradeError):
        releases.verify_envelope(sign(manifest), keys, channel="stable", sequence=12)


def test_preview_prerelease_is_allowed_only_on_preview(signed_release):
    manifest, keys, sign = signed_release
    manifest.update(channel="preview", version="1.3.0rc1")
    assert releases.verify_envelope(sign(manifest), keys, channel="preview")["version"] == "1.3.0rc1"


@pytest.mark.parametrize("artifact,field,value", [
    ("bundle", "sha256", "incorrect"), ("wheel", "sha256", "incorrect"),
    ("bundle", "size", True), ("bundle", "size", 0),
    ("bundle", "size", releases.MAX_BUNDLE + 1),
    ("bundle", "url", "http://example.org/plaintext"),
    ("bundle", "url", "https://secret@example.org/payload"),
    ("bundle", "url", None),
    ("wheel", "path", "../payload.whl"), ("wheel", "path", "/wheel/payload.whl"),
    ("wheel", "path", "wheel/C:\\payload.whl"),
    ("wheel", "path", "wheel/payload.zip"),
])
def test_signed_artifact_metadata_is_validated(signed_release, artifact, field, value):
    manifest, keys, sign = signed_release
    manifest["artifacts"][artifact][field] = value
    with pytest.raises(UpgradeError):
        releases.verify_envelope(sign(manifest), keys, channel="stable")


def _archive(path, entries):
    with tarfile.open(path, "w:gz") as archive:
        for name, kind, data in entries:
            info = tarfile.TarInfo(name)
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif kind in {"symlink", "hardlink"}:
                info.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
                info.linkname = data
                archive.addfile(info)
            else:
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))


def test_regular_bundle_extracts_bytes(tmp_path):
    archive = tmp_path / "release.tar.gz"
    _archive(archive, [("wheel", "dir", b""), ("wheel/copilot.whl", "file", b"wheel-bytes"),
                       ("requirements.txt", "file", b"locked requirements")])
    releases.extract_bundle(archive, tmp_path / "result")
    assert (tmp_path / "result/wheel/copilot.whl").read_bytes() == b"wheel-bytes"


@pytest.mark.parametrize("name", ["../escaped", "/escaped", "a/../../escaped",
                                 "a\\..\\escaped", "C:/escaped", "."])
def test_tar_paths_cannot_escape_destination(tmp_path, name):
    archive = tmp_path / "bad.tar.gz"
    _archive(archive, [(name, "file", b"payload")])
    with pytest.raises(UpgradeError, match="unsafe"):
        releases.extract_bundle(archive, tmp_path / "result")
    assert not (tmp_path / "escaped").exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_tar_links_are_rejected(tmp_path, kind):
    archive = tmp_path / "bad.tar.gz"
    _archive(archive, [("link", kind, "../outside")])
    with pytest.raises(UpgradeError, match="unsafe"):
        releases.extract_bundle(archive, tmp_path / "result")


def test_tar_duplicate_normalized_directory_is_rejected(tmp_path):
    archive = tmp_path / "bad.tar.gz"
    _archive(archive, [("a/b", "dir", b""), ("a//b/", "dir", b"")])
    with pytest.raises(UpgradeError, match="unsafe"):
        releases.extract_bundle(archive, tmp_path / "result")


def test_tar_duplicate_file_is_rejected_before_overwriting(tmp_path):
    archive = tmp_path / "bad.tar.gz"
    _archive(archive, [("file", "file", b"original"), ("./file", "file", b"replacement")])
    with pytest.raises(UpgradeError, match="unsafe"):
        releases.extract_bundle(archive, tmp_path / "result")
    assert (tmp_path / "result/file").read_bytes() == b"original"


def test_tar_existing_symlink_destination_is_rejected(tmp_path):
    archive = tmp_path / "bad.tar.gz"
    _archive(archive, [("escaped", "file", b"payload")])
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = tmp_path / "result"
    destination.symlink_to(outside, target_is_directory=True)
    with pytest.raises(UpgradeError, match="empty"):
        releases.extract_bundle(archive, destination)
    assert not (outside / "escaped").exists()


@pytest.mark.parametrize("bound", ["bytes", "members"])
def test_compressed_tar_bombs_are_bounded(tmp_path, monkeypatch, bound):
    archive = tmp_path / "bomb.tar.gz"
    if bound == "bytes":
        monkeypatch.setattr(releases, "MAX_EXTRACTED_SIZE", 1024)
        _archive(archive, [("large", "file", b"0" * 2048)])
    else:
        monkeypatch.setattr(releases, "MAX_MEMBERS", 1)
        _archive(archive, [("one", "file", b""), ("two", "file", b"")])
    with pytest.raises(UpgradeError, match="unsafe"):
        releases.extract_bundle(archive, tmp_path / "result")


def test_redirect_never_carries_auth_to_another_host():
    request = urllib.request.Request("https://api.github.com/releases/assets/12",
                                     headers={"Authorization": "Bearer test-secret"})
    redirected = releases._Redirect().redirect_request(
        request, None, 302, "Found", {}, "https://release-assets.githubusercontent.com/blob")
    assert redirected.get_header("Authorization") is None
    assert redirected.full_url == "https://release-assets.githubusercontent.com/blob"
    same = releases._Redirect().redirect_request(
        request, None, 302, "Found", {}, "https://api.github.com/another")
    assert same.get_header("Authorization") == "Bearer test-secret"


@pytest.mark.parametrize("url", ["http://api.github.com/asset", "https://secret@api.github.com/asset", "file:///tmp/asset"])
def test_redirect_rejects_downgrade_and_url_credentials(url):
    request = urllib.request.Request("https://api.github.com/asset",
                                     headers={"Authorization": "Bearer test-secret"})
    with pytest.raises(UpgradeError):
        releases._Redirect().redirect_request(request, None, 302, "Found", {}, url)


def test_github_token_never_sent_to_unrelated_origin(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "test-secret")
    requests = []

    class Response(io.BytesIO):
        headers = {}

    class Opener:
        def open(self, request, **kwargs):
            requests.append(request)
            return Response(b"metadata")

    fetcher = releases.Fetcher()
    fetcher.opener = Opener()
    assert fetcher.fetch("https://example.org/release.json") == b"metadata"
    assert requests[0].get_header("Authorization") is None
    assert fetcher.fetch("https://api.github.com/repos/owner/repo/releases/tags/v1") == b"metadata"
    assert requests[1].get_header("Authorization") == "Bearer test-secret"
    assert fetcher.fetch("https://api.github.com:8443/release.json") == b"metadata"
    assert requests[2].get_header("Authorization") is None


def test_github_metadata_cannot_redirect_token_to_insecure_asset(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "test-secret")
    requests = []

    class Response(io.BytesIO):
        headers = {}

    class Opener:
        def open(self, request, **kwargs):
            requests.append(request)
            return Response(json.dumps({"assets": [{"name": "release.tar.gz",
                "url": "http://api.github.com/asset"}]}).encode())

    fetcher = releases.Fetcher()
    fetcher.opener = Opener()
    with pytest.raises(UpgradeError):
        fetcher.fetch("https://github.com/owner/repo/releases/download/v1/release.tar.gz")
    assert len(requests) == 1


@pytest.mark.parametrize("header,body", [("100", b"small"), (None, b"a" * 11)])
def test_fetch_size_is_bounded_with_and_without_content_length(monkeypatch, header, body):
    class Response(io.BytesIO):
        headers = {"Content-Length": header} if header else {}

    fetcher = releases.Fetcher()
    monkeypatch.setattr(fetcher.opener, "open", lambda *a, **kw: Response(body))
    with pytest.raises(UpgradeError, match="size limit"):
        fetcher.fetch("https://example.org/bundle", limit=10)


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX fcntl failure injection")
def test_unbounded_file_lock_surfaces_non_contention_errors(tmp_path, monkeypatch):
    import fcntl

    def unavailable(*args):
        raise OSError(errno.ENOLCK, "no locks available")

    monkeypatch.setattr(fcntl, "flock", unavailable)
    with pytest.raises(UpgradeError, match="advisory lock"):
        with file_lock(tmp_path / "lock", timeout=None):
            pytest.fail("unprotected execution")
