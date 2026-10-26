"""Host completion evidence; dispatch and process ownership remain canonical.

A quality-workflow receipt proves the frozen phases completed. It does not
supply an unobserved exit code for their outer wrapper or adjudicate quality.
"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path

from infermatrix_copilot.persistence import immutable_write_bytes
from infermatrix_copilot.trace_store import file_lock
from ledger import Refused


def hashed(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@contextmanager
def execution_lock(path):
    with file_lock(path, blocking=False) as held:
        if not held:
            raise Refused('original stage or coordinator is still active; reconcile without dispatch')
        yield


def _receipt(directory, phases_sha256, packet, config):
    directory = Path(directory).resolve()
    files = {}
    for phase in packet['phases']:
        root = directory / phase['name']
        for name in ('phase-process.json', 'stdout.txt', 'stderr.txt'):
            path = root / name
            if not path.resolve(strict=True).is_relative_to(directory):
                raise Refused('quality phase output escapes the original stage')
            files[str(path.relative_to(directory))] = hashed(path)
        process = json.loads((root / 'phase-process.json').read_text())
        if (not isinstance(process, dict) or set(process) != {'returncode', 'reason'} or type(process['returncode']) is not int
                or process['returncode'] != 0 or process['reason'] is not None):
            raise Refused('quality phase did not complete normally')
    return {'schema': 'quality-workflow-completion/1', 'phases_sha256': phases_sha256,
            'config_sha256': hashed(config), 'files': files}


def write_completion(directory, phases_sha256, packet, config):
    receipt = _receipt(directory, phases_sha256, packet, config)
    immutable_write_bytes(Path(directory) / 'quality-completion.json',
                          json.dumps(receipt, sort_keys=True).encode())


def quality_completion_proof(output, manifest, stage, position):
    from quality_stage_arm import load_phases
    directory = Path(output) / f'{position:02d}-{stage["arm"]}'
    command = stage['command']
    if Path(command[2]).name != 'quality_stage_arm.py':
        raise Refused('missing original terminal stage receipt')
    try:
        path = Path(command[command.index('--phases') + 1])
        expected = command[command.index('--sha256') + 1]
        packet = load_phases(path, expected)
        if packet['schema'] != 'quality-stage/2':
            raise Refused('legacy quality stage has no durable completion protocol')
        config = directory / 'guard-config.json'
        expected_config = {'plan': manifest['plan'], 'case_id': stage['case_id'],
            'arm': stage['arm'], 'binary': manifest['binary'],
            'ledger_path': str(Path(output) / 'calls.sqlite')}
        if json.loads(config.read_text()) != expected_config:
            raise Refused('completion config differs from the original stage')
        if not (directory / 'quality-stage.lock').is_file():
            raise Refused('quality stage has no original execution lock')
        with execution_lock(directory / 'quality-stage.lock'):
            receipt = directory / 'quality-completion.json'
            if json.loads(receipt.read_text()) != _receipt(directory, expected, packet, config):
                raise Refused('quality completion evidence changed')
            return {'source': 'quality_workflow', 'receipt_sha256': hashed(receipt)}
    except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        raise Refused('missing or invalid original quality completion evidence') from exc
