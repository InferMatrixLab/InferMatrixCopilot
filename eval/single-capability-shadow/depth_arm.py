"""Installed depth judgment over verified proofs or legacy archived fixture data.

The legacy archive path tests packet acceptance with two facets; changing its
mode annotation does not establish strict source proofs. The source-repo path
verifies all seven rendered facets against committed objects, preserves their
stored modes, and captures judgments for later independent adjudication. Neither
path alone evaluates the complete generation workflow.
"""
import argparse
import hashlib
import json
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace

import infermatrix_copilot
from infermatrix_copilot.kb_service.depth_judge import review_facets
from infermatrix_copilot.kb_service.init_budget import Budget
from arm_runtime import model_gateway, judgment_context


def load_proof_packet(inputs, repository, mode):
    """Admit rendered proofs against committed objects, before model dispatch."""
    from infermatrix_copilot.kb_service.git_source import GitSource
    from infermatrix_copilot.kb_service.knowledge_coverage import Feature
    from infermatrix_copilot.kb_service.knowledge_depth import FACETS, verified_blocks, depth_acceptance_mode

    manifest = json.loads((inputs / 'manifest.json').read_text())
    data = (inputs / 'depth-proof-input.json').read_bytes()
    if hashlib.sha256(data).hexdigest() != manifest['files']['depth-proof-input.json']['sha256']:
        raise ValueError('frozen depth proof input changed')
    packet = json.loads(data)
    source = GitSource(repository)
    pin, blocks = packet['pin'], packet['blocks']
    if source.resolve(pin) != pin or set(blocks) != set(FACETS):
        raise ValueError('depth proof input needs an exact pin and all seven facets')
    feature = Feature(**packet['feature'])
    with tempfile.TemporaryDirectory(prefix='depth-proof-source-') as temporary:
        tree = source.export(pin, temporary)
        verified, problems = verified_blocks(packet['page'], feature, tree, pin)
        if problems or verified != blocks:
            raise ValueError('depth source proofs do not reproduce the frozen blocks')
    # Use the canonical parser present in both audited SDKs. Proof structure
    # and body identity were already checked by verified_blocks above.
    modes = [depth_acceptance_mode(json.loads(re.search(
        r"<!-- kb:depth-proof (.*?) -->", block, re.S)[1])) for block in blocks.values()]
    if any(value != mode for value in modes):
        raise ValueError('requested depth mode differs from stored proofs')
    paths = packet['evidence_files']
    if not isinstance(paths, list) or not paths or any(not isinstance(path, str) for path in paths):
        raise ValueError('depth proof input needs committed evidence file paths')
    evidence = [{'kind': 'upstream_text', 'source_reference': f"{packet['repository']}@{pin}:{path}",
                 'text': source.read(pin, path).decode('utf-8')} for path in paths]
    return {'feature': feature.id, 'pin': pin, 'evidence': evidence,
            'existing_knowledge': packet.get('existing_knowledge', {})}, blocks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--shim", required=True)
    parser.add_argument("--mode", choices=("strict", "lightweight"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument('--source-repo', type=Path, help='Verify a seven-facet proof input against this frozen Git repository')
    parser.add_argument('--capture', action='store_true', help='Retain actual judgments for independent adjudication')
    args = parser.parse_args()
    if args.source_repo and not args.capture:
        parser.error('--source-repo requires --capture; fixture verdict assertions are not quality labels')
    settings, role = judgment_context(args.shim)
    if args.source_repo:
        payload, blocks = load_proof_packet(args.inputs, args.source_repo, args.mode)
    else:
        manifest = json.loads((args.inputs / "manifest.json").read_text())
        data = (args.inputs / "depth-seven-prompt.txt").read_bytes()
        assert hashlib.sha256(data).hexdigest() == manifest["files"]["depth-seven-prompt.txt"]["sha256"]
        payload = json.loads(data.decode().removeprefix("<untrusted_data>\n").removesuffix("\n</untrusted_data>"))
        blocks = {facet: payload["sections"][facet] + '\n<!-- kb:depth-proof ' +
                  json.dumps({"acceptance_mode": args.mode}) + ' -->' for facet in ("flow", "configuration")}
    records = []
    gateway = model_gateway(settings, shim=settings.strict_backend_cli, recorder=records.append)
    rt = SimpleNamespace(gateway=gateway, judge=role, unlimited_subscription=False)
    result = review_facets(rt, Budget(1), SimpleNamespace(judge_call_usd=.01),
                           feature=payload["feature"], pin=payload["pin"], blocks=blocks,
                           existing=payload["existing_knowledge"], evidence=payload["evidence"])
    # Fake native response deliberately omits configuration. Both validators
    # see the same incomplete packet; only lightweight may retain flow.
    if not args.capture:
        assert result["facets"]["configuration"]["verdict"] == "unjudged", result
        assert result["facets"]["flow"]["verdict"] == ("pass" if args.mode == "lightweight" else "unjudged"), result
    scope = 'verified seven-facet source proofs' if args.source_repo else 'legacy two-facet packet fixture'
    args.output.write_text(json.dumps({"result": result, "records": records, 'input_scope': scope}, ensure_ascii=False, indent=2))
    print(json.dumps({"package_file": infermatrix_copilot.__file__, "mode": args.mode,
                      "flow": result["facets"]["flow"]["verdict"],
                      "configuration": result["facets"]["configuration"]["verdict"],
                      'input_scope': scope, 'adjudication': 'pending' if args.capture else 'fixture assertions'}))


if __name__ == "__main__":
    main()
