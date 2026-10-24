import re
import shlex
import subprocess

from infermatrix_copilot.sdk.v1 import build_direct_review_prompt


def test_direct_navigation_excludes_unrelated_target_branch_changes(tmp_path):
    def git(*args):
        return subprocess.check_output(['git', '-C', str(tmp_path), *args]).decode().strip()
    git('init', '-q')
    git('config', 'user.name', 'offline')
    git('config', 'user.email', 'offline@example.invalid')
    (tmp_path / 'source.py').write_text('value = 1\n')
    git('add', '.')
    git('commit', '-qm', 'common ancestor')
    ancestor = git('rev-parse', 'HEAD')
    (tmp_path / 'source.py').write_text('value = 2\n')
    git('commit', '-qam', 'PR change')
    head = git('rev-parse', 'HEAD')
    git('checkout', '-q', '--detach', ancestor)
    (tmp_path / 'target-only.py').write_text('unrelated = True\n')
    git('add', '.')
    git('commit', '-qm', 'target advances')
    base = git('rev-parse', 'HEAD')
    git('checkout', '-q', '--detach', head)
    assert 'target-only.py' in git('diff', base, head)
    pull = dict(number=7, html_url='https://github.com/org/repo/pull/7',
                base_sha=base, head_sha=head, title='change', body='')
    prompt = build_direct_review_prompt(pull, {})
    commands = re.findall(r'`(git diff (?:--name-status )?' + base + r'\.{2,3}' + head + r')`', prompt)
    assert len(commands) == 2
    for command in commands:
        assert base + '...' + head in command
        result = git(*shlex.split(command)[1:])
        assert 'source.py' in result and 'target-only.py' not in result
