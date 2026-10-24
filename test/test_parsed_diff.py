"""The same parsed paths and changes drive planning and publication anchors."""
from infermatrix_copilot.review.diff import parse_diff, diff_index
from infermatrix_copilot.review.planner import diff_signals


def test_git_quoted_utf8_paths_match_planning_and_anchor_identity():
    quoted = r'"b/docs/\347\237\245\350\257\206 guide.md"'
    diff = ('diff --git ' + quoted.replace('b/', 'a/', 1) + ' ' + quoted + '\n'
            '--- ' + quoted.replace('b/', 'a/', 1) + '\n+++ ' + quoted + '\n'
            '@@ -1 +1 @@\n-old\n+new\n')
    path = 'docs/知识 guide.md'
    assert diff_signals(diff).files == (path,)
    assert diff_index(diff)[path].right == [[(1, 'new')]]
    assert parse_diff(diff)[0].raw == diff


def test_rename_keeps_both_planning_paths_and_only_new_side_anchor():
    diff = ('diff --git a/old.py b/new.py\nrename from old.py\nrename to new.py\n'
            '--- a/old.py\n+++ b/new.py\n@@ -3 +7 @@\n-old\n+new\n')
    item, = parse_diff(diff)
    assert item.kind == 'renamed'
    assert diff_signals(diff).files == ('old.py', 'new.py')
    assert set(diff_index(diff)) == {'new.py'}
    assert item.hunks[0].lines[0].old_line == 3
    assert item.hunks[0].lines[1].new_line == 7


def test_deleted_binary_and_mode_only_metadata_preserve_exact_excerpts():
    deleted = 'diff --git a/gone b/gone\ndeleted file mode 100644\nBinary files a/gone and /dev/null differ\n'
    mode = 'diff --git a/script b/script\nold mode 100644\nnew mode 100755\n'
    first, second = parse_diff(deleted + mode)
    assert (first.kind, first.binary, first.new_path) == ('deleted', True, '')
    assert (second.old_mode, second.new_mode) == ('100644', '100755')
    assert first.raw == deleted and second.raw == mode
    assert diff_signals(deleted + mode).files == ('gone', 'script')
    assert 'gone' not in diff_index(deleted + mode)


def test_header_shaped_source_lines_are_changes_inside_a_hunk():
    diff = '--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n--- text\n+++ text\n'
    assert diff_signals(diff).files == ('a.py',)
    assert (diff_signals(diff).insertions, diff_signals(diff).deletions) == (1, 1)
    assert diff_index(diff)['a.py'].right == [[(1, '++ text')]]


def test_missing_or_excess_hunk_lines_cannot_prove_publication_anchors():
    for patch in ('@@ -1,2 +1,2 @@\n-old\n+new\n',
                  '@@ -1 +1 @@\n-old\n+new\n+excess\n',
                  '@@ broken +1 @@\n+new\n', '+no hunk\n'):
        index = diff_index('--- a/a.py\n+++ b/a.py\n' + patch)['a.py']
        assert index.state == 'incomplete'


def test_missing_whole_hunk_is_not_claimed_detectable():
    index = diff_index('--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n')['a.py']
    assert index.state == 'no_clipping_detected'  # never "complete"


def test_plain_unified_files_keep_separate_hunks_and_exact_excerpts():
    first = '--- a/first\n+++ b/first\n@@ -1 +1 @@\n-old\n+new\n'
    second = '--- a/second\n+++ b/second\n@@ -1 +1 @@\n-before\n+after\n'
    assert [item.raw for item in parse_diff(first + second)] == [first, second]
    assert set(diff_index(first + second)) == {'first', 'second'}
