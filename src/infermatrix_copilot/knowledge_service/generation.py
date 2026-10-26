"""Curation application binding; dispatch checkpoints stay in the shared runner."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from ..budgeting import copy_native_outcome, native_outcome_unknown
from ..contracts.models import KnowledgeCurationError


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def _pending(message, cause=None):
    error=KnowledgeCurationError(message)
    if cause is not None:
        copy_native_outcome(error,cause)
    error.native_outcome_pending=True
    return error


def generate_proposal(curator,prompt,schema,*,provider,command,model,timeout_seconds):
    from ..providers.json_session import JSONSessionError,run_readonly_json
    from ..process_execution import ProcessCleanupError
    from .curation import _knowledge_files,_model_workspace,_check_unchanged
    originals=_knowledge_files(curator._workspace)
    try:
        with _model_workspace(originals) as snapshot:
            try:
                return run_readonly_json(prompt,cwd=snapshot,command=command,provider=provider,model=model,
                    output_schema=schema,idle_timeout_s=timeout_seconds,absolute_timeout_s=timeout_seconds,
                    output_format="json",allow_sessionless_retry=False,skip_git_repo_check=True,
                    repair_prompt="Your previous response was not a valid knowledge proposal. Do not inspect or modify files. Preserve its rules and evidence, and return only one JSON object with a rules array matching the original schema.",
                    result_validator=lambda data: None if isinstance(data.get("rules"),list)
                    else "proposal must contain a rules array")["payload"]
            except JSONSessionError as exc:
                raise copy_native_outcome(KnowledgeCurationError(str(exc)),exc,
                                          assume_sent=exc.timeout) from exc
            except ProcessCleanupError as exc:
                raise _pending(str(exc),exc) from exc
    finally:
        _check_unchanged(curator._workspace,originals)


def generation_pending(journal,*,scope=None):
    from ..kb_service.quality_review import GENERATION_CHECKPOINT_KEYS,checkpoint_pending
    from ..kb_service.models import ModelUnavailable
    if not isinstance(journal,dict) or set(journal)-{'context','calls'}:
        raise _pending('invalid curation generation checkpoint; reconcile original dispatch')
    context=journal.get('context');calls=journal.get('calls',{})
    if (not isinstance(calls,dict) or (context is not None and not isinstance(context,dict))
            or (calls and context is None) or (context is not None and scope is not None and context.get('scope')!=scope)):
        raise _pending('curation generation scope differs; retain original dispatch')
    pending=False
    for unit,state in calls.items():
        if not isinstance(unit,str) or not isinstance(state,dict):
            raise _pending('invalid curation generation unit')
        try:
            pending |= checkpoint_pending(state,**GENERATION_CHECKPOINT_KEYS)
        except ModelUnavailable as exc:
            raise _pending(str(exc),exc) from exc
        binding=state.get('generation_dispatched_binding')
        if binding is not None and (binding['inputs'].get('context_sha256')!=_digest(context)
                                    or binding['inputs'].get('unit')!=unit):
            raise _pending('curation generation context changed; reconcile original dispatch')
    return pending


def generate_checkpoint(curator,journal,save,prompt,schema,*,unit,scope,provider,command,model,
                        timeout_seconds,generate):
    from ..kb_service.quality_review import GENERATION_CHECKPOINT_KEYS,checkpoint_review
    from ..kb_service.models import ModelUnavailable
    from ..source_fingerprint import package_tree_hash
    from .curation import _knowledge_files
    import jsonschema
    jsonschema.Draft202012Validator.check_schema(schema)
    if type(unit) is not int or unit<0:
        raise ValueError('curation generation unit must be a nonnegative integer')
    if generation_pending(journal,scope=scope):
        raise _pending('curation generation outcome unknown; reconcile original dispatch')
    code=getattr(curator,'_generation_code_sha256',None)
    if code is None:
        code=curator._generation_code_sha256=package_tree_hash(Path(__file__).resolve().parents[1])
    context=json.loads(json.dumps({'scope':scope,'workspace':str(curator._workspace),
        'knowledge':{str(name):hashlib.sha256(data).hexdigest() for name,data in _knowledge_files(curator._workspace).items()},
        'provider':provider,'command':list(command),'model':model,'timeout_seconds':timeout_seconds,
        'schema_sha256':_digest(schema),'callback':generate is not None,'code_sha256':code}))
    if journal.get('context',context)!=context:
        raise _pending('curation generation inputs or release changed; retain the original checkpoint')
    journal.setdefault('context',context)
    state=journal.setdefault('calls',{}).setdefault(str(unit),{})
    binding={'context_sha256':_digest(context),'unit':str(unit),'request_sha256':_digest({'prompt':prompt,'schema':schema})}
    old=state.get('generation_dispatched_binding')
    if old is not None and old['inputs']!=binding:
        raise _pending('original curation generation request changed; reconcile before retry')
    def invoke():
        try:
            data=generate(prompt,schema) if generate is not None else curator.generate_proposal(
                prompt,schema,provider=provider,command=command,model=model,timeout_seconds=timeout_seconds)
            return {'data':data}
        except KnowledgeCurationError as exc:
            raise copy_native_outcome(ModelUnavailable(str(exc),allow_fallback=False),exc) from exc
    try:
        result=checkpoint_review(state,invoke,save,binding=binding,**GENERATION_CHECKPOINT_KEYS)
    except ModelUnavailable as exc:
        raise copy_native_outcome(KnowledgeCurationError(str(exc)),exc) from exc
    return deepcopy(result['data'])
