import hashlib
import json

import pytest

from freeze_review_host import frozen_changed_files


@pytest.fixture
def file_pages(tmp_path):
    pages = [[{"filename": "added.py", "status": "added", "additions": 1,
               "deletions": 0, "patch": "@@ -0,0 +1 @@\n+value = 1"}],
             [{"filename": "image.bin", "status": "removed", "additions": 0,
               "deletions": 0}]]
    raw = json.dumps(pages).encode()
    (tmp_path / "github-files.json").write_bytes(raw)
    manifest = {"files": {"github-files.json": {"sha256": hashlib.sha256(raw).hexdigest()}}}
    request = {"repository": "org/project", "pr": 1, "changed_files_input": "github-files.json",
               "changed_paths": [{"path": "added.py", "status": "added"},
                                 {"path": "image.bin", "status": "removed"}]}
    return tmp_path, manifest, request


def test_frozen_api_pages_use_host_codec_without_inventing_missing_patches(file_pages):
    rows = frozen_changed_files(*file_pages)
    assert rows[0].status == "added" and rows[0].additions == 1
    assert rows[1].status == "removed" and rows[1].patch is None


def test_rejects_changed_raw_api_pages(file_pages):
    case, manifest, request = file_pages
    (case / "github-files.json").write_text("[]")
    with pytest.raises(ValueError, match="input changed"):
        frozen_changed_files(case, manifest, request)


def test_rejects_mismatched_request_paths(file_pages):
    case, manifest, request = file_pages
    request["changed_paths"] = request["changed_paths"][:1]
    with pytest.raises(ValueError, match="paths do not match"):
        frozen_changed_files(case, manifest, request)


def test_rejects_alternative_input_paths(file_pages):
    case, manifest, request = file_pages
    request["changed_files_input"] = "../private.json"
    with pytest.raises(ValueError, match="unsupported"):
        frozen_changed_files(case, manifest, request)


def test_rejects_unpaged_payload(file_pages):
    case, manifest, request = file_pages
    raw = b'[{}]'
    (case / "github-files.json").write_bytes(raw)
    manifest["files"]["github-files.json"]["sha256"] = hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError, match="must contain API pages"):
        frozen_changed_files(case, manifest, request)
