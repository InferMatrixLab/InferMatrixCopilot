"""Host-only admission of a complete independently labeled quality comparison."""
from pathlib import Path

from ledger import Refused
from stage_selection import select_stages


STRATA = frozenset({"direct", "strict", "screening", "module-card", "foundation",
                    "strict-depth", "lightweight-depth", "discovery", "history", "maintenance"})


def validate_quality(manifest, *, output=None, for_dispatch=True):
    """Keep labels on the host, outside every worker mount and stage output.

    This freezes scope and acceptance criteria; it does not adjudicate results.
    Legacy conformance plans omit this descriptor and retain their own scope.
    """
    value = manifest.get("quality_evaluation")
    if value is None:
        return
    phased = isinstance(value, dict) and value.get('schema') == 'native-quality/2'
    keys = {"schema", "cases", "acceptance"} | ({"external_cases"} if phased else set())
    active = set(manifest["plan"]["native_call_limits"])
    if (not isinstance(value, dict) or set(value) != keys
            or value["schema"] not in ("native-quality/1", "native-quality/2")
            or not isinstance(value["cases"], dict) or set(value["cases"]) != STRATA
            or not active or not active <= STRATA or (not phased and active != STRATA)
            or not isinstance(value["acceptance"], str) or not value["acceptance"].strip()):
        raise Refused("freeze all ten quality strata and explicit acceptance criteria")
    external = value.get('external_cases', {})
    if (not isinstance(external, dict) or set(external) != STRATA - active
            or any(not isinstance(row, dict) or set(row) != {'status', 'reason', 'evidence'}
                   or row['status'] not in ('retained', 'deferred')
                   or not isinstance(row['reason'], str) or not row['reason'].strip()
                   or not isinstance(row['evidence'], dict) or not row['evidence']
                   for row in external.values())):
        raise Refused('every unexecuted quality case needs explicit retained/deferred evidence')
    external_paths, external_hashes = set(), set()
    _, retained_stages = select_stages(manifest)
    for row in [*external.values(), *retained_stages.values()]:
        for path, sha in row['evidence'].items():
            if (not isinstance(path, str) or not Path(path).is_absolute()
                    or not isinstance(sha, str) or len(sha) != 64
                    or set(sha) - set('0123456789abcdef') or manifest['bindings'].get(path) != sha):
                raise Refused('external quality evidence must be hash-bound')
            external_paths.add(Path(path).resolve()); external_hashes.add(sha)
    shared = manifest["plan"].get("shared_execution", {})
    by_arm = shared.get("source_roots_by_arm")
    if (not isinstance(by_arm, dict) or set(by_arm) != {"baseline", "candidate"}
            or any(not isinstance(roots, list) or not roots for roots in by_arm.values())):
        raise Refused("quality comparison needs explicit worker roots for both arms")
    paths = [path for selected in by_arm.values() for path in selected]
    knowledge = shared.get("knowledge_by_arm", {})
    schemas = shared.get("schema_files_by_arm", {})
    if (not isinstance(knowledge, dict) or set(knowledge) - {"baseline", "candidate"}
            or not isinstance(schemas, dict) or set(schemas) - {"baseline", "candidate"}
            or any(not isinstance(files, dict) for files in schemas.values())):
        raise Refused("freeze quality knowledge and schema mounts")
    paths.extend(path for path in knowledge.values() if path is not None)
    if 'native_knowledge_by_case' in manifest['plan'] or 'case_deadlines' in manifest['plan']:
        from shared_execution import plan_for_case
        for case in sorted(active):
            selected = plan_for_case(manifest['plan'], case)['shared_execution']['knowledge_by_arm']
            paths.extend(path for path in selected.values() if path is not None)
    paths.extend(path for files in schemas.values() for path in files)
    paths.extend(stage.get("environment", {})["KNOWLEDGE_ROOT"] for stage in manifest["stages"]
                 if "KNOWLEDGE_ROOT" in stage.get("environment", {}))
    roots = []
    for path in paths:
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise Refused("quality worker roots must be absolute")
        roots.append(Path(path).resolve())
    if output is not None:
        roots.append(Path(output).resolve())
    inputs, labels, input_hashes, label_hashes = set(), set(), set(), set()
    for case, row in value["cases"].items():
        scope = "source-audit-workflow" if case == "maintenance" else "full-native-workflow"
        if (not isinstance(row, dict) or set(row) != {"scope", "inputs", "private_labels"}
                or row["scope"] != scope):
            raise Refused("quality case must declare its complete measured workflow scope")
        for key, selected in (("inputs", inputs), ("private_labels", labels)):
            files = row[key]
            if not isinstance(files, dict) or not files:
                raise Refused("quality case needs frozen inputs and independent private labels")
            for path, sha in files.items():
                if (not isinstance(path, str) or not Path(path).is_absolute()
                        or not isinstance(sha, str) or len(sha) != 64
                        or set(sha) - set("0123456789abcdef")
                        or manifest["bindings"].get(path) != sha):
                    raise Refused("quality inputs and private labels must be hash-bound")
                selected.add(Path(path).resolve())
                (input_hashes if key == "inputs" else label_hashes).add(sha)
    if inputs & external_paths or input_hashes & external_hashes:
        raise Refused('retained/deferred quality evidence cannot be model inputs')
    if inputs & labels or input_hashes & label_hashes:
        raise Refused("private quality labels cannot be model inputs")
    for label in labels | external_paths:
        if any(label == root or root in label.parents for root in roots):
            raise Refused("private quality labels are inside a worker or output root")
    for stage in manifest["stages"]:
        if any(str(label) in argument for label in labels | external_paths for argument in stage["command"]):
            raise Refused("private quality labels cannot be stage arguments")
    if for_dispatch:
        from init_native_arm import stage_role_error
        from role_profiles import roles
        configured = roles(manifest['plan'])
        from arm_runtime import _workflow_timeout
        for case in sorted(active & {'direct', 'strict'}):
            _workflow_timeout(manifest['plan'], case_id=case, require_explicit=True)
        if 'strict' in active:
            from role_profiles import invocation_role
            for kind in ('harness', 'harness_session'):
                invocation_role(manifest['plan'], name=None, kind=kind, case_id='strict')
        if 'history' in active:
            from role_profiles import role
            role(manifest['plan'], 'pr-reviewer')
        for case in sorted(active):
            problem = stage_role_error(case, configured)
            if problem:
                raise Refused(f'{case}: {problem}')
