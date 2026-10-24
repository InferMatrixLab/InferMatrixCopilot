import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from infermatrix_copilot.source_fingerprint import source_tree_hash
from infermatrix_copilot.improve.artifacts import ArtifactError, tree_hash, validate_policy


def test_source_identity_preserves_root_order_bytes_and_exclusions(tmp_path):
    contents = {'src/z.py': b'code\r\n', 'playbooks/a.yaml': b'workflow\n',
                'adapters/demo/adapter.py': b'\xff\x00', 'skills/demo/SKILL.md': b'skill'}
    for name, value in reversed(list(contents.items())):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    expected = hashlib.sha256(json.dumps([(name, hashlib.sha256(value).hexdigest())
        for name, value in contents.items()], separators=(',', ':')).encode()).hexdigest()
    assert source_tree_hash(tmp_path) == tree_hash(tmp_path) == expected
    for name in ('knowledge/ignored.md', 'src/__pycache__/ignored.py', 'src/ignored.pyc'):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('excluded')
    assert source_tree_hash(tmp_path) == expected
    (tmp_path / 'src/z.py').write_bytes(b'code\n')
    assert source_tree_hash(tmp_path) != expected


@pytest.mark.parametrize('link', ['src/linked.py', 'src/__pycache__/linked.py'])
def test_artifact_wrapper_preserves_symlink_refusal_and_error_type(tmp_path, link):
    path = tmp_path / link
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(tmp_path / 'absent')
    with pytest.raises(ArtifactError, match='symlink in artifact'):
        tree_hash(tmp_path)
    with pytest.raises(ValueError, match='symlink in artifact'):
        source_tree_hash(tmp_path)


def test_neutral_hash_owner_remains_protected_from_candidate_mutation():
    with pytest.raises(ArtifactError, match='protected mutation path'):
        validate_policy({'paths': ['src/infermatrix_copilot/source_fingerprint.py']})


def test_trace_fingerprint_does_not_import_improvement(tmp_path):
    (tmp_path / 'src/infermatrix_copilot').mkdir(parents=True)
    (tmp_path / 'playbooks').mkdir()
    code = '''
import sys
from pathlib import Path
import infermatrix_copilot.trace_store as trace
# Exercise source-tree detection without importing product code from a checkout.
trace.__file__ = str(Path(sys.argv[1]) / 'src/infermatrix_copilot/trace_store.py')
trace._ENV = None
value = trace.environment_fingerprint()
assert len(value['source_tree_sha']) == 64
assert not any(name.startswith('infermatrix_copilot.improve') for name in sys.modules)
'''
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path)], env=os.environ.copy(),
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_init_policy_identity_includes_neutral_fingerprint_implementation(monkeypatch):
    from infermatrix_copilot.kb_service.init_execution import policy_digest
    before = policy_digest()
    original = Path.read_bytes
    def changed(path):
        data = original(path)
        return data + b"\n# changed fingerprint algorithm\n" if path.name == "source_fingerprint.py" else data
    monkeypatch.setattr(Path, "read_bytes", changed)
    assert policy_digest() != before


def test_package_identity_binds_installed_code_and_resources_independent_of_location(tmp_path):
    import shutil
    from infermatrix_copilot.source_fingerprint import package_tree_hash
    package = tmp_path / 'package'
    (package / 'data').mkdir(parents=True)
    (package / 'module.py').write_text('implementation\n')
    (package / 'data/policy.yaml').write_text('policy: original\n')
    first = package_tree_hash(package)
    copied = tmp_path / 'relocated'
    shutil.copytree(package, copied)
    assert package_tree_hash(copied) == first
    (copied / '__pycache__').mkdir()
    (copied / '__pycache__/module.pyc').write_bytes(b'cache')
    assert package_tree_hash(copied) == first
    (copied / 'data/policy.yaml').write_text('policy: changed\n')
    assert package_tree_hash(copied) != first
    (package / 'module.py').write_text('different implementation\n')
    assert package_tree_hash(package) != first


def test_package_identity_refuses_linked_resources(tmp_path):
    from infermatrix_copilot.source_fingerprint import package_tree_hash
    (tmp_path / 'policy').symlink_to(tmp_path / 'missing')
    with pytest.raises(ValueError, match='symlink in artifact'):
        package_tree_hash(tmp_path)


def test_failed_environment_fingerprint_is_never_cached_as_partial_identity(tmp_path, monkeypatch):
    import infermatrix_copilot.trace_store as trace
    import infermatrix_copilot.source_fingerprint as fingerprint
    package = tmp_path / 'src/infermatrix_copilot'
    package.mkdir(parents=True)
    (tmp_path / 'playbooks').mkdir()
    monkeypatch.setattr(trace, '__file__', str(package / 'trace_store.py'))
    monkeypatch.setattr(trace, '_ENV', None)
    def fail(*args, **kwargs):
        raise ValueError('incomplete source identity')
    monkeypatch.setattr(fingerprint, 'source_tree_hash', fail)
    for _ in range(2):
        with pytest.raises(ValueError, match='incomplete source identity'):
            trace.environment_fingerprint()
        assert trace._ENV is None


def test_offline_recovery_can_recheck_runtime_after_files_change(tmp_path, monkeypatch):
    import infermatrix_copilot.trace_store as trace
    package = tmp_path / 'package'
    package.mkdir()
    code = package / 'trace_store.py'
    code.write_text('original')
    monkeypatch.setattr(trace, '__file__', str(code))
    monkeypatch.setattr(trace, '_ENV', None)
    original = trace.environment_fingerprint()
    code.write_text('changed')
    assert trace.environment_fingerprint() == original
    assert trace.environment_fingerprint(refresh=True)['runtime_package_sha256'] != original['runtime_package_sha256']
