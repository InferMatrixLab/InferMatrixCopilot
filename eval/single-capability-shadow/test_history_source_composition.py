"""History input policies preserve legacy fixtures and exact new PR evidence."""
from types import SimpleNamespace

import pytest

from init_fixture_policies import history_world


def test_default_history_policy_keeps_original_formatter_translation():
    source = SimpleNamespace(history_evidence=lambda repo, number: {
        "number": number, "files": [{"filename": "pkg/util.py", "patch": "+old"}], "threads": ["retained"]})
    gateway = object();world = {}
    assert history_world({"_setup": lambda actual: (gateway, source)}, world) == (gateway, source)
    assert source.history_evidence("o/toy", 102)["files"] == [{"filename": "tools/fmt/z.py", "patch": "+fix"}]
    assert source.history_evidence("o/toy", 101)["files"] == [{"filename": "pkg/util.py", "patch": "+old"}]
    assert source.history_evidence("o/toy", 102)["threads"] == ["retained"]


def test_frozen_history_source_retains_real_files_patches_and_identity():
    row = {"merge_commit_sha": "a" * 40, "number": 102,
           "files": [{"filename": "tools/fmt/z.py", "patch": "@@ -1 +1 @@\n-old\n+new"},
                     {"filename": "tools/fmt/test_contract.py", "patch": "@@ -0,0 +1 @@\n+assert contract"}],
           "reviews": [{"body": "preserve both files"}], "threads": []}
    source = SimpleNamespace(history_evidence=lambda *args: row)
    original = source.history_evidence;gateway = object();world = {"fixture": True};seen = []
    def prepare(actual):
        seen.append(actual)
        return gateway, source
    result = history_world({"prepare_history": prepare,
                           "_setup": lambda _: pytest.fail("custom evidence entered legacy preparation")}, world)
    assert result == (gateway, source) and seen == [world]
    assert source.history_evidence is original
    assert source.history_evidence("o/toy", 102) == row
    assert len(source.history_evidence("o/toy", 102)["files"]) == 2
