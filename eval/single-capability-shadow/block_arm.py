"""Installed init block judgment over a frozen proposal, without label access."""
import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import infermatrix_copilot
from infermatrix_copilot.kb_service.init_support import judge, classify_verdict
from infermatrix_copilot.kb_service.init_budget import Budget
from infermatrix_copilot.knowledge_service.l1 import Block
from arm_runtime import model_gateway, judgment_context


def load_block(inputs, case):
    """One model-free request contract for runtime and phase admission."""
    from ledger import Refused
    if case not in ('screening', 'module-card', 'foundation'):
        raise Refused('unsupported judgment case')
    name = case + '-input.json'
    manifest = json.loads((inputs / 'manifest.json').read_text())
    data = (inputs / name).read_bytes()
    if hashlib.sha256(data).hexdigest() != manifest['files'][name]['sha256']:
        raise Refused('frozen block input changed')
    capsule = json.loads(data)
    kind, operation = ('rule', 'add') if case == 'screening' else ('prose', 'prose')
    if capsule['op'] != operation:
        raise Refused(f'{case} judgment requires its actual {kind}/{operation} contract')
    text = capsule['text']
    return capsule, Block(kind=kind, path=capsule['page'], rule_id='', op=operation,
                          sha256=hashlib.sha256(text.encode()).hexdigest()), hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--case", choices=("screening", "module-card", "foundation"), required=True)
    parser.add_argument("--shim", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    settings, role = judgment_context(args.shim, args.model)
    capsule, block, input_sha256 = load_block(args.inputs, args.case)
    records = []
    gateway = model_gateway(settings, shim=settings.strict_backend_cli, recorder=records.append)
    rt = SimpleNamespace(gateway=gateway, judge=role, unlimited_subscription=False)
    text = capsule["text"]
    result = judge(rt, Budget(1), SimpleNamespace(judge_call_usd=.01), block,
                   base={}, head={capsule["page"]: text}, evidence=capsule["evidence"])
    report = {"result": result.to_dict(), "verdict": classify_verdict(result), "records": records,
              "package_file": infermatrix_copilot.__file__, "case": args.case,
              "input_sha256": input_sha256, "scope": "init block judgment only"}
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({key: report[key] for key in ("package_file", "case", "verdict", "input_sha256")}))


if __name__ == "__main__":
    main()
