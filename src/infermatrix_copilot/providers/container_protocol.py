"""Terminal worker receipts, separate from native provider event formats."""
import hashlib
import json

from .process import ProcessOutcome

MARKER = '\nINFERMATRIX_WORKER_RECEIPT_V1 '


class NativeOutputLines:
    """Project validated native stderr without the final worker control lines.

    Keep at most two stderr lines pending. Only the canonical receipt decoder's
    validated result determines what is control data. On failure retain those
    lines for diagnostics, including any malformed receipt.
    """
    def __init__(self, observer):
        self.observer, self.pending, self.emitted = observer, [], 0

    def __call__(self, channel, line):
        if channel != 'stderr':
            self.observer(channel, line)
            return
        self.pending.append(line)
        if len(self.pending) > 2:
            self.observer('stderr', self.pending.pop(0))
            self.emitted += 1

    def finish(self, decoded=None):
        if decoded is None:
            remaining = self.pending
        else:
            lines = decoded.stderr.split('\n')
            if not lines[-1]:
                lines.pop()
            remaining = self.pending[:max(0, len(lines) - self.emitted)]
        self.pending = []
        for line in remaining:
            self.observer('stderr', line)


def receipt_line(request, digests, returncode, reason):
    value = {'request_sha256': hashlib.sha256(json.dumps(request).encode()).hexdigest(),
             'streams': {name: digest.hexdigest() for name, digest in digests.items()},
             'returncode': returncode, 'reason': reason}
    return MARKER + json.dumps(value) + '\n'


def decode_receipt(outcome, request_sha256, *, required=False):
    """Verify complete streams and remove the worker's final control record.

    A missing receipt is usable only when the live controller already observed
    the exit. It cannot establish completion after loss of that observation.
    """
    stderr, marker, tail = outcome.stderr.rpartition(MARKER)
    if not marker:
        if required:
            raise ValueError('worker terminal receipt missing')
        return outcome
    try:
        value = json.loads(tail)
        if (value['request_sha256'] != request_sha256
                or type(value['returncode']) is not int
                or value['returncode'] != outcome.returncode
                or value['reason'] not in (None, 'absolute', 'idle', 'cancelled')
                or value['streams'] != {
                    'stdout': hashlib.sha256(outcome.stdout.encode()).hexdigest(),
                    'stderr': hashlib.sha256(stderr.encode()).hexdigest()}):
            raise ValueError('worker receipt differs from execution')
        return ProcessOutcome(outcome.stdout, stderr, outcome.returncode,
                              outcome.reason or value['reason'])
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError('invalid worker terminal receipt') from exc
