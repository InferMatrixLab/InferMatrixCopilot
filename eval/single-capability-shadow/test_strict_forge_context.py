import json
import subprocess

import pytest

from strict_host import frozen_gh, forge_cli


@pytest.fixture
def case():
    row = {'pr': 7, 'repository': 'org/repo', 'expected_head_sha': 'a' * 40,
           'title': 'frozen title', 'body': 'frozen body'}
    host = {'business_context': {'pull': {'base_ref': 'release', 'labels': ['bug']},
        'pr_review_context': [
            {'kind': 'issue_comment', 'author': 'author', 'body': 'general discussion'},
            {'kind': 'review_summary', 'author': 'peer', 'body': 'review summary', 'state': 'COMMENTED'},
            {'kind': 'inline_comment', 'author': 'peer', 'path': 'source.py',
             'original_line': 17, 'body': 'prior-head concern'}]}}
    return row, host


def test_v2_preserves_discussion_types_and_known_authors(case):
    row, host = case
    gh = frozen_gh(row, host, context_version=2)
    code, raw = gh(['pr', 'view', '7', '--json', 'title,body,labels,headRefName,comments,reviews,commits'])
    data = json.loads(raw)
    assert code == 0 and data['baseRefName'] == 'release'
    assert data['comments'] == [{'author': {'login': 'author'}, 'body': 'general discussion'}]
    assert data['reviews'] == [{'author': {'login': 'peer'}, 'body': 'review summary', 'state': 'COMMENTED'}]
    assert data['labels'] == [{'name': 'bug'}]
    assert data['headRefName'] == ''  # unavailable, never a synthetic branch
    code, raw = gh(['api', 'repos/org/repo/pulls/7/comments'])
    inline = [json.loads(line) for line in raw.splitlines()]
    assert inline == [{'user': 'peer', 'path': 'source.py', 'line': None, 'body': 'prior-head concern'}]


def test_default_retains_legacy_wire_bytes(case):
    row, host = case
    gh = frozen_gh(row, host)
    _, raw = gh(['api', 'repos/org/repo/pulls/7/comments'])
    expected = '\n'.join(json.dumps({'user': 'frozen-shadow-fixture', 'path': x.get('path'),
        'line': x.get('line'), 'body': x['body']}) for x in host['business_context']['pr_review_context'])
    assert raw == expected
    assert gh(['pr', 'view', '7', '--json', 'title,body'])[1] == json.dumps({
        'baseRefName': 'main', 'headRefOid': 'a' * 40, 'title': 'frozen title',
        'body': 'frozen body', 'labels': [], 'headRefName': 'shadow', 'comments': [], 'reviews': []})


def test_legacy_repository_read_does_not_require_discussions(case):
    row, _ = case
    code, raw = frozen_gh(row, {})(['repo', 'view'])
    assert code == 0 and json.loads(raw)['nameWithOwner'] == 'org/repo'


@pytest.mark.parametrize('version', [0, 3, True, '2'])
def test_unknown_version_cannot_silently_choose_another_contract(case, version):
    with pytest.raises(ValueError, match='unsupported frozen forge context version'):
        frozen_gh(*case, context_version=version)


def test_v2_does_not_silently_drop_unknown_discussion_kinds(case):
    row, host = case
    host['business_context']['pr_review_context'][0]['kind'] = 'unknown'
    with pytest.raises(ValueError, match='unsupported frozen discussion kind'):
        frozen_gh(row, host, context_version=2)


def test_actual_cli_child_receives_v2_descriptor(tmp_path, case):
    row, host = case
    with forge_cli(tmp_path, {'kind': 'case', 'row': row, 'host': host, 'context_version': 2}):
        out = subprocess.run(['gh', 'api', 'repos/org/repo/pulls/7/comments'],
                             capture_output=True, text=True, check=True)
        assert json.loads(out.stdout)['user'] == 'peer'
