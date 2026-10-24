"""Deliver a large argument inside Node, without shell expansion or model tools."""
import hashlib
import json
from pathlib import Path
import uuid

HOOK = r'''const fs = require('node:fs');
const crypto = require('node:crypto');
const data = JSON.parse(fs.readFileSync(process.env.INFERMATRIX_NODE_ARGUMENT, 'utf8'));
delete process.env.INFERMATRIX_NODE_ARGUMENT;
delete process.env.NODE_OPTIONS;
if (!data || !['probe', 'run'].includes(data.operation) ||
    typeof data.value !== 'string' || typeof data.sentinel !== 'string' ||
    typeof data.prefix !== 'string') throw new Error('invalid native argument envelope');
if (data.operation === 'probe') process.argv.push(data.prefix + data.sentinel);
const index = process.argv.indexOf(data.prefix + data.sentinel);
if (index < 0) throw new Error('native argument placeholder is unavailable');
process.argv[index] = data.prefix + data.value;
const proof = {schema: 'node-argument/1', sha256: crypto.createHash('sha256').update(data.value, 'utf8').digest('hex')};
if (data.operation === 'probe') {
  fs.writeSync(1, JSON.stringify(proof) + '\n');
  process.exit(0);
}
fs.writeSync(1, JSON.stringify({type: 'native.argument.binding', payload: proof}) + '\n');
'''


class NodeArgument:
    def __init__(self, directory, value, *, prefix):
        self.path = Path(directory) / 'native-argument.json'
        hook = Path(directory) / 'native-argument.cjs'
        self.value = {'operation': 'probe', 'prefix': prefix, 'sentinel': uuid.uuid4().hex, 'value': value}
        self.proof = {'schema': 'node-argument/1', 'sha256': hashlib.sha256(value.encode()).hexdigest()}
        hook.write_text(HOOK, encoding='utf-8');hook.chmod(0o600)
        self._write()
        self.env = {'NODE_OPTIONS': '--require=' + json.dumps(str(hook)),
                    'INFERMATRIX_NODE_ARGUMENT': str(self.path)}
        self.argument = prefix + self.value['sentinel']

    def _write(self):
        self.path.write_text(json.dumps(self.value, ensure_ascii=False), encoding='utf-8')
        self.path.chmod(0o600)

    def accept_probe(self, stdout, code, timed_out):
        try:
            if timed_out or code or json.loads(stdout) != self.proof:
                raise ValueError('missing native argument proof')
        except (TypeError, ValueError):
            raise ValueError('native CLI does not support the isolated Node argument envelope') from None
        self.value['operation'] = 'run'
        self._write()

    def validate_events(self, events):
        proofs = [event.get('payload') for event in events if event.get('type') == 'native.argument.binding']
        if proofs != [self.proof]:
            raise ValueError('native argument differs from its original input')
