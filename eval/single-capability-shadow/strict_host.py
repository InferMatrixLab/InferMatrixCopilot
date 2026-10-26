"""One frozen read-only forge adapter for parent and standard durable child."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import runpy
import shlex
import sys


def frozen_gh(row, host, *, context_version=1):
    if type(context_version) is not int or context_version not in (1, 2):
        raise ValueError('unsupported frozen forge context version')
    # Legacy PR/repository reads did not require a discussion packet.
    context = host['business_context'] if context_version == 2 else {}
    discussions = context['pr_review_context'] if context_version == 2 else []
    if context_version == 2 and any(item.get('kind') not in {
        'issue_comment', 'inline_comment', 'review_summary'
    } for item in discussions):
        raise ValueError('unsupported frozen discussion kind')

    def source(kind):
        return [item for item in discussions if item.get('kind') == kind]

    def gh(command, cwd=None):
        if command[:2] == ['pr', 'view']:
            assert command[2] == str(row['pr'])
            if 'state,isDraft,mergeable,mergeStateStatus' in command:
                return 1, 'PR gate state not independently frozen'
            if context_version == 2:
                return 0, json.dumps({'baseRefName': context['pull']['base_ref'],
                    'headRefOid': row['expected_head_sha'], 'title': row['title'], 'body': row['body'],
                    'labels': [{'name': value} for value in context['pull'].get('labels', [])],
                    'headRefName': '',
                    'comments': [{'author': {'login': item.get('author')}, 'body': item['body']}
                                 for item in source('issue_comment')],
                    'reviews': [{'author': {'login': item.get('author')}, 'body': item['body'],
                                 'state': item.get('state')}
                                for item in source('review_summary')]})
            return 0, json.dumps({'baseRefName': 'main', 'headRefOid': row['expected_head_sha'],
                'title': row['title'], 'body': row['body'], 'labels': [],
                'headRefName': 'shadow', 'comments': [], 'reviews': []})
        if command[:2] == ['repo', 'view']:
            return 0, json.dumps({'nameWithOwner': row['repository']})
        if command[:2] == ['api', f"repos/{row['repository']}/pulls/{row['pr']}/comments"]:
            if context_version == 2:
                # The original renderer requests this exact jq projection.
                # An original_line cannot be promoted to a current-head line.
                return 0, '\n'.join(json.dumps({'user': item.get('author'),
                    'path': item.get('path'), 'line': item.get('line'), 'body': item['body']})
                    for item in source('inline_comment'))
            return 0, '\n'.join(json.dumps({'user': 'frozen-shadow-fixture', 'path': item.get('path'),
                'line': item.get('line'), 'body': item['body']})
                for item in host['business_context']['pr_review_context'])
        if command[:2] == ['issue', 'view']:
            return 1, 'No separately frozen issue response; use only supplied PR text'
        raise ValueError('unfrozen forge request')
    return gh


@contextmanager
def forge_cli(root, descriptor):
    root = Path(root) / 'frozen-forge'
    root.mkdir(mode=0o700)
    path = root / 'request.json'
    # This adapter also runs under the original installed SDK, which predates
    # the persistence module. Private fixture files are exclusive creations.
    with path.open('x') as stream:
        stream.write(json.dumps(descriptor, sort_keys=True) + '\n')
    script = root / 'gh'
    command = [sys.executable, '-I', '-B', str(Path(__file__).resolve()), str(path)]
    with script.open('x') as stream:
        stream.write('#!/bin/sh\nexec ' + shlex.join(command) + ' "$@"\n')
    script.chmod(0o700)
    previous = {key: os.environ.get(key) for key in ('PATH', 'SHADOW_FORGE_REFUSAL')}
    os.environ['PATH'] = str(root) + os.pathsep + (previous['PATH'] or os.defpath)
    os.environ['SHADOW_FORGE_REFUSAL'] = str(root / 'unfrozen.json')
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def require_frozen_reads():
    marker = os.environ.get('SHADOW_FORGE_REFUSAL')
    if marker and (Path(marker).exists() or Path(marker).is_symlink()):
        from ledger import Refused
        raise Refused('unfrozen forge request refuses further model dispatch')


def main():
    descriptor = json.loads(Path(sys.argv[1]).read_bytes())
    if descriptor['kind'] == 'fixture':
        path = Path(descriptor['fixture'])
        if hashlib.sha256(path.read_bytes()).hexdigest() != descriptor['sha256']:
            raise ValueError('frozen forge fixture changed')
        gh = runpy.run_path(str(path))['_fake_gh'](descriptor['head'])
    elif descriptor['kind'] == 'case':
        gh = frozen_gh(descriptor['row'], descriptor['host'],
                       context_version=descriptor.get('context_version', 1))
    else:
        raise ValueError('unknown frozen forge adapter')
    try:
        code, text = gh(sys.argv[2:])
    except (ValueError, AssertionError):
        marker = Path(sys.argv[1]).parent / 'unfrozen.json'
        try:
            with marker.open('x') as stream:
                stream.write('{"schema":"shadow-forge-refusal/1"}\n')
        except FileExistsError:
            pass
        return 78
    print(text)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
