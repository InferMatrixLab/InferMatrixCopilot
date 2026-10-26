"""Report production source changes against the frozen capability-refactor baseline."""
from __future__ import annotations

import argparse
import ast
import io
import json
from pathlib import Path
import subprocess
import tarfile


def counts(files):
    result = {scope: {"files": 0, "lines": 0, "python_lines": 0, "statements": 0, "classes": 0}
              for scope in ("knowledge", "provider")}
    for name, raw in files:
        tree = ast.parse(raw, filename=name) if name.endswith(".py") else ast.Module(body=[], type_ignores=[])
        metrics = {"files": 1, "lines": len(raw.splitlines()),
                   "python_lines": len(raw.splitlines()) if name.endswith(".py") else 0,
                   "statements": sum(isinstance(node, ast.stmt) for node in ast.walk(tree)),
                   "classes": sum(isinstance(node, ast.ClassDef) for node in ast.walk(tree))}
        scopes = ["provider"]
        if name.startswith(("src/infermatrix_copilot/kb_service/", "src/infermatrix_copilot/knowledge_service/", "knowledge/tools/")):
            scopes.append("knowledge")
        for scope in scopes:
            for key, value in metrics.items():
                result[scope][key] += value
    return result


def measure(root, baseline):
    paths = ("src/infermatrix_copilot", "playbooks", "adapters", "knowledge/tools")
    suffixes = (".py", ".yaml", ".yml", ".json")
    archive = subprocess.check_output(["git", "archive", baseline, *paths], cwd=root)
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        before = counts((entry.name, source.extractfile(entry).read()) for entry in source
                        if entry.isfile() and entry.name.endswith(suffixes) and not Path(entry.name).name.startswith("test_"))
    after = counts((path.relative_to(root).as_posix(), path.read_bytes())
                   for directory in paths for path in sorted((root / directory).rglob("*"))
                   if path.is_file() and path.suffix in suffixes and not path.name.startswith("test_"))
    delta = {scope: {key: after[scope][key] - before[scope][key] for key in before[scope]}
             for scope in before}
    return {"baseline": subprocess.check_output(["git", "rev-parse", baseline], cwd=root, text=True).strip(),
            "scope": "provider Python, runtime YAML/JSON, packaged validator entrypoints; excludes tests, docs, and developer tools; AST counts are Python only",
            "before": before, "after": after, "delta": delta,
            "size_reduced": all(delta[scope]["lines"] < 0 and delta[scope]["statements"] < 0
                                     for scope in delta)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="647e6e02b083e61801e1bccd20c30a1aa7f590ec")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--bot-root", type=Path)
    parser.add_argument("--bot-baseline", default="1e70e98310f191dc7ea77ffa508eaa0093e5b0c6")
    parser.add_argument("--completion-baseline", type=Path,
                        help="directory containing pre-completion copilot/reviewbot-source.tar.gz snapshots")
    args = parser.parse_args()
    result = measure(Path(__file__).resolve().parents[1], args.baseline)
    if args.bot_root:
        bot_root = args.bot_root.resolve()
        archive = subprocess.check_output(["git", "archive", args.bot_baseline, "src/omni_reviewbot"], cwd=bot_root)
        with tarfile.open(fileobj=io.BytesIO(archive)) as source:
            before = counts((entry.name, source.extractfile(entry).read()) for entry in source
                            if entry.isfile() and entry.name.endswith((".py", ".yaml", ".yml", ".json")))["provider"]
        after = counts((path.relative_to(bot_root).as_posix(), path.read_bytes())
                       for path in sorted((bot_root / "src/omni_reviewbot").rglob("*"))
                       if path.is_file() and path.suffix in (".py", ".yaml", ".yml", ".json"))["provider"]
        result["bot"] = {"baseline": subprocess.check_output(["git", "rev-parse", args.bot_baseline], cwd=bot_root, text=True).strip(),
                         "scope": "all bot production Python and runtime YAML/JSON under src; excludes tests, docs and scripts",
                         "before": before, "after": after,
                         "delta": {key: after[key] - before[key] for key in before}}
        result["size_reduced"] &= all(result["bot"]["delta"][key] < 0 for key in ("lines", "statements"))
    if args.completion_baseline:
        result["completion_baseline"] = {}
        roots = {"copilot": Path(__file__).resolve().parents[1]}
        if args.bot_root:
            roots["reviewbot"] = args.bot_root.resolve()
        for name, root in roots.items():
            snapshot = args.completion_baseline / f"{name}-source.tar.gz"
            with tarfile.open(snapshot) as source:
                before = counts((entry.name, source.extractfile(entry).read()) for entry in source
                                if entry.isfile() and entry.name.endswith((".py", ".yaml", ".yml", ".json")))["provider"]
            after = counts((path.relative_to(root).as_posix(), path.read_bytes())
                           for path in sorted((root / "src").rglob("*"))
                           if path.is_file() and path.suffix in (".py", ".yaml", ".yml", ".json"))["provider"]
            result["completion_baseline"][name] = {
                "snapshot": str(snapshot.resolve()),
                "scope": "production Python and runtime YAML/JSON under src; excludes docs, tests and tools",
                "before": before, "after": after,
                "delta": {key: after[key] - before[key] for key in before}}
    report = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report)
    print(report, end="")


if __name__ == "__main__":
    main()
