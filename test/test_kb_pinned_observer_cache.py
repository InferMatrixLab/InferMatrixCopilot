"""Successful immutable reads are reused without hiding read errors or live PR changes."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import tempfile
from threading import Barrier
import unittest

from infermatrix_copilot.knowledge_service.facts import FactsError
from infermatrix_copilot.knowledge_service.pinned_claims import PinnedObserver


class PinnedObserverCacheTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="kb-observer-cache-test-")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.git("init", "-q")
        (self.root / "src").mkdir()
        (self.root / "src/main.py").write_text("answer = 1\n")
        self.git("add", ".")
        self.git("-c", "user.name=KB Test", "-c", "user.email=kb-test@example.invalid", "commit", "-qm", "first")
        self.old = self.git("rev-parse", "HEAD").decode().strip()
        (self.root / "src/main.py").write_text("answer = 2\n")
        (self.root / "new-component").mkdir()
        (self.root / "new-component/entry.py").write_text("ready = True\n")
        self.git("add", ".")
        self.git("-c", "user.name=KB Test", "-c", "user.email=kb-test@example.invalid", "commit", "-qm", "second")
        self.new = self.git("rev-parse", "HEAD").decode().strip()
        self.observer = PinnedObserver(self.root, "example/repo", self.new)
        self.calls = []
        original = self.observer._git
        def counting(*args, **kwargs):
            self.calls.append(args)
            return original(*args, **kwargs)
        self.observer._git = counting

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], stderr=subprocess.PIPE)

    def fail_once(self, predicate):
        original = self.observer._git
        failed = False
        def flaky(*args, **kwargs):
            nonlocal failed
            if predicate(args) and not failed:
                failed = True
                self.calls.append(args)
                return subprocess.CompletedProcess(args, 128, b"", b"injected read failure")
            return original(*args, **kwargs)
        self.observer._git = flaky

    def test_success_reuse_is_bound_to_commit_and_does_not_change_with_head(self):
        self.assertEqual(self.observer.file_text(self.old, "src/main.py"), "answer = 1\n")
        self.assertEqual(self.observer.file_text(self.new, "src/main.py"), "answer = 2\n")
        before = len(self.calls)
        for _ in range(100):
            self.assertEqual(self.observer.file_text(self.old, "src/main.py"), "answer = 1\n")
            self.assertEqual(self.observer.file_text(self.new, "src/main.py"), "answer = 2\n")
            self.assertTrue(self.observer.path_exists(self.old, "src/main.py"))
        self.assertEqual(len(self.calls), before)
        (self.root / "src/main.py").write_text("uncommitted change\n")
        self.assertEqual(self.observer.file_text(self.old, "src/main.py"), "answer = 1\n")
        self.assertEqual(self.observer.file_text(self.new, "src/main.py"), "answer = 2\n")

    def test_top_level_is_bound_to_commit_and_returned_sets_cannot_poison_cache(self):
        old = self.observer.top_level(self.old)
        self.assertEqual(old, {"src"})
        old.add("invented")
        self.assertEqual(self.observer.top_level(self.old), {"src"})
        self.assertEqual(self.observer.top_level(self.new), {"src", "new-component"})
        before = len(self.calls)
        self.observer.top_level(self.new).clear()
        self.assertEqual(self.observer.top_level(self.new), {"src", "new-component"})
        self.assertEqual(len(self.calls), before)

    def test_moving_head_and_branch_are_resolved_before_cache_reuse(self):
        self.git("checkout", "-q", "--detach", self.old)
        self.assertEqual(self.observer.file_text("HEAD", "src/main.py"), "answer = 1\n")
        self.assertEqual(self.observer.top_level("HEAD"), {"src"})
        self.assertFalse(self.observer.path_exists("HEAD", "new-component/entry.py"))
        self.git("checkout", "-q", "--detach", self.new)
        self.assertEqual(self.observer.file_text("HEAD", "src/main.py"), "answer = 2\n")
        self.assertEqual(self.observer.top_level("HEAD"), {"src", "new-component"})
        self.assertTrue(self.observer.path_exists("HEAD", "new-component/entry.py"))
        self.git("update-ref", "refs/heads/probe", self.old)
        self.assertEqual(self.observer.file_text("probe", "src/main.py"), "answer = 1\n")
        self.git("update-ref", "refs/heads/probe", self.new)
        self.assertEqual(self.observer.file_text("probe", "src/main.py"), "answer = 2\n")
        self.assertEqual(self.observer.head(), self.new)

    def test_only_a_successful_empty_lookup_can_reuse_known_missing_entry(self):
        self.assertIsNone(self.observer.file_text(self.new, "missing.py"))
        before = len(self.calls)
        self.assertFalse(self.observer.path_exists(self.new, "missing.py"))
        self.assertIsNone(self.observer.file_text(self.new, "missing.py"))
        self.assertEqual(len(self.calls), before)
        self.assertIsNone(self.observer.file_text(self.new, "src"))
        self.assertTrue(self.observer.path_exists(self.new, "src"))

    def test_entry_read_failure_remains_unknown_and_is_retried(self):
        self.fail_once(lambda args: args[:2] == ("ls-tree", "-z"))
        with self.assertRaises(FactsError):
            self.observer.file_text(self.new, "src/main.py")
        self.assertEqual(self.observer.file_text(self.new, "src/main.py"), "answer = 2\n")
        self.assertEqual(sum(args[:2] == ("ls-tree", "-z") for args in self.calls), 2)

    def test_blob_read_failure_remains_unknown_and_is_retried(self):
        self.fail_once(lambda args: args[:2] == ("cat-file", "blob"))
        with self.assertRaises(FactsError):
            self.observer.file_text(self.new, "src/main.py")
        self.assertEqual(self.observer.file_text(self.new, "src/main.py"), "answer = 2\n")
        before = len(self.calls)
        self.assertEqual(self.observer.file_text(self.new, "src/main.py"), "answer = 2\n")
        self.assertEqual(len(self.calls), before)
        self.assertEqual(sum(args[:2] == ("cat-file", "blob") for args in self.calls), 2)

    def test_top_level_failure_is_not_cached_as_empty(self):
        self.fail_once(lambda args: args[:2] == ("ls-tree", "--name-only"))
        with self.assertRaises(FactsError):
            self.observer.top_level(self.new)
        self.assertEqual(self.observer.top_level(self.new), {"src", "new-component"})
        self.assertEqual(sum(args[:2] == ("ls-tree", "--name-only") for args in self.calls), 2)

    def test_unexpected_lookup_output_is_not_cached_as_verified_missing(self):
        original = self.observer._git
        unexpected = True
        def broken(*args, **kwargs):
            if args[:2] == ("ls-tree", "-z") and unexpected:
                self.calls.append(args)
                return subprocess.CompletedProcess(args, 0, b"unparsed-output\0", b"")
            return original(*args, **kwargs)
        self.observer._git = broken
        self.assertIsNone(self.observer.file_text(self.new, "src/main.py"))
        self.assertIsNone(self.observer.file_text(self.new, "src/main.py"))
        self.assertEqual(sum(args[:2] == ("ls-tree", "-z") for args in self.calls), 2)
        unexpected = False
        self.assertEqual(self.observer.file_text(self.new, "src/main.py"), "answer = 2\n")

    def test_fresh_observer_rechecks_source_and_detects_new_read_failure(self):
        self.observer.file_text(self.new, "src/main.py")
        fresh = PinnedObserver(self.root, "example/repo", self.new)
        def unreadable(*args, **kwargs):
            return subprocess.CompletedProcess(args, 128, b"", b"injected read failure")
        fresh._git = unreadable
        with self.assertRaises(FactsError):
            fresh.file_text(self.new, "src/main.py")

    def test_malformed_peer_does_not_hide_a_valid_lookup(self):
        original = self.observer._git

        def with_malformed_peer(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[:2] == ("ls-tree", "-z"):
                result.stdout = b"unparsed-output\0" + result.stdout
            return result

        self.observer._git = with_malformed_peer
        self.assertEqual(self.observer.file_text(self.new, "src/main.py"), "answer = 2\n")
        before = len(self.calls)
        self.assertEqual(self.observer.file_text(self.new, "src/main.py"), "answer = 2\n")
        self.assertEqual(len(self.calls), before)

    def test_thirteen_threads_share_one_successful_source_read(self):
        barrier = Barrier(13)
        def read(_):
            barrier.wait()
            return self.observer.file_text(self.new, "src/main.py")
        with ThreadPoolExecutor(max_workers=13) as pool:
            self.assertEqual(list(pool.map(read, range(13))), ["answer = 2\n"] * 13)
        self.assertEqual(sum(args[:2] == ("ls-tree", "-z") for args in self.calls), 1)
        self.assertEqual(sum(args[:2] == ("cat-file", "blob") for args in self.calls), 1)

    def test_live_pull_metadata_is_never_cached(self):
        calls = []
        def pull(repo, number):
            calls.append((repo, number))
            return {"number": number, "revision": len(calls)}
        observer = PinnedObserver(self.root, "example/repo", self.new, pull=pull)
        self.assertEqual(observer.pull(7)["revision"], 1)
        self.assertEqual(observer.pull(7)["revision"], 2)
        self.assertEqual(calls, [("example/repo", 7)] * 2)


if __name__ == "__main__":
    unittest.main()
