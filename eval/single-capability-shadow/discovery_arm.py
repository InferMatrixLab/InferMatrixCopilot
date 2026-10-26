"""Installed discovery judgment over explicit evidence, with native archival."""
import argparse
import hashlib
import json
from pathlib import Path

import infermatrix_copilot
from infermatrix_copilot.kb_service.feature_discovery_index import build_discovery_index
from infermatrix_copilot.kb_service.init_feature_discovery import DiscoveryEngine
from infermatrix_copilot.kb_service.runtime import trace_recorder
from arm_runtime import model_gateway, judgment_context
from infermatrix_copilot.trace_store import TraceStore


def source_case(folder, repository, destination):
    """Use committed objects and the installed candidate/index validators."""
    from infermatrix_copilot.kb_service.git_source import GitSource
    from infermatrix_copilot.kb_service.init_feature_discovery import validate_candidates, validate_candidate
    raw = (folder / 'discovery-relation-input.json').read_bytes()
    expected = json.loads((folder / 'manifest.json').read_text())['files']['discovery-relation-input.json']
    if hashlib.sha256(raw).hexdigest() != expected['sha256']:
        raise ValueError('frozen discovery source input changed')
    packet = json.loads(raw)
    source = GitSource(repository)
    if source.resolve(packet['pin']) != packet['pin']:
        raise ValueError('discovery source needs an exact Git commit')
    validate_candidates(packet)
    for row in packet['candidates']:
        validate_candidate(row)
    tree = source.export(packet['pin'], destination)
    index = build_discovery_index(tree, pin=packet['pin'], scope=packet['scope'], doc_globs=[])
    return packet, index


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("shim")
    parser.add_argument("inputs", nargs="?", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--capture", action="store_true")
    parser.add_argument('--case-inputs', type=Path)
    parser.add_argument('--source-repo', type=Path)
    args = parser.parse_args()
    if bool(args.case_inputs) != bool(args.source_repo) or args.source_repo and not args.capture:
        parser.error('--case-inputs and --source-repo require each other and --capture')
    settings, role = judgment_context(args.shim, args.model)
    shim = args.shim
    root = Path.cwd()
    if args.source_repo:
        if args.inputs:
            parser.error('source and archived discovery inputs are mutually exclusive')
        packet, index = source_case(args.case_inputs, args.source_repo, root / 'source')
        pin, repository, rows = packet['pin'], packet['repository'], packet['candidates']
        scope = 'Committed Git source and source-authored relationship proposals; judgment supplement, not native generation.'
    elif args.inputs:
        folder = args.inputs
        raw = (folder / "maintenance-input.json").read_bytes()
        manifest = json.loads((folder / "manifest.json").read_text())
        expected = manifest["files"]["maintenance-input.json"]
        if isinstance(expected, dict):
            expected = expected["sha256"]
        assert hashlib.sha256(raw).hexdigest() == expected
        witness = json.loads(raw)["evidence"][0]
        path = witness["path"]
        target = root / path
        assert not Path(path).is_absolute() and target.resolve().is_relative_to(root.resolve())
        target.parent.mkdir(parents=True)
        target.write_text("\n" * (witness["start_line"] - 1) + witness["excerpt"] + "\n")
        pin, repository = witness["sha"], witness["repository"]
        evidence = [{"path": path, "start": witness["start_line"], "end": witness["end_line"]}]
        scope = "Archived parse-method excerpt only; blank lines are padding, not evidence of absent source. Git identity is archive provenance, not independently attested."
        primary = "FinancialParser.parse routes existing files by suffix to PDF, image or CSV handlers."
        subcapability = "FinancialParser.parse rejects existing inputs with unsupported suffixes using ValueError."
        outside = "FinancialParser.parse routes existing JSON files to a JSON handler."
        roots = [path.split("/")[0] + "/"]
    else:
        (root / "src").mkdir()
        source = 'def open_session(identity):\n    return {"identity": identity}\n\ndef identity_of(session):\n    return session["identity"]\n'
        (root / "src/session.py").write_text(source)
        pin, repository = hashlib.sha256(source.encode()).hexdigest()[:40], "offline/session"
        evidence = [{"path": "src/session.py", "start": 1, "end": 5}]
        scope = "Explicit synthetic fixture, not an upstream Git snapshot."
        primary, subcapability, outside = "Create a session carrying the supplied identity.", "Read the identity from a session.", "Schedule sessions on a GPU."
        roots = ["src/"]
    if not args.source_repo:
        index = build_discovery_index(root, pin=pin, scope={"roots": roots, "exclude": []}, doc_globs=[])
        rows = [{"id": name, "title": name, "description": description,
             "owner": "runtime", "relation": relation, "related_id": target, "aliases": [],
             "evidence": evidence, "evidence_scope": scope}
            for name, relation, target, description in [
                ("primary", "new", "", primary), ("alias", "alias", "primary", primary),
                ("subcapability", "subcapability", "primary", subcapability),
                ("outside", "new", "", outside), ("self-alias", "alias", "self-alias", primary)]]
    traces = TraceStore(root / "traces", environ={})
    gateway = model_gateway(settings, shim=settings.strict_backend_cli, recorder=trace_recorder(traces))

    packets = []
    def call(_, system, prompt, validate):
        packets.append({"system": system, "prompt": prompt})
        return gateway.call_json(role, system=system, prompt=prompt, validate=validate)

    engine = DiscoveryEngine(index, seeds=[], owners=sorted({row['owner'] for row in rows}), state={"candidates": {row["id"]: row for row in rows}},
                             call=call, save=lambda: None, concurrency=1, repository=repository, pin=pin)
    results = engine._review(rows, [])
    if not args.capture:
        assert {key: result["supported"] for key, result in results.items()} == {
            "primary": "yes", "alias": "yes", "subcapability": "yes", "outside": "no", "self-alias": "unsure"}, results
        assert results["alias"]["related_id"] == results["subcapability"]["related_id"] == "primary"
    proof = next((result['judge_receipt'] for result in results.values() if 'judge_receipt' in result), None)
    records = traces.query(kind="model_call")
    assert len(records) == 1
    archived_reply = traces.blob(records[0]["outputs"]["reply"])
    if proof is not None:
        assert proof['trace_id'] == records[0]['id']
        assert hashlib.sha256(archived_reply.encode()).hexdigest() == proof["reply_sha256"]
    (root / "discovery.json").write_text(json.dumps({"rows": rows, "results": results, "packet": packets[0], "archive_record": records[0]}, indent=2))
    print(json.dumps({"package_file": infermatrix_copilot.__file__,
                      "new_candidate": results.get('primary', {}).get('supported'),
                      "self_alias": results.get('self-alias', {}).get('supported'),
                      "verdicts": {key: value['supported'] for key, value in results.items()},
                      "native_archive_verified": proof is not None,
                      "adjudication": "pending" if args.capture else "scripted assertions passed",
                      "relationships": sorted({row['relation'] for row in rows}),
                      "scope": scope + " No catalog publication."}))


if __name__ == "__main__":
    main()
