import hashlib,json
from pathlib import Path
import pytest
from block_arm import load_block
from ledger import Refused

@pytest.mark.parametrize('case', ['foundation','module-card'])
def test_new_prose_case_cannot_silently_become_empty_before_rewrite(tmp_path,case):
    path=tmp_path/(case+'-input.json')
    capsule={'text':'source-grounded prose','page':'repos/toy/rules.md','op':'edit','evidence':[]}
    path.write_text(json.dumps(capsule));manifest=tmp_path/'manifest.json'
    manifest.write_text(json.dumps({'files':{path.name:{'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}}}))
    with pytest.raises(Refused,match='actual prose/prose contract'):load_block(tmp_path,case)
    capsule['op']='prose';path.write_text(json.dumps(capsule))
    manifest.write_text(json.dumps({'files':{path.name:{'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}}}))
    _,block,_=load_block(tmp_path,case)
    assert block.kind=='prose' and block.op=='prose'
