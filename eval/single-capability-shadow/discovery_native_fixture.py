"""Offline native response policy for full-stage execution conformance only."""
import json
import sys


RELATIONSHIPS = {
    "engine-step": ("new", ""),
    "alias-step": ("alias", "engine-step"),
    "supplement-step": ("implementation_supplement", "engine-step"),
    "child-step": ("subcapability", "engine-step"),
    "shared-step": ("shared_component", "engine-step"),
    "obsolete-step": ("outdated", ""),
}


def response(prompt, *, relationships=False, boundary_exclusion=False):
    if relationships and boundary_exclusion:
        raise ValueError("freeze one discovery fixture variant")
    packet = json.loads(prompt.rsplit('<untrusted_data>\n', 1)[1].split('</untrusted_data>', 1)[0])
    if 'candidates' in packet:
        decisions = {row['id']: {'supported': 'yes',
            'relation': RELATIONSHIPS[row['id']][0] if relationships else 'new',
            'related_id': RELATIONSHIPS[row['id']][1] if relationships else '',
            'reason': 'The cited implementation exposes the engine step.'}
            for row in packet['candidates']}
        if boundary_exclusion and 'outside-step' in decisions and packet.get('catalog_boundary_audit'):
            decisions['outside-step'].update(supported='no', relation='unknown',
                reason='GPU scheduling is outside the contract demonstrated by the pinned engine source.')
        return {'decisions': decisions}
    assert 'round' in packet, 'unplanned offline model request'
    offered = next((row for row in packet.get('files', []) if row['path'] == 'pkg/core.py'), None)
    rows = []
    if offered and packet['round'] != 'doc':
        rows.append({'id': 'engine-step', 'title': 'Engine step', 'owner': 'core',
            'description': 'Advance the engine through its public step contract.',
            'relation': 'new', 'related_id': '', 'aliases': [],
            'evidence': [{'path': offered['path'], 'start': offered['start'], 'end': offered['end']}]})
    if relationships and rows:
        original = rows[0]
        rows = [{**original, 'id': key, 'title': key.replace('-', ' ').title(),
                 'relation': relation, 'related_id': target}
                for key, (relation, target) in RELATIONSHIPS.items()]
    if boundary_exclusion and rows:
        rows.append({**rows[0], 'id': 'outside-step', 'title': 'GPU scheduling',
                     'description': 'Schedule the engine step on a GPU.'})
    return {'candidates': rows}


if __name__ == '__main__':
    if sys.argv[1:] == ['login', 'status']:
        print('Logged in using ChatGPT')
        raise SystemExit(0)
    assert sys.argv[1] == 'exec' and '--json' in sys.argv
    model = sys.argv[sys.argv.index('-m' if '-m' in sys.argv else '--model') + 1]
    data = response(sys.stdin.read())
    print(json.dumps({'type': 'item.completed', 'model': model,
        'item': {'type': 'agent_message', 'text': json.dumps(data)}}))
    print(json.dumps({'type': 'turn.completed', 'model': model,
                     'usage': {'input_tokens': 7, 'output_tokens': 2}}))
