"""Bind the PR4870 training replay to source, carried concern and explicit labels."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-snapshot", type=Path, required=True)
    parser.add_argument("--archived-inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = json.loads((args.source_snapshot / "manifest.json").read_text())
    repo = args.source_snapshot / "repository"
    base, head = source["base"], source["head"]
    prior = "fc40c1b23615c9d13ecaeb7e77feaf70caba48e6"
    path = "vllm_omni/entrypoints/openai/serving_speech.py"

    def git(*command):
        return subprocess.check_output(["git", "-C", str(repo), *command])

    git("merge-base", "--is-ancestor", prior, head)
    before, after = (git("show", revision + ":" + path).decode() for revision in (prior, head))
    assert 'is_streaming_request = request.is_streaming() and bool(async_chunk)' in before
    assert 'qwen3_full_payload = self._tts_model_type == "qwen3_tts" and not bool(async_chunk)' in after
    assert 'is_streaming_request = request.is_streaming() and not qwen3_full_payload' in after
    archived = json.loads((args.archived_inputs / "review-pr4870.json").read_text())
    title = "[Bugfix] Fix Qwen3-TTS no-async-chunk payload splitting"
    carried = {"finding_id": "shadow-pr4870-streaming-scope", "source_head_sha": prior,
               "severity": "P2", "path": path, "line": 3507,
               "title": "Restrict FINAL_ONLY coercion to Qwen3-TTS",
               "body": "At the prior head, every async_chunk=False model loses DELTA streaming. Recheck whether non-Qwen3 models retain their existing behavior.",
               "disputed": False}
    context = {"pull": {"number": 4870, "title": title, "body": archived["text"],
                "base_sha": base, "head_sha": head, "base_ref": "main",
                "html_url": "https://github.com/vllm-project/vllm-omni/pull/4870"},
               "existing_feedback_status": "checked",
               "pr_review_context": [{"kind": "inline_comment", "thread_ref": "inline_comment:948700001",
                   "path": path, "line": 3507, "resolved": True,
                   "body": "The previous broad FINAL_ONLY change was scoped to qwen3_tts. Verify the current source before repeating this concern.",
                   "fixture_origin": "Synthetic discussion identity, based on the archived resolved concern; never publish to GitHub."}],
               "ci_evidence": {"head_sha": head, "read_status": "unavailable"},
               "ci_notice": "No independently frozen CI status. Test claims in the historical PR description are unverified author evidence."}
    statuses = git("diff", "--name-status", "--no-renames", base, head).decode().splitlines()
    changes = [{"path": line.split("\t", 1)[1], "status": {"M": "modified", "A": "added", "D": "removed"}[line[0]]} for line in statuses]
    request = {"repository": "vllm-project/vllm-omni", "pr": 4870, "base_sha": base,
               "expected_head_sha": head, "title": title, "body": archived["text"],
               "changed_paths": changes, "carried_findings": [carried], "business_context": context,
               "source_bundle_sha256": source["bundle"]["sha256"], "publication": "disabled"}
    labels = {"split": "train", "carried_finding": {"finding_id": carried["finding_id"],
               "expected_outcome": "fixed", "evidence": {"path": path, "start": 3500, "end": 3510},
               "reason": "The head guards FINAL_ONLY with qwen3_tts and retains other models' DELTA coercion."},
              "expected_new_finding": {"topic": "Lost hidden-axis multimodal slicing",
               "evidence": [{"path": "vllm_omni/worker/gpu_ar_model_runner.py", "start": 1728, "end": 1791},
                            {"path": "vllm_omni/utils/mm_outputs.py", "start": 108, "end": 149}],
               "mechanism": "The caller now passes only total scheduled tokens. A tensor whose leading dimension matches a larger padded hidden-state axis no longer matches seq_len; to_payload_element clones the entire tensor for each request instead of slicing it.",
               "counterexample": {"scheduled_tokens": 3, "hidden_axis": 4, "tensor_rows": 4, "request_slices": [[0, 1], [1, 3]]},
               "provenance": "Existing training-set latent-gap label references follow-up PR4910; mechanism checked against this frozen head."},
              "hard_failures": ["Treating unavailable CI as passed", "Repeating the carried concern as still affected without contrary source evidence", "Referencing a discussion outside the frozen set", "Publishing any result"],
              "limitations": "New-finding mechanism is source-checked, not reproduced in a GPU serving runtime."}
    root = args.output
    root.mkdir(parents=True, mode=0o700, exist_ok=False)
    files = {}
    for name, obj in (("request.json", request), ("labels.json", labels), ("source-manifest.json", source)):
        data = (json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
        (root / name).write_bytes(data)
        files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    diff = (args.source_snapshot / "review.diff").read_bytes()
    (root / "review.diff").write_bytes(diff)
    files["review.diff"] = {"sha256": hashlib.sha256(diff).hexdigest(), "bytes": len(diff)}
    (root / "manifest.json").write_text(json.dumps({"files": files,
        "status": "frozen review case; shared knowledge snapshot and arm configuration pending"}, indent=2) + "\n")
    print(json.dumps({"files": len(files), "head": head, "model_calls": 0}))


if __name__ == "__main__":
    main()
