"""One preview reader rejects corruption without regeneration or state rewrites."""
from dataclasses import asdict
import json
import os
from pathlib import Path

import pytest

from infermatrix_copilot.kb_service.init_support import InitError, InitPublisher, InitRecord, save_prepared
from infermatrix_copilot.kb_service.preview import file_hashes, read_preview
from infermatrix_copilot.kb_service.init_stages import _dry_run_files, run_stage
from test_kb_init_knowledge import KnowledgeGateway, _chain
from test_kb_init_modules import _modules_lifecycle
from test_kb_init_skeleton import _runtime, world  # noqa: F401


def preview(tmp_path, *, legacy=False, files=None):
    files = files if files is not None else {'knowledge/page.md':'# Unicode 页面\n'}
    folder = tmp_path / 'knowledge-dryrun'
    InitPublisher.dry_run(folder, files, title='preview', body='checked body')
    record = InitRecord('knowledge','toy',kb_base_sha='base',started_at=12.0,
                        status='dry_run',files=sorted(files),pr={'dry_run_dir':str(folder)})
    if legacy:
        save_prepared(tmp_path/'knowledge-publish.json',base_sha='base',branch='b',files=files,
                      title='preview',body='checked body',author=('a','b'),when=12.0)
    else:
        record.pr['checked_files_sha256'] = file_hashes(files)
    return record, folder/'tree'


@pytest.mark.parametrize('legacy',[False,True])
def test_valid_preview_and_deleted_files_keep_original_receipt_shape(tmp_path, legacy):
    files={'knowledge/page.md':'# Unicode 页面\n','knowledge/old.md':None}
    record, root=preview(tmp_path,legacy=legacy,files=files)
    before=json.dumps(asdict(record),sort_keys=True)
    assert read_preview(record)==files
    assert _dry_run_files(record)=={'knowledge/page.md':'# Unicode 页面\n'}
    assert json.dumps(asdict(record),sort_keys=True)==before
    if legacy:
        with pytest.raises(InitError,match='no checked receipt'):
            read_preview(record,require_checked=True)


@pytest.mark.parametrize('damage',['edit','remove','add','symlink','fifo'])
def test_preview_corruption_refuses_without_repair(tmp_path,damage):
    record,root=preview(tmp_path)
    target=root/'knowledge/page.md'
    if damage=='edit':target.write_text('invalid modified content')
    elif damage=='remove':target.unlink()
    elif damage=='add':(root/'extra.md').write_text('# Not reviewed\n')
    elif damage=='symlink':
        outside=tmp_path/'outside.md';outside.write_text('# Unicode 页面\n')
        target.unlink();target.symlink_to(outside)
    else:
        if not hasattr(os,'mkfifo'):pytest.skip('FIFO unavailable')
        target.unlink();os.mkfifo(target)
    before=json.dumps(asdict(record),sort_keys=True)
    with pytest.raises(InitError,match='preview'):
        read_preview(record)
    assert json.dumps(asdict(record),sort_keys=True)==before
    if damage=='remove':
        assert not target.exists()


def test_receipts_check_file_set_and_deleted_marker_content(tmp_path):
    record,root=preview(tmp_path,files={'knowledge/old.md':None})
    (root/'knowledge/old.md.DELETED').write_text('changed marker')
    with pytest.raises(InitError,match='deletion marker'):read_preview(record)
    (root/'knowledge/old.md.DELETED').write_text('')
    record.files=[]
    with pytest.raises(InitError,match='file set'):read_preview(record)


def test_legacy_prepared_identity_cannot_be_rebound(tmp_path):
    record,_=preview(tmp_path,legacy=True)
    record.kb_base_sha='other'
    with pytest.raises(InitError,match='another stage input'):read_preview(record)


@pytest.mark.parametrize('damage',['edit','remove','add'])
def test_finished_foundation_cache_rejects_tampering_without_new_calls(world,damage):
    gateway=KnowledgeGateway();_chain(world,gateway)
    rt=_runtime(world,gateway)
    record=run_stage(rt,_modules_lifecycle(),'knowledge',dry_run=True)
    assert record.status=='dry_run',record.problems
    state=InitRecord.path(rt.state_dir,'toy','knowledge')
    state_before=state.read_bytes();calls=len(gateway.calls)
    root=Path(record.pr['dry_run_dir'])/'tree'
    target=next(root.rglob('knowledge-core.md'))
    if damage=='edit':target.write_text('BROKEN CACHE PROBE\n')
    elif damage=='remove':target.unlink()
    else:(root/'not-reviewed.md').write_text('# Unexpected\n')
    try:
        result=run_stage(rt,_modules_lifecycle(),'knowledge',dry_run=True)
    except InitError as exc:assert 'preview' in str(exc)
    else:assert result.status=='blocked' and any('preview' in p for p in result.problems)
    assert len(gateway.calls)==calls and state.read_bytes()==state_before


@pytest.mark.parametrize('files',[{'../escape': 'x'},{'.':'x'},{'x':None,'x.DELETED':'collision'},{'x':42}])
def test_invalid_new_preview_cannot_erase_original_snapshot(tmp_path,files):
    record,root=preview(tmp_path)
    target=root/'knowledge/page.md';before=target.read_bytes()
    with pytest.raises(InitError):
        InitPublisher.dry_run(root.parent,files,title='invalid',body='not checked')
    assert target.read_bytes()==before


def test_legacy_windows_newlines_keep_original_text_hash_contract(tmp_path):
    record,root=preview(tmp_path)
    (root/'knowledge/page.md').write_bytes('# Unicode 页面\r\n'.encode())
    assert read_preview(record)=={'knowledge/page.md':'# Unicode 页面\n'}


def test_producer_windows_newlines_round_trip_as_logical_text(tmp_path):
    record,_=preview(tmp_path,files={"knowledge/page.md":"# Page\r\n"})
    assert read_preview(record)=={"knowledge/page.md":"# Page\n"}


def test_owner_review_reads_untrusted_edits_without_changing_approval(tmp_path):
    record,root=preview(tmp_path)
    original=dict(record.pr["checked_files_sha256"])
    (root/"knowledge/page.md").write_text("# Owner changed this\n")
    (root/"knowledge/moved.md").write_text("# Owner moved this\n")
    assert read_preview(record,owner_review=True)=={
        "knowledge/page.md":"# Owner changed this\n", "knowledge/moved.md":"# Owner moved this\n"}
    assert record.pr["checked_files_sha256"]==original
    with pytest.raises(InitError,match="preview"):
        read_preview(record)


@pytest.mark.parametrize("damage", ["symlink", "fifo"])
def test_owner_review_keeps_canonical_containment_and_regular_file_checks(tmp_path,damage):
    record,root=preview(tmp_path)
    target=root/"knowledge/page.md";target.unlink()
    if damage=="symlink":
        outside=tmp_path/"outside.md";outside.write_text("outside");target.symlink_to(outside)
    else:
        if not hasattr(os,"mkfifo"):pytest.skip("FIFO unavailable")
        os.mkfifo(target)
    with pytest.raises(InitError,match="preview"):
        read_preview(record,owner_review=True)


def _edited_skeleton_chain(world):
    gateway=KnowledgeGateway();_chain(world,gateway)
    runtime=_runtime(world,gateway)
    skeleton=InitRecord.load(runtime.state_dir,"toy","skeleton")
    target=Path(skeleton.pr["dry_run_dir"])/"tree/knowledge/repos/toy/_routes.yaml"
    target.write_text(target.read_text()+"\n# Owner input changed\n")
    return gateway,runtime


def test_new_draft_identity_binds_edited_predecessor_bytes(world):
    gateway,runtime=_edited_skeleton_chain(world)
    previous=InitRecord.load(runtime.state_dir,"toy","modules")
    calls=len(gateway.calls)
    current=run_stage(runtime,_modules_lifecycle(),"modules",dry_run=True)
    assert current.status=="dry_run",current.problems
    assert current.inputs_digest!=previous.inputs_digest and len(gateway.calls)>calls


@pytest.mark.parametrize("pending",["budget","quality"])
def test_changed_owner_input_cannot_replace_unresolved_previous_call(world,pending):
    gateway,runtime=_edited_skeleton_chain(world)
    previous=InitRecord.load(runtime.state_dir,"toy","modules")
    previous.status="blocked"
    if pending=="budget":
        previous.budget_calls["f"*32]={"status":"reserved","reserved_usd":0.01,
                                      "accounted_usd":0.01,"accounting":"fixed"}
    else:
        previous.coverage.setdefault("quality_reviews",{})["unknown"]={"review_dispatched":True}
    state=previous.save(runtime.state_dir);before=state.read_bytes();calls=len(gateway.calls)
    with pytest.raises(InitError,match="unresolved model call"):
        run_stage(runtime,_modules_lifecycle(),"modules",dry_run=True)
    assert state.read_bytes()==before and len(gateway.calls)==calls


def test_original_upstream_preview_is_untrusted_input_without_fabricating_receipt(tmp_path):
    record,_=preview(tmp_path,files={"knowledge/page.md":"# Unicode 页面\n", "knowledge/old.md":None})
    record.pr.pop("checked_files_sha256")
    before=json.dumps(asdict(record),sort_keys=True)
    assert read_preview(record,owner_review=True)=={"knowledge/page.md":"# Unicode 页面\n"}
    assert json.dumps(asdict(record),sort_keys=True)==before
    with pytest.raises(InitError):read_preview(record)
    with pytest.raises(InitError,match="cannot be treated as a checked"):
        read_preview(record,owner_review=True,require_checked=True)


def test_original_discovery_hashes_validate_without_prepared_publication(tmp_path):
    files={"adapters/toy/knowledge-coverage.yaml":"features: []\n",
           "eval/feature-discovery/toy.json":"{}\n"}
    record,root=preview(tmp_path,files=files);hashes=record.pr.pop("checked_files_sha256")
    record.stage="feature-discovery"
    record.discovery={"report_path":"eval/feature-discovery/toy.json",
                      "report_sha256":hashes["eval/feature-discovery/toy.json"],
                      "catalog_sha256":hashes["adapters/toy/knowledge-coverage.yaml"]}
    before=json.dumps(asdict(record),sort_keys=True)
    assert read_preview(record)==files
    assert json.dumps(asdict(record),sort_keys=True)==before
    with pytest.raises(InitError,match="no checked receipt"):
        read_preview(record,require_checked=True)
    (root/"eval/feature-discovery/toy.json").write_text("unchecked")
    with pytest.raises(InitError,match="preview"):
        read_preview(record)
