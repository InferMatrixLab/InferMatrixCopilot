"""One immutable native intent/result gate, independent of workflow policy."""
import hashlib
import json
from pathlib import Path

from ..persistence import immutable_write_bytes
from ..trace_store import NativeCompletionRecorder, TraceStore, file_lock
from ..sandbox.base import SandboxPending


def canonical(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False) + '\n').encode()


def run_original_native(*, root, name, identity, dispatch, project):
    """Recover one original receipt or dispatch once; uncertainty never rearms it.

    Callers own request/result policy. This gate owns the lock, original intent,
    native attempt identity and publication of a derived result. The canonical
    native workflow owns the actual call, archive and domain settlement hooks.
    """
    root = Path(root)
    if not root.is_absolute() or root.is_symlink():
        raise ValueError('native execution requires an absolute original state directory')
    root.mkdir(parents=True, exist_ok=True)
    raw = canonical(identity)
    with file_lock(root / (name + '.lock'), blocking=False) as held:
        if not held:
            raise SandboxPending('original ' + name + ' execution is still running', prior_execution=True)
        intent, result_path = (root / (name + suffix) for suffix in ('-intent.json', '-result.json'))
        marker = root / (name + '-attempt.json')
        store = TraceStore(root / 'traces')

        def publish(record):
            store.validate_native_attempt(record, allow_failed=True)
            if record.get('result', {}).get('native_outcome_pending') is True:
                raise SandboxPending('original ' + name + ' work remains unresolved', prior_execution=True)
            if result_path.is_symlink():
                raise ValueError('native result cannot be a symlink')
            saved = None
            if result_path.exists():
                previous = json.loads(result_path.read_bytes())
                if (set(previous) != {'request_sha256', 'result'}
                        or previous['request_sha256'] != hashlib.sha256(raw).hexdigest()):
                    raise ValueError('native result differs from its original request')
                saved = previous['result']
            result = project(record, store, saved)
            immutable_write_bytes(result_path, canonical({'request_sha256': hashlib.sha256(raw).hexdigest(),
                                                         'result': result}), exist_ok=True)
            return result

        if intent.exists() or intent.is_symlink():
            if intent.is_symlink() or intent.read_bytes() != raw:
                raise SandboxPending(name + ' request differs from original execution', prior_execution=True)
            try:
                if marker.is_symlink():
                    raise ValueError('changed original attempt marker')
                attempt_id = json.loads(marker.read_text())['native_attempt_id']
                records = [record for record, _ in store.iter_records(kind='model_call')
                           if record.get('result', {}).get('native_attempt_id') == attempt_id]
                if len(records) != 1:
                    raise ValueError('missing or ambiguous original native receipt')
                return publish(records[0])
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise SandboxPending('original ' + name + ' outcome requires reconciliation', prior_execution=True) from exc
        if result_path.exists() or result_path.is_symlink() or marker.exists() or marker.is_symlink():
            raise SandboxPending(name + ' result has no original intent', prior_execution=True)
        immutable_write_bytes(intent, raw)
        recorder = NativeCompletionRecorder(store, strict=True)

        class Recorder:
            entry = None
            receipt = None
            def durable(self):
                return self

            def begin_call(self, entry):
                archive = recorder.begin_call(entry)
                immutable_write_bytes(marker, canonical({'native_attempt_id': archive.id}))
                return archive

            def __call__(self, entry):
                self.entry = entry
                self.receipt = recorder(entry)
                return self.receipt

        return publish(dispatch(Recorder(), store))
