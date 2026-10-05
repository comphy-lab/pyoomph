#!/usr/bin/env python3
"""Offline integration tests for the comphy fork synchroniser."""
from __future__ import annotations

import base64
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
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

    def add_workflow_commits(self, *, main_parent: str, develop_parent: str,
                             source_first: bool = True) -> dict[str, str]:
        result: dict[str, str] = {}
        if source_first:
            main_parent = self.commit(
                main_parent, {"more-main.txt": "more main\n"}, "Upstream main source")
            develop_parent = self.commit(
                develop_parent, {"more-develop.txt": "more develop\n"}, "Upstream develop source")
            result["main_source"] = main_parent
            result["develop_source"] = develop_parent
        workflow = (
            "name: unreviewed\n"
            "on: [push]\n"
            "jobs:\n"
            "  x:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - run: true\n"
        )
        result["main_workflow"] = self.commit(
            main_parent,
            {".github/workflows/upstream-main.yml": workflow},
            "Upstream main workflow",
        )
        result["develop_workflow"] = self.commit(
            develop_parent,
            {".github/workflows/upstream-develop.yml": workflow},
            "Upstream develop workflow",
        )
        self.push(self.upstream, main=result["main_workflow"], develop=result["develop_workflow"])
        return result


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

    def prepare(self, name: str = "sync", *, publish_workflows: bool = False):
        return sync.prepare(
            self.fixture.checkout(name),
            os.fspath(self.fixture.fork),
            os.fspath(self.fixture.upstream),
            publish=True,
            publish_workflows=publish_workflows,
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
            with self.assertRaises(RuntimeError) as ctx:
                self.prepare()

        self.assertTrue(raced)
        self.assertRegex(str(ctx.exception), r"non-fast-forward|rejected|failed to push")
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

    def test_workflow_files_wait_for_reviewed_publication(self):
        graph = self.fixture.populate()
        baseline = self.prepare("baseline")
        extra = self.fixture.add_workflow_commits(
            main_parent=graph["upstream_main"],
            develop_parent=graph["upstream_develop"],
        )

        gated = self.prepare("gated")

        self.assertEqual(
            self.fixture.remote_revision(self.fixture.fork, "refs/heads/main"),
            extra["main_source"],
        )
        self.assertEqual(
            self.fixture.remote_revision(self.fixture.fork, "refs/heads/develop"),
            extra["develop_source"],
        )
        self.assertEqual(
            self.fixture.remote_revision(self.fixture.fork, "refs/heads/comphy"),
            graph["comphy"],
        )
        self.assertNotEqual(
            self.fixture.remote_revision(self.fixture.fork, "refs/heads/main"),
            extra["main_workflow"],
        )
        self.assertIn("main", gated["workflow_gate"])
        self.assertIn("develop", gated["workflow_gate"])
        self.assertEqual(
            gated["workflow_gate"]["main"]["paths"],
            [".github/workflows/upstream-main.yml"],
        )
        self.assertNotIn(
            f"{extra['main_workflow']}:refs/heads/main", gated["intended_refspecs"])
        self.assertNotIn(
            f"{extra['develop_workflow']}:refs/heads/develop", gated["intended_refspecs"])
        self.assertEqual(gated["status"], "candidate")
        self.assertEqual(gated["mirrors"]["develop"]["publish"], extra["develop_source"])
        names = self.fixture._run(
            self.fixture.fork, "ls-tree", "-r", "--name-only", gated["candidate_sha"]
        ).stdout.splitlines()
        self.assertNotIn(".github/workflows/upstream-develop.yml", names)
        self.assertNotEqual(gated["candidate_sha"], baseline["candidate_sha"])

        reviewed = self.prepare("reviewed", publish_workflows=True)

        self.assertEqual(
            self.fixture.remote_revision(self.fixture.fork, "refs/heads/main"),
            extra["main_workflow"],
        )
        self.assertEqual(
            self.fixture.remote_revision(self.fixture.fork, "refs/heads/develop"),
            extra["develop_workflow"],
        )
        self.assertEqual(reviewed["workflow_gate"], {})
        self.assertIn(
            f"{extra['main_workflow']}:refs/heads/main", reviewed["intended_refspecs"])
        names = self.fixture._run(
            self.fixture.fork, "ls-tree", "-r", "--name-only", reviewed["candidate_sha"]
        ).stdout.splitlines()
        self.assertIn(".github/workflows/upstream-develop.yml", names)
        self.assertEqual(
            self.fixture.remote_revision(self.fixture.fork, "refs/heads/comphy"),
            graph["comphy"],
        )

    def test_workflow_only_update_publishes_no_refs_until_review(self):
        graph = self.fixture.populate()
        baseline = self.prepare("baseline")
        self.fixture._run(
            self.fixture.fork, "update-ref", "refs/heads/comphy", baseline["candidate_sha"])
        extra = self.fixture.add_workflow_commits(
            main_parent=graph["upstream_main"],
            develop_parent=graph["upstream_develop"],
            source_first=False,
        )
        heads_before = self.fixture.remote_heads(self.fixture.fork)

        report = self.prepare("workflow-only")

        self.assertEqual(report["status"], "workflow-review")
        self.assertEqual(report["publication"], "no-changes")
        self.assertFalse(report["published"])
        self.assertEqual(report["intended_refspecs"], [])
        self.assertEqual(self.fixture.remote_heads(self.fixture.fork), heads_before)
        self.assertEqual(report["candidate_sha"], baseline["candidate_sha"])
        self.assertEqual(report["mirrors"]["main"]["publish"], graph["upstream_main"])
        self.assertEqual(report["mirrors"]["main"]["after"], extra["main_workflow"])
        self.assertIn(".github/workflows/upstream-main.yml", report["workflow_gate"]["main"]["paths"])


class GitHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        SCRATCHPAD.mkdir(exist_ok=True)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="comphy-sync-helpers-", dir=SCRATCHPAD)

    def tearDown(self):
        self.temporary.cleanup()

    def test_redact_secrets_strips_tokens_and_basic_auth(self):
        token = "ghs_secretvalue123"
        auth = base64.b64encode(("x-access-token:" + token).encode()).decode()
        text = (
            f"fatal: Authentication failed for {token}\n"
            f"AUTHORIZATION: basic {auth}\n"
        )
        with mock.patch.dict(os.environ, {"GH_TOKEN": token, "GITHUB_TOKEN": token, "COMPHY_SYNC_TOKEN": token}):
            redacted = sync.redact_secrets(text)
        self.assertNotIn(token, redacted)
        self.assertNotIn(auth, redacted)
        self.assertIn("***", redacted)
        self.assertIn("Authentication failed", redacted)

    def test_git_failure_includes_stderr(self):
        root = Path(self.temporary.name) / "empty-repo"
        root.mkdir()
        sync.git(root, "init", "--quiet")
        with self.assertRaises(RuntimeError) as ctx:
            sync.git(root, "rev-parse", "--verify", "refs/heads/missing")
        self.assertIn("Needed a single revision", str(ctx.exception))
        self.assertNotIn("exited", str(ctx.exception))

    def test_git_failure_redacts_token_from_stderr(self):
        token = "ghs_super_secret_value"
        completed = subprocess.CompletedProcess(
            args=["git", "push", "--atomic", "fork"],
            returncode=1,
            stdout="",
            stderr=(
                "remote: error: GH013: Repository rule violations found\n"
                f"remote: refusing token {token}\n"
            ),
        )
        with mock.patch.dict(os.environ, {"GH_TOKEN": token}):
            with mock.patch("subprocess.run", return_value=completed):
                with self.assertRaises(RuntimeError) as ctx:
                    sync.git(Path("."), "push", "--atomic", "fork")
        self.assertIn("Repository rule violations", str(ctx.exception))
        self.assertNotIn(token, str(ctx.exception))
        self.assertIn("***", str(ctx.exception))

    def test_format_command_error_uses_called_process_stderr(self):
        token = "ghs_called_process_token"
        error = subprocess.CalledProcessError(
            1,
            ["git", "push", "--atomic", "fork"],
            output="",
            stderr=(
                " ! [remote rejected] main (cannot update workflow files)\n"
                f"Authorization: {token}\n"
            ),
        )
        with mock.patch.dict(os.environ, {"GH_TOKEN": token}):
            message = sync.format_command_error(error)
        self.assertIn("cannot update workflow files", message)
        self.assertNotIn(token, message)
        self.assertNotIn("returned non-zero exit status", message)

    def test_publish_environment_ignores_checkout_credentials(self):
        token = "ghs_pat_value"
        leaked = "AUTHORIZATION: basic leaked-checkout-token"
        with mock.patch.dict(os.environ, {
            "GH_TOKEN": token,
            "GITHUB_TOKEN": "ghs_actions_token",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": leaked,
        }):
            env = sync.publish_environment(token)
        auth = base64.b64encode(("x-access-token:" + token).encode()).decode()
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("GH_TOKEN", env)
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(
            env["GIT_CONFIG_KEY_0"],
            "http.https://github.com/comphy-lab/pyoomph.git.extraheader",
        )
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "AUTHORIZATION: basic " + auth)
        self.assertNotIn("leaked-checkout-token", env["GIT_CONFIG_VALUE_0"])
        self.assertNotIn("ghs_actions_token", env.get("GIT_CONFIG_VALUE_0", ""))

    def test_main_records_redacted_git_stderr(self):
        token = "ghs_not_a_real_token"
        stderr = (
            "remote: error: GH013: Repository rule violations found\n"
            f"remote: GITHUB_TOKEN cannot update workflow files ({token})\n"
            " ! [remote rejected] main "
            "(refusing to allow a GitHub App to create or update workflow files)\n"
        )
        error = subprocess.CalledProcessError(
            1, ["git", "push", "--atomic", "fork"], "", stderr)
        report_path = Path(self.temporary.name) / "receipts" / "sync.json"
        argv = ["sync.py", "--publish", "--report", str(report_path)]
        environ = {
            "GH_TOKEN": token,
            "GITHUB_TOKEN": token,
            "GITHUB_REPOSITORY": "comphy-lab/pyoomph",
        }
        with mock.patch.dict(os.environ, environ, clear=False):
            with mock.patch.object(sync, "prepare", side_effect=error):
                with mock.patch.object(sys, "argv", argv):
                    with redirect_stdout(StringIO()) as stdout:
                        with self.assertRaises(SystemExit) as ctx:
                            sync.main()
        self.assertEqual(ctx.exception.code, 1)
        logged = stdout.getvalue()
        self.assertIn("cannot update workflow files", logged)
        self.assertNotIn(token, logged)
        report_text = report_path.read_text()
        report = json.loads(report_text)
        self.assertEqual(report["status"], "error")
        self.assertIn("cannot update workflow files", report["error"])
        self.assertIn("remote rejected", report["error"])
        self.assertNotIn(token, report["error"])
        self.assertNotIn(token, report_text)

    def test_main_publish_workflows_requires_environment_token(self):
        report_path = Path(self.temporary.name) / "receipts" / "sync.json"
        argv = ["sync.py", "--publish", "--publish-workflows", "--report", str(report_path)]
        environ = {
            "GH_TOKEN": "ghs_actions_token",
            "COMPHY_SYNC_TOKEN": "",
            "GITHUB_REPOSITORY": "comphy-lab/pyoomph",
        }
        with mock.patch.dict(os.environ, environ, clear=False):
            with mock.patch.object(sys, "argv", argv):
                with redirect_stderr(StringIO()):
                    with self.assertRaises(SystemExit) as ctx:
                        sync.main()
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
