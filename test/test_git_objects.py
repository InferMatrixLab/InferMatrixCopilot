"""Object-reader policies stay distinct while their parsers are shared."""

import subprocess
from types import SimpleNamespace

import pytest

from infermatrix_copilot.git_objects import batch_blobs, raw_changes, tree_entries
from infermatrix_copilot.kb_service.git_source import GitSource
from infermatrix_copilot.kb_service.sources import KnowledgeRepo, SourceError
from infermatrix_copilot.knowledge_service.gate_verifier import GateError, Git


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], stderr=subprocess.PIPE)


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
@pytest.mark.parametrize("bare", [False, True])
def test_source_and_governed_readers_preserve_bytes_modes_and_hash_width(tmp_path, object_format, bare):
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q", f"--object-format={object_format}")
    git(work, "config", "user.email", "test@example.invalid")
    git(work, "config", "user.name", "Test")
    directory = work / "knowledge/repos/demo"
    directory.mkdir(parents=True)
    text = "含空格\t名称.md"
    (directory / text).write_bytes("真实内容\r\n".encode())
    (directory / "binary.md").write_bytes(b"\xff\x00\n")
    (directory / "executable.md").write_bytes(b"executable\n")
    (directory / "executable.md").chmod(0o755)
    (directory / "link.md").symlink_to(text)
    git(work, "add", ".")
    git(work, "commit", "-qm", "baseline")
    pin = git(work, "rev-parse", "HEAD").decode().strip()
    (directory / text).write_text("dirty working content")
    path = work
    if bare:
        path = tmp_path / "mirror.git"
        subprocess.check_output(["git", "clone", "-q", "--bare", str(work), str(path)])
    source = GitSource(path)
    prefix = "knowledge/repos/demo/"
    assert source.object_format == object_format
    assert source.read(pin, prefix + text) == "真实内容\r\n".encode()
    assert source.files(pin)[prefix + "executable.md"]["mode"] == "100755"
    assert prefix + "link.md" not in source.files(pin)
    expected = {prefix + text: "真实内容\r\n"}
    assert KnowledgeRepo(path)._texts(pin, (prefix,), (".md",)) == expected
    assert Git(path).texts(pin, (prefix,), (".md",)) == expected
    assert KnowledgeRepo(path)._texts(pin, (prefix,), ()) == expected
    assert Git(path).texts(pin, (prefix,), ()) == {}
    exported = source.export(pin, tmp_path / "exported")
    assert (exported / prefix / text).read_bytes() == "真实内容\r\n".encode()
    assert (exported / prefix / "binary.md").read_bytes() == b"\xff\x00\n"
    assert (exported / prefix / "executable.md").stat().st_mode & 0o111
    assert not (exported / prefix / "link.md").exists()
    with pytest.raises(SourceError):
        source.read(pin, "../outside")
    with pytest.raises(SourceError):
        source.read(pin, prefix + "link.md")


@pytest.mark.parametrize("width", [40, 64])
def test_raw_manifest_null_objects_keep_modes_and_nul_paths(width):
    zero, oid = "0" * width, "a" * width
    raw = (f":000000 100755 {zero} {oid} A\0".encode() + b"tab\tline\n.py\0"
           + f":100644 000000 {oid} {zero} D\0deleted.md\0".encode())
    assert raw_changes(raw) == [
        {"path": "tab\tline\n.py", "status": "A", "old_blob": "", "new_blob": oid,
         "old_mode": "", "new_mode": "100755"},
        {"path": "deleted.md", "status": "D", "old_blob": oid, "new_blob": "",
         "old_mode": "100644", "new_mode": ""},
    ]


def test_tree_parser_retains_non_utf8_path_bytes():
    oid = "a" * 40
    assert list(tree_entries(f"100755 blob {oid}\t".encode() + b"bad\xff\tname\0")) == [
        ("100755", "blob", oid, b"bad\xff\tname")]


@pytest.mark.parametrize("malformed", [b"unparsed-output", b"100644 blob\tpath", b"\xff blob oid\tpath"])
def test_tree_parser_tolerant_lookup_keeps_valid_peers(malformed):
    valid = b"100644 blob abc\ttab\tline\n.py\0"
    with pytest.raises(ValueError):
        list(tree_entries(malformed + b"\0" + valid))
    assert list(tree_entries(malformed + b"\0" + valid, malformed="skip")) == [
        ("100644", "blob", "abc", b"tab\tline\n.py")]


def test_tree_parser_rejects_unknown_policy_even_for_empty_reply():
    with pytest.raises(ValueError, match="unsupported malformed"):
        list(tree_entries(b"", malformed="ignore"))


@pytest.mark.parametrize("symlink", [False, True])
def test_review_replay_keeps_committed_executable_bytes_and_refuses_links(tmp_path, symlink):
    from infermatrix_copilot.improve.artifacts import ArtifactError
    from infermatrix_copilot.improve.runtime import freeze_review

    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q")
    git(work, "config", "user.email", "test@example.invalid")
    git(work, "config", "user.name", "Test")
    (work / "app.py").write_bytes(b"print('pinned')\r\n")
    (work / "app.py").chmod(0o755)
    if symlink:
        (work / "link.py").symlink_to("app.py")
    git(work, "add", ".")
    git(work, "commit", "-qm", "baseline")
    pin = git(work, "rev-parse", "HEAD").decode().strip()
    (work / "app.py").write_text("dirty source")
    settings = SimpleNamespace(knowledge_dir=tmp_path / "absent-knowledge")
    state = {"repo_path": str(work), "pr_base_sha": pin, "pr_head_sha": pin,
             "diff_text": "", "task_spec": {"repo": "test/repo", "pr": 1}}
    if symlink:
        with pytest.raises(ArtifactError, match="non-regular source"):
            freeze_review(settings, state)
    else:
        frozen = freeze_review(settings, state)
        assert frozen["base_files"] == frozen["head_files"] == {"app.py": "print('pinned')\r\n"}


@pytest.mark.parametrize("reply", [b"missing\n", b"a blob 4\nabc\n", b"a tree 0\n\n",
                                  b"b blob 1\nx\n", b"a blob 1\nx!", b"a blob 1\nx\nextra"])
def test_batch_reader_rejects_unknown_truncated_and_extra_objects(reply):
    with pytest.raises(ValueError):
        list(batch_blobs(reply, ["a"]))


@pytest.mark.parametrize("publisher", [False, True])
@pytest.mark.parametrize("manifest", [False, True])
def test_malformed_git_reply_keeps_each_adapters_error_type(tmp_path, monkeypatch, publisher, manifest):
    adapter = Git(tmp_path) if publisher else KnowledgeRepo(tmp_path)
    failure = ValueError("unreadable object reply")

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(adapter, "run" if publisher else "_git", fail)
    with pytest.raises(GateError if publisher else SourceError) as caught:
        if manifest:
            (adapter.raw_diff if publisher else adapter.raw_manifest)("old", "new")
        else:
            (adapter.texts if publisher else adapter._texts)("pin", ("knowledge/",), (".md",))
    assert str(caught.value) == str(failure) and caught.value.__cause__ is failure
