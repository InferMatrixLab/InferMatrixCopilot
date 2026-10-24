"""One schema-file projection; business acceptance keeps its original schema."""
from copy import deepcopy
import json
from ..sandbox.inputs import merge_inputs, normalize_inputs

SCHEMA_PATH = '/tmp/inputs/readonly-schema.json'
_MAPS = ('properties', 'patternProperties', '$defs', 'definitions', 'dependentSchemas')
_LISTS = ('allOf', 'anyOf', 'oneOf', 'prefixItems')
_VALUES = ('additionalProperties', 'unevaluatedProperties', 'additionalItems', 'unevaluatedItems',
           'contains', 'propertyNames', 'not', 'if', 'then', 'else', 'contentSchema')


def codex_wire_schema(schema):
    """Encode anchored empty alternatives as equivalent optional groups.

    Visit schema positions only. Defaults/examples/enums are literal data and
    are preserved; host validation still uses the unmodified original schema.
    """
    def rewrite(value):
        if not isinstance(value, dict):
            return value
        result = deepcopy(value)
        pattern = result.get('pattern')
        if isinstance(pattern, str) and pattern.startswith('^(?:$|') and pattern.endswith(')$'):
            result['pattern'] = '^(?:' + pattern[len('^(?:$|'):-2] + ')?$'
        for key in _MAPS:
            if isinstance(result.get(key), dict):
                result[key] = {name: rewrite(child) for name, child in result[key].items()}
        for key in _LISTS:
            if isinstance(result.get(key), list):
                result[key] = [rewrite(child) for child in result[key]]
        for key in (*_VALUES, 'items'):
            if isinstance(result.get(key), (dict, bool)):
                result[key] = rewrite(result[key])
        if isinstance(result.get('items'), list):
            result['items'] = [rewrite(child) for child in result['items']]
        if isinstance(result.get('dependencies'), dict):
            result['dependencies'] = {name: rewrite(child) if isinstance(child, (dict, bool)) else child
                                      for name, child in result['dependencies'].items()}
        return result
    return rewrite(deepcopy(schema))


def schema_json(schema, *, provider=None, ensure_ascii=True):
    """One serializer for original intent and provider wire representations."""
    value = codex_wire_schema(schema) if provider == 'codex' else schema
    return json.dumps(value, ensure_ascii=ensure_ascii)


def schema_inputs(inputs, schema, *, provider=None):
    """Own the reserved schema path and preserve original request-byte identity."""
    original = schema_json(schema)
    encoded = schema_json(schema, provider=provider)
    value = normalize_inputs(inputs)
    if value and SCHEMA_PATH in value['files']:
        if value['files'][SCHEMA_PATH] not in (original, encoded):
            raise ValueError('frozen schema input differs from the structured request')
        value = deepcopy(value)
        value['files'][SCHEMA_PATH] = encoded
        return normalize_inputs(value)
    return merge_inputs(value, {'files': {SCHEMA_PATH: encoded}, 'sealed_dirs': []})
