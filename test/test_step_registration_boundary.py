"""Importing the registry facade must not load workflow effect modules."""

import os
import subprocess
import sys
from pathlib import Path


def test_builtin_steps_load_only_when_registry_is_assembled():
    source = Path(__file__).resolve().parents[1] / "src"
    code = """
import sys
from infermatrix_copilot.engine.steps import register_builtin_steps
from infermatrix_copilot.engine.registry import StepRegistry

prefix = 'infermatrix_copilot.engine.steps.'
for name in ('workspace', 'review', 'report', 'pr', 'issue', 'profile',
             'rebase_v3', 'rebase_knowledge'):
    assert prefix + name not in sys.modules, name

registry = register_builtin_steps(StepRegistry())
assert {'workspace.guard_clean', 'review.patch_gate', 'pr.fetch_diff'} <= set(registry.names())
for name in ('workspace', 'review', 'report', 'pr', 'issue', 'profile',
             'rebase_v3', 'rebase_knowledge'):
    assert prefix + name in sys.modules, name
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONPATH": str(source)},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_run_reservation_import_does_not_load_orchestrator_or_transports():
    source = Path(__file__).resolve().parents[1] / "src"
    code = """
import sys
from infermatrix_copilot.app.reservation import RunReservation
for name in ('infermatrix_copilot.app.core',
             'infermatrix_copilot.app.run_service',
             'infermatrix_copilot.cli.entry',
             'infermatrix_copilot.mcp_server'):
    assert name not in sys.modules, name
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONPATH": str(source)},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_execution_kernel_does_not_import_domain_implementations():
    source = Path(__file__).resolve().parents[1] / "src"
    code = """
import sys
from infermatrix_copilot.engine.executor import Executor
assert not any(name.startswith(('infermatrix_copilot.improve',
                               'infermatrix_copilot.kb_service',
                               'infermatrix_copilot.knowledge_service'))
               for name in sys.modules)
"""
    result = subprocess.run([sys.executable, "-c", code],
                            env={**os.environ, "PYTHONPATH": str(source)},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
