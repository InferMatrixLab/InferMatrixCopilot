"""GPUs on demand from the canhazgpu scheduler (owner decision, 2026-11-25).

A campaign used to hold one `gpu run` reservation, four cards, for its whole
run, and every GPU command inherited CUDA_VISIBLE_DEVICES from it. In demand
mode nothing is held between commands: each GPU command asks the scheduler for
the cards it needs and holds them exactly as long as it runs.

    IMX_GPU_MODE=demand    opt in. Unset, or any other value: the caller already
                           holds the cards and commands inherit them.
    IMX_GPU_HELD=1         set inside a command tree that already holds its cards;
                           nothing below asks the scheduler again.
    IMX_GPU_WAIT=30m       bounds the time a command waits in the scheduler's
                           queue before it is reported as not run. Unset, it
                           waits until the cards are granted (owner decision,
                           2026-11-26: nothing gives up on cards).

How many cards a command asks for is what it needs (the same decision: never
which cards, only how many): `command_gpus`. `IMX_GPU_MAX` caps it; a test that
declares more skips by its own guard, as it did whenever fewer cards were visible.

Measured on the scheduler (2026-11-25): `gpu run` prints its progress to stdout
and its errors to stderr ("Error: wait timeout exceeded"), exits 1 when it gave
up, names the granted cards as "Reserved N GPU(s): [a b]" and sets
CUDA_VISIBLE_DEVICES to them. Its own output is kept out of test logs: on this
machine it carries a "failed to activate booking" warning, which matches the
watchdog's `failed to` pattern and would start an LLM review of every job.
Its idle release takes one unit and at most three hours (`idle_timeout`); its
command timeout takes any duration in seconds.
"""
from __future__ import annotations

import ast
import os
import re
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

MODE_ENV = "IMX_GPU_MODE"
HELD_ENV = "IMX_GPU_HELD"
WAIT_ENV = "IMX_GPU_WAIT"
MAX_ENV = "IMX_GPU_MAX"
#: No bound: a command waits in the scheduler's queue until it gets its cards.
DEFAULT_WAIT = ""
#: The scheduler stops a command this long after the runner's own timers
#: would have, so the runner's timeout stays the one that fires.
SCHEDULER_TIMEOUT_MARGIN_SEC = 900
#: The longest idle release the scheduler accepts; it refuses the request
#: outright above this ("idle timeout cannot exceed 3h").
IDLE_MAX_SEC = 3 * 3600
#: The wrapper's own status for "the scheduler granted no cards in time".
NO_GPU_RC = 97
#: The run level a test command selects, and the levels there are. A test of
#: another level skips by its own guard when the cards are not there.
RUN_LEVEL_OPTION = "--run-level"
RUN_LEVELS = ("core_model", "advanced_model", "full_model")
DEFAULT_RUN_LEVEL = "core_model"
#: A declared need is read from the test files' source; agents edit tests while
#: they work, so it is kept only briefly.
_NEED_TTL_SEC = 300.0

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
# Commands that already handle the cards themselves: the scheduler, and the
# repository's module test wrapper (named per repository by rebase_engine).
_SELF_SERVED = re.compile(r"\bgpu\s+run\b|\bimx-[a-z0-9]+-pytest\b")
# Outside quotes, each of these characters starts a new simple command.
_COMMAND_START = ";|&()\n"


@dataclass(frozen=True)
class GpuDemand:
    """Whether to ask the scheduler per command, how long to wait for it, and
    the most cards one command asks for."""
    enabled: bool = False
    wait: str = DEFAULT_WAIT
    maximum: int | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "GpuDemand":
        env = os.environ if env is None else env
        try:
            maximum = int(env.get(MAX_ENV, "") or 0) or None
        except ValueError:
            maximum = None
        return cls(enabled=env.get(MODE_ENV, "") == "demand"
                   and not env.get(HELD_ENV),
                   wait=(env.get(WAIT_ENV) or "").strip(), maximum=maximum)


def duration_seconds(text: str, default: float = 0.0) -> float:
    """`30m`, `2h`, `90s`, `1h30m` -> seconds; `default` for anything else."""
    m = _DURATION.match((text or "").strip())
    if not m or not any(m.groups()):
        return default
    h, minute, s = (float(g) if g else 0.0 for g in m.groups())
    return h * 3600 + minute * 60 + s


def command_gpus(command: str, minimum: int = 0,
                 root: Path | str | None = None,
                 maximum: int | None = None) -> int:
    """The cards one test command needs.

    A `cards_N` in the command is the selection itself (every job of the
    manifest says `hw: any`): its largest. Otherwise, and given the `root` the
    command runs in, the tests it names are read: the largest number of cards
    that a test of the command's run level declares in those files, with a
    `cards_N` mark or with a decorator's `num_cards=`. Tests of other levels do
    not count: they skip by their own guard when the cards are not there. A
    test that declares nothing needs one (`minimum`). `maximum` caps what the
    command and its tests say, never the caller's `minimum`.
    """
    command = command or ""
    found = [int(n) for n in _CARDS.findall(command) if 1 <= int(n) <= 16]
    if found or root is None:
        return max(minimum, _capped(max(found, default=0), maximum))
    level = _run_level(command)
    key = (str(root), tuple(_test_paths(command, Path(root))), level)
    now = time.monotonic()
    hit = _NEEDS.get(key)
    if hit is None or hit[0] < now:
        need = max([_file_need(path, level) for path in key[1]] or [0])
        for stale in [k for k, v in _NEEDS.items() if v[0] < now]:
            del _NEEDS[stale]
        _NEEDS[key] = hit = (now + _NEED_TTL_SEC, need)
    return max(minimum, _capped(hit[1], maximum))


def _capped(need: int, maximum: int | None) -> int:
    return min(need, maximum) if maximum and need else need


_NEEDS: dict[tuple, tuple[float, int]] = {}


def _words(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:                     # an unclosed quote
        return command.split()


def _option(words: list[str], name: str) -> str | None:
    for index, word in enumerate(words):
        if word == name and index + 1 < len(words):
            return words[index + 1]
        if word.startswith(name + "="):
            return word.split("=", 1)[1]
    return None


def _run_level(command: str) -> str:
    """What a test command selects: its `--run-level`, else the one level its
    `-m` expression names, else the default."""
    words = _words(command)
    level = _option(words, RUN_LEVEL_OPTION)
    if level:
        return level
    expression = _option(words, "-m") or ""
    named = [name for name in RUN_LEVELS if re.search(rf"\b{name}\b", expression)]
    return named[0] if len(named) == 1 else DEFAULT_RUN_LEVEL


def _test_paths(command: str, root: Path) -> list[Path]:
    """The test files a command names: a file itself, or the test files under
    a directory it names that lies inside `root`, below it, and is named like
    a test directory (`tests`, `testing`, `test_*`). A node id counts as its
    file. Nothing else is walked: a command may say `cd /tmp`."""
    out: list[Path] = []
    for word in _words(command):
        if word.startswith("-"):
            continue
        path = root / re.split(r"::|\[", word, maxsplit=1)[0]
        try:
            inside = path.resolve().is_relative_to(root.resolve()) and path.resolve() != root.resolve()
        except OSError:
            continue
        if not inside:
            continue
        if path.is_file() and path.suffix == ".py":
            out.append(path)
        elif path.is_dir() and any(part.startswith("test")
                                   for part in path.relative_to(root).parts):
            out.extend(_test_files(path))
    return out


#: Never read more test files than this for one command, nor walk these.
_MAX_TEST_FILES = 4000
_SKIP_DIRS = {".git", ".hg", ".venv", "venv", "node_modules", "__pycache__", ".tox", "build"}


def _test_files(directory: Path) -> list[Path]:
    out: list[Path] = []
    for here, dirs, files in os.walk(directory):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            if (name.startswith("test_") or name.endswith("_test.py")) and name.endswith(".py"):
                out.append(Path(here) / name)
                if len(out) >= _MAX_TEST_FILES:
                    return out
    return out


def _file_need(path: Path, level: str) -> int:
    """The largest number of cards that a test of `level` in one test file
    declares. Read from the source: names inside strings are not needs (the
    repository's own tests quote CI commands that say `cards_8`)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError, SyntaxError, RecursionError):
        return 0
    need = 0
    module_marks = _pytestmark(tree)
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            own = module_marks + _pytestmark(node)
            need = max(need, max((_test_need(fn, own, level) for fn in node.body
                                  if isinstance(fn, ast.FunctionDef)), default=0))
        elif isinstance(node, ast.FunctionDef):
            need = max(need, _test_need(node, module_marks, level))
    return need


def _pytestmark(scope: ast.Module | ast.ClassDef) -> list[str]:
    """The marks a module or a class applies through `pytestmark`."""
    return [name for stmt in scope.body
            if isinstance(stmt, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "pytestmark"
                for target in stmt.targets)
            for name in _mark_names(stmt.value)]


def _mark_names(node: ast.expr) -> list[str]:
    """`pytest.mark.cards_2` is `cards_2`; a list of marks is each of them; a
    mark called with arguments is the mark's own name."""
    if isinstance(node, (ast.List, ast.Tuple)):
        return [name for item in node.elts for name in _mark_names(item)]
    if isinstance(node, ast.Call):
        return _mark_names(node.func)
    if isinstance(node, ast.Attribute):
        return [node.attr]
    return []


def _test_need(fn: ast.FunctionDef, inherited: list[str], level: str) -> int:
    own = [name for d in fn.decorator_list for name in _mark_names(d)]
    if not fn.name.startswith("test") or level not in inherited + own:
        return 0
    need = max((int(name[6:]) for name in inherited + own if _CARDS.fullmatch(name)),
               default=0)
    for decorator in fn.decorator_list:
        if isinstance(decorator, ast.Call):
            for keyword in decorator.keywords:
                if keyword.arg == "num_cards":
                    need = max(need, _num_cards(keyword.value))
    return need


def _num_cards(node: ast.expr) -> int:
    """A decorator's `num_cards=`: one number for every platform, or the one
    for CUDA."""
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return node.value
    if isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values):
            if (isinstance(key, ast.Constant) and key.value == "cuda"
                    and isinstance(value, ast.Constant) and type(value.value) is int):
                return value.value
    return 0


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
    the command's status once the cards were granted, with NO_GPU_RC when a
    bounded `wait` expired, and with the scheduler's status for its other
    failures."""
    ask = shlex.join([
        "gpu", "run", "--gpus", str(gpus),
        "--timeout", f"{int(timeout_sec)}s",
        # the scheduler releases an idle card after 30 min by default; a test
        # may take longer before its first kernel (a model download), and the
        # timeout already bounds this reservation
        "--idle-timeout", idle_timeout(timeout_sec),
        # no bound: the scheduler queues the command until it gets its cards
        *(["--wait", wait] if wait else []),
        "--note", note, "--",
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


def idle_timeout(seconds: float) -> str:
    """`seconds` as an idle release the scheduler takes: one unit (seconds,
    minutes or hours), rounded up so the reservation outlives the command's
    own timers, and never above IDLE_MAX_SEC. It parses one unit only. Given
    `2h15m` it refuses the request; given `8100s` it accepts, then hands its
    supervisor `"2h15m"`, which that process cannot read (the card is still
    granted and released, but never for idleness)."""
    seconds = min(int(seconds), IDLE_MAX_SEC)
    if seconds < 60:
        return f"{max(seconds, 1)}s"
    minutes = -(-seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    return f"{-(-seconds // 3600)}h"


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
    if hit < 0:
        return ""
    error = text[hit:]
    usage = error.find("\nUsage:")  # the flag list that follows is not the error
    return (error if usage < 0 else error[:usage]).strip()[:300]


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
