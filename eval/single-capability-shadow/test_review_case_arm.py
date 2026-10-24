import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from test_arm_runtime import arm, configured, settings  # noqa: F401

from review_case_arm import load_case, load_host, review_diff_revisions


def test_candidate_direct_uses_explicit_sandbox_profile_and_standard_worker(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from infermatrix_copilot.sdk.v1 import RepositoryRef
    import infermatrix_copilot.sdk.v1 as sdk
    import arm_runtime
    import durable_worker
    from review_case_arm import candidate_runtime
    calls = []
    native = {'kind': 'sandbox', 'fixture': 'profile-only; never dispatched'}
    @contextmanager
    def compose(overrides, *, shim, for_worker):
        assert for_worker and shim == tmp_path/'shim'
        yield {'native_session_profiles': {'codex': native}, 'strict_backend_model': 'frozen',
               'strict_backend_effort': 'high', 'strict_backend_timeout_s': 300}
    class Runtime:
        def __init__(self, **kwargs): calls.append(kwargs)
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(arm_runtime, 'sdk_runtime', compose)
    monkeypatch.setattr(durable_worker, 'runtime_options', lambda shim: {'worker_prefix': ('standard-worker',)})
    monkeypatch.setattr(sdk, 'ReviewRuntime', Runtime)
    options = SimpleNamespace(shim=tmp_path/'shim', repo=tmp_path/'repo', model='frozen', timeout=900)
    with candidate_runtime(options, RepositoryRef('vllm-omni','vllm-project/vllm-omni')) as (_, config, profile):
        assert profile['execution_config'] is native
        assert profile['idle_timeout_s'] == profile['absolute_timeout_s'] == 300
        assert profile['effort'] == 'high' and config.direct_profiles == {'codex': profile}
        assert config.backend == 'codex'
    assert calls[0]['worker_prefix'] == ('standard-worker',)


def test_candidate_direct_refuses_old_sdk_before_runtime_or_controls(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import infermatrix_copilot.sdk.v1 as sdk
    import arm_runtime
    from ledger import Refused
    from review_case_arm import candidate_runtime
    monkeypatch.setattr(sdk,'provider_descriptor',lambda _:SimpleNamespace(capabilities=()))
    monkeypatch.setattr(arm_runtime,'sdk_runtime',lambda *a,**k:pytest.fail('must refuse before composition'))
    with pytest.raises(Refused,match='scoped structured session'):
        with candidate_runtime(SimpleNamespace(),None):pass


def test_candidate_public_sdk_reserves_frozen_sandbox_without_parent_dispatch(arm, frozen_case, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from infermatrix_copilot.sdk.v1 import RepositoryRef, DirectReviewRequest, DirectReviewRunRequest, ChangedPath
    from review_case_arm import candidate_runtime
    _, calls, shim, plan = arm
    path=shim.parent/'config.json';config=json.loads(path.read_text())
    config['plan'].update(worker_execution='durable',call_identity='agent-step/1')
    path.write_text(json.dumps(config))
    inputs,repo,git=frozen_case
    row=json.loads((inputs/'request.json').read_text());git('remote','add','origin','https://github.com/vllm-project/vllm-omni.git')
    home=tmp_path/'driver';home.mkdir();monkeypatch.chdir(home)
    repository=RepositoryRef('vllm-omni','vllm-project/vllm-omni')
    options=SimpleNamespace(repo=repo,shim=shim,model='fixture',timeout=900)
    review=DirectReviewRequest('control-only',repository,7,row['expected_head_sha'],'frozen','body',
                               (ChangedPath('source.py','modified'),))
    with candidate_runtime(options,repository) as (runtime,configuration,profile):
        monkeypatch.setattr(runtime._core._q,'put',lambda *args:None)
        request=DirectReviewRunRequest(review,str(repo),'control-only-reservation','control only',
                                      {'type':'object'},profile)
        first=runtime.reserve_direct_review(request);second=runtime.reserve_direct_review(request)
        assert first.created and not second.created and first.run_id==second.run_id
        saved=json.loads((Path(configuration.run_root)/first.run_id/'request.json').read_text())
        frozen=saved['params']['direct_review']['profile']
        assert frozen['execution_config']==plan['shared_execution']['profile']
        assert frozen['absolute_timeout_s']==30 and frozen['effort']=='high'
        assert runtime.find_review(request.idempotency_key,expected_head_sha=review.expected_head_sha).run_id==first.run_id
    assert not calls and not (shim.parent.parent/'calls.sqlite').exists()


@pytest.fixture
def frozen_case(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout
    git("init", "-q")
    git("config", "user.name", "offline")
    git("config", "user.email", "offline@example.invalid")
    (repo / "source.py").write_text("x = 1\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD").decode().strip()
    (repo / "source.py").write_text("x = 2\n")
    git("commit", "-qam", "head")
    head = git("rev-parse", "HEAD").decode().strip()
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "request.json").write_text(json.dumps({"publication": "disabled", "base_sha": base, "expected_head_sha": head}))
    (inputs / "source-manifest.json").write_text("{}")
    (inputs / "review.diff").write_bytes(git("diff", "--no-ext-diff", "--binary", base, head))
    manifest = {"files": {p.name: {"sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in inputs.iterdir()}}
    # No labels file: the execution driver must not need adjudication labels.
    manifest["files"]["labels.json"] = {"sha256": "0" * 64}
    (inputs / "manifest.json").write_text(json.dumps(manifest))
    return inputs, repo, git


def test_input_check_does_not_read_labels(frozen_case):
    inputs, repo, _ = frozen_case
    assert load_case(inputs, repo)["publication"] == "disabled"


def test_rejects_tampered_frozen_request(frozen_case):
    inputs, repo, _ = frozen_case
    (inputs / "request.json").write_text("{}")
    with pytest.raises(ValueError, match="frozen input changed"):
        load_case(inputs, repo)


def test_rejects_dirty_source(frozen_case):
    inputs, repo, _ = frozen_case
    (repo / "source.py").write_text("x = 3\n")
    with pytest.raises(ValueError, match="must be clean"):
        load_case(inputs, repo)


def test_rejects_wrong_head(frozen_case):
    inputs, repo, git = frozen_case
    git("checkout", "--detach", "HEAD~1")
    with pytest.raises(ValueError, match="frozen head"):
        load_case(inputs, repo)


def rebind_inputs(inputs, request, diff):
    (inputs / "request.json").write_text(json.dumps(request))
    (inputs / "review.diff").write_bytes(diff)
    manifest = json.loads((inputs / "manifest.json").read_text())
    for name in ("request.json", "review.diff"):
        manifest["files"][name] = {"sha256": hashlib.sha256((inputs / name).read_bytes()).hexdigest()}
    (inputs / "manifest.json").write_text(json.dumps(manifest))


@pytest.fixture
def diverged_case(frozen_case):
    inputs, repo, git = frozen_case
    request = json.loads((inputs / "request.json").read_text())
    merge_base = request["base_sha"]
    head = request["expected_head_sha"]
    git("checkout", "--quiet", "--detach", merge_base)
    (repo / "target-only.py").write_text("unrelated_target_change = True\n")
    git("add", ".")
    git("commit", "-qm", "target branch advances")
    request["base_sha"] = git("rev-parse", "HEAD").decode().strip()
    git("checkout", "--quiet", "--detach", head)
    request.update(diff_semantics="pull-request-merge-base-v1", diff_base_sha=merge_base)
    pr_diff = git("diff", "--no-ext-diff", "--no-textconv", "--binary", merge_base, head)
    assert b"target-only.py" not in pr_diff
    assert b"target-only.py" in git("diff", request["base_sha"], head)
    rebind_inputs(inputs, request, pr_diff)
    return inputs, repo, git, request


def test_pr_diff_excludes_target_branch_changes(diverged_case):
    inputs, repo, _, request = diverged_case
    assert load_case(inputs, repo) == request


def test_legacy_case_retains_exact_revision_evidence(diverged_case):
    inputs, repo, git, request = diverged_case
    del request["diff_semantics"]
    del request["diff_base_sha"]
    exact = git("diff", "--no-ext-diff", "--binary", request["base_sha"], request["expected_head_sha"])
    rebind_inputs(inputs, request, exact)
    assert load_case(inputs, repo) == request


@pytest.mark.parametrize("change, message", [
    ({"diff_semantics": "unknown"}, "unsupported"),
    ({"diff_base_sha": None}, "requires a frozen merge base"),
    ({"diff_base_sha": "0" * 40}, "merge base does not match"),
    ({"diff_semantics": "exact-revisions-v1"}, "exact-revision diff base"),
])
def test_pr_diff_rejects_unbound_semantics_or_base(diverged_case, change, message):
    inputs, repo, _, request = diverged_case
    request.update(change)
    rebind_inputs(inputs, request, (inputs / "review.diff").read_bytes())
    with pytest.raises(ValueError, match=message):
        load_case(inputs, repo)


def test_pr_diff_rejects_two_revision_bytes(diverged_case):
    inputs, repo, git, request = diverged_case
    rebind_inputs(inputs, request, git("diff", "--no-ext-diff", "--binary", request["base_sha"], request["expected_head_sha"]))
    with pytest.raises(ValueError, match="checkout diff does not match"):
        load_case(inputs, repo)


def test_ambiguous_merge_base_cannot_silently_select_one():
    request = {"base_sha": "base", "expected_head_sha": "head",
               "diff_semantics": "pull-request-merge-base-v1", "diff_base_sha": "first"}
    with pytest.raises(ValueError, match="merge base does not match"):
        review_diff_revisions(request, lambda *args: b"first\nsecond\n")


@pytest.fixture
def host_packet(tmp_path):
    case = tmp_path / "case"
    case.mkdir()
    (case / "request.json").write_text('{"review":"frozen"}')
    host = tmp_path / "host"
    host.mkdir()
    packet = {"request_sha256": hashlib.sha256((case / "request.json").read_bytes()).hexdigest(),
              "business_context": {"patch_evidence": {"path": ".evidence/patch.txt"}}}
    (host / "packet.json").write_text(json.dumps(packet))
    (host / "patch.txt").write_text("frozen patch")
    def bind():
        (host / "manifest.json").write_text(json.dumps({"files": {
            name: hashlib.sha256((host / name).read_bytes()).hexdigest()
            for name in ("packet.json", "patch.txt")}}))
    bind()
    return case, host, bind


def test_host_packet_checks_bytes_and_request_identity(host_packet):
    case, host, _ = host_packet
    assert load_host(host, case)["business_context"]["patch_evidence"]["path"] == ".evidence/patch.txt"
    (case / "request.json").write_text("another request")
    with pytest.raises(ValueError, match="different review request"):
        load_host(host, case)


def test_rejects_modified_patch_bytes(host_packet):
    case, host, _ = host_packet
    (host / "patch.txt").write_text("changed evidence")
    with pytest.raises(ValueError, match="frozen host evidence changed"):
        load_host(host, case)


@pytest.mark.parametrize("path", ["../outside", "/absolute/outside"])
def test_rejects_patch_paths_outside_workspace(host_packet, path):
    case, host, bind = host_packet
    packet = json.loads((host / "packet.json").read_text())
    packet["business_context"]["patch_evidence"]["path"] = path
    (host / "packet.json").write_text(json.dumps(packet))
    bind()
    with pytest.raises(ValueError, match="must be contained"):
        load_host(host, case)
