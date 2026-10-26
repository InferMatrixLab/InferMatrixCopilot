"""Offline check: installed original/candidate transports traverse native guard.

Uses only a generated local fake vendor executable. No model service is called.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import time


def packet_comparison(before, after):
    """Keep raw prompt identities; permit only JSON serialization drift."""
    def normalized(packet):
        if set(packet) != {'system', 'prompt'}:
            raise AssertionError('unexpected judgment packet fields')
        prompt = packet['prompt']
        prefix, suffix = '<untrusted_data>\n', '\n</untrusted_data>\n'
        if not prompt.startswith(prefix) or not prompt.endswith(suffix):
            raise AssertionError('changed non-JSON judgment prompt')
        def unique(pairs):
            if len({key for key, _ in pairs}) != len(pairs):
                raise AssertionError('duplicate judgment input key')
            return dict(pairs)
        data = json.loads(prompt[len(prefix):-len(suffix)], object_pairs_hook=unique)
        return json.dumps({'system': packet['system'], 'data': data}, sort_keys=True,
                          ensure_ascii=False, allow_nan=False)
    if len(before) != len(after):
        raise AssertionError('judgment packet count differs')
    normalized_pairs = [(normalized(a), normalized(b)) for a, b in zip(before, after)]
    if any(a != b for a, b in normalized_pairs):
        raise AssertionError('judgment packet content differs')
    def hashes(packets):
        return [hashlib.sha256(json.dumps(p, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                for p in packets]
    return {'content_equal': True, 'raw_bytes_equal': before == after,
            'baseline_sha256': hashes(before), 'candidate_sha256': hashes(after),
            'normalization': 'JSON serialization only; exact systems, values, types and list ordering'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-python", type=Path, required=True)
    parser.add_argument("--candidate-python", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("completion", "direct", "strict", "maintenance", "depth-strict", "depth-lightweight", "depth-proof", "screening", "module-card", "foundation", "foundation-stage", "modules-stage", "screening-stage", "history", "discovery", "discovery-stage"), default="completion")
    parser.add_argument("--strict-fixture", type=Path)
    parser.add_argument("--history-fixture", type=Path)
    parser.add_argument("--foundation-fixture", "--stage-fixture", dest="foundation_fixture", type=Path)
    parser.add_argument("--history-validator-tools", type=Path)
    parser.add_argument("--knowledge-inputs", type=Path)
    parser.add_argument("--review-inputs", type=Path)
    parser.add_argument("--review-repo", type=Path)
    parser.add_argument("--host-inputs", type=Path)
    parser.add_argument("--forge-context-version", type=int, choices=(1, 2), default=1)
    parser.add_argument("--knowledge-root", type=Path)
    parser.add_argument("--invalid-maintenance-quote", action="store_true")
    parser.add_argument("--incomplete-depth-packet", action="store_true")
    parser.add_argument("--mixed-lightweight", action="store_true")
    parser.add_argument("--enforce-profile", action="store_true", help="Exercise live profile admission with the fake provider")
    parser.add_argument("--capture-judgments", action="store_true")
    parser.add_argument("--foundation-correction", action="store_true")
    parser.add_argument("--native-generation", action="store_true")
    parser.add_argument("--retain-evaluation-payloads", action="store_true",
                        help="Explicitly retain frozen history evaluation inputs/results in the private output root")
    parser.add_argument("--fake-reject-all", action="store_true", help="Test capture mode with deliberately different fake judgments")
    parser.add_argument('--shared-profile', type=Path, required=True,
                        help='Complete canonical sandbox profile for both installed arms')
    parser.add_argument('--native-binary', type=Path,
                        help='Frozen fixture executable already installed in the selected sandbox image')
    parser.add_argument('--host-timeout', type=int, default=180,
                        help='Outer shim/SDK deadline including sandbox preparation; native calls keep their separate deadline')
    parser.add_argument('--native-timeout', type=int, default=60)
    parser.add_argument('--stage-timeout', type=int, help='Explicit bounded arm workflow deadline; does not extend any native call')
    options = parser.parse_args()
    if not 0 < options.native_timeout <= 180 or not options.native_timeout <= options.host_timeout <= 7200:
        parser.error('freeze native timeout <=180 and a larger bounded host timeout')
    if options.native_generation and (options.mode not in {"foundation-stage", "screening-stage", "history", "discovery-stage"} or options.foundation_correction):
        parser.error("native generation requires capped foundation-stage, screening-stage, discovery-stage or history")
    if options.mode == "discovery-stage" and (not options.native_generation or not options.foundation_fixture or not options.history_validator_tools):
        parser.error("discovery-stage requires native generation, stage fixture and validator tools")
    if options.retain_evaluation_payloads and not (options.native_generation and options.mode == 'history'):
        parser.error('payload retention is an explicit native history evaluation option')
    if options.native_generation and options.mode == 'history' and not options.retain_evaluation_payloads:
        parser.error('native history requires --retain-evaluation-payloads and frozen input hashes')
    if options.foundation_correction and options.mode != "foundation-stage":
        parser.error("foundation-correction requires foundation-stage")
    if options.capture_judgments and options.mode not in {"depth-proof", "discovery", "foundation-stage", "modules-stage", "screening-stage", "discovery-stage", "history", "strict"}:
        parser.error("capture-judgments requires a supported knowledge adapter")
    if options.fake_reject_all and not options.capture_judgments:
        parser.error("fake-reject-all requires capture-judgments")
    if options.capture_judgments and options.mode == "strict" and (not options.review_inputs or options.fake_reject_all):
        parser.error("Strict capture requires frozen review inputs and its structured fake response")
    if options.review_inputs and (options.mode not in {"direct", "strict"} or not options.review_repo or not options.knowledge_root):
        parser.error("frozen review inputs require review mode, repository and shared knowledge root")
    if options.review_inputs and options.mode == "strict" and not options.host_inputs:
        parser.error("frozen Strict requires the common host packet")
    if options.invalid_maintenance_quote and options.mode != "maintenance":
        parser.error("invalid-maintenance-quote requires maintenance mode")
    if options.incomplete_depth_packet and options.mode != "depth-proof":
        parser.error("incomplete-depth-packet requires depth-proof mode")
    if options.mixed_lightweight and (options.mode != "depth-proof" or options.incomplete_depth_packet):
        parser.error("mixed-lightweight requires depth-proof mode without incomplete-depth-packet")
    stage_timeout = options.stage_timeout if options.stage_timeout is not None else (600 if options.review_inputs else 300)
    if not 0 < stage_timeout <= 7200:
        parser.error('stage-timeout must be positive and at most 7200 seconds')
    root = options.output.absolute()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    binary = root / "fake-codex"
    binary.write_text(f"#!{options.candidate_python.absolute()}\n"
                      "import json, os, sys\n"
                      "assert sys.argv[1] == 'exec' and '--json' in sys.argv\n"
                      "assert 'GH_TOKEN' not in os.environ and 'OPENAI_API_KEY' not in os.environ\n"
                      "assert 'frozen shadow prompt' in sys.stdin.read()\n"
                      'print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"guard reached"}}))\n'
                      'print(json.dumps({"type":"turn.completed","usage":{"input_tokens":7,"output_tokens":2}}))\n')
    binary.chmod(0o700)
    if options.mode in {"foundation-stage", "modules-stage"}:
        if not options.foundation_fixture or not options.history_validator_tools:
            parser.error("stage comparison needs --stage-fixture and standalone validator tools")
        binary.write_text(f"#!{options.candidate_python.absolute()}\n"
                          "import json,sys\nprompt=sys.stdin.read()\nassert prompt\n"
                          "data={'dimensions':{'faithful':'no' if ('Engine.step parses JSON' in prompt or 'Linter.run parses JSON' in prompt) else 'yes',"
                          "'does_not_weaken':'yes','non_contradictory':'yes'},'reasons':{'faithful':'The shown function returns a constant; it does not parse JSON or schedule GPU work.'}}\n"
                          "print(json.dumps({'type':'item.completed','model':'offline-model','item':{'type':'agent_message','text':json.dumps(data)}}))\n"
                          "print(json.dumps({'type':'turn.completed','model':'offline-model','usage':{'input_tokens':7,'output_tokens':2}}))\n")
    if options.mode == "screening-stage":
        if not options.foundation_fixture or not options.history_validator_tools:
            parser.error("screening-stage needs --stage-fixture and standalone validator tools")
        binary.write_text(f"#!{options.candidate_python.absolute()}\n"
                          "import json,sys\nprompt=sys.stdin.read()\nassert prompt\n"
                          "import re\nstart=re.search(r'\\{\\s*\"change\":',prompt).start()\n"
                          "payload=json.JSONDecoder().raw_decode(prompt[start:])[0]\n"
                          "rule=payload['change']['rule_id']\n"
                          "answer='no' if rule=='TOY-I2' else 'unsure' if rule=='TOY-I3' else 'yes'\n"
                          "data={'dimensions':{key:answer for key in ('faithful','non_contradictory','actionable')},'reasons':{'faithful':'Scripted screening policy fixture.'}}\n"
                          "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(data)}}))\n"
                          "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':7,'output_tokens':2}}))\n")
    if options.mode == "discovery-stage":
        binary.write_text(f"#!{options.candidate_python.absolute()}\n" +
            Path(__file__).with_name("discovery_native_fixture.py").read_text())
    if options.mode == "discovery":
        payload = {"decisions": {name: {"supported": supported, "relation": relation, "related_id": target,
                    "reason": "Scripted source-grounded decision for the supplied implementation excerpt."}
                    for name, supported, relation, target in [
                        ("primary", "yes", "new", ""), ("alias", "yes", "alias", "primary"),
                        ("subcapability", "yes", "subcapability", "primary"),
                        ("outside", "no", "unknown", ""), ("self-alias", "yes", "alias", "self-alias")]}}
        binary.write_text(f"#!{options.candidate_python.absolute()}\n"
                          "import json,sys\nassert sys.stdin.read()\n"
                          f"payload={payload!r}\n"
                          "print(json.dumps({'type':'item.completed','model':'offline-model','item':{'type':'agent_message','text':json.dumps(payload)}}))\n"
                          "print(json.dumps({'type':'turn.completed','model':'offline-model','usage':{'input_tokens':7,'output_tokens':2}}))\n")
    if options.mode == "history":
        if options.history_fixture is None or options.history_validator_tools is None:
            raise ValueError("history mode needs frozen test fixture and standalone preparation validators")
        payload = {"verdict": "approve", "findings": [], "summary": "Checked the complete two-commit aggregate and source witnesses."}
        binary.write_text(f"#!{options.candidate_python.absolute()}\n"
                          "import json,sys\nassert sys.stdin.read()\n"
                          f"payload={payload!r}\n"
                          "print(json.dumps({'type':'item.completed','model':'offline-model','item':{'type':'agent_message','text':json.dumps(payload)}}))\n"
                          "print(json.dumps({'type':'turn.completed','model':'offline-model','usage':{'input_tokens':7,'output_tokens':2}}))\n")
    block_case = options.mode in {"screening", "module-card", "foundation"}
    if block_case:
        if options.knowledge_inputs is None:
            raise ValueError("block mode needs frozen inputs")
        dimensions = {"faithful": "yes" if options.mode == "foundation" else "no", "non_contradictory": "yes"}
        dimensions["actionable" if options.mode == "screening" else "does_not_weaken"] = "yes"
        payload = {"dimensions": dimensions, "reasons": {"faithful": "The supplied suffix dispatch has no JSON branch."}}
        binary.write_text(f"#!{options.candidate_python.absolute()}\n"
                          "import json,sys\nassert sys.stdin.read()\n"
                          f"payload={payload!r}\n"
                          "print(json.dumps({'type':'item.completed','model':'offline-model','item':{'type':'agent_message','text':json.dumps(payload)}}))\n"
                          "print(json.dumps({'type':'turn.completed','model':'offline-model','usage':{'input_tokens':7,'output_tokens':2}}))\n")
    if options.mode in {"depth-strict", "depth-lightweight"}:
        if options.knowledge_inputs is None:
            raise ValueError("depth mode needs frozen inputs")
        payload = {"facets": {"flow": {"dimensions": {"faithful": "yes", "non_contradictory": "yes", "does_not_weaken": "yes"}, "reason": "Suffix dispatch is shown in the supplied parse method."}}}
        binary.write_text(f"#!{options.candidate_python.absolute()}\n"
                          "import json,sys\nassert sys.stdin.read()\n"
                          f"payload={payload!r}\n"
                          "print(json.dumps({'type':'item.completed','model':'offline-model','item':{'type':'agent_message','text':json.dumps(payload)}}))\n"
                          "print(json.dumps({'type':'turn.completed','model':'offline-model','usage':{'input_tokens':7,'output_tokens':2}}))\n")
    if options.mode == "depth-proof":
        facets = "flow api configuration dependencies failure_modes tradeoffs validation".split()
        if options.incomplete_depth_packet:
            facets.remove("configuration")
        payload = {"facets": {facet: {"dimensions": {"faithful": "yes", "non_contradictory": "yes", "does_not_weaken": "yes"},
                                     "reason": "The supplied fixture source supports the scoped statement."} for facet in facets}}
        if options.mixed_lightweight:
            payload["facets"]["configuration"]["dimensions"]["faithful"] = "no"
            payload["facets"]["configuration"]["reason"] = "DEFAULT_INCREMENT is 1, not the claimed 2."
            payload["facets"]["tradeoffs"]["dimensions"]["faithful"] = "unsure"
            payload["facets"]["tradeoffs"]["reason"] = "No comparative performance evidence is shown."
            del payload["facets"]["validation"]
        binary.write_text(f"#!{options.candidate_python.absolute()}\n"
                          "import json,sys\nassert sys.stdin.read()\n"
                          f"payload={payload!r}\n"
                          "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(payload)}}))\n"
                          "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':7,'output_tokens':2}}))\n")
    if options.mode == "maintenance":
        if options.knowledge_inputs is None:
            raise ValueError("maintenance mode needs frozen inputs")
        fixture_input = json.loads((options.knowledge_inputs / "maintenance-input.json").read_text())
        quote = 'raise ValueError(f"不支持的文件格式: {suffix}")'
        assert quote in fixture_input["evidence"][0]["excerpt"]
        payload = {"outcome": "contradicted", "reason": "JSON input enters unsupported suffix branch.",
                   "witnesses": [0], "conflicts": ["Existing JSON input is parsed successfully."],
                   "assessments": [{"claim": "Existing JSON input is parsed successfully.", "witness": 0,
                                    "quote": quote, "relation": "contradicts", "explanation": "Only PDF, image and CSV suffixes are handled."}]}
        if options.invalid_maintenance_quote:
            payload["assessments"][0]["quote"] = "invented witness text not present in the source"
        binary.write_text(f"#!{options.candidate_python.absolute()}\n"
                          "import json,sys\nassert sys.stdin.read()\n"
                          f"payload={payload!r}\n"
                          "print(json.dumps({'type':'item.completed','model':'offline-model','item':{'type':'agent_message','text':json.dumps(payload)}}))\n"
                          "print(json.dumps({'type':'turn.completed','model':'offline-model','usage':{'input_tokens':7,'output_tokens':2}}))\n")
    if options.mode == "strict":
        if options.strict_fixture is None and not options.review_inputs:
            raise ValueError("Strict mode requires frozen test_e2e_strict_mock.py fixture")
        binary.write_text(f"#!{options.candidate_python.absolute()}\n" + '''
import json, os, sys
from infermatrix_copilot.engine.agent_runtime import BASE_OUTPUT_SCHEMA
assert 'GH_TOKEN' not in os.environ and 'OPENAI_API_KEY' not in os.environ
assert sys.stdin.read()
payload={k:([] if 'list' in v else 'x') for k,v in BASE_OUTPUT_SCHEMA.items()}
payload.update(status='success',summary='guard reached',confidence='high',next_action='none',failure_kind=None,review_comments=[{'file':'feature.py','line':2,'severity':'major','comment':'ZeroDivisionError when x is 0.','evidence':'return 1 / x','suggestion':'guard x == 0','_verified':True,'_anchor_unverified':False}])
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(payload)}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':7,'output_tokens':2}}))
''')
        if options.review_inputs:
            frozen = json.loads((options.review_inputs / "request.json").read_text())
            payload = {"status": "success", "summary": "Reviewed frozen multimodal split.",
                       "confidence": "high", "next_action": "none", "failure_kind": None,
                       "review_comments": [{"file": "vllm_omni/worker/gpu_ar_model_runner.py", "line": 1740,
                           "severity": "major", "comment": "Scheduler-only split length misses padded hidden-axis tensors.",
                           "evidence": "mm_seq_len = scheduler_output.total_num_scheduled_tokens",
                           "suggestion": "Preserve both valid sequence axes for slicing.", "_verified": True}],
                       "finding_rechecks": [{"finding_id": item["finding_id"], "head_sha": frozen["expected_head_sha"],
                           "outcome": "fixed", "evidence": "serving_speech.py scopes full payload to qwen3_tts."}
                           for item in frozen["carried_findings"]]}
            binary.write_text(f"#!{options.candidate_python.absolute()}\n"
                              "import json,sys\nfrom infermatrix_copilot.engine.agent_runtime import BASE_OUTPUT_SCHEMA\n"
                              "assert sys.stdin.read()\n"
                              "result={k:([] if 'list' in v else 'x') for k,v in BASE_OUTPUT_SCHEMA.items()}\n"
                              f"result.update({payload!r})\n"
                              "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(result)}}))\n"
                              "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':7,'output_tokens':2}}))\n")
    if options.mode == "direct":
        repo = root / "repo"
        repo.mkdir()
        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], check=True,
                                  capture_output=True, text=True).stdout.strip()
        git("init", "-q")
        git("config", "user.email", "offline@example.invalid")
        git("config", "user.name", "offline")
        (repo / "source.py").write_text("value = 1\n")
        git("add", "source.py")
        git("commit", "-qm", "offline snapshot")
        git("remote", "add", "origin", "https://github.com/vllm-project/vllm-omni.git")
        head = git("rev-parse", "HEAD")
        checks = "scope_alignment existing_reuse code_placement diff_size_budget change_minimality resource_budget initialization_order failure_propagation concurrency_scheduling native_api_contract feature_enabled_path test_evidence_ci_alignment historical_comments".split()
        payload = {"reviewed_head_sha": head, "summary": "guard reached", "findings": [],
                   "review_checks": {name: "Verified offline source." for name in checks},
                   "subtraction_signal": "none", "subtraction": [], "minimality_proof": None,
                   "existing_feedback_status": "checked", "finding_rechecks": [], "finding_dispositions": []}
        if options.review_inputs:
            frozen = json.loads((options.review_inputs / "request.json").read_text())
            head = frozen["expected_head_sha"]
            repo = options.review_repo.resolve()
            payload["reviewed_head_sha"] = head
            payload["summary"] = '''### PR description
This frozen change adjusts batched multimodal splitting and Qwen3 speech response selection.

### Change flow
```mermaid
flowchart LR
A["[EXISTING] Scheduled requests"]:::existing --> B["[CHANGED] Multimodal splitting"]:::changed
classDef existing fill:#e5e7eb
classDef changed fill:#fef3c7
classDef new fill:#dcfce7
classDef removed fill:#fee2e2
```'''
            payload["finding_rechecks"] = [
                {"finding_id": item["finding_id"], "head_sha": head, "outcome": "fixed",
                 "evidence": "serving_speech.py scopes qwen3_full_payload to qwen3_tts; other models retain streaming."}
                for item in frozen["carried_findings"]]
        binary.write_text(f"#!{options.candidate_python.absolute()}\n"
                          "import json, os, pathlib, sys\n"
                          "assert '--output-last-message' in sys.argv\n"
                          "assert 'GH_TOKEN' not in os.environ and 'OPENAI_API_KEY' not in os.environ\n"
                          "assert sys.stdin.read()\n"
                          f"payload={payload!r}\n"
                          "pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(json.dumps(payload))\n"
                          'print(json.dumps({"type":"turn.completed","usage":{"input_tokens":7,"output_tokens":2}}))\n')
    if options.fake_reject_all:
        response = {"dimensions": {key: "no" for key in ("faithful", "non_contradictory", "does_not_weaken")},
                    "reasons": {"faithful": "Deliberately changed fake response for capture testing."}}
        if options.mode == "depth-proof":
            response = {"facets": {key: {"dimensions": response["dimensions"], "reason": "Changed fake response."}
                        for key in "flow api configuration dependencies failure_modes tradeoffs validation".split()}}
        elif options.mode == "discovery":
            response = {"decisions": {key: {"supported": "no", "relation": "unknown", "related_id": "", "reason": "Changed fake response."}
                        for key in ("primary", "alias", "subcapability", "outside", "self-alias")}}
        elif options.mode == "screening-stage":
            response["dimensions"] = {key: "no" for key in ("faithful", "non_contradictory", "actionable")}
        elif options.mode == "history":
            response = {"verdict": "request_changes", "findings": ["Deliberately changed fake aggregate finding."],
                        "summary": "Changed fake response for capture testing."}
        binary.write_text(f"#!{options.candidate_python.absolute()}\nimport json,sys\nassert sys.stdin.read()\n"
                          f"response={response!r}\n"
                          "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(response)}}))\n"
                          "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':7,'output_tokens':2}}))\n")
    if options.native_binary is not None:
        binary = options.native_binary.resolve(strict=True)
    case_id = "installed-" + options.mode
    allowance = 3 if options.mode == "screening-stage" else 2 if options.mode == "modules-stage" else 4 if options.mode == "foundation-stage" else 12 if options.mode == "strict" else 32 if options.mode == "discovery-stage" else 1
    if options.mode == "screening-stage" and options.capture_judgments:
        allowance = 4
    if options.foundation_correction:
        allowance = 13
    if options.native_generation:
        allowance = 14 if options.mode == "foundation-stage" else 4 if options.mode == "history" else 32 if options.mode == "discovery-stage" else 6
    plan = {"native_call_limits": {case_id: allowance}, "native_timeout_s": options.native_timeout, 'host_timeout_s': options.host_timeout,
            "provider_binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}
    if options.enforce_profile or options.shared_profile:
        plan.update(execution_kind="fixture", supply_profile_defaults=True,
                    execution_profile={"model": "offline-model", "reasoning_effort": "high",
                                       "config_overrides": ['model_reasoning_effort="high"']})
        if options.mode == 'discovery-stage':
            plan['execution_profile']['roles'] = {
                'generator': {'provider': 'codex', 'model': 'fixture-generator', 'effort': 'high'},
                'judge': {'provider': 'codex', 'model': 'offline-model', 'effort': 'high'}}
        if options.mode in {"strict", "discovery-stage"}:
            plan["call_identity"] = "agent-step/1"
        if options.mode == "strict":
            plan["worker_execution"] = "durable"
            profiles = {}
            for arm, python in (("baseline", options.baseline_python), ("candidate", options.candidate_python)):
                info = json.loads(subprocess.check_output([str(python), "-I", "-c",
                    "import sys,json,infermatrix_copilot;from pathlib import Path;print(json.dumps({'python':sys.executable,'package_root':str(Path(infermatrix_copilot.__file__).resolve().parent.parent)}))"], text=True))
                repository = (json.loads((options.review_inputs / 'request.json').read_text())['repository'].split('/')[-1]
                              if options.review_inputs else 'widget')
                profiles[arm] = {**info, "repository": repository,
                    "run_root": str(root / arm / "runs"), "workspace_root": str(root / arm),
                    "allowed_tools": ["grep", "list_dir", "read_file"]}
            plan["execution_profile"]["mcp_by_arm"] = profiles
    profile = json.loads(options.shared_profile.read_text())
    plan['shared_execution'] = {'schema': 'shared-sandbox/1', 'profile': profile,
        'source_roots_by_arm': {arm: [str(root / arm), *([str(repo)] if options.mode == 'direct' else [])]
                               for arm in ('baseline', 'candidate')},
        'knowledge_by_arm': {arm: str(options.knowledge_root.resolve()) for arm in ('baseline', 'candidate')}
                            if options.knowledge_root else {}}
    if options.mode == 'direct':
        schema = Path(subprocess.check_output([str(options.baseline_python), '-I', '-c',
            "from importlib.resources import files;print(files('omni_reviewbot').joinpath('schemas/review-result-v1.json'))"],
            text=True).strip()).resolve()
        plan['shared_execution']['schema_files_by_arm'] = {'baseline': {str(schema): hashlib.sha256(schema.read_bytes()).hexdigest()}}
    if options.retain_evaluation_payloads:
        sources = [path for path in options.history_fixture.resolve().parent.glob('*.py') if path.is_file()]
        sources += [path for path in options.history_validator_tools.resolve().rglob('*') if path.is_file()]
        inputs = {'schema': 'shadow-retained-inputs/1', 'files': {
            str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(sources)}}
        retained = root / 'retained-inputs.json'
        retained.write_text(json.dumps(inputs, sort_keys=True) + '\n')
        plan['payload_retention'] = {'schema': 'shadow-payload-retention/1',
            'mode': 'retain-request-result', 'publication': 'disabled', 'case_ids': [case_id],
            'root': str(root), 'input_manifest': str(retained),
            'input_manifest_sha256': hashlib.sha256(retained.read_bytes()).hexdigest()}
    ledger = root / "calls.sqlite"
    guard = Path(__file__).with_name("codex_guard.py").resolve()
    program = """
import json, sys
from types import SimpleNamespace
import infermatrix_copilot
from arm_runtime import native_completion
settings=SimpleNamespace(strict_backend_cli=sys.argv[1], strict_backend_model='', strict_backend_timeout_s=15)
reply=native_completion(settings, shim=sys.argv[1], system='shadow', messages=[{'role':'user','content':'frozen shadow prompt'}])
text=''.join(block.text or '' for block in reply.blocks)
assert text == 'guard reached', text
print(json.dumps({'package_file':infermatrix_copilot.__file__, 'text':text, 'usage':reply.usage}))
"""
    results = {}
    for arm, python in (("baseline", options.baseline_python), ("candidate", options.candidate_python)):
        arm_root = root / arm
        arm_root.mkdir(mode=0o700)
        (arm_root / 'scratch').mkdir(mode=0o700)
        config = arm_root / "config.json"
        config.write_text(json.dumps({"ledger_path": str(ledger), "plan": plan,
                                      "case_id": case_id, "arm": arm, "binary": str(binary)}))
        shim = arm_root / "codex"
        command = [str(options.candidate_python.absolute()), str(guard), "--config", str(config), "--"]
        shim.write_text("#!/bin/sh\nexec " + shlex.join(command) + ' "$@"\n')
        shim.chmod(0o700)
        arm_program = program
        if options.mode == "direct":
            common = f"repo={str(repo)!r}\nhead={head!r}\n"
            if arm == "baseline":
                arm_program = common + """
import json, sys, omni_reviewbot
from omni_reviewbot.codex_runner import CodexRunner
from omni_reviewbot.models import PullRequest
from arm_runtime import host_timeout
pull=PullRequest(number=7,title='offline',body='offline',html_url='https://github.com/vllm-project/vllm-omni/pull/7',head_sha=head,base_sha=head,base_ref='main')
result=CodexRunner(command=[sys.argv[1]],timeout_seconds=host_timeout(sys.argv[1])).review(repo,pull,{'existing_feedback_status':'checked','pr_review_context':[]})
assert result.reviewed_head_sha==head and result.summary=='guard reached'
print(json.dumps({'package_file':omni_reviewbot.__file__,'text':result.summary,'reviewed_head_sha':result.reviewed_head_sha}))
"""
            else:
                arm_program = common + """
import json, sys, time
from pathlib import Path
import infermatrix_copilot
from infermatrix_copilot.sdk.v1 import ChangedPath,DirectReviewRequest,DirectReviewRunRequest,RepositoryRef,ReviewRuntime,ReviewRuntimeConfig,direct_review_schema
from arm_runtime import host_timeout
repository=RepositoryRef(alias='vllm-omni',full_name='vllm-project/vllm-omni')
profile={'provider':'codex','command':[sys.argv[1]],'model':'','idle_timeout_s':host_timeout(sys.argv[1]),'absolute_timeout_s':host_timeout(sys.argv[1])}
config=ReviewRuntimeConfig(repository,repo,str(Path(repo).parent),run_root=str(Path.cwd()/'runs'),direct_profiles={'codex':profile})
request=DirectReviewRunRequest(DirectReviewRequest('offline',repository,7,head,'offline','offline',(ChangedPath('source.py','modified'),)),repo,'offline-guard-direct','Frozen offline business input',direct_review_schema(),profile)
with ReviewRuntime(config=config) as runtime:
    first=runtime.reserve_direct_review(request)
    second=runtime.reserve_direct_review(request)
    assert first.created and not second.created and first.run_id==second.run_id
    deadline=time.monotonic()+host_timeout(sys.argv[1])+30
    while True:
        result=runtime.get_result(first.run_id)
        if result.terminal: break
        if time.monotonic()>deadline: raise RuntimeError('durable child timed out')
        time.sleep(.05)
    assert result.state=='done', result.payload
    assert result.review.direct_result['summary']=='guard reached'
    assert result.review.reviewed_head_sha==head
with ReviewRuntime(config=config) as recovered:
    found=recovered.find_review(request.idempotency_key,expected_head_sha=head)
    assert found.run_id==first.run_id
    assert recovered.get_result(found.run_id).review.direct_result==result.review.direct_result
print(json.dumps({'package_file':infermatrix_copilot.__file__,'text':'guard reached','reviewed_head_sha':head,'run_id':first.run_id,'recovered_without_dispatch':True}))
"""
        invocation = [str(python.absolute()), "-I", "-c", arm_program, str(shim)]
        if options.review_inputs and options.mode == "direct":
            invocation = [str(python.absolute()), "-I", str(Path(__file__).with_name("review_case_arm.py").resolve()),
                          "--arm", arm, "--inputs", str(options.review_inputs.resolve()),
                          "--repo", str(repo), "--shim", str(shim)]
            if options.host_inputs:
                invocation.extend(["--host-inputs", str(options.host_inputs.resolve())])
        if options.mode == "strict":
            if options.review_inputs:
                invocation = [str(python.absolute()), "-I", str(Path(__file__).with_name("strict_review_case_arm.py").resolve()),
                              "--inputs", str(options.review_inputs.resolve()), "--repo", str(options.review_repo.resolve()),
                              "--host-inputs", str(options.host_inputs.resolve()), "--shim", str(shim)]
                if options.forge_context_version != 1:
                    invocation.extend(["--forge-context-version", str(options.forge_context_version)])
            else:
                invocation = [str(python.absolute()), "-I", str(Path(__file__).with_name("strict_arm.py").resolve()),
                              str(shim), str(options.strict_fixture.resolve())]
        if options.mode == "history":
            invocation = [str(python.absolute()), "-I", str(Path(__file__).with_name("history_native_arm.py" if options.native_generation else "history_arm.py").resolve()),
                          str(shim), str(options.history_fixture.resolve()), str(options.history_validator_tools.resolve())]
        if options.mode in {"foundation-stage", "modules-stage"}:
            entry = ("modules_native_arm.py" if options.native_generation else "modules_arm.py") if options.mode == "modules-stage" else ("foundation_native_arm.py" if options.native_generation else "foundation_arm.py")
            invocation = [str(python.absolute()), "-I", str(Path(__file__).with_name(entry).resolve()),
                          str(shim), str(options.foundation_fixture.resolve()), str(options.history_validator_tools.resolve())]
        if options.mode == "screening-stage":
            invocation = [str(python.absolute()), "-I", str(Path(__file__).with_name("screening_native_arm.py" if options.native_generation else "screening_arm.py").resolve()),
                          str(shim), str(options.foundation_fixture.resolve()), str(options.history_validator_tools.resolve())]
        if options.foundation_correction:
            invocation.extend(["--correction", "--simulate-subscription-auth"])
        if options.mode == "discovery-stage":
            invocation = [str(python.absolute()), "-I", str(Path(__file__).with_name("discovery_native_arm.py").resolve()),
                          str(shim), str(options.foundation_fixture.resolve()), str(options.history_validator_tools.resolve())]
        if options.mode == "discovery":
            invocation = [str(python.absolute()), "-I", str(Path(__file__).with_name("discovery_arm.py").resolve()), str(shim)]
            if options.knowledge_inputs:
                invocation.append(str(options.knowledge_inputs.resolve()))
        if options.mode == "maintenance":
            invocation = [str(python.absolute()), "-I", str(Path(__file__).with_name("maintenance_arm.py").resolve()),
                          "--shim", str(shim), "--model", "offline-model", "--inputs", str(options.knowledge_inputs.resolve()),
                          "--output", str(arm_root / "audit.json")]
        if options.mode in {"depth-strict", "depth-lightweight"}:
            invocation = [str(python.absolute()), "-I", str(Path(__file__).with_name("depth_arm.py").resolve()),
                          "--shim", str(shim), "--mode", options.mode.removeprefix("depth-"),
                          "--inputs", str(options.knowledge_inputs.resolve()), "--output", str(arm_root / "depth.json")]
        if options.mode == "depth-proof":
            invocation = [str(python.absolute()), "-I", str(Path(__file__).with_name("strict_depth_arm.py").resolve()), "--shim", str(shim)]
            if options.incomplete_depth_packet:
                invocation.append("--incomplete")
            if options.mixed_lightweight:
                invocation.append("--mixed-lightweight")
        if block_case:
            invocation = [str(python.absolute()), "-I", str(Path(__file__).with_name("block_arm.py").resolve()),
                          "--shim", str(shim), "--model", "offline-model", "--case", options.mode,
                          "--inputs", str(options.knowledge_inputs.resolve()), "--output", str(arm_root / "block.json")]
        if options.capture_judgments:
            invocation += ["--capture", "--model", "offline-model", "--timeout", "15"]
            if options.mode in {"foundation-stage", "modules-stage", "screening-stage", "history"}:
                invocation += ["--effort", "high"]
        elif options.mode in {'foundation-stage', 'modules-stage', 'screening-stage', 'history'}:
            invocation += ['--model', 'offline-model', '--effort', 'high']
        invocation = [invocation[0], '-I', '-B', str(Path(__file__).with_name('arm_entry.py').resolve()),
                      '--config', str(config), '--', *invocation[2:]]
        from infermatrix_copilot.process_execution import run_process
        if options.native_generation:
            # Controls use the same installed supervisor as arm execution. The
            # original distribution predates it; never import current product
            # modules into the original interpreter.
            control = run_process([str(shim), 'login', 'status'], cwd=arm_root,
                deadline=time.monotonic() + options.host_timeout, output='spool',
                env={'PATH': '/usr/bin:/bin', 'HOME': str(arm_root),
                     'TMPDIR': str(arm_root / 'scratch')})
            (arm_root / 'subscription-preflight.json').write_text(json.dumps({
                'returncode': control.returncode, 'reason': control.reason,
                'stdout': control.stdout, 'stderr': control.stderr}))
            if control.returncode or control.reason:
                raise RuntimeError(f'{arm} subscription control failed before model workflow')
        result = run_process(invocation,
                                cwd=arm_root, output='spool',
                                deadline=time.monotonic() + stage_timeout,
                                env={"PATH": "/usr/bin:/bin", "HOME": str(arm_root),
                                     'TMPDIR': str(arm_root / 'scratch'),
                                     "GH_TOKEN": "fake", "OPENAI_API_KEY": "fake",
                                     **({"KNOWLEDGE_ROOT": str(options.knowledge_root.resolve())}
                                        if options.knowledge_root else {})})
        (arm_root / "stdout.txt").write_text(result.stdout)
        (arm_root / "stderr.txt").write_text(result.stderr)
        (arm_root / 'stage-process.json').write_text(json.dumps({'returncode': result.returncode, 'reason': result.reason}) + '\n')
        expected_exit = 1 if options.invalid_maintenance_quote else 0
        if result.returncode != expected_exit or result.reason is not None:
            raise RuntimeError(f"{arm} failed: {result.stderr}")
        results[arm] = json.loads(result.stdout)
        if block_case:
            assert results[arm]["verdict"] == ("pass" if options.mode == "foundation" else "fail")
        if options.mode == "maintenance":
            audit = json.loads((arm_root / "audit.json").read_text())
            assert audit["status"] == ("failed" if options.invalid_maintenance_quote else "validated")
            if options.invalid_maintenance_quote:
                assert "quote is not in its original witness" in audit["error"], audit
        if "site-packages" not in results[arm]["package_file"]:
            raise RuntimeError("arm did not import an installed distribution")
    with sqlite3.connect(ledger) as db:
        rows = db.execute("SELECT arm,status,result FROM calls ORDER BY arm").fetchall()
    assert {arm for arm, _, _ in rows} == {"baseline", "candidate"}
    assert all(status == "complete" for _, status, _ in rows)
    if options.mode not in {'strict', 'discovery-stage'}:
        assert len(rows) == allowance * 2
    for arm, _, receipt in rows:
        assert json.loads(receipt)["usage"] == {"input_tokens": 7, "output_tokens": 2}
    if options.mode in {"direct", "strict", "history"}:
        assert results["baseline"]["reviewed_head_sha"] == results["candidate"]["reviewed_head_sha"]
    report = {"kind": "offline-installed-transport-guard", "mode": options.mode, "model_calls": 0,
              "invalid_maintenance_quote": options.invalid_maintenance_quote,
              "incomplete_depth_packet": options.incomplete_depth_packet,
              "mixed_lightweight": options.mixed_lightweight,
              "capture_judgments": options.capture_judgments,
              "fake_reject_all": options.fake_reject_all, "native_generation": options.native_generation,
              "retained_evaluation_payloads": options.retain_evaluation_payloads,
              "native_fake_processes": len(rows), "arms": results,
              "guard_sha256": hashlib.sha256(guard.read_bytes()).hexdigest(), "plan": plan,
              "limitations": [("standard durable Strict child and evaluation account exercised"
                                if options.mode == "strict" else "Strict workflow and other provider paths not exercised"),
                              "not a model-quality comparison or complete bypass audit"]}
    if options.mode == "strict" and options.strict_fixture:
        report["fixture_sha256"] = hashlib.sha256(options.strict_fixture.read_bytes()).hexdigest()
    if options.mode in {"foundation-stage", "modules-stage", "screening-stage"}:
        stage = "screening" if options.mode == "screening-stage" else "modules" if options.mode == "modules-stage" else "foundation"
        before = json.loads((root / "baseline" / (stage + ".json")).read_text())
        after = json.loads((root / "candidate" / (stage + ".json")).read_text())
        report['judgment_packet_comparison'] = packet_comparison(before['packets'], after['packets'])
        for key in ("tree", "verdicts"):
            assert before[key] == after[key], key
        report[stage + "_packet_and_tree_parity"] = True
    if options.host_inputs and options.mode == "direct":
        before = json.loads((root / "baseline/context.json").read_text())
        run_dir = root / "candidate/runs" / results["candidate"]["run_id"]
        after = json.loads((run_dir / "request.json").read_text())["params"]["direct_review"]["business_context"]
        keys = ("ci_evidence", "diff_size", "patch_evidence", "pr_review_context",
                "existing_feedback_status", "host_evidence_provenance")
        parity = {key: before[key] == after[key] for key in keys}
        assert all(parity.values()), parity
        expected = (options.host_inputs / "patch.txt").read_bytes()
        assert (root / "baseline/workspace" / before["patch_evidence"]["path"]).read_bytes() == expected
        assert (run_dir / "workspace" / after["patch_evidence"]["path"]).read_bytes() == expected
        report["host_context_parity"] = parity
        report["patch_sha256"] = hashlib.sha256(expected).hexdigest()
        report["host_manifest_sha256"] = hashlib.sha256((options.host_inputs / "manifest.json").read_bytes()).hexdigest()
        report["review_driver_sha256"] = hashlib.sha256(Path(__file__).with_name("review_case_arm.py").read_bytes()).hexdigest()
    if options.mode == 'discovery-stage':
        left, right = [json.loads((root / arm / 'discovery.json').read_bytes()) for arm in ('baseline', 'candidate')]
        assert left['publication_tree'] == right['publication_tree'], 'catalog publication tree differs'
        report['discovery_publication_tree_parity'] = True
        report['discovery_packet_comparison'] = {
            'raw_equal': left['packets'] == right['packets'],
            'systems_equal': [p['system'] for p in left['packets']] == [p['system'] for p in right['packets']],
            'baseline_sha256': [hashlib.sha256(json.dumps(p, sort_keys=True).encode()).hexdigest() for p in left['packets']],
            'candidate_sha256': [hashlib.sha256(json.dumps(p, sort_keys=True).encode()).hexdigest() for p in right['packets']],
            'interpretation': 'Full-stage inputs retain per-arm trace IDs, durations, usage projections and derived audit hashes; no prompt content or byte parity is claimed.'}
        assert report['discovery_packet_comparison']['systems_equal']
        assert left['record']['pin'] == right['record']['pin']
        report['discovery_workflow'] = {arm: {key: data['discovery'].get(key) for key in
            ('done', 'catalog_sha256', 'feature_ids')} for arm, data in zip(('baseline', 'candidate'), (left, right))}
    if options.mode == "history":
        before = json.loads((root / "baseline/history.json").read_text())["packet"]
        after = json.loads((root / "candidate/history.json").read_text())["packet"]
        assert before == after
        left = json.loads((root / 'baseline/history.json').read_text())
        right = json.loads((root / 'candidate/history.json').read_text())
        report['history_packet_comparison'] = packet_comparison(left['packets'], right['packets'])
        assert left['tree'] == right['tree']
        report['history_full_tree_parity'] = True
        report["aggregate_packet_sha256"] = hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest()
        report["preparation_validator_hashes"] = {name: hashlib.sha256((options.history_validator_tools / name).read_bytes()).hexdigest()
                                                 for name in ("check_knowledge_tree.py", "check_wiki_lint.py")}
    (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
