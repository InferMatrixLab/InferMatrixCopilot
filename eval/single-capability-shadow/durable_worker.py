"""Evaluation policy injection around the standard installed worker entry point.

No workflow, provider dispatch, retry or receipt implementation lives here.
"""
import argparse
import hashlib
import inspect
import json
import os
from pathlib import Path
import runpy
import sys
import time
from uuid import uuid4


def runtime_options(shim):
    from arm_runtime import load_arm_config, supports_shared_execution
    from ledger import Refused
    config, value = load_arm_config(shim)
    if value['plan'].get('worker_execution') != 'durable':
        raise Refused('freeze durable worker execution in the evaluation plan')
    if value['plan'].get('call_identity') != 'agent-step/1':
        raise Refused('freeze logical agent call identity in the evaluation plan')
    if not supports_shared_execution():
        return {}  # original SDK accounts through its existing native CLI guard
    from infermatrix_copilot.sdk.v1 import StrictRuntime
    if ('worker_prefix' not in inspect.signature(StrictRuntime).parameters
            or getattr(StrictRuntime, 'worker_call_identity', None) != 'agent-step/1'):
        raise Refused('current evaluation SDK lacks trusted worker composition')
    return {'worker_prefix': (sys.executable, '-I', '-B', str(Path(__file__).resolve()),
                              '--config', str(config), '--shim', str(Path(shim).resolve()), '--')}


def wait_result(runtime, run_id, timeout):
    from ledger import Refused
    deadline = time.monotonic() + timeout
    while True:
        result = runtime.get_result(run_id)
        if result.terminal:
            return result
        if time.monotonic() >= deadline:
            raise Refused('original durable worker remains unresolved; reconcile without replacement')
        time.sleep(.1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--shim', type=Path, required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if (len(command) != 4 or command[:2] != ['-m', 'infermatrix_copilot']
            or command[2] not in ('--execute-reserved', '--execute-strict-reserved')
            or Path(command[3]).name != command[3]):
        parser.error('expected the standard reserved workflow command')
    # Import evaluation helpers only. Product imports remain installed under -I.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    os.environ['SHADOW_ARM_CONFIG'] = str(args.config.resolve(strict=True))
    from arm_runtime import load_arm_config, sdk_runtime
    from infermatrix_copilot.persistence import immutable_write_bytes
    from infermatrix_copilot.config import Settings
    import infermatrix_copilot
    _, config = load_arm_config(args.shim)
    runtime_options(args.shim)  # refuses an unfrozen mode or unsupported SDK
    if 'site-packages' not in infermatrix_copilot.__file__:
        raise RuntimeError('durable evaluation requires an installed product')
    run = Path.cwd() / command[3]
    effective = Settings(_env_file=None)
    startup_id = uuid4().hex
    proof = (json.dumps({
        'schema': 'shadow-durable-worker/1', 'startup_id': startup_id,
        'pid': os.getpid(), 'parent_pid': os.getppid(),
        'arm': config['arm'], 'case_id': config['case_id'],
        'config_sha256': hashlib.sha256(args.config.read_bytes()).hexdigest(),
        'package_file': infermatrix_copilot.__file__, 'entry': str(Path(__file__).resolve()),
        'workflow_settings': {key: getattr(effective, key) for key in
            ('ensemble_parallel', 'review_verify_concurrency', 'review_lens_backends')}},
        sort_keys=True) + '\n').encode()
    entries = run / 'evaluation-workers'
    entries.mkdir(mode=0o700, exist_ok=True)
    immutable_write_bytes(entries / f'{startup_id}.json', proof)
    try:
        # Retain the first-entry compatibility record. It is provenance, not a
        # second execution claim: the SDK owns queued adoption and run locking.
        immutable_write_bytes(run / 'evaluation-worker.json', proof)
    except FileExistsError:
        pass
    with sdk_runtime({'_env_file': None}, shim=args.shim):
        sys.argv = command[1:]
        runpy.run_module(command[1], run_name='__main__', alter_sys=True)


if __name__ == '__main__':
    main()
