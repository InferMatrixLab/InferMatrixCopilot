from copy import deepcopy
import json
import pytest
from jsonschema import Draft202012Validator
from infermatrix_copilot.providers.schema_projection import codex_wire_schema, schema_inputs, SCHEMA_PATH

OLD = '^(?:$|(?:issue_comment|inline_comment|review_summary):[1-9][0-9]*)$'

@pytest.mark.parametrize('value',['','issue_comment:1','inline_comment:9','review_summary:42',
 'issue_comment:0','inline_comment:01','foo:1','x\n'])
def test_wire_conversion_preserves_acceptance(value):
 original={'type':'string','pattern':OLD,'maxLength':500}
 wire=codex_wire_schema(original)
 assert wire['pattern']!=OLD and wire['maxLength']==500
 assert Draft202012Validator(original).is_valid(value)==Draft202012Validator(wire).is_valid(value)
 assert original['pattern']==OLD


def test_only_schema_positions_are_encoded_without_touching_data():
 original={'type':'object','properties':{'pattern':{'type':'string','pattern':'^(?:$|x)$'}},
  'default':{'pattern':'^(?:$|data)$'},'examples':[{'pattern':'^(?:$|example)$'}],
  'enum':[{'pattern':'^(?:$|enum)$'}],'$defs':{'x':{'pattern':'^(?:$|a|b)$'}},
  'dependencies':{'x':['y'],'z':{'properties':{'x':{'pattern':'^(?:$|nested)$'}}}}}
 before=deepcopy(original);wire=codex_wire_schema(original)
 assert original==before
 assert wire['properties']['pattern']['pattern']=='^(?:x)?$'
 assert wire['$defs']['x']['pattern']=='^(?:a|b)?$'
 assert wire['dependencies']['z']['properties']['x']['pattern']=='^(?:nested)?$'
 assert wire['dependencies']['x']==['y']
 for key in ('default','examples','enum'):assert wire[key]==original[key]
 assert codex_wire_schema(wire)==wire


@pytest.mark.parametrize('schema',[True,False,{'pattern':'^x$'},{'pattern':'(?:$|x)'}])
def test_unaffected_schemas_keep_exact_serialization(schema):
 assert json.dumps(codex_wire_schema(schema))==json.dumps(schema)


def test_original_intent_bytes_and_projection_are_distinct_immutable_values():
 schema={'type':'string','pattern':'^(?:$|x)$'}
 frozen={'files':{'/tmp/inputs/source':'data'},'sealed_dirs':[],'directories':['/tmp/inputs/empty']}
 before=deepcopy(frozen);original=schema_inputs(frozen,schema)
 assert frozen==before and original['files'][SCHEMA_PATH]==json.dumps(schema)
 wire=schema_inputs(original,schema,provider='codex')
 assert original['files'][SCHEMA_PATH]==json.dumps(schema)
 assert wire['files'][SCHEMA_PATH]==json.dumps(codex_wire_schema(schema))
 assert wire['directories']==['/tmp/inputs/empty']
 assert schema_inputs(wire,schema,provider='codex')==wire


def test_conflicting_reserved_schema_cannot_be_replaced():
 with pytest.raises(ValueError,match='differs'):
  schema_inputs({'files':{SCHEMA_PATH:'{}'},'sealed_dirs':[]},{'type':'string'},provider='codex')


def test_legacy_host_and_sandbox_use_same_wire_encoder(tmp_path):
    import sys
    from infermatrix_copilot.providers.json_session import run_readonly_json
    script=tmp_path/'native.py'
    script.write_text('''import json,pathlib,sys
schema=json.loads(pathlib.Path(sys.argv[sys.argv.index('--output-schema')+1]).read_text())
assert schema['properties']['value']['pattern']=='^(?:x)?$'
pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text('{"value":"x"}')
print('{"type":"turn.completed"}')
''')
    schema={'type':'object','properties':{'value':{'type':'string','pattern':'^(?:$|x)$'}},'required':['value']}
    before=deepcopy(schema)
    result=run_readonly_json('evidence',cwd=tmp_path,command=[sys.executable,str(script)],provider='codex',
        output_schema=schema,idle_timeout_s=5,absolute_timeout_s=10)
    assert result['payload']=={'value':'x'} and schema==before


def test_original_intent_projection_keeps_legacy_serialization_exact():
    from infermatrix_copilot.sandbox.inputs import merge_inputs
    from infermatrix_copilot.providers.schema_projection import schema_inputs
    schema={'type':'object','properties':{'value':{'pattern':'^(?:$|é)$'}}}
    source={'files':{'/tmp/inputs/source':'é'},'sealed_dirs':[]}
    legacy=merge_inputs(source,{'files':{SCHEMA_PATH:json.dumps(schema)},'sealed_dirs':[]})
    assert schema_inputs(source,schema)==legacy
