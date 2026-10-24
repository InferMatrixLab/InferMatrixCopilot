"""Validate canonical native process diagnostics without dispatch or settlement."""
import hashlib
from ..trace_store import redact


def native_process_result(store, record, *, entry=None):
    """Read a terminal original receipt; live bytes must match its redacted blobs."""
    store.validate_native_attempt(record, allow_failed=True)
    result = {key: record['result']['native_exit'][key] for key in ('returncode', 'reason')}
    for channel in ('stdout', 'stderr'):
        archived = store.blob(record['outputs']['native_' + channel])
        if entry is not None:
            original = entry['native_process'][channel]
            if redact(original, store._environ) != archived:
                raise ValueError('live native diagnostics differ from their archived receipt')
            result[channel] = original
        else:
            result[channel] = archived
        if hashlib.sha256(result[channel].encode()).hexdigest() != record['result']['native_exit'][channel + '_sha256']:
            raise ValueError('original native bytes require their retained process receipt')
    if entry is not None and any(entry['native_process'][key] != result[key] for key in ('returncode', 'reason')):
        raise ValueError('native exit differs from its archived receipt')
    return result
