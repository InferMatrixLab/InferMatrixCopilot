"""One execution boundary for checkout and pinned-snapshot knowledge validators."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ..contracts.models import KnowledgeValidatorResult
from ..process_execution import ProcessCleanupError, process_cleanup_facts, run_command

VALIDATOR_IDS = (
    "knowledge/tools/check_knowledge_tree.py",
    "knowledge/tools/check_wiki_lint.py",
)
PROVIDER_IMPORT_ROOT = "INFERMATRIX_KNOWLEDGE_PROVIDER"


@dataclass(frozen=True)
class ValidatorExecution:
    result: KnowledgeValidatorResult
    code_sha256: str = ""
    output_sha256: str = ""
    implementation_sha256: str = ""

    def receipt(self) -> dict:
        return {"name": Path(self.result.validator_id).name, "code_sha256": self.code_sha256,
                "output_sha256": self.output_sha256, "exit_code": self.result.returncode,
                "implementation_sha256": self.implementation_sha256}


@dataclass(frozen=True)
class ValidationReport:
    executions: tuple[ValidatorExecution, ...] = ()
    issues: tuple[str, ...] = ()

    @property
    def results(self) -> tuple[KnowledgeValidatorResult, ...]:
        return tuple(item.result for item in self.executions)

    @property
    def passed(self) -> bool:
        return not self.issues and all(item.passed for item in self.results)

    @property
    def pending(self) -> bool:
        return any(item.status == "unresolved" for item in self.results)

    def problems(self) -> list[str]:
        problems = list(self.issues)
        for result in self.results:
            if result.passed:
                continue
            name = Path(result.validator_id).name
            lines = [line for line in result.output.splitlines() if line.startswith("错误")]
            if lines:
                problems.extend(f"{name}: {line}" for line in lines)
            else:
                problems.append(f"{name} {result.status}: {result.output}")
        return problems


def _validator_path(workspace: Path, validator_id: str) -> Path | None:
    path = workspace / validator_id
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(workspace.resolve()):
        return None
    return path


def validator_preflight(workspace: Path) -> tuple[KnowledgeValidatorResult, ...]:
    return tuple(KnowledgeValidatorResult(
        validator_id=validator_id, passed=False, status="missing", returncode=None,
        output="validator is missing or not a contained regular file",
    ) for validator_id in VALIDATOR_IDS if _validator_path(workspace, validator_id) is None)


def implementation_digest() -> str:
    """Bind the installed policies as well as the snapshot's entry scripts."""
    root = Path(__file__).resolve().parent
    paths = ("validation.py", "format.py", "validators/tree.py", "validators/wiki.py",
             "../process_execution.py", "../process_tree.py")
    material = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in paths}
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_workspace(workspace: str | Path, *, timeout_seconds: int = 600,
                       stop_on_failure: bool = False) -> ValidationReport:
    """Run the fixed validators in order, retaining exact output/code evidence.

    Subprocesses isolate the CLI validators' diagnostic state. Pin the provider
    import root so exported/portable workspaces do not need a source checkout.
    """
    workspace = Path(workspace).resolve()
    if timeout_seconds < 1:
        raise ValueError("validator timeout must be positive")
    environment = os.environ.copy()
    import_root = str(Path(__file__).resolve().parents[2])
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, (import_root, environment.get("PYTHONPATH"))))
    environment[PROVIDER_IMPORT_ROOT] = import_root
    try:
        implementation_sha256 = implementation_digest()
    except OSError as exc:
        result = KnowledgeValidatorResult(
            validator_id=VALIDATOR_IDS[0], passed=False, status="error", returncode=None,
            output=f"validator implementation could not be fingerprinted ({type(exc).__name__})",
        )
        return ValidationReport((ValidatorExecution(result),))
    executions = []
    for validator_id in VALIDATOR_IDS:
        path = _validator_path(workspace, validator_id)
        code, output, code_hash, output_hash = None, "", "", ""
        if path is None:
            status, output = "missing", "validator is missing or not a contained regular file"
        else:
            try:
                code_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                completed = run_command([sys.executable, validator_id], cwd=workspace, env=environment,
                                        text=False, exit_policy="owned", timeout=timeout_seconds)
            except ProcessCleanupError as exc:
                status = "unresolved"
                output = json.dumps(process_cleanup_facts(exc), sort_keys=True)
            except subprocess.TimeoutExpired:
                status, output = "timeout", "validator timed out"
            except OSError as exc:
                status, output = "error", f"validator could not run ({type(exc).__name__})"
            else:
                code = completed.returncode
                status = "passed" if code == 0 else "failed"
                raw_output = completed.stdout + completed.stderr
                output_hash = hashlib.sha256(raw_output).hexdigest()
                output = raw_output.decode("utf-8", errors="replace").strip().replace(str(workspace), "<workspace>")[-4000:]
        result = KnowledgeValidatorResult(validator_id=validator_id, passed=status == "passed",
                                          status=status, returncode=code, output=output)
        executions.append(ValidatorExecution(result, code_hash, output_hash, implementation_sha256))
        if status == "unresolved" or (stop_on_failure and not result.passed):
            break
    return ValidationReport(tuple(executions))


def validate_snapshot(repository: str | Path, revision: str, changes: Mapping[str, str | None], *,
                      timeout_seconds: int = 300) -> ValidationReport:
    """Validate an exported pinned tree plus candidate files without modifying it."""
    with tempfile.TemporaryDirectory(prefix="kb-validate-") as scratch:
        root = Path(scratch)
        exported = run_command(["git", "-C", str(repository), "archive", "--format=tar", revision,
                                   "knowledge", "doc/knowledge", "adapters"], text=False, exit_policy="owned")
        if exported.returncode:
            return ValidationReport(issues=(f"cannot export the knowledge tree at {revision[:12]}: "
                                            + exported.stderr.decode(errors="replace")[:300],))
        with tarfile.open(fileobj=io.BytesIO(exported.stdout)) as archive:
            for member in archive.getmembers():
                target = (root / member.name).resolve()
                if not target.is_relative_to(root) or member.issym() or member.islnk():
                    continue
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    target.write_bytes(source.read() if source is not None else b"")
        for relative, text in changes.items():
            target = (root / relative).resolve()
            if not target.is_relative_to(root):
                return ValidationReport(issues=(f"candidate path is outside the knowledge workspace: {relative}",))
            if text is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
        run_command(["git", "init", "-q", str(root)], text=False,
                    exit_policy="owned").check_returncode()
        return validate_workspace(root, timeout_seconds=timeout_seconds)
