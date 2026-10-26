"""Deterministic external execution boundary for ledger/coordinator unit tests.

Actual sandbox/native/archive integration is covered separately. This helper is
never imported by the production guard or workflow drivers.
"""
from dataclasses import asdict
from pathlib import Path
import shutil
import sys

import codex_guard
from infermatrix_copilot.providers.codex import CodexTransport
from infermatrix_copilot.providers.process import run_process


def shared_plan(root, binary):
    return {'schema': 'shared-sandbox/1', 'profile': {
        'kind': 'docker', 'docker': '/bin/true', 'socket': str(Path(root) / 'unit-daemon'),
        'image': 'sha256:' + 'a' * 64, 'native_cli': str(binary)},
        'source_roots_by_arm': {arm: [str(root)] for arm in ('baseline', 'candidate')},
        'knowledge_by_arm': {arm: str(root) for arm in ('baseline', 'candidate')}}


def simulate_shared(*, plan, arm, args, cwd, prompt, bridge, schema, root, native_observer=None, hidden_paths=()):
    binary = plan['shared_execution']['profile']['native_cli']
    result = run_process([binary, *args], cwd=cwd, env={'PATH': '/usr/bin:/bin'}, input=prompt,
                         deadline=__import__('time').monotonic() + plan.get('native_timeout_s', 180))
    adapter = CodexTransport(None)
    if native_observer:
        for event in adapter.readonly_events(result.stdout, validate=False):
            native_observer({'type': 'native.codex.event', 'payload': event})
    return {**adapter.process_receipt(asdict(result)), 'cost_usd': None}


def bootstrap_guard(directory):
    """Unit-only launcher; product imports follow the parent's loaded package."""
    import infermatrix_copilot
    directory = Path(directory);directory.mkdir(exist_ok=True)
    script = directory / 'guard-unit.py'
    product = str(Path(infermatrix_copilot.__file__).resolve().parent.parent)
    driver = str(Path(codex_guard.__file__).resolve().parent)
    script.write_text('import sys\n' + 'sys.path[:0]=' + repr([driver, product]) + '\n'
        'import codex_guard\nfrom shadow_test_support import simulate_shared\n'
        'codex_guard.execute_shared=simulate_shared\nraise SystemExit(codex_guard.main())\n')
    shutil.copy2(Path(__file__).with_name('ledger.py'), directory / 'ledger.py')
    return script
