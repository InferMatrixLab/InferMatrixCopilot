"""Freeze archived source material and a labeled maintenance challenge.

Historical model replies are deliberately not imported. Archive hashes establish
byte provenance; they do not independently attest the upstream Git object.
"""
import argparse
import gzip
import hashlib
import json
from pathlib import Path


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-index", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    case = json.loads(args.archive_index.read_text())["examples"]["depth:lightweight:7"]
    root = args.output
    root.mkdir(parents=True, mode=0o700, exist_ok=False)
    files = {}

    def write(name, value):
        data = value if isinstance(value, bytes) else (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
        path = root / name
        path.write_bytes(data)
        path.chmod(0o600)
        files[name] = {"sha256": sha(data), "bytes": len(data)}

    blobs = {}
    for key, reference in case["inputs"].items():
        digest = reference.removeprefix("sha256:")
        blob = Path(case["archive"]) / "blobs" / digest[:2] / (digest + ".gz")
        data = gzip.decompress(blob.read_bytes())
        if sha(data) != digest:
            raise ValueError("archive input digest differs")
        blobs[key] = data
        write(f"depth-seven-{key}.txt", data)
    prompt = blobs["prompt"].decode()
    payload = json.loads(prompt.removeprefix("<untrusted_data>\n").removesuffix("\n</untrusted_data>"))
    witness, = [e for e in payload["evidence"] if e["source_reference"].endswith(":L82-L103")]
    reference = witness["source_reference"]
    owner_pin, source_path, _ = reference.rsplit(":", 2)
    repository, pin = owner_pin.rsplit("@", 1)
    excerpt = "\n".join(line.split(": ", 1)[1] for line in witness["text"])
    evidence = {"repository": repository, "sha": pin, "path": source_path,
                "start_line": 82, "end_line": 103, "excerpt": excerpt,
                "content_sha256": sha(excerpt.encode()), "source_reference": reference}
    claim = ("FinancialParser.parse raises FileNotFoundError when the input file is missing. "
             "For an existing .json input file it dispatches to a JSON parser and returns a FinancialDocument.")
    write("maintenance-input.json", {"unit": {"unit_id": "shadow-financial-parser-inputs",
          "text": claim, "upstream_pin": pin}, "evidence": [evidence]})
    quote = 'raise ValueError(f"不支持的文件格式: {suffix}")'
    assert quote in excerpt
    write("maintenance-label.json", {"expected_outcome": "contradicted", "witness": 0,
          "supported_claim": "Missing input raises FileNotFoundError (lines 84–85).",
          "contradicted_claim": "Existing .json input is parsed successfully.",
          "counterexample": "Suffix dispatch accepts .pdf, image formats and .csv; .json enters the else branch at lines 95–96.",
          "required_quote_example": quote,
          "acceptance": "A contradicted verdict must cite the provided witness and explain the unsupported .json dispatch; unknown is inconclusive and verified is a quality failure.",
          "label_origin": "Manual comparison to supplied pinned excerpt; historical verdict not used."})
    block_cases = {
        "screening": {"op": "add", "text": "When invoking FinancialParser.parse, pass an existing .json file directly: the parser dispatches JSON input and returns a FinancialDocument."},
        "module-card": {"op": "prose", "text": "FinancialParser is the document parsing entry point. Its parse method checks file existence and natively handles PDF, images, CSV and JSON input."},
        "foundation": {"op": "prose", "text": "FinancialParser.parse checks file existence before suffix dispatch. It handles .pdf, .png/.jpg/.jpeg and .csv. Unsupported suffixes raise ValueError before post-processing; a missing file raises FileNotFoundError."},
    }
    for name, block in block_cases.items():
        write(f"{name}-input.json", {**block,
              "page": "repos/jiuwenswarm/components/financial-document-parser/rules.md",
              "evidence": [evidence], "upstream_pin": pin})
        write(f"{name}-label.json", {
              "expected_verdict": "pass" if name == "foundation" else "fail",
              "faithful": "yes" if name == "foundation" else "no",
              "basis": "The exact parse method handles PDF, images and CSV, checks existence first, and raises ValueError for other suffixes.",
              "counterexample": None if name == "foundation" else "An existing .json file reaches the unsupported-suffix exception.",
              "policy_scope": "Source judgment of a frozen proposal; full stage generation and publication are not simulated.",
              "label_origin": "Manual comparison with the supplied source witness; no historical verdict imported."})
    write("provenance.json", {"source_record": case["id"], "source_archive": case["archive"],
          "source_inputs": case["inputs"], "transformation": "Copy seven-facet input unchanged; remove displayed line-number prefixes from L82–L103 for maintenance witness; author one supported and one contradicted claim.",
          "limitations": "Archive bytes verified; upstream Git object not independently loaded. Depth input remains lightweight, not a strict proof packet."})
    (root / "manifest.json").write_text(json.dumps({"files": files,
          "status": "partial frozen input set; not a complete pilot or evaluation result"}, indent=2) + "\n")
    print(json.dumps({"files": len(files), "output": str(root), "model_calls": 0}))


if __name__ == "__main__":
    main()
