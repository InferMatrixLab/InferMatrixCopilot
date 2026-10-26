"""Read the selected execution host's native executable without running it."""
import hashlib
import json
import os
from pathlib import Path
import sys
import time


def probe(profile, *, timeout_s=30):
    result = profile.execute(
        ['/usr/local/bin/python', '-I', '-m', __name__], cwd='/tmp/work', env={},
        input=json.dumps({'native_cli': profile.native_cli}),
        deadline=time.monotonic() + timeout_s)
    if result.reason is not None or result.returncode:
        raise ValueError('native binary identity control failed')
    value = json.loads(result.stdout)
    if (not isinstance(value, dict) or set(value) != {'schema', 'sha256'}
            or value['schema'] != 'native-identity/1' or not isinstance(value['sha256'], str)
            or len(value['sha256']) != 64 or any(c not in '0123456789abcdef' for c in value['sha256'])):
        raise ValueError('invalid native binary identity')
    return {**value, 'context': profile.sandbox.context()}


def main():
    value = json.load(sys.stdin)
    if (not isinstance(value, dict) or set(value) != {'native_cli'}
            or not isinstance(value['native_cli'], str) or not Path(value['native_cli']).is_absolute()):
        raise ValueError('invalid native identity request')
    binary = Path(value['native_cli'])
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError('native binary is unavailable')
    print(json.dumps({'schema': 'native-identity/1', 'sha256': hashlib.sha256(binary.read_bytes()).hexdigest()}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
