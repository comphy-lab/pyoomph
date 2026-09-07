#!/usr/bin/env python3
"""Offline tests for the fail-closed CoMPhy release boundary."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

try:
    from . import release
except ImportError:  # Support ``python comphy/test_release.py``.
    import release


CANDIDATE = "a" * 40
COMPHY = "2" * 40
UPSTREAM = "3" * 40
RUN_ID = 123456789
TAG = "comphy-2026.09.07.1"


def successful_run() -> dict:
    return {
        "id": RUN_ID,
        "status": "completed",
        "conclusion": "success",
        "event": "workflow_dispatch",
        "head_sha": COMPHY,
        "head_branch": "comphy",
        "html_url": f"https://github.com/comphy-lab/pyoomph/actions/runs/{RUN_ID}",
        "repository": {"full_name": "comphy-lab/pyoomph"},
        "head_repository": {"full_name": "comphy-lab/pyoomph"},
        "workflow_id": 42,
        "path": ".github/workflows/comphy-maintenance.yml",
        "name": "CoMPhy upstream maintenance",
    }


def successful_evidence(wheel: Path) -> tuple[dict, dict]:
    wheel_hash = hashlib.sha256(wheel.read_bytes()).hexdigest()
    sync = {
        "schema_version": 1,
        "fork": "https://github.com/comphy-lab/pyoomph.git",
        "upstream": "https://github.com/cdiddens/pyoomph.git",
        "status": "candidate",
        "publication": "readback-verified",
        "published": True,
        "comphy_sha": COMPHY,
        "candidate_sha": CANDIDATE,
        "candidate_ref": f"sync/upstream-{COMPHY[:12]}-{UPSTREAM[:12]}",
        "mirrors": {"develop": {"before": UPSTREAM, "after": UPSTREAM}},
    }
    verification = {
        "schema": "comphy-pyoomph-verification-v1",
        "status": "passed",
        "candidate_sha": CANDIDATE,
        "source_sha": CANDIDATE,
        "wheel_sha256": wheel_hash,
        "installed_candidate": {"has_mpi": True},
        "cleanup": {"complete": True},
        "stages": [
            {"name": "create_environment", "exit_code": 0},
            {"name": "install_candidate", "exit_code": 0},
            {"name": "doctor", "exit_code": 0},
            {
                "name": "regressions",
                "exit_code": 0,
                "stdout": "4 passed in 12.34s\n",
                "stderr": "",
            },
        ],
    }
    return sync, verification


class FakeGitHub:
    """In-memory GitHub boundary; it never invokes Git or the network."""

    def __init__(self, artifact_source: Path | None = None):
        self.artifact_source = artifact_source
        self.calls: list[tuple] = []
        self.run = successful_run()
        self.comphy_sha = COMPHY
        self.candidate_sha = CANDIDATE
        self.candidate_parents = [COMPHY, UPSTREAM]
        self.ref = None
        self.tag_object = None
        self.release = None
        self.release_assets: list[dict] = []
        self.uploaded: dict[str, bytes] = {}
        self._next_asset_id = 100
        self.fail_publish_once = False

    def api(self, path, *, method="GET", fields=None, allow_not_found=False):
        fields = fields or {}
        self.calls.append(("api", method, path, copy.deepcopy(fields), allow_not_found))
        if path.endswith(f"actions/runs/{RUN_ID}/artifacts?per_page=100"):
            return {
                "artifacts": [
                    {"id": 10, "name": "comphy-sync", "expired": False},
                    {"id": 11, "name": "comphy-verification", "expired": False},
                ]
            }
        if path.endswith(f"actions/runs/{RUN_ID}"):
            return copy.deepcopy(self.run)
        if "actions/workflows/" in path:
            return {
                "id": 42,
                "name": "CoMPhy upstream maintenance",
                "path": ".github/workflows/comphy-maintenance.yml",
            }
        if path.endswith("git/refs/heads/comphy") and method == "PATCH":
            self.comphy_sha = fields["sha"]
            return {"ref": "refs/heads/comphy", "object": {"sha": self.comphy_sha}}
        if path.endswith("git/ref/heads/comphy"):
            return {"ref": "refs/heads/comphy", "object": {"sha": self.comphy_sha}}
        if "/compare/" in path:
            status = "identical" if self.candidate_sha == self.comphy_sha else "behind"
            return {"status": status}
        if "git/ref/tags/" in path or "git/refs/tags/" in path:
            if self.ref is None and allow_not_found:
                return None
            return copy.deepcopy(self.ref)
        if f"commits/{CANDIDATE}" in path or f"git/commits/{CANDIDATE}" in path:
            return {
                "sha": self.candidate_sha,
                "parents": [{"sha": sha} for sha in self.candidate_parents],
                "commit": {"tree": {"sha": "4" * 40}},
            }
        if "/releases/tags/" in path:
            if self.release is None and allow_not_found:
                return None
            return copy.deepcopy(self.release)
        if "/releases?per_page=100&page=" in path and method == "GET":
            return [] if self.release is None else [copy.deepcopy(self.release)]
        if path.endswith("/releases") and method == "POST":
            self.release = {
                "id": 88,
                "tag_name": fields["tag_name"],
                "draft": bool(fields.get("draft", True)),
                "target_commitish": fields["target_commitish"],
                "body": fields["body"],
                "assets": [],
            }
            return copy.deepcopy(self.release)
        if path.endswith("/releases/88") and method == "PATCH":
            if fields.get("draft") is False and self.fail_publish_once:
                self.fail_publish_once = False
                raise release.ReleaseError("simulated interruption before publication")
            self.release.update(fields)
            if fields.get("draft") is False:
                self.release["immutable"] = True
            self.release["assets"] = copy.deepcopy(self.release_assets)
            return copy.deepcopy(self.release)
        if path.endswith("/releases/88") and method == "GET":
            self.release["assets"] = copy.deepcopy(self.release_assets)
            return copy.deepcopy(self.release)
        if path.endswith("/immutable-releases"):
            return {"enabled": True}
        if path.endswith("/git/tags") and method == "POST":
            self.tag_object = {
                "sha": "5" * 40,
                "message": fields["message"],
                "object": {"sha": fields["object"], "type": "commit"},
            }
            return copy.deepcopy(self.tag_object)
        if path.endswith("/git/refs") and method == "POST":
            self.ref = {
                "ref": fields["ref"],
                "object": {"sha": fields["sha"], "type": "tag"},
            }
            return copy.deepcopy(self.ref)
        if "/git/tags/" in path and method == "GET":
            if self.tag_object is None:
                raise AssertionError("tag object was not configured")
            return copy.deepcopy(self.tag_object)
        raise AssertionError(f"Unexpected GitHub API call: {method} {path} {fields}")

    def download_artifacts(self, run_id: int, destination: Path):
        self.calls.append(("download_artifacts", run_id, destination))
        if run_id != RUN_ID or self.artifact_source is None:
            raise AssertionError("unexpected artifact download")
        destination.mkdir(parents=True, exist_ok=False)
        for source in self.artifact_source.iterdir():
            target = destination / source.name
            if source.is_dir():
                target.mkdir()
                for child in source.rglob("*"):
                    relative = child.relative_to(source)
                    if child.is_dir():
                        (target / relative).mkdir(parents=True, exist_ok=True)
                    else:
                        (target / relative).parent.mkdir(parents=True, exist_ok=True)
                        (target / relative).write_bytes(child.read_bytes())
            else:
                target.write_bytes(source.read_bytes())

    def upload_assets(self, tag: str, paths):
        paths = list(paths)
        self.calls.append(("upload_assets", tag, [path.name for path in paths]))
        for path in paths:
            data = path.read_bytes()
            asset = {
                "id": self._next_asset_id,
                "name": path.name,
                "size": len(data),
            }
            self._next_asset_id += 1
            self.release_assets.append(asset)
            self.uploaded[path.name] = data
        if self.release is not None:
            self.release["assets"] = copy.deepcopy(self.release_assets)
        return copy.deepcopy(self.release_assets)

    def download_asset(self, asset_id):
        self.calls.append(("download_asset", asset_id))
        asset = next(asset for asset in self.release_assets if asset["id"] == asset_id)
        return self.uploaded[asset["name"]]


class ReleaseValidationTests(unittest.TestCase):
    def test_tag_is_exact_and_calendar_valid(self):
        self.assertEqual(release.validate_tag(TAG), TAG)
        for invalid in (
            "v2026.09.07",
            "comphy-2026.9.07.1",
            "comphy-2026.02.30.1",
            "comphy-2026.09.07.0",
            "comphy-2026.09.07.1^{commit}",
            " comphy-2026.09.07.1",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(release.ReleaseError):
                release.validate_tag(invalid)

    def test_prepare_rejects_malformed_sha_and_run_id_before_io(self):
        fake = FakeGitHub()
        cases = (
            ("abc", RUN_ID),
            (CANDIDATE.upper(), RUN_ID),
            (CANDIDATE, 0),
            (CANDIDATE, -1),
            (CANDIDATE, "123"),
            (CANDIDATE, True),
        )
        with tempfile.TemporaryDirectory() as raw:
            for candidate, run_id in cases:
                fake.calls.clear()
                with self.subTest(candidate=candidate, run_id=run_id):
                    with self.assertRaises(release.ReleaseError):
                        release.prepare(TAG, candidate, run_id, Path(raw) / "bundle", github=fake)
                    self.assertEqual(fake.calls, [])

    def test_run_must_be_successful_and_from_exact_workflow_and_repository(self):
        mutations = (
            ("failed", lambda run: run.update(conclusion="failure")),
            ("in progress", lambda run: run.update(status="in_progress", conclusion=None)),
            ("wrong repository", lambda run: run["repository"].update(full_name="attacker/pyoomph")),
            ("wrong workflow", lambda run: run.update(path=".github/workflows/other.yml", name="Other")),
        )
        for label, mutate in mutations:
            run = successful_run()
            mutate(run)
            with self.subTest(label=label), self.assertRaises(release.ReleaseError):
                release.validate_run(run, RUN_ID)

    def test_required_run_artifacts_must_be_unique_current_and_identified(self):
        valid = {
            "artifacts": [
                {"id": 10, "name": "comphy-sync", "expired": False},
                {"id": 11, "name": "comphy-verification", "expired": False},
            ]
        }
        release.validate_artifacts(valid)
        for label, mutate in (
            ("missing", lambda value: value["artifacts"].pop()),
            ("duplicate", lambda value: value["artifacts"].append(copy.deepcopy(value["artifacts"][0]))),
            ("expired", lambda value: value["artifacts"][0].update(expired=True)),
            ("unidentified", lambda value: value["artifacts"][0].update(id="10")),
        ):
            listing = copy.deepcopy(valid)
            mutate(listing)
            with self.subTest(label=label), self.assertRaises(release.ReleaseError):
                release.validate_artifacts(listing)

    def test_evidence_rejects_candidate_and_wheel_hash_mismatches(self):
        with tempfile.TemporaryDirectory() as raw:
            wheel = Path(raw) / "pyoomph.whl"
            wheel.write_bytes(b"wheel")
            sync, verification = successful_evidence(wheel)
            for label, mutate in (
                ("sync candidate", lambda: sync.update(candidate_sha="d" * 40)),
                ("verification candidate", lambda: verification.update(candidate_sha="b" * 40)),
                ("source candidate", lambda: verification.update(source_sha="c" * 40)),
                ("wheel hash", lambda: verification.update(wheel_sha256="0" * 64)),
            ):
                sync, verification = successful_evidence(wheel)
                mutate()
                with self.subTest(label=label), self.assertRaises(release.ReleaseError):
                    release.validate_evidence(sync, verification, CANDIDATE, wheel, successful_run())

    def test_evidence_requires_an_unskipped_passing_regression_stage(self):
        with tempfile.TemporaryDirectory() as raw:
            wheel = Path(raw) / "pyoomph.whl"
            wheel.write_bytes(b"wheel")
            for label, change in (
                ("missing", lambda report: report.update(stages=report["stages"][:-1])),
                ("failed", lambda report: report["stages"][-1].update(exit_code=1)),
                ("skipped", lambda report: report["stages"][-1].update(stdout="6 passed, 1 skipped")),
                ("no pass count", lambda report: report["stages"][-1].update(stdout="completed")),
            ):
                sync, verification = successful_evidence(wheel)
                change(verification)
                with self.subTest(label=label), self.assertRaises(release.ReleaseError):
                    release.validate_evidence(sync, verification, CANDIDATE, wheel, successful_run())

    def test_stale_candidate_is_not_promotable(self):
        fake = FakeGitHub()
        fake.comphy_sha = "9" * 40
        state = release.release_eligibility(fake, CANDIDATE)
        self.assertFalse(state["promotable"])
        self.assertIn("stale", state["state"])


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.environment = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_REPOSITORY": "comphy-lab/pyoomph",
            "GITHUB_REF": "refs/heads/comphy",
            "PYOOMPH_RELEASE_APPROVED": "1",
        }

    @staticmethod
    def make_artifacts(root: Path) -> Path:
        artifacts = root / "artifacts"
        sync_dir = artifacts / "comphy-sync"
        verification_dir = artifacts / "comphy-verification"
        sync_dir.mkdir(parents=True)
        verification_dir.mkdir()
        wheel = verification_dir / "pyoomph-0.0.0-cp313-linux.whl"
        wheel.write_bytes(b"candidate wheel bytes")
        sync, verification = successful_evidence(wheel)
        (sync_dir / "sync.json").write_text(json.dumps(sync), encoding="utf-8")
        (verification_dir / "verification.json").write_text(
            json.dumps(verification), encoding="utf-8"
        )
        (verification_dir / "build-packages.txt").write_text(
            "mpi4py==4.1.0\n", encoding="utf-8"
        )
        (verification_dir / "CMakeCache.txt").write_text(
            "PYOOMPH_USE_MPI:BOOL=ON\n", encoding="utf-8"
        )
        return artifacts

    def make_bundle(self, root: Path) -> tuple[Path, FakeGitHub]:
        artifacts = self.make_artifacts(root)
        fake = FakeGitHub(artifacts)
        bundle = root / "bundle"
        release.prepare(TAG, CANDIDATE, RUN_ID, bundle, github=fake)
        fake.calls.clear()
        return bundle, fake

    def publish(self, bundle: Path, fake: FakeGitHub):
        with mock.patch.dict(os.environ, self.environment, clear=True):
            return release.publish(bundle, github=fake)

    def test_publish_requires_exact_actions_approval_context(self):
        with tempfile.TemporaryDirectory() as raw:
            bundle, _ = self.make_bundle(Path(raw))
            for key, value in (
                ("GITHUB_ACTIONS", "false"),
                ("GITHUB_REPOSITORY", "other/pyoomph"),
                ("GITHUB_REF", "refs/heads/main"),
                ("PYOOMPH_RELEASE_APPROVED", "0"),
            ):
                environment = dict(self.environment)
                environment[key] = value
                fake = FakeGitHub()
                with self.subTest(key=key), mock.patch.dict(os.environ, environment, clear=True):
                    with self.assertRaises(release.ReleaseError):
                        release.publish(bundle, github=fake)
                    self.assertEqual(fake.calls, [])

    def test_publish_rejects_any_manifest_or_file_tampering(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for label, tamper in (
                ("schema", lambda bundle: json.loads((bundle / "manifest.json").read_text()) | {"schema": "evil"}),
                ("repository", lambda bundle: json.loads((bundle / "manifest.json").read_text()) | {"repository": "other/pyoomph"}),
            ):
                (root / label).mkdir()
                bundle, _ = self.make_bundle(root / label)
                manifest = tamper(bundle)
                (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                fake = FakeGitHub()
                with self.subTest(label=label), self.assertRaises(release.ReleaseError):
                    self.publish(bundle, fake)
                self.assertEqual(fake.calls, [])

            (root / "asset").mkdir()
            bundle, _ = self.make_bundle(root / "asset")
            wheel_name = json.loads((bundle / "manifest.json").read_text())["wheel"]
            (bundle / wheel_name).write_bytes(b"tampered wheel")
            fake = FakeGitHub()
            with self.assertRaises(release.ReleaseError):
                self.publish(bundle, fake)
            self.assertEqual(fake.calls, [])

    def test_publish_refuses_existing_tag_ref_or_unrelated_release(self):
        with tempfile.TemporaryDirectory() as raw:
            bundle, prepared = self.make_bundle(Path(raw))
            for label, configure in (
                ("tag", lambda gh: setattr(gh, "ref", {
                    "ref": f"refs/tags/{TAG}",
                    "object": {"sha": "8" * 40, "type": "commit"},
                })),
                ("published release", lambda gh: setattr(gh, "release", {"id": 7, "tag_name": TAG, "draft": False, "assets": []})),
                ("unrelated draft", lambda gh: setattr(gh, "release", {"id": 7, "tag_name": TAG, "draft": True, "body": "unrelated", "assets": []})),
            ):
                fake = FakeGitHub(prepared.artifact_source)
                configure(fake)
                with self.subTest(label=label), self.assertRaises(release.ReleaseError):
                    self.publish(bundle, fake)

    def test_publish_verifies_draft_assets_before_making_release_public(self):
        with tempfile.TemporaryDirectory() as raw:
            bundle, fake = self.make_bundle(Path(raw))

            result = self.publish(bundle, fake)

            self.assertFalse(fake.release["draft"])
            publish_index = next(
                index for index, call in enumerate(fake.calls)
                if call[0] == "api" and call[1] in {"PATCH", "POST"}
                and call[2].endswith("/releases/88") and call[3].get("draft") is False
            )
            upload_index = next(index for index, call in enumerate(fake.calls) if call[0] == "upload_assets")
            download_indices = [index for index, call in enumerate(fake.calls) if call[0] == "download_asset"]
            self.assertLess(upload_index, publish_index)
            self.assertTrue(download_indices)
            verified_before_publish = [index for index in download_indices if index < publish_index]
            self.assertEqual(len(verified_before_publish), len(fake.uploaded))
            self.assertEqual(result["tag"], TAG)

    def test_publish_refuses_a_candidate_that_became_stale(self):
        with tempfile.TemporaryDirectory() as raw:
            bundle, fake = self.make_bundle(Path(raw))
            fake.comphy_sha = "9" * 40

            with self.assertRaisesRegex(release.ReleaseError, "stale"):
                self.publish(bundle, fake)

            self.assertFalse(any(call[0] == "upload_assets" for call in fake.calls))
            self.assertFalse(any(call[0] == "api" and call[1] != "GET" for call in fake.calls))

    def test_retry_resumes_the_exact_draft_without_reuploading_assets(self):
        with tempfile.TemporaryDirectory() as raw:
            bundle, fake = self.make_bundle(Path(raw))
            fake.fail_publish_once = True

            with self.assertRaisesRegex(release.ReleaseError, "simulated interruption"):
                self.publish(bundle, fake)
            self.assertTrue(fake.release["draft"])
            first_asset_ids = {
                asset["name"]: asset["id"] for asset in fake.release_assets
            }
            fake.calls.clear()

            result = self.publish(bundle, fake)

            self.assertFalse(fake.release["draft"])
            self.assertEqual(
                {asset["name"]: asset["id"] for asset in fake.release_assets},
                first_asset_ids,
            )
            self.assertFalse(any(call[0] == "upload_assets" for call in fake.calls))
            self.assertTrue(result["immutable"])

    def test_retry_accepts_the_exact_already_published_immutable_release(self):
        with tempfile.TemporaryDirectory() as raw:
            bundle, fake = self.make_bundle(Path(raw))
            first = self.publish(bundle, fake)
            fake.calls.clear()

            second = self.publish(bundle, fake)

            self.assertEqual(second["release_id"], first["release_id"])
            self.assertTrue(second["recovered"])
            self.assertFalse(any(call[0] == "upload_assets" for call in fake.calls))
            self.assertFalse(any(call[0] == "api" and call[1] != "GET" for call in fake.calls))

    def test_retry_accepts_github_tag_newline_and_branch_commitish(self):
        for newline, branch in ((True, False), (False, True), (True, True)):
            with self.subTest(newline=newline, branch=branch), tempfile.TemporaryDirectory() as raw:
                bundle, fake = self.make_bundle(Path(raw))
                fake.fail_publish_once = True
                with self.assertRaisesRegex(release.ReleaseError, "simulated interruption"):
                    self.publish(bundle, fake)
                if newline:
                    fake.tag_object["message"] += "\n"
                if branch:
                    fake.release["target_commitish"] = "comphy"
                fake.calls.clear()
                result = self.publish(bundle, fake)
                self.assertTrue(result["immutable"])
                self.assertFalse(any(call[0] == "upload_assets" for call in fake.calls))

    def test_retry_refuses_a_conflicting_explicit_commitish_sha(self):
        with tempfile.TemporaryDirectory() as raw:
            bundle, fake = self.make_bundle(Path(raw))
            fake.fail_publish_once = True
            with self.assertRaisesRegex(release.ReleaseError, "simulated interruption"):
                self.publish(bundle, fake)
            fake.release["target_commitish"] = "c" * 40
            fake.calls.clear()
            with self.assertRaisesRegex(release.ReleaseError, "different commit"):
                self.publish(bundle, fake)
            self.assertFalse(any(call[0] == "api" and call[1] != "GET" for call in fake.calls))

    def test_prepare_builds_a_complete_hash_bound_bundle_without_writes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifacts = self.make_artifacts(root)
            fake = FakeGitHub(artifacts)
            bundle = root / "proposal"

            manifest = release.prepare(TAG, CANDIDATE, RUN_ID, bundle, github=fake)

            self.assertTrue(bundle.is_dir())
            self.assertEqual(manifest["schema"], "comphy-pyoomph-release-v1")
            self.assertEqual(manifest["candidate_sha"], CANDIDATE)
            self.assertEqual(manifest["verification_run"], RUN_ID)
            self.assertEqual(manifest["eligibility"]["state"], "fresh")
            expected = {
                "sync.json", "verification.json", "build-packages.txt",
                "CMakeCache.txt", "RELEASE-NOTES.md", manifest["wheel"],
                "manifest.json", "SHA256SUMS",
            }
            self.assertEqual({path.name for path in bundle.iterdir()}, expected)
            self.assertFalse(any(call[0] == "upload_assets" for call in fake.calls))
            self.assertFalse(any(call[0] == "api" and call[1] != "GET" for call in fake.calls))


if __name__ == "__main__":
    unittest.main()
