"""Execute frozen quality phases; model dispatch stays in existing arm drivers.

Only the current installed controller runs this wrapper. Each phase selects its
original/current installed interpreter explicitly. There is no phase retry or
recovery loop: the paired coordinator retains authority over the original stage.
"""
import argparse
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import time

from infermatrix_copilot.persistence import immutable_write_bytes
from infermatrix_copilot.process_execution import run_process
from ledger import Refused


def load_phases(path, expected):
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise Refused("quality phase manifest changed")
    packet = json.loads(raw)
    if (not isinstance(packet, dict) or set(packet) != {"schema", "arm_entry", "files", "phases"}
            or packet["schema"] not in ("quality-stage/1", "quality-stage/2")
            or not isinstance(packet["files"], dict) or not packet["files"]
            or not isinstance(packet["phases"], list) or not packet["phases"]):
        raise Refused("freeze complete quality phase commands and identities")
    required = {packet["arm_entry"]}
    if packet["schema"] == "quality-stage/2":
        import stage_completion
        required.add(str(Path(stage_completion.__file__).resolve()))
    names = set()
    for phase in packet["phases"]:
        if (not isinstance(phase, dict) or set(phase) != {"name", "python", "driver", "arguments", "timeout_s"}
                or not isinstance(phase["name"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", phase["name"])
                or phase["name"] in names
                or not isinstance(phase["arguments"], list)
                or any(not isinstance(arg, str) or not arg for arg in phase["arguments"])
                or type(phase["timeout_s"]) is not int or not 0 < phase["timeout_s"] <= 7200):
            raise Refused("invalid or duplicate quality phase")
        names.add(phase["name"])
        required.update((phase["python"], phase["driver"]))
    if not required <= set(packet["files"]):
        raise Refused("quality phase executable is not hash-bound")
    for filename, digest in packet["files"].items():
        if (not isinstance(filename, str) or not Path(filename).is_absolute()
                or hashlib.sha256(Path(filename).read_bytes()).hexdigest() != digest):
            raise Refused("quality phase executable or helper changed")
    # Admission uses the same request constructor as the measured judgment.
    for phase in packet['phases']:
        if Path(phase['driver']).name == 'block_arm.py':
            from block_arm import load_block
            arguments = phase['arguments']
            try:
                inputs = Path(arguments[arguments.index('--inputs') + 1])
                case = arguments[arguments.index('--case') + 1]
            except (ValueError, IndexError) as exc:
                raise Refused('freeze block judgment inputs and case') from exc
            load_block(inputs, case)
    return packet


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phases", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--shim", required=True)
    args = parser.parse_args()
    packet = load_phases(args.phases, args.sha256)
    if packet['schema'] == 'quality-stage/2':
        from stage_completion import execution_lock
        lock = execution_lock(Path.cwd() / 'quality-stage.lock')
    else:
        lock = nullcontext()
    with lock:
        execute_phases(args, packet)


def execute_phases(args, packet):
    config = os.environ["SHADOW_ARM_CONFIG"]
    # Refuse any partial/completed prior invocation before starting a new phase.
    roots = [Path.cwd() / phase["name"] for phase in packet["phases"]]
    if any(root.exists() for root in roots):
        raise Refused("original quality phase exists; reconcile its coordinator stage")
    for phase, root in zip(packet["phases"], roots):
        root.mkdir(mode=0o700)
        scratch = root / 'tmp'; scratch.mkdir(mode=0o700)
        environment = dict(os.environ); environment.pop('PYTHONPATH', None)
        environment['TMPDIR'] = str(scratch)
        environment['TRACE_STORE_ROOT'] = str(root / 'runs' / 'traces')
        arguments = [args.shim if arg == "{shim}" else arg for arg in phase["arguments"]]
        command = [phase["python"], "-I", "-B", packet["arm_entry"], "--config", config,
                   "--", phase["driver"], *arguments]
        with (root / "stdout.txt").open("wb") as stdout, (root / "stderr.txt").open("wb") as stderr:
            result = run_process(command, cwd=root, env=environment,
                deadline=time.monotonic() + phase["timeout_s"], output="spool",
                on_chunk=lambda channel, data: (stdout if channel == "stdout" else stderr).write(data))
        receipt = {"returncode": result.returncode, "reason": result.reason}
        immutable_write_bytes(root / "phase-process.json", json.dumps(receipt, sort_keys=True).encode())
        if result.returncode or result.reason is not None:
            raise Refused("quality phase failed; preserve native outcomes and reconcile the original stage")
    if packet['schema'] == 'quality-stage/2':
        from stage_completion import write_completion
        write_completion(Path.cwd(), args.sha256, packet, config)
    print(json.dumps({"phases": len(roots), "scope": "frozen workflow and supplemental phases",
                      "adjudication": "pending", "publication": "disabled"}))


if __name__ == "__main__":
    main()
