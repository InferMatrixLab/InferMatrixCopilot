"""Shared run artifact locations; no workflow or provider business policy."""
from pathlib import Path
import hashlib

NATIVE_SESSIONS_DIR = 'native-sessions'


def native_session_dir(run_dir, step_name):
    return Path(run_dir) / NATIVE_SESSIONS_DIR / hashlib.sha256(step_name.encode()).hexdigest()


def model_call_trace_roots(run_dir, configured_root=None):
    """Canonical request stores used by a run, without creating any stores."""
    roots = set()
    if configured_root:
        roots.add(Path(configured_root).expanduser().resolve())
    roots.update(p.resolve() for p in (Path(run_dir) / NATIVE_SESSIONS_DIR).glob('*/traces'))
    return tuple(sorted(root for root in roots if (root / 'records').is_dir()))
