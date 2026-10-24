"""Evaluation-only native Codex dispatch boundary, shared by both shadow arms.

Runs in the candidate evaluation environment. Legacy arguments translate into
the shared provider/sandbox workflow; arm business policies remain independent.
No invocation is admitted without a frozen per-case allowance.
"""
from __future__ import annotations

import hashlib
import base64
import json
import os
from pathlib import Path
import sys
import time

from infermatrix_copilot.providers.base import json_events

from shared_execution import execute_shared, validate_shared_request, subscription_control, provider_binary_hash

from ledger import Ledger, Refused


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def option_path(args, name, cwd):
    values = []
    for index, arg in enumerate(args):
        if arg == name:
            if index + 1 == len(args):
                raise Refused(f"missing {name} value")
            values.append(args[index + 1])
        elif arg.startswith(name + "="):
            values.append(arg.split("=", 1)[1])
    if len(values) > 1:
        raise Refused(f"duplicate {name}")
    if not values:
        return None
    path = Path(cwd) / values[0]
    if path.is_symlink():
        raise Refused("shadow schema/output file cannot be a symlink")
    return path.absolute()


def validate_bridge(overrides, policy):
    import tomllib
    prefix = "mcp_servers.infermatrix-tools."
    parsed = {}
    for override in overrides:
        key = override.split("=", 1)[0].strip()
        if not key.startswith(prefix) or key in parsed:
            raise Refused("unexpected or duplicate Strict bridge config")
        try:
            parsed[key] = tomllib.loads(override)["mcp_servers"]["infermatrix-tools"][key.removeprefix(prefix)]
        except (ValueError, KeyError, TypeError) as exc:
            raise Refused("invalid Strict bridge config") from exc
    if set(parsed) != {prefix + key for key in ("command", "args", "env")}:
        raise Refused("Strict bridge needs exactly command, args and env")
    if parsed[prefix + "command"] != policy["python"] or parsed[prefix + "env"] != {"PYTHONPATH": policy["package_root"]}:
        raise Refused("Strict bridge interpreter or package differs from frozen policy")
    argv = parsed[prefix + "args"]
    if not isinstance(argv, list) or len(argv) != 4 or argv[:3] != ["-m", "infermatrix_copilot.tool_bridge", "--spec"]:
        raise Refused("Strict bridge command is outside frozen policy")
    path = Path(argv[3]).resolve(strict=True)
    run_root = Path(policy["run_root"]).resolve(strict=True)
    if path.parent.name != "bridge" or path.suffix != ".json" or path.parent.parent.parent != run_root:
        raise Refused("Strict bridge spec escapes the original run root")
    raw = path.read_bytes()
    data = json.loads(raw)
    scope = data.get("scope", {})
    if (data.get("version") != 2 or data.get("repo") != policy["repository"] or "rebase" in data
            or Path(data.get("run_dir", "")).resolve() != path.parent.parent
            or scope.get("read_only") is not True
            or sorted(scope.get("allowed_tools", [])) != sorted(policy["allowed_tools"])
            or scope.get("path_scope") is not None or scope.get("read_roots") or scope.get("executables")):
        raise Refused("Strict bridge scope differs from the frozen read-only policy")
    if not Path(scope.get("root", "")).resolve(strict=True).is_relative_to(Path(policy["workspace_root"]).resolve(strict=True)):
        raise Refused("Strict bridge source root escapes its workspace")
    if Path(data.get("trace_path", "")).resolve() != path.parent.parent / "bridge_trace.jsonl":
        raise Refused("Strict bridge trace escapes its run")
    if data.get("trace_store_root") and not Path(data["trace_store_root"]).resolve().is_relative_to(run_root):
        raise Refused("Strict bridge trace store escapes the original run root")
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def validate_execution_profile(args, profile):
    """Validate the frozen native options before reserving a paid call.

    Paths vary by arm and are bound separately in the request digest. Config
    overrides must be exact frozen values; unknown CLI switches fail closed.
    """
    if not isinstance(profile, dict) or not isinstance(profile.get("model"), str) or not profile["model"]:
        raise Refused("live execution needs a frozen explicit model")
    config = profile.get("config_overrides", [])
    if not isinstance(config, list) or any(not isinstance(value, str) for value in config) or len(set(config)) != len(config):
        raise Refused("freeze unique config overrides")
    import tomllib
    parsed = {}
    for override in config:
        try:
            item = tomllib.loads(override)
        except tomllib.TOMLDecodeError as exc:
            raise Refused("invalid frozen config override") from exc
        key = override.split("=", 1)[0].strip()
        if key in parsed or key not in {"model_reasoning_effort"} and not key.startswith("mcp_servers."):
            raise Refused("frozen config may only set reasoning effort or explicit MCP bindings")
        parsed[key] = item
    effort = profile.get("reasoning_effort")
    if not isinstance(effort, str) or not effort or parsed.get("model_reasoning_effort") != {"model_reasoning_effort": effort}:
        raise Refused("freeze explicit matching reasoning effort")
    aliases = {"-m": "model", "--model": "model", "-s": "sandbox", "--sandbox": "sandbox",
               "-c": "config", "--config": "config", "-C": "cwd", "--cd": "cwd",
               "--output-schema": "schema", "--output-last-message": "output"}
    switches = {"--json", "--ephemeral", "--ignore-user-config", "--strict-config", "--skip-git-repo-check"}
    values, seen = {}, set()
    index = 1
    while index < len(args):
        arg = args[index]
        if arg == "-" and index == len(args) - 1:
            index += 1
            continue
        key, sep, value = arg.partition("=")
        if key in aliases:
            if not sep:
                index += 1
                if index == len(args):
                    raise Refused("missing native option value")
                value = args[index]
            values.setdefault(aliases[key], []).append(value)
        elif arg in switches and arg not in seen:
            seen.add(arg)
        else:
            raise Refused("native option is outside the frozen execution policy")
        index += 1
    if values.get("model") != [profile["model"]]:
        raise Refused("native model differs from the frozen profile")
    if values.get("sandbox") != ["read-only"]:
        raise Refused("live evaluation requires one read-only sandbox option")
    actual_config = values.get("config", [])
    bridge = None
    if profile.get("mcp_bridge"):
        bridge_values = [value for value in actual_config if value.split("=", 1)[0].strip().startswith("mcp_servers.")]
        if bridge_values:
            bridge = validate_bridge(bridge_values, profile["mcp_bridge"])
            actual_config = [value for value in actual_config if value not in bridge_values]
    if sorted(actual_config) != sorted(config):
        raise Refused("native config differs from the frozen profile")
    if any(len(value) != 1 for key, value in values.items() if key != "config"):
        raise Refused("duplicate native option")
    if not {"--ignore-user-config", "--strict-config", "--json"}.issubset(seen):
        raise Refused("live evaluation must isolate user configuration")
    if args[-1:] != ["-"]:
        raise Refused("live evaluation prompt must come from stdin")
    return bridge


def apply_profile_defaults(args, profile):
    """Supply only missing options for old callers; never replace their choices."""
    if not args or args[0] != "exec" or not isinstance(profile, dict):
        raise Refused("profile defaults require an exec command and frozen profile")
    options = {arg.split("=", 1)[0] for arg in args[1:]}
    added = []
    if not options.intersection({"-m", "--model"}):
        added += ["--model", profile.get("model", "")]
    # An agent call may already carry MCP overrides but omit reasoning effort.
    supplied_config_keys = set()
    for index, arg in enumerate(args):
        if arg in {"-c", "--config"} and index + 1 < len(args):
            supplied_config_keys.add(args[index + 1].split("=", 1)[0].strip())
        elif arg.startswith("--config="):
            supplied_config_keys.add(arg[len("--config="):].split("=", 1)[0].strip())
    for override in profile.get("config_overrides", []):
        if override.split("=", 1)[0].strip() not in supplied_config_keys:
            added += ["-c", override]
    for flag in ("--ignore-user-config", "--strict-config"):
        if flag not in options:
            added.append(flag)
    effective = [args[0], *added, *args[1:]]
    validate_execution_profile(effective, profile)
    return effective


def dispatch(*, ledger_path, plan, case_id, arm, binary, args, cwd, env,
             prompt, on_line=None):
    """Reserve each CLI invocation and return its exact captured process outcome.

    A terminal process alone is insufficient: only a successful Codex terminal
    turn event settles the reservation. Timeout, malformed output and exceptions
    retain the original intent for reconciliation. Validation of model content is
    performed independently by the arm after receiving this native response.
    """
    if not plan.get("native_call_limits"):
        raise Refused("freeze native call allowances before dispatch")
    if not args or args[0] != "exec" or "--json" not in args:
        raise Refused("shadow guard only admits Codex exec JSON events")
    supplied_args = list(args)
    from shared_execution import plan_for_case
    selected_plan = plan_for_case(plan, case_id)
    if 'roles' in plan.get('execution_profile', {}):
        from role_profiles import native_plan
        if plan.get('supply_profile_defaults') is True and not any(
                arg in ('-m', '--model') or arg.startswith(('--model=', '-m=')) for arg in args):
            args = apply_profile_defaults(args, plan['execution_profile'])
        selected_plan = native_plan(selected_plan, args)
    profile = dict(selected_plan.get("execution_profile") or {})
    if "mcp_by_arm" in profile:
        profile["mcp_bridge"] = profile.pop("mcp_by_arm").get(arm)
    if plan.get("supply_profile_defaults") is True:
        args = apply_profile_defaults(args, profile)
    bridge = None
    if plan.get("execution_kind") == "live" or "execution_profile" in plan:
        bridge = validate_execution_profile(args, profile)
    binary = Path(binary).resolve(strict=True)
    binary_digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    if binary_digest != provider_binary_hash(plan, 'codex'):
        raise Refused("provider binary differs from the frozen profile")
    from shared_execution import native_deadline
    native_deadline(selected_plan)
    # Bind the environment without persisting credential values. The runner must
    # still supply the reviewed subscription-only environment to both arms.
    request = {"binary_sha256": binary_digest, "args": args,
               "cwd": str(Path(cwd).resolve()), "prompt": prompt,
               "environment_sha256": digest(env)}
    if plan.get("supply_profile_defaults") is True:
        request["supplied_args"] = supplied_args
    if bridge:
        request["bridge_spec"] = bridge
    schema = option_path(args, "--output-schema", cwd)
    output = option_path(args, "--output-last-message", cwd)
    if schema is not None:
        request["schema_sha256"] = hashlib.sha256(schema.read_bytes()).hexdigest()
    if 'native_profiles_by_case' in plan:
        request['native_profile_sha256'] = digest(selected_plan['shared_execution']['profiles'])
    request_digest = digest(request)
    call_id = digest({"case": case_id, "arm": arm, "request": request_digest})
    def replay(receipt):
        encoded = receipt.get('output_file_base64')
        if output is not None and encoded is not None:
            data = base64.b64decode(encoded, validate=True)
            if output.exists():
                if output.read_bytes() != data:
                    raise Refused('existing result file differs from completed receipt')
            else:
                fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(data)
        if on_line is not None:
            lines = receipt.get('stream')
            if lines is None:  # Original receipts retain their original shape.
                lines = [[channel, line] for channel in ('stdout', 'stderr')
                         for line in receipt.get(channel, '').splitlines()]
            for channel, line in lines:
                on_line(channel, line)
        return receipt

    if 'shared_execution' not in plan:
        cached = Ledger.read_completed(ledger_path, plan, call_id, case_id=case_id, arm=arm,
                                       request_digest=request_digest)
        if cached is not None:
            return replay(cached)
        raise Refused('retained plans are read-only; freeze a new shared-sandbox/1 plan for dispatch')
    validate_shared_request(selected_plan, arm, args, cwd, schema, output)
    if os.environ.get('SHADOW_FORGE_REFUSAL'):
        from strict_host import require_frozen_reads
        require_frozen_reads()
    ledger = Ledger(ledger_path, plan)
    try:
        cached = ledger.reserve(call_id, case_id=case_id, arm=arm, request_digest=request_digest)
        if cached is not None:
            return replay(cached)
        stream = []
        def forward(channel, line):
            stream.append([channel, line])
            if on_line is not None:
                on_line(channel, line)
        def observe(event):
            if event.get('type') == 'native.codex.event':
                forward('stdout', json.dumps(event['payload']))
        started = time.monotonic()
        receipt = execute_shared(plan=selected_plan, arm=arm, args=args, cwd=cwd, prompt=prompt,
            bridge=bridge, schema=schema, root=Path(ledger_path).with_suffix('.native') / call_id,
            native_observer=observe, hidden_paths=[str(ledger_path), str(ledger_path) + '-wal', str(ledger_path) + '-shm',
                *([str(output)] if output is not None else [])])
        # Native JSON events stream through the canonical observer. Preserve
        # remaining diagnostic lines and the original raw streams separately.
        for line in receipt['stdout'].splitlines():
            if not json_events(line):
                forward('stdout', line)
        for line in receipt['stderr'].splitlines():
            forward('stderr', line)
        receipt.update(effective_args=args, supplied_args=supplied_args, bridge_spec=bridge,
            seconds=time.monotonic() - started, request_sha256=request_digest,
            usage_policy='codex-native-events/v1', stream=stream)
        if output is not None:
            if output.is_symlink():
                raise Refused('native result file became a symlink; reconcile original call')
            text = receipt.pop('output_text')
            receipt['output_file_base64'] = base64.b64encode(text.encode()).decode('ascii')
            if output.exists():
                if output.read_bytes() != text.encode():
                    raise Refused('existing result file differs from completed receipt')
            else:
                fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as file:
                    file.write(text.encode())
        else:
            receipt.pop('output_text', None)
        if receipt['native_complete']:
            ledger.complete(call_id, receipt)
        else:
            path = Path(ledger_path).with_name(f'{call_id}.partial.json')
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as file:
                json.dump(receipt, file, sort_keys=True)
        return receipt
    finally:
        ledger.close()


def main():
    """A generated arm-local shim supplies a fixed config then vendor arguments."""
    import argparse
    from infermatrix_copilot.providers.base import sanitized_env
    from infermatrix_copilot.execution_environment import readonly_environment

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("args", nargs=argparse.REMAINDER)
    options = parser.parse_args()
    config = json.loads(options.config.read_text())
    args = options.args[1:] if options.args[:1] == ["--"] else options.args
    # Subscription-only filtering strips API overrides and publishing identity.
    # The model binary is supplied by frozen absolute path, never by shim PATH.
    environment = readonly_environment(sanitized_env())
    try:
        if args == ['login', 'status']:
            admitted = subscription_control(plan=config['plan'], arm=config['arm'],
                root=options.config.resolve().parent / 'subscription-control')
            print('Logged in using ChatGPT' if admitted else 'Not logged in')
            return 0 if admitted else 1
        receipt = dispatch(ledger_path=config["ledger_path"], plan=config["plan"],
                           case_id=config["case_id"], arm=config["arm"],
                           binary=config["binary"], args=args, cwd=os.getcwd(),
                           env=environment, prompt=sys.stdin.read(),
                           on_line=lambda channel, line: print(
                               line, file=sys.stdout if channel == "stdout" else sys.stderr, flush=True))
    except Refused as exc:
        print(f"shadow dispatch refused: {exc}", file=sys.stderr)
        return 78
    return 0 if receipt["native_complete"] else 75


if __name__ == "__main__":
    raise SystemExit(main())
