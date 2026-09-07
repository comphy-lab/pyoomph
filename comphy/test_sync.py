#!/usr/bin/env python3
"""Offline integration tests for the comphy fork synchroniser."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

try:
    from . import sync
except ImportError:  # Support ``python comphy/test_sync.py``.
    import sync


SCRATCHPAD = Path(__file__).resolve().parents[1] / "Scratchpad"


class GitFixture:
    """A source repository and two local bare remotes with controlled history."""

    def __init__(self, root: Path):
        self.root = root
        self.source = root / "source"
        self.fork = root / "fork.git"
        self.upstream = root / "upstream.git"
        self._run(root, "init", "--quiet", "--initial-branch=main", self.source)
        self._run(root, "init", "--quiet", "--bare", "--initial-branch=main", self.fork)
        self._run(root, "init", "--quiet", "--bare", "--initial-branch=main", self.upstream)
        self._run(self.source, "config", "user.name", "Sync test")
        self._run(self.source, "config", "user.email", "sync-test@example.invalid")
        self._run(self.source, "config", "commit.gpgsign", "false")
        (self.source / "base.txt").write_text("base\n")
        self._run(self.source, "add", "base.txt")
        self._run(self.source, "commit", "--quiet", "-m", "Base")
        self.base = self.revision(self.source, "HEAD")

    @staticmethod
    def _run(root: Path, *args: object, check: bool = True):
        return subprocess.run(
            ["git", "-C", os.fspath(root), *(os.fspath(arg) for arg in args)],
            check=check,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def revision(self, root: Path, ref: str) -> str:
        return self._run(root, "rev-parse", "--verify", ref).stdout.strip()

    def commit(self, parent: str, changes: dict[str, str], message: str) -> str:
        self._run(self.source, "checkout", "--quiet", "--detach", parent)
        for relative, contents in changes.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents)
        self._run(self.source, "add", "--all")
        self._run(self.source, "commit", "--quiet", "-m", message)
        return self.revision(self.source, "HEAD")

    def push(self, remote: Path, **branches: str) -> None:
        refspecs = [f"{sha}:refs/heads/{branch}" for branch, sha in branches.items()]
        self._run(self.source, "push", "--quiet", remote, *refspecs)

    def remote_revision(self, remote: Path, ref: str) -> str:
        return self.revision(remote, ref)

    def remote_heads(self, remote: Path) -> dict[str, str]:
        result = self._run(remote, "for-each-ref", "--format=%(refname) %(objectname)",
                           "refs/heads")
        return dict(line.split() for line in result.stdout.splitlines())

    def checkout(self, name: str) -> Path:
        path = self.root / name
        path.mkdir()
        return path

    def populate(self, *, conflict: bool = False,
                 divergent_develop: bool = False) -> dict[str, str]:
        fork_main = self.commit(self.base, {"fork-main.txt": "fork main\n"}, "Fork main")
        upstream_main = self.commit(
            fork_main, {"upstream-main.txt": "upstream main\n"}, "Upstream main")
        fork_develop = self.commit(
            self.base,
            {"develop-base.txt": "develop base\n", "shared.txt": "shared base\n"},
            "Fork develop",
        )
        upstream_parent = self.base if divergent_develop else fork_develop
        upstream_changes = (
            {"shared.txt": "upstream version\n"}
            if conflict
            else {"upstream-develop.txt": "upstream develop\n"}
        )
        upstream_develop = self.commit(upstream_parent, upstream_changes, "Upstream develop")
        comphy_changes = (
            {"shared.txt": "comphy version\n"}
            if conflict
            else {"comphy.txt": "comphy work\n"}
        )
        comphy = self.commit(fork_develop, comphy_changes, "Comphy work")
        self.push(self.fork, main=fork_main, develop=fork_develop, comphy=comphy)
        self.push(self.upstream, main=upstream_main, develop=upstream_develop)
        return {
            "fork_main": fork_main,
            "upstream_main": upstream_main,
            "fork_develop": fork_develop,
            "upstream_develop": upstream_develop,
            "comphy": comphy,
        }


class PrepareIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        SCRATCHPAD.mkdir(exist_ok=True)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="comphy-sync-tests-", dir=SCRATCHPAD)
        self.fixture = GitFixture(Path(self.temporary.name))

    def tearDown(self):
        self.temporary.cleanup()

    def prepare(self, name: str = "sync"):
        return sync.prepare(
            self.fixture.checkout(name),
            os.fspath(self.fixture.fork),
            os.fspath(self.fixture.upstream),
            publish=True,
        )

    def test_fast_forwards_mirrors_and_preserves_comphy(self):
        graph = self.fixture.populate()

        report = self.prepare()

        self.assertEqual(report["status"], "candidate")
        self.assertTrue(report["published"])
        self.assertTrue(report["candidate_created"])
        self.assertEqual(
            self.fixture.remote_revision(self.fixture.fork, "refs/heads/main"),
            graph["upstream_main"],
        )
        self.assertEqual(
            self.fixture.remote_revision(self.fixture.fork, "refs/heads/develop"),
            graph["upstream_develop"],
        )
        self.assertEqual(
            self.fixture.remote_revision(self.fixture.fork, "refs/heads/comphy"),
            graph["comphy"],
        )
        candidate = self.fixture.remote_revision(
            self.fixture.fork, f"refs/heads/{report['candidate_ref']}")
        parents = self.fixture._run(
            self.fixture.fork, "show", "-s", "--format=%P", candidate
        ).stdout.strip().split()
        self.assertEqual(parents, [graph["comphy"], graph["upstream_develop"]])

    def test_reuses_identical_candidate_idempotently(self):
        self.fixture.populate()
        first = self.prepare("first-sync")
        heads_after_first = self.fixture.remote_heads(self.fixture.fork)

        second = self.prepare("second-sync")

        self.assertEqual(second["status"], "candidate")
        self.assertFalse(second["published"])
        self.assertEqual(second["publication"], "no-changes")
        self.assertFalse(second["candidate_created"])
        self.assertEqual(second["candidate_ref"], first["candidate_ref"])
        self.assertEqual(second["candidate_sha"], first["candidate_sha"])
        self.assertEqual(self.fixture.remote_heads(self.fixture.fork), heads_after_first)

    def test_conflict_preserves_comphy_but_publishes_safe_mirrors(self):
        graph = self.fixture.populate(conflict=True)

        report = self.prepare()

        self.assertEqual(report["status"], "conflict")
        self.assertEqual(report["conflicts"], ["shared.txt"])
        self.assertEqual(report["candidate_sha"], "")
        self.assertEqual(report["candidate_ref"], "")
        self.assertEqual(
            self.fixture.remote_revision(self.fixture.fork, "refs/heads/comphy"),
            graph["comphy"],
        )
        self.assertEqual(
            self.fixture.remote_revision(self.fixture.fork, "refs/heads/main"),
            graph["upstream_main"],
        )
        self.assertEqual(
            self.fixture.remote_revision(self.fixture.fork, "refs/heads/develop"),
            graph["upstream_develop"],
        )

    def test_divergent_upstream_ref_refuses_every_publish(self):
        graph = self.fixture.populate(divergent_develop=True)
        heads_before = self.fixture.remote_heads(self.fixture.fork)

        with self.assertRaisesRegex(RuntimeError, r"develop: upstream diverged"):
            self.prepare()

        self.assertEqual(self.fixture.remote_heads(self.fixture.fork), heads_before)
        self.assertEqual(
            self.fixture.remote_revision(self.fixture.fork, "refs/heads/main"),
            graph["fork_main"],
        )

    def test_destination_race_rejects_the_whole_atomic_push(self):
        graph = self.fixture.populate()
        race = self.fixture.commit(
            graph["fork_main"], {"race.txt": "competing update\n"}, "Destination race")
        original_git = sync.git
        raced = False

        def race_before_push(root, *args, **kwargs):
            nonlocal raced
            if not raced and args[:3] == ("push", "--atomic", "fork"):
                raced = True
                self.fixture.push(self.fixture.fork, main=race)
            return original_git(root, *args, **kwargs)

        with mock.patch.object(sync, "git", side_effect=race_before_push):
            with self.assertRaises(subprocess.CalledProcessError):
                self.prepare()

        self.assertTrue(raced)
        heads = self.fixture.remote_heads(self.fixture.fork)
        self.assertEqual(heads["refs/heads/main"], race)
        self.assertEqual(heads["refs/heads/develop"], graph["fork_develop"])
        self.assertEqual(heads["refs/heads/comphy"], graph["comphy"])
        candidate_ref = (
            f"refs/heads/sync/upstream-{graph['comphy'][:12]}-"
            f"{graph['upstream_develop'][:12]}"
        )
        self.assertNotIn(candidate_ref, heads)

    def test_readback_failure_preserves_confirmed_push_receipt(self):
        graph = self.fixture.populate()
        report = {}
        original_git = sync.git

        def fail_readback(root, *args, **kwargs):
            if args[:2] == ("ls-remote", "fork"):
                raise subprocess.CalledProcessError(1, ["git", "ls-remote", "fork"])
            return original_git(root, *args, **kwargs)

        with mock.patch.object(sync, "git", side_effect=fail_readback):
            with self.assertRaises(subprocess.CalledProcessError):
                sync.prepare(
                    self.fixture.checkout("readback-failure"),
                    os.fspath(self.fixture.fork),
                    os.fspath(self.fixture.upstream),
                    publish=True,
                    report=report,
                )

        self.assertTrue(report["published"])
        self.assertEqual(report["publication"], "push-confirmed")
        self.assertEqual(len(report["intended_refspecs"]), 3)
        self.assertIn(f"{graph['upstream_main']}:refs/heads/main", report["intended_refspecs"])
        self.assertIn(
            f"{graph['upstream_develop']}:refs/heads/develop",
            report["intended_refspecs"],
        )
        self.assertEqual(
            self.fixture.remote_revision(self.fixture.fork, "refs/heads/main"),
            graph["upstream_main"],
        )
        self.assertEqual(
            self.fixture.remote_revision(self.fixture.fork, "refs/heads/develop"),
            graph["upstream_develop"],
        )


if __name__ == "__main__":
    unittest.main()
