"""Discovery supplements use pinned source rather than fabricated Git identity."""
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from discovery_arm import source_case


@pytest.fixture
def case(tmp_path):
    repo = tmp_path / 'repo';repo.mkdir();(repo / 'pkg').mkdir()
    (repo / 'pkg/core.py').write_text('def step():\n    return 19\n')
    for command in [('init', '-q'), ('add', '.'), ('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
        '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'source')]:
        subprocess.run(['git', '-C', str(repo), *command], check=True, capture_output=True)
    pin = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    folder = tmp_path / 'inputs';folder.mkdir()
    packet = {'repository': 'o/toy', 'pin': pin, 'scope': {'roots': ['pkg/'], 'exclude': []},
              'candidates': [{'id': 'step', 'title': 'Step', 'description': 'Return the integer 19.',
                  'owner': 'core', 'relation': 'new', 'related_id': '', 'aliases': [],
                  'evidence': [{'path': 'pkg/core.py', 'start': 1, 'end': 2}]}]}
    def freeze():
        raw = json.dumps(packet).encode();(folder / 'discovery-relation-input.json').write_bytes(raw)
        (folder / 'manifest.json').write_text(json.dumps({'files': {'discovery-relation-input.json':
            {'sha256': hashlib.sha256(raw).hexdigest()}}}))
    return repo, folder, packet, freeze


def test_committed_source_is_exported_despite_dirty_working_tree(case, tmp_path):
    repo, folder, packet, freeze = case;freeze()
    (repo / 'pkg/core.py').write_text("raise AssertionError('dirty')\n")
    loaded, index = source_case(folder, repo, tmp_path / 'export')
    assert loaded == packet
    assert index.production == ['pkg/core.py']
    assert index.entries['pkg/core.py']['text'] == 'def step():\n    return 19\n'


@pytest.mark.parametrize('damage', ['manifest', 'pin', 'candidate'])
def test_invalid_input_cannot_create_source_export(case, tmp_path, damage):
    repo, folder, packet, freeze = case
    if damage == 'pin':packet['pin'] = 'f' * 40
    elif damage == 'candidate':packet['candidates'][0]['owner'] = '../outside'
    freeze()
    if damage == 'manifest':(folder / 'discovery-relation-input.json').write_text('{}')
    with pytest.raises((ValueError, RuntimeError)):source_case(folder, repo, tmp_path / 'export')
    assert not (tmp_path / 'export').exists()
