"""GPUs on demand from the canhazgpu scheduler (owner decision, 2026-11-25).

A campaign used to hold one `gpu run` reservation, four cards, for its whole
run, and every GPU command inherited CUDA_VISIBLE_DEVICES from it. In demand
mode nothing is held between commands: each GPU command asks the scheduler for
the cards it needs and holds them exactly as long as it runs.

    IMX_GPU_MODE=demand    opt in. Unset, or any other value: the caller already
                           holds the cards and commands inherit them.
    IMX_GPU_HELD=1         set inside a command tree that already holds its cards;
                           nothing below asks the scheduler again.
    IMX_GPU_WAIT=30m       how long a command waits in the scheduler's queue
                           before it is reported as not run.

Measured on the scheduler (2026-11-25): `gpu run` prints its progress to stdout
and its errors to stderr ("Error: wait timeout exceeded"), exits 1 when it gave
up, names the granted cards as "Reserved N GPU(s): [a b]" and sets
CUDA_VISIBLE_DEVICES to them. Its own output is kept out of test logs: on this
machine it carries a "failed to activate booking" warning, which matches the
watchdog's `failed to` pattern and would start an LLM review of every job.
"""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

MODE_ENV = "IMX_GPU_MODE"
HELD_ENV = "IMX_GPU_HELD"
WAIT_ENV = "IMX_GPU_WAIT"
DEFAULT_WAIT = "30m"
#: The scheduler stops a command this long after the runner's own timers
#: would have, so the runner's timeout stays the one that fires.
SCHEDULER_TIMEOUT_MARGIN_SEC = 900
#: The wrapper's own status for "the scheduler granted no cards in time".
NO_GPU_RC = 97

_CARDS = re.compile(r"\bcards_(\d{1,2})\b")
_WAIT_EXPIRED = "wait timeout exceeded"
_GRANTED = re.compile(r"Reserved \d+ GPU\(s\): \[([0-9 ]+)\]")
_DURATION = re.compile(r"^(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?$")

# A test run an agent typed in a shell is found per simple command: split
# where a new simple command starts (outside quotes), strip what may precede
# the command word, then look at the command word itself. One pattern over
# the whole line kept matching `pytest` as an argument (`grep -rn pytest`,
# `echo pytest`).
_LEAD = re.compile(
    r"^(?:[\s(]+|\b(?:then|do|else|nohup|time|exec)\b\s+"
    r"|\btimeout\s+\S+\s+|\benv\s+(?:-\S+\s+)*|\bsudo\s+(?:-\S+\s+)*"
    r"|\buv\s+run\s+(?:--\S+\s+)*"
    r"|[A-Za-z_][A-Za-z0-9_]*=(?:'[^']*'|\"[^\"]*\"|[^\s;&|()]+)\s+)+")
_EXE = re.compile(r"(?:\S*/)?(?P<exe>pytest|python[0-9.]*)(?=[\s)&|;]|$)")
_PYTEST_MODULE = re.compile(r"\s+-m\s+pytest(?=[\s)&|;]|$)")
# Python running CUDA code. The token alone is not enough: agents grep and
# read this text far more often than they run it.
_CUDA_CODE = re.compile(
    r"torch\.cuda\b|cuda\.is_available\(|device\s*=\s*['\"]cuda|--device[=\s]+['\"]?cuda")
_PYTHON_STDIN = re.compile(r"\bpython[0-9.]*\s+-\s*(?:<<|<)")
# Commands that already handle the cards themselves.
_SELF_SERVED = re.compile(r"\bgpu\s+run\b|\bimx-omni-pytest\b")
# Outside quotes, each of these characters starts a new simple command.
_COMMAND_START = ";|&()\n"


@dataclass(frozen=True)
class GpuDemand:
    """Whether to ask the scheduler per command, and how long to wait for it."""
    enabled: bool = False
    wait: str = DEFAULT_WAIT

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "GpuDemand":
        env = os.environ if env is None else env
        return cls(enabled=env.get(MODE_ENV, "") == "demand"
                   and not env.get(HELD_ENV),
                   wait=env.get(WAIT_ENV) or DEFAULT_WAIT)


def duration_seconds(text: str, default: float = 0.0) -> float:
    """`30m`, `2h`, `90s`, `1h30m` -> seconds; `default` for anything else."""
    m = _DURATION.match((text or "").strip())
    if not m or not any(m.groups()):
        return default
    h, minute, s = (float(g) if g else 0.0 for g in m.groups())
    return h * 3600 + minute * 60 + s


def command_gpus(command: str, minimum: int = 0) -> int:
    """Cards a test command needs: the largest `cards_N` pytest marker it
    selects (every job of the manifest says `hw: any`), else `minimum`."""
    found = [int(n) for n in _CARDS.findall(command or "") if 1 <= int(n) <= 16]
    return max([minimum, *found])


def shell_needs_gpu(command: str) -> bool:
    """Whether a shell command an agent typed starts a test run, or Python
    that touches CUDA, without the gated wrapper (which asks for its cards
    itself). A command that already names `gpu run` or the wrapper is left
    alone."""
    if _SELF_SERVED.search(command or ""):
        return False
    # a script read from stdin: its text is the rest of the command
    if _PYTHON_STDIN.search(command) and _CUDA_CODE.search(command):
        return True
    return any(_simple_command_needs_gpu(part)
               for part in _simple_commands(command))


def _simple_commands(command: str) -> list[str]:
    """The simple commands of a shell line: split where the next one starts,
    at a list separator or a parenthesis that is not inside quotes."""
    out: list[str] = []
    buf: list[str] = []
    quote = ""
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < len(command):
                buf.append(command[i:i + 2])
                i += 2
                continue
            if ch == quote:
                quote = ""
            buf.append(ch)
        elif ch in "'\"":
            quote = ch
            buf.append(ch)
        elif ch in _COMMAND_START:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    out.append("".join(buf))
    return out


def _simple_command_needs_gpu(segment: str) -> bool:
    """`pytest` or `python -m pytest` as the command word, or `python` with
    CUDA code in the same simple command."""
    body = _LEAD.sub("", segment, count=1)
    found = _EXE.match(body)
    if not found:
        return False
    rest = body[found.end():]
    if found["exe"] == "pytest" or _PYTEST_MODULE.match(rest):
        return True
    return bool(_CUDA_CODE.search(body))


def scheduler_wrap(command: str, *, gpus: int, timeout_sec: float, wait: str,
                   note: str, side_log: Path) -> str:
    """A shell program (for `bash --norc -c`) that runs `command` under the
    scheduler. The command's stdout and stderr stay where the caller put them
    and the scheduler's own output goes to `side_log`. The program exits with
    the command's status once the cards were granted, with NO_GPU_RC when the
    wait expired, and with the scheduler's status for its other failures."""
    ask = shlex.join([
        "gpu", "run", "--gpus", str(gpus),
        "--timeout", f"{int(timeout_sec)}s",
        # the scheduler releases an idle card after 30 min by default; a test
        # may take longer before its first kernel (a model download), and the
        # timeout already bounds this reservation
        "--idle-timeout", f"{int(timeout_sec)}s",
        "--wait", wait, "--note", note, "--",
        "bash", "--norc", "-c", "exec >&3 2>&4\n" + command])
    side = shlex.quote(str(side_log))
    return "\n".join([
        "exec 3>&1 4>&2",
        f"{ask} > {side} 2>&1",
        "rc=$?",
        f'if [ "$rc" -ne 0 ] && grep -q {shlex.quote(_WAIT_EXPIRED)} {side}; '
        f"then exit {NO_GPU_RC}; fi",
        'exit "$rc"',
    ])


def granted_gpus(side_log: Path) -> list[str]:
    """The physical cards the scheduler named for one command."""
    try:
        found = _GRANTED.search(Path(side_log).read_text(errors="replace"))
    except OSError:
        return []
    return found.group(1).split() if found else []


def scheduler_error(side_log: Path) -> str:
    """The scheduler's own failure text of one command, for a log the command
    failed anyway; "" when it has none."""
    try:
        text = Path(side_log).read_text(errors="replace")
    except OSError:
        return ""
    hit = text.find("Error:")
    return text[hit:hit + 300].strip() if hit >= 0 else ""


INHERITED_DEFAULT = "0,1"
ON_DEMAND_TEXT = "set per command by the scheduler (none held by this session)"


def session_devices(env: Mapping[str, str] | None = None) -> str:
    """The cards a session may hand to its commands: CUDA_VISIBLE_DEVICES
    when it holds them, else the historical default `0,1`. A session whose
    commands each ask the scheduler holds none and says so with ""."""
    env = os.environ if env is None else env
    if "CUDA_VISIBLE_DEVICES" in env:
        return env["CUDA_VISIBLE_DEVICES"]
    return "" if GpuDemand.from_env(env).enabled else INHERITED_DEFAULT


def prompt_devices(devices: str, env: Mapping[str, str] | None = None) -> str:
    """What a prompt names as the session's cards. A session whose commands
    each ask the scheduler holds none: the prompt says so instead of offering
    a selection the agent would then use on cards it does not hold."""
    if devices or not GpuDemand.from_env(env).enabled:
        return devices
    return ON_DEMAND_TEXT
