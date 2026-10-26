"""Display provenance cannot create a second copy of the same rule behavior."""
from infermatrix_copilot.kb_service.init_history import _body_key
from infermatrix_copilot.kb_service.init_quick_maps import with_rule_sources


def test_new_policy_compares_source_annotated_rules_to_legacy_bodies():
    body = '- 触发：VersionedMap.apply changes\n- 强制：Preserve values and revision on both failures.\n'
    first = with_rule_sources(body, ['pkg/revisions.py'])
    second = with_rule_sources(body, ['pkg/another.py'])
    assert _body_key(first, ignore_sources=True) == _body_key(second, ignore_sources=True) == _body_key(body)
    assert _body_key(first) != _body_key(body)  # Legacy comparison remains literal.


def test_source_like_behavior_and_code_examples_remain_part_of_the_key():
    body = '- 强制：Retain this exact example.\n\n```text\n- 源码： `pkg/a.py`\n```\n'
    assert _body_key(body, ignore_sources=True) == body.strip()
    prose = '- 源码： `pkg/a.py` must stay visible to the caller.\n'
    assert _body_key(prose, ignore_sources=True) == prose.strip()
    assert _body_key(body.replace('pkg/a.py', 'pkg/B.py'), ignore_sources=True) != _body_key(body, ignore_sources=True)


def test_inline_paths_symbols_and_literals_keep_case_and_spacing():
    body = '- 强制：`pkg/A.py::Value` returns "A  B".\n'
    linked = with_rule_sources(body, ['pkg/other.py'])
    assert _body_key(linked, ignore_sources=True) == body.strip()
    for changed in [body.replace('pkg/A.py', 'pkg/a.py'), body.replace('A  B', 'A B')]:
        assert _body_key(changed, ignore_sources=True) != _body_key(linked, ignore_sources=True)
