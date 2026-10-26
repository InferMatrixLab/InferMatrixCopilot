"""Checked sources survive placement and future map refresh without init state."""
import pytest
from infermatrix_copilot.kb_service.init_content import apply_rules
from infermatrix_copilot.kb_service.init_quick_maps import (
    render_quick_map, refresh_quick_maps, with_quick_map, with_rule_sources,
)
from infermatrix_copilot.knowledge_service.lifecycle import LifecycleError, Page
from infermatrix_copilot.knowledge_service.ops import KnowledgeOperation

PAGE = 'repos/toy/rules.md'
ROUTES = ('schema_version: 1\nowners:\n- owner: core\n  path: repos/toy/rules.md\n'
          '  signals: [engine, tick]\n  scope_prefixes: [pkg/]\n')
RULE = ('## TOY-I3 — Preserve failed writes\n\n- 触发：When changing VersionedMap.apply\n'
        '- 强制：Check revision before capacity and leave both values and revision unchanged on failure.\n')


def test_sources_are_durable_and_capacity_uses_the_same_rule_text():
    section = with_rule_sources(RULE, ['pkg/revisions.py', 'pkg/revisions.py'])
    assert section.count('`pkg/revisions.py`') == 1
    assert with_rule_sources(section, ['pkg/revisions.py']) == section
    files = apply_rules({}, [KnowledgeOperation('add', PAGE, 'TOY-I3', section)],
                        titles={PAGE: 'Core rules'}, tags=['toy'], today='2026-10-17',
                        release='init-pinned', routes_text=ROUTES, include_quickmaps=True)
    files, _, problems = refresh_quick_maps(files, {}, ROUTES, briefing_docs=())
    assert not problems
    row = next(l for l in files[PAGE].splitlines() if '| TOY-I3 |' in l)
    assert row.endswith('| `pkg/revisions.py` |')
    before = files[PAGE]
    # A later stage has no previous init record, adds a different rule and
    # rebuilds the map from durable page content only.
    other = '## TOY-I4 — Another rule\n\n- 强制：Keep the second independently reviewed contract unchanged across repeated calls.\n'
    files = apply_rules(files, [KnowledgeOperation('add', PAGE, 'TOY-I4', other)],
                        titles={}, tags=['toy'], today='2026-10-18', release='init-next',
                        routes_text=ROUTES, include_quickmaps=True)
    files, _, problems = refresh_quick_maps(files, {PAGE: before}, ROUTES, briefing_docs=())
    assert not problems and row in files[PAGE]
    assert Page.parse(files[PAGE]).rule('TOY-I3').text.startswith(Page.parse(before).rule('TOY-I3').text)
    assert Page.parse(files[PAGE]).rule('TOY-I3').footer.since == 'init-pinned'


def test_existing_inline_sources_keep_their_exact_bytes():
    section = RULE + '\n- 验收：Inspect `pkg/revisions.py::VersionedMap.apply`.\n'
    assert with_rule_sources(section, ['pkg/revisions.py']) == section


@pytest.mark.parametrize('path', ['runner.py', 'Makefile', 'pkg/source name.py', 'pkg/a|b.py'])
def test_explicit_checked_sources_include_root_files_and_escape_table_pipes(path):
    section = with_rule_sources(RULE, [path])
    rendered = render_quick_map(section, signals=['engine'], prefixes=['pkg/'])
    row = next(l for l in rendered.splitlines() if '| TOY-I3 |' in l)
    assert '`' + path.replace('|', '\\|') + '`' in row
    assert '触发词：engine。' in rendered


@pytest.mark.parametrize('path', ['../outside.py', '/outside.py', 'pkg/a`b.py', 'pkg/a\nb.py',
                                 ' source.py', 'source.py '])
def test_unrepresentable_sources_are_not_rendered_as_invented_paths(path):
    with pytest.raises(LifecycleError, match='source path'):
        with_rule_sources(RULE, [path])


def test_handwritten_map_is_never_replaced():
    manual = '## Manual Direct map\n\nUse the maintainer entrypoint.\n'
    page = '# Core\n\n' + manual + '\n' + with_rule_sources(RULE, ['pkg/revisions.py'])
    assert with_quick_map(page, render_quick_map(page, signals=[], prefixes=[])) == page
