"""Explicit host selection; omitted stages retain evidence and never become passes."""
from pathlib import Path

from ledger import Refused


def select_stages(manifest):
    count = len(manifest['stages'])
    value = manifest.get('stage_selection')
    if value is None:
        return tuple(range(count)), {}
    if (not isinstance(value, dict) or set(value) != {'schema', 'positions', 'retained'}
            or value['schema'] != 'selected-stages/1'
            or not isinstance(value['positions'], list) or not value['positions']
            or any(type(position) is not int or not 0 <= position < count for position in value['positions'])
            or value['positions'] != sorted(set(value['positions']))
            or len(value['positions']) >= count
            or not isinstance(value['retained'], dict)):
        raise Refused('freeze a nonempty proper stage selection and retained original evidence')
    omitted = {str(position) for position in range(count) if position not in value['positions']}
    if set(value['retained']) != omitted:
        raise Refused('every omitted stage needs explicit original pending/retained/deferred evidence')
    for row in value['retained'].values():
        if (not isinstance(row, dict) or set(row) != {'status', 'reason', 'evidence'}
                or not isinstance(row['status'], str)
                or row['status'] not in {'pending', 'retained', 'deferred'}
                or not isinstance(row['reason'], str) or not row['reason'].strip()
                or not isinstance(row['evidence'], dict) or not row['evidence']):
            raise Refused('stage omission cannot silently waive original outcomes')
        for path, sha in row['evidence'].items():
            if (not isinstance(path, str) or not Path(path).is_absolute()
                    or not isinstance(sha, str) or len(sha) != 64
                    or set(sha) - set('0123456789abcdef')
                    or manifest['bindings'].get(path) != sha):
                raise Refused('retained stage evidence must be hash-bound')
    return tuple(value['positions']), value['retained']
