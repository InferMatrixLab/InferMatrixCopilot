"""Native Codex policy data; execution remains in the existing adapter.

The host freezes the complete native catalog. Keep model ordering, priorities,
instructions, context, inference and reasoning metadata; only tool-selection
fields change. Default model selection remains native rather than invented.
"""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json

from ..sandbox.inputs import normalize_inputs

VERIFIED_VERSION = 'codex-cli 0.162.1'
EFFORTS = ('minimal', 'low', 'medium', 'high', 'xhigh')
CATALOG_PATH = '/tmp/inputs/codex-models.json'
TOOL_METADATA = {
    'apply_patch_tool_type': None,
    'experimental_supported_tools': [],
    'supports_search_tool': False,
    'tool_mode': 'direct',
    'multi_agent_version': 'disabled',
}
DISABLED_FEATURES = (
    'multi_agent_v2', 'code_mode', 'code_mode_only', 'apps', 'shell_tool',
    'unified_exec', 'browser_use', 'browser_use_external',
    'browser_use_full_cdp_access', 'computer_use', 'image_generation',
    'view_image', 'plugins', 'remote_plugin', 'skill_search',
    'skill_mcp_dependency_install', 'tool_suggest', 'sleep_tool', 'goals',
    'hooks', 'workspace_dependencies', 'multi_agent',
)
REQUIREMENT_FALSE_FLAGS = ('allowBrowserAndComputerUse', 'allowAppshots', 'allowRemoteControl')
REQUIREMENT_FIELDS = (*REQUIREMENT_FALSE_FLAGS, 'allowedWebSearchModes', 'featureRequirements', 'allowedLoginMethods')


def requirements_view(value):
    """Retain only policy facts; opaque administrator values remain private."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError('invalid native requirements response')
    active = sorted(key for key, item in value.items() if item is not None)
    values = {key: deepcopy(value[key]) for key in REQUIREMENT_FIELDS if key in active}
    for key, item in values.items():
        if (key in REQUIREMENT_FALSE_FLAGS and type(item) is not bool
                or key == 'featureRequirements' and (not isinstance(item, dict)
                    or any(type(required) is not bool for required in item.values()))
                or key == 'allowedWebSearchModes' and (not isinstance(item, list)
                    or any(mode not in ('disabled', 'cached', 'live', 'indexed') for mode in item))
                or key == 'allowedLoginMethods' and (not isinstance(item, list)
                    or any(method not in ('api', 'chatgpt') for method in item))):
            raise ValueError('invalid native managed requirement facts')
    return {'active_fields': active, 'values': values}


def validate_requirements(proof):
    if proof.get('requirements_checked') is not True:
        raise ValueError('native managed requirements were not checked')
    if proof.get('requirements_empty') is True:
        if proof.get('requirements') is not None:
            raise ValueError('inconsistent native managed requirements')
        return
    requirements = proof.get('requirements')
    if not isinstance(requirements, dict):
        raise ValueError('native managed requirements are unavailable')
    fields, values = requirements.get('active_fields'), requirements.get('values')
    if (not isinstance(fields, list) or not fields or any(not isinstance(key, str) for key in fields)
            or not isinstance(values, dict)
            or len(set(fields)) != len(fields) or set(fields) != set(values)
            or any(key not in REQUIREMENT_FIELDS for key in fields)):
        raise ValueError('native managed requirements contain unsupported constraints')
    for key, value in values.items():
        # Native 0.162.1 includes this unrestricted auth baseline whenever it
        # returns any requirements. A narrower login contract needs admission
        # parity and remains unsupported here.
        if key == 'allowedLoginMethods' and (not isinstance(value, list)
                or len(value) != 2 or set(value) != {'api', 'chatgpt'}):
            raise ValueError('native managed login requirements are not verified')
        if key in REQUIREMENT_FALSE_FLAGS and value is not False:
            raise ValueError('native managed requirements conflict with tool restrictions')
        if key == 'allowedWebSearchModes' and (not isinstance(value, list)
                or not value or any(mode not in ('disabled', 'cached', 'live', 'indexed') for mode in value)
                or 'disabled' not in value):
            raise ValueError('native managed requirements do not permit disabled web search')
        if key == 'featureRequirements' and (not isinstance(value, dict) or not value
                or any(feature not in DISABLED_FEATURES or required is not False
                       for feature, required in value.items())):
            raise ValueError('native managed feature requirements are not compatible')


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def validate_effort(value):
    if value and value not in EFFORTS:
        raise ValueError(f'codex reasoning effort must be one of {EFFORTS}: {value!r}')


def normalize_policy(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {'kind', 'version', 'catalog'}
            or value['kind'] != 'codex-restricted' or value['version'] != VERIFIED_VERSION):
        raise ValueError('unsupported native Codex completion policy')
    catalog = value['catalog']
    models = catalog.get('models') if isinstance(catalog, dict) else None
    if not isinstance(models, list) or not models or any(not isinstance(model, dict) for model in models):
        raise ValueError('native policy requires the original model catalog')
    names = [model.get('slug') for model in models]
    if any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
        raise ValueError('native policy requires unique native model identities')
    # One capacity/UTF-8 implementation owns both host and worker constraints.
    normalize_inputs({'files': {CATALOG_PATH: canonical(catalog)}, 'sealed_dirs': ['/etc/codex']})
    return json.loads(canonical(value))


@dataclass(frozen=True)
class CodexCompletionPolicy:
    original_json: str
    catalog_json: str
    model_names: tuple[str, ...]
    legacy_inputs: bool = False

    @classmethod
    def build(cls, value, *, legacy_inputs=False):
        value = normalize_policy(value)
        if value is None:
            raise ValueError('native policy is missing')
        catalog = deepcopy(value['catalog'])
        for model in catalog['models']:
            model.update(deepcopy(TOOL_METADATA))
        return cls(canonical(value['catalog']), canonical(catalog),
                   tuple(model['slug'] for model in catalog['models']), legacy_inputs)

    @property
    def identity(self):
        return {'version': VERIFIED_VERSION,
                'source_catalog_sha256': hashlib.sha256(self.original_json.encode()).hexdigest(),
                'catalog_sha256': hashlib.sha256(self.catalog_json.encode()).hexdigest()}

    def inputs(self):
        # Original schema-1 results retain their original directory projection.
        # New executions preserve requirements.toml and legacy managed policy.
        return normalize_inputs({'files': {CATALOG_PATH: self.catalog_json},
            'sealed_dirs': ['/etc/codex'] if self.legacy_inputs else [],
            **({} if self.legacy_inputs else {'sealed_files': ['/etc/codex/config.toml']})})

    def arguments(self, *, model='', effort=''):
        if model and model not in self.model_names:
            raise ValueError('requested model is absent from the original native catalog')
        arguments = ['-c', 'model_catalog_json=' + json.dumps(CATALOG_PATH),
                     '-c', 'web_search="disabled"', '-c', 'tools.update_plan.enabled=false',
                     '-c', 'tools.experimental_request_user_input.enabled=false']
        for feature in DISABLED_FEATURES:
            arguments.extend(('--disable', feature))
        if model:
            arguments.extend(('-c', 'model=' + json.dumps(model)))
        if effort:
            validate_effort(effort)
            arguments.extend(('-c', 'model_reasoning_effort=' + json.dumps(effort)))
        return arguments

    def validate_configuration(self, proof, *, model='', effort='', allowed_mcp=()):
        effective = proof.get('effective') if isinstance(proof, dict) else None
        if (not isinstance(effective, dict) or proof.get('version') != VERIFIED_VERSION
                or proof.get('methods_sent') != ['initialize', 'config/read', 'configRequirements/read']
                or effective.get('model_catalog_json') != CATALOG_PATH
                or effective.get('model') != (model or None)
                or effort and effective.get('model_reasoning_effort') != effort
                or effective.get('configured_startup_keys')
                or effective.get('web_search') != 'disabled'):
            raise ValueError('effective native configuration differs from the requested policy')
        validate_requirements(proof)
        features, tools, servers = effective.get('features'), effective.get('tools'), effective.get('mcp_servers')
        if (not isinstance(features, dict) or any(features.get(name) is not False for name in DISABLED_FEATURES)
                or not isinstance(tools, dict) or tools.get('update_plan') is not False
                or tools.get('experimental_request_user_input') is not False
                or not isinstance(servers, dict)
                or {name for name, server in servers.items() if server.get('enabled') is not False} != set(allowed_mcp)):
            raise ValueError('effective native tool controls are not verified')
