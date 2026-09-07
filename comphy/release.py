#!/usr/bin/env python3
"""Prepare and publish evidence-bound, immutable CoMPhy pyoomph releases."""

from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any, Iterable


REPOSITORY = "comphy-lab/pyoomph"
WORKFLOW_PATH = ".github/workflows/comphy-maintenance.yml"
WORKFLOW_EVENTS = {"schedule", "workflow_dispatch"}
ARTIFACTS = ("comphy-sync", "comphy-verification")
FORK_URL = "https://github.com/comphy-lab/pyoomph.git"
UPSTREAM_URL = "https://github.com/cdiddens/pyoomph.git"
SCHEMA = "comphy-pyoomph-release-v1"
API_VERSION = "2026-03-10"
TAG_PATTERN = re.compile(r"comphy-(\d{4})\.(\d{2})\.(\d{2})\.([1-9]\d*)\Z")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
REQUIRED_STAGES = {
    "create_environment", "install_candidate", "doctor", "regressions"
}
REQUIRED_ASSETS = {
    "sync.json", "verification.json", "build-packages.txt", "CMakeCache.txt"
}


class ReleaseError(RuntimeError):
    """A release precondition or read-back check failed."""


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_tag(tag: str) -> str:
    match = TAG_PATTERN.fullmatch(tag)
    if not match:
        raise ReleaseError("tag must have the form comphy-YYYY.MM.DD.N with N >= 1")
    try:
        date(*(int(part) for part in match.groups()[:3]))
    except ValueError as error:
        raise ReleaseError(f"tag contains an invalid calendar date: {tag}") from error
    return tag


def validate_sha(value: str, label: str = "candidate SHA") -> str:
    if not SHA_PATTERN.fullmatch(value):
        raise ReleaseError(f"{label} must be a full 40-character hexadecimal SHA")
    return value.lower()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseError(f"cannot read JSON evidence {path}: {error}") from error
    if not isinstance(value, dict):
        raise ReleaseError(f"JSON evidence must be an object: {path}")
    return value


class GitHub:
    """Small injectable adapter around ``gh``; every invocation uses typed argv."""

    def __init__(self, repo: str = REPOSITORY):
        if repo != REPOSITORY:
            raise ReleaseError(f"GitHub target is fixed to {REPOSITORY}")
        self.repo = repo

    @staticmethod
    def _run(args: list[str], *, binary: bool = False,
             allow_failure: bool = False) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(
                args, capture_output=True, text=not binary, timeout=300,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ReleaseError(f"GitHub command failed: {error}") from error
        if result.returncode and not allow_failure:
            stderr = result.stderr if not binary else result.stderr.decode(errors="replace")
            raise ReleaseError(stderr.strip() or f"GitHub command exited {result.returncode}")
        return result

    def api(self, path: str, *, method: str = "GET",
            fields: dict[str, Any] | None = None,
            allow_not_found: bool = False) -> Any:
        if not path.startswith((f"repos/{REPOSITORY}/", f"/repos/{REPOSITORY}/")):
            raise ReleaseError(f"refusing GitHub API target outside {REPOSITORY}: {path}")
        args = ["gh", "api", "--method", method,
                "-H", "Accept: application/vnd.github+json",
                "-H", f"X-GitHub-Api-Version: {API_VERSION}", path]
        if fields:
            args.extend(["--input", "-"])
        try:
            result = subprocess.run(
                args, input=json.dumps(fields) if fields else None,
                capture_output=True, text=True, timeout=300, check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ReleaseError(f"GitHub API failed: {error}") from error
        if result.returncode:
            if allow_not_found and ("HTTP 404" in result.stderr or "Not Found" in result.stderr):
                return None
            raise ReleaseError(result.stderr.strip() or f"GitHub API exited {result.returncode}")
        if not result.stdout.strip():
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ReleaseError(f"GitHub API returned invalid JSON for {path}") from error

    def download_artifacts(self, run_id: int, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=False)
        for name in ARTIFACTS:
            result = self._run([
                "gh", "run", "download", str(run_id), "--repo", self.repo,
                "--name", name, "--dir", str(destination / name),
            ], allow_failure=True)
            if result.returncode:
                raise ReleaseError(f"could not download required artifact {name}: "
                                   f"{result.stderr.strip()}")

    def upload_assets(self, tag: str, paths: Iterable[Path]) -> None:
        args = ["gh", "release", "upload", tag, "--repo", self.repo]
        args.extend(str(path) for path in paths)
        self._run(args)

    def download_asset(self, asset_id: int) -> bytes:
        path = f"repos/{self.repo}/releases/assets/{asset_id}"
        args = ["gh", "api", "--method", "GET",
                "-H", "Accept: application/octet-stream",
                "-H", f"X-GitHub-Api-Version: {API_VERSION}", path]
        return self._run(args, binary=True).stdout


def validate_run(run: dict[str, Any], run_id: int) -> None:
    if run.get("id") != run_id:
        raise ReleaseError("verification run ID does not match the requested run")
    if run.get("repository", {}).get("full_name") != REPOSITORY:
        raise ReleaseError(f"verification run is not from {REPOSITORY}")
    if (run.get("head_repository", {}).get("full_name") != REPOSITORY
            or run.get("head_branch") != "comphy"):
        raise ReleaseError("verification run did not execute on the fork comphy branch")
    if run.get("path") != WORKFLOW_PATH:
        raise ReleaseError(f"verification run did not use {WORKFLOW_PATH}")
    if run.get("event") not in WORKFLOW_EVENTS:
        raise ReleaseError("verification run has an ineligible trigger event")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise ReleaseError("expected a completed, successful maintenance run")
    expected_url = f"https://github.com/{REPOSITORY}/actions/runs/{run_id}"
    if run.get("html_url") != expected_url:
        raise ReleaseError("verification run has an unexpected URL")


def validate_artifacts(response: dict[str, Any]) -> None:
    artifacts = response.get("artifacts")
    if not isinstance(artifacts, list):
        raise ReleaseError("verification run artifact listing is missing")
    by_name: dict[str, list[dict[str, Any]]] = {}
    for artifact in artifacts:
        if isinstance(artifact, dict):
            by_name.setdefault(str(artifact.get("name")), []).append(artifact)
    for name in ARTIFACTS:
        matches = by_name.get(name, [])
        if len(matches) != 1:
            raise ReleaseError(f"expected exactly one {name} Actions artifact")
        artifact = matches[0]
        if artifact.get("expired") is not False or not isinstance(artifact.get("id"), int):
            raise ReleaseError(f"required Actions artifact is expired or invalid: {name}")


def _find_one(root: Path, name: str, *, required: bool = True) -> Path | None:
    matches = [path for path in root.rglob(name) if _safe_artifact_file(root, path)]
    if len(matches) != 1:
        if not required and not matches:
            return None
        raise ReleaseError(f"expected exactly one {name} in downloaded evidence; found {len(matches)}")
    return matches[0]


def _find_wheel(root: Path) -> Path:
    wheels = [path for path in root.rglob("*.whl") if _safe_artifact_file(root, path)]
    if len(wheels) != 1:
        raise ReleaseError(f"expected exactly one wheel in downloaded evidence; found {len(wheels)}")
    return wheels[0]


def _safe_artifact_file(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
        resolved = path.resolve(strict=True)
    except (OSError, ValueError):
        return False
    if not resolved.is_relative_to(root.resolve()) or not path.is_file():
        return False
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return False
    return True


def validate_evidence(sync: dict[str, Any], verification: dict[str, Any],
                      candidate_sha: str, wheel: Path,
                      run: dict[str, Any]) -> dict[str, str]:
    candidate_sha = validate_sha(candidate_sha)
    comphy_sha = validate_sha(str(sync.get("comphy_sha", "")), "sync comphy SHA")
    if run.get("head_sha", "").lower() != comphy_sha:
        raise ReleaseError("maintenance run head SHA does not match sync comphy SHA")
    if sync.get("fork") != FORK_URL or sync.get("upstream") != UPSTREAM_URL:
        raise ReleaseError("sync receipt contains unexpected source repository URLs")
    if sync.get("schema_version") != 1:
        raise ReleaseError("sync receipt has an unsupported schema")
    if str(sync.get("candidate_sha", "")).lower() != candidate_sha:
        raise ReleaseError("sync receipt candidate SHA does not match the requested candidate")
    if sync.get("status") not in {"candidate", "current"}:
        raise ReleaseError("sync receipt does not describe a releasable candidate")
    if sync.get("status") == "candidate" and (
            sync.get("published") is not True
            or sync.get("publication") != "readback-verified"):
        raise ReleaseError("candidate publication was not confirmed by the sync receipt")
    if verification.get("schema") != "comphy-pyoomph-verification-v1":
        raise ReleaseError("verification receipt has an unsupported schema")
    if verification.get("status") != "passed":
        raise ReleaseError("verification receipt did not pass")
    for key in ("candidate_sha", "source_sha"):
        if str(verification.get(key, "")).lower() != candidate_sha:
            raise ReleaseError(f"verification {key} does not match the requested candidate")
    if verification.get("cleanup", {}).get("complete") is not True:
        raise ReleaseError("verification cleanup is incomplete")
    if verification.get("installed_candidate", {}).get("has_mpi") is not True:
        raise ReleaseError("verification wheel was not built with MPI")
    stages = verification.get("stages")
    if not isinstance(stages, list):
        raise ReleaseError("verification stages are missing")
    by_name = {stage.get("name"): stage for stage in stages if isinstance(stage, dict)}
    missing = REQUIRED_STAGES - by_name.keys()
    if missing:
        raise ReleaseError("verification is missing required stages: " + ", ".join(sorted(missing)))
    if any(by_name[name].get("exit_code") != 0 for name in REQUIRED_STAGES):
        raise ReleaseError("one or more required verification stages did not exit zero")
    regression = by_name["regressions"].get("stdout", "")
    passed = re.search(r"(?:^|\s)(\d+) passed(?:[,\s]|$)", regression)
    if not passed or int(passed.group(1)) != 4 or re.search(r"\d+ skipped", regression):
        raise ReleaseError("verification must record exactly four passed, unskipped tests")
    actual_wheel_hash = sha256_file(wheel)
    if verification.get("wheel_sha256") != actual_wheel_hash:
        raise ReleaseError("wheel SHA-256 does not match the verification receipt")
    upstream_sha = validate_sha(
        str(sync.get("mirrors", {}).get("develop", {}).get("after", "")),
        "upstream develop SHA",
    )
    return {"comphy_sha": comphy_sha, "upstream_sha": upstream_sha,
            "wheel_sha256": actual_wheel_hash}


def _ref_sha(gh: GitHub, branch: str) -> str:
    ref = gh.api(f"repos/{REPOSITORY}/git/ref/heads/{branch}")
    return validate_sha(ref["object"]["sha"], f"{branch} SHA")


def release_eligibility(gh: GitHub, candidate_sha: str,
                        comphy_sha: str | None = None) -> dict[str, Any]:
    candidate_sha = validate_sha(candidate_sha)
    evidence_base = validate_sha(comphy_sha, "comphy SHA") if comphy_sha else None
    current = _ref_sha(gh, "comphy")
    comparison = gh.api(f"repos/{REPOSITORY}/compare/{candidate_sha}...{current}")
    contained = comparison.get("status") in {"ahead", "identical"}
    commit = gh.api(f"repos/{REPOSITORY}/commits/{candidate_sha}")
    parents = commit.get("parents", [])
    first_parent = parents[0].get("sha", "").lower() if parents else ""
    fresh = first_parent == current
    state = "contained" if contained else "fresh" if fresh else "stale"
    return {"state": state, "promotable": contained or fresh,
            "candidate_sha": candidate_sha, "comphy_sha": current,
            "first_parent": first_parent, "evidence_base": evidence_base}


def _ensure_absent(gh: GitHub, tag: str) -> None:
    ref = gh.api(f"repos/{REPOSITORY}/git/ref/tags/{tag}", allow_not_found=True)
    release = _release_by_tag(gh, tag)
    if ref is not None or release is not None:
        raise ReleaseError(f"tag or release already exists: {tag}")


def build_release_notes(tag: str, candidate_sha: str, run: dict[str, Any],
                        source: dict[str, str], wheel_name: str,
                        wheel_hash: str) -> str:
    return f"""# {tag}

Tested CoMPhy snapshot of pyoomph.

- Upstream develop: `{source['upstream_sha']}`
- Candidate: `{candidate_sha}`
- Candidate base: `{source['comphy_sha']}`
- Verification run: {run['html_url']}
- Wheel: `{wheel_name}`
- Wheel SHA-256: `{wheel_hash}`
- Build: Ubuntu 24.04, Python 3.13, OpenMPI 4, `PYOOMPH_USE_MPI=ON`

The bounded verification covers four serial software regressions: remeshing RSS,
remeshing lifetime, state-file continuation, and planar Taylor--Culick restart.
MPI availability is checked at import, but distributed MPI correctness, scaling,
other platforms, numerical convergence, and campaign-level scientific validity
are outside this release's evidence.
"""


def _copy_evidence(download: Path, stage: Path) -> tuple[Path, dict[str, Path]]:
    wheel = _find_wheel(download)
    selected: dict[str, Path] = {}
    for name in REQUIRED_ASSETS:
        selected[name] = _find_one(download, name)
    wheel_target = stage / wheel.name
    shutil.copyfile(wheel, wheel_target, follow_symlinks=False)
    for name, source in selected.items():
        shutil.copyfile(source, stage / name, follow_symlinks=False)
    return wheel_target, selected


def _write_checksums(root: Path, names: Iterable[str]) -> None:
    lines = [f"{sha256_file(root / name)}  {name}" for name in sorted(names)]
    (root / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def prepare(tag: str, candidate_sha: str, verification_run: int, output: Path,
            github: GitHub | None = None) -> dict[str, Any]:
    tag = validate_tag(tag)
    candidate_sha = validate_sha(candidate_sha)
    if type(verification_run) is not int or verification_run < 1:
        raise ReleaseError("verification run must be a positive integer")
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ReleaseError(f"output already exists: {output}")
    if not output.parent.is_dir():
        raise ReleaseError(f"output parent does not exist: {output.parent}")
    gh = github or GitHub()
    run = gh.api(f"repos/{REPOSITORY}/actions/runs/{verification_run}")
    validate_run(run, verification_run)
    validate_artifacts(gh.api(
        f"repos/{REPOSITORY}/actions/runs/{verification_run}/artifacts?per_page=100"
    ))
    _ensure_absent(gh, tag)
    with tempfile.TemporaryDirectory(prefix=".comphy-release-", dir=output.parent) as raw:
        temporary = Path(raw)
        download = temporary / "download"
        gh.download_artifacts(verification_run, download)
        sync_path = _find_one(download, "sync.json")
        verification_path = _find_one(download, "verification.json")
        wheel_source = _find_wheel(download)
        sync = load_json(sync_path)
        verification = load_json(verification_path)
        source = validate_evidence(sync, verification, candidate_sha, wheel_source, run)
        eligibility = release_eligibility(gh, candidate_sha)
        stage = temporary / "bundle"
        stage.mkdir()
        wheel, _ = _copy_evidence(download, stage)
        notes = build_release_notes(tag, candidate_sha, run, source,
                                    wheel.name, source["wheel_sha256"])
        (stage / "RELEASE-NOTES.md").write_text(notes, encoding="utf-8")
        payload_names = sorted(path.name for path in stage.iterdir())
        manifest = {
            "schema": SCHEMA,
            "repository": REPOSITORY,
            "tag": tag,
            "candidate_sha": candidate_sha,
            "verification_run": verification_run,
            "run_url": run["html_url"],
            "source": {"comphy_sha": source["comphy_sha"],
                       "upstream_sha": source["upstream_sha"]},
            "eligibility": eligibility,
            "wheel": wheel.name,
            "files": {name: {"sha256": sha256_file(stage / name),
                              "size": (stage / name).stat().st_size}
                      for name in payload_names},
        }
        (stage / "manifest.json").write_bytes(_json_bytes(manifest))
        _write_checksums(stage, [*payload_names, "manifest.json"])
        stage.replace(output)
    print(json.dumps({"bundle": str(output), "tag": tag,
                      "candidate_sha": candidate_sha,
                      "eligibility": manifest["eligibility"]}, sort_keys=True))
    return manifest


def _validate_bundle(bundle: Path) -> dict[str, Any]:
    if not bundle.is_dir() or bundle.is_symlink():
        raise ReleaseError(f"bundle is not a directory: {bundle}")
    entries = list(bundle.iterdir())
    unsafe = [path.name for path in entries if path.is_symlink() or not path.is_file()]
    if unsafe:
        raise ReleaseError("bundle contains non-regular entries: " + ", ".join(sorted(unsafe)))
    manifest = load_json(bundle / "manifest.json")
    if manifest.get("schema") != SCHEMA or manifest.get("repository") != REPOSITORY:
        raise ReleaseError("bundle manifest has an unsupported schema or repository")
    validate_tag(str(manifest.get("tag", "")))
    validate_sha(str(manifest.get("candidate_sha", "")))
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ReleaseError("bundle manifest contains no files")
    wheel_name = manifest.get("wheel")
    expected_files = REQUIRED_ASSETS | {"RELEASE-NOTES.md"}
    if (not isinstance(wheel_name, str) or not wheel_name.endswith(".whl")
            or wheel_name not in files or not expected_files <= files.keys()):
        raise ReleaseError("bundle is missing required release evidence")
    for name, receipt in files.items():
        if not isinstance(receipt, dict):
            raise ReleaseError(f"bundle manifest receipt is not an object: {name}")
        if Path(name).name != name or name in {"manifest.json", "SHA256SUMS"}:
            raise ReleaseError(f"unsafe bundle filename: {name}")
        path = bundle / name
        if not path.is_file() or path.is_symlink():
            raise ReleaseError(f"bundle file is missing or unsafe: {name}")
        if sha256_file(path) != receipt.get("sha256") or path.stat().st_size != receipt.get("size"):
            raise ReleaseError(f"bundle hash or size mismatch: {name}")
    checksums = (bundle / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    expected = [f"{sha256_file(bundle / name)}  {name}"
                for name in sorted([*files, "manifest.json"])]
    if checksums != expected:
        raise ReleaseError("SHA256SUMS does not match the bundle")
    actual = {path.name for path in bundle.iterdir() if path.is_file() and not path.is_symlink()}
    if actual != set(files) | {"manifest.json", "SHA256SUMS"}:
        raise ReleaseError("bundle contains unrecorded or missing files")
    return manifest


def _revalidate_remote_evidence(gh: GitHub, manifest: dict[str, Any], bundle: Path) -> None:
    run_id = manifest["verification_run"]
    run = gh.api(f"repos/{REPOSITORY}/actions/runs/{run_id}")
    validate_run(run, run_id)
    validate_artifacts(gh.api(
        f"repos/{REPOSITORY}/actions/runs/{run_id}/artifacts?per_page=100"
    ))
    with tempfile.TemporaryDirectory(prefix="comphy-release-evidence-") as raw:
        download = Path(raw) / "download"
        gh.download_artifacts(run_id, download)
        sync = load_json(_find_one(download, "sync.json"))
        verification = load_json(_find_one(download, "verification.json"))
        wheel = _find_wheel(download)
        source = validate_evidence(sync, verification, manifest["candidate_sha"], wheel, run)
        if source != {**manifest["source"], "wheel_sha256":
                      manifest["files"][manifest["wheel"]]["sha256"]}:
            raise ReleaseError("fresh Actions evidence differs from the prepared bundle")
        for name in REQUIRED_ASSETS:
            if name in manifest["files"]:
                fresh = _find_one(download, name)
                if sha256_file(fresh) != manifest["files"][name]["sha256"]:
                    raise ReleaseError(f"fresh Actions evidence differs for {name}")


def _tag_target(gh: GitHub, ref: dict[str, Any]) -> tuple[str, str]:
    obj = ref.get("object", {})
    if obj.get("type") != "tag":
        return str(obj.get("sha", "")).lower(), ""
    tag_object = gh.api(f"repos/{REPOSITORY}/git/tags/{obj['sha']}")
    return str(tag_object.get("object", {}).get("sha", "")).lower(), tag_object.get("message", "")


def _release_by_tag(gh: GitHub, tag: str) -> dict[str, Any] | None:
    release = gh.api(f"repos/{REPOSITORY}/releases/tags/{tag}", allow_not_found=True)
    if release is not None:
        return release
    # Authenticated tag lookup normally sees drafts; retain a paginated-list fallback.
    page = 1
    while True:
        releases = gh.api(f"repos/{REPOSITORY}/releases?per_page=100&page={page}")
        if not isinstance(releases, list):
            raise ReleaseError("release listing returned invalid data")
        for candidate in releases:
            if candidate.get("tag_name") == tag:
                return candidate
        if len(releases) < 100:
            return None
        page += 1


def _existing_retry(gh: GitHub, manifest: dict[str, Any], marker: str,
                    notes: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    tag = manifest["tag"]
    ref = gh.api(f"repos/{REPOSITORY}/git/ref/tags/{tag}", allow_not_found=True)
    release = _release_by_tag(gh, tag)
    if ref is None and release is None:
        return None, None
    if ref is None:
        raise ReleaseError("recoverable partial publication: draft release exists without its tag")
    target, message = _tag_target(gh, ref)
    if target != manifest["candidate_sha"] or message.removesuffix("\n") != f"{tag}\n\n{marker}":
        raise ReleaseError("existing tag is unrelated or targets a different commit; preserved unchanged")
    if release is None:
        return ref, None
    if release.get("tag_name") != tag:
        raise ReleaseError("existing release is unrelated; preserved unchanged")
    commitish = str(release.get("target_commitish", "")).lower()
    if SHA_PATTERN.fullmatch(commitish) and commitish != manifest["candidate_sha"]:
        raise ReleaseError("existing draft targets a different commit; preserved unchanged")
    if release.get("body") != notes:
        raise ReleaseError("existing draft release notes differ from this bundle; preserved unchanged")
    if release.get("draft") is not True and release.get("immutable") is not True:
        raise ReleaseError("existing release is neither a draft retry nor immutable; preserved unchanged")
    return ref, release


def _check_assets(gh: GitHub, release_id: int, bundle: Path,
                  names: Iterable[str],
                  verified: dict[str, tuple[int, int]] | None = None) -> dict[str, tuple[int, int]]:
    release = gh.api(f"repos/{REPOSITORY}/releases/{release_id}")
    assets = {asset["name"]: asset for asset in release.get("assets", [])}
    if set(assets) != set(names):
        raise ReleaseError("draft release asset names do not exactly match the bundle")
    readback: dict[str, tuple[int, int]] = {}
    for name in names:
        expected = sha256_file(bundle / name)
        asset = assets[name]
        identity = (asset["id"], asset.get("size", -1))
        if identity[1] != (bundle / name).stat().st_size:
            raise ReleaseError(f"uploaded asset size mismatch: {name}")
        digest = asset.get("digest")
        if digest:
            if digest != f"sha256:{expected}":
                raise ReleaseError(f"uploaded asset digest mismatch: {name}")
        elif verified is not None:
            if verified.get(name) != identity:
                raise ReleaseError(f"published asset identity changed: {name}")
        elif hashlib.sha256(gh.download_asset(asset["id"])).hexdigest() != expected:
            raise ReleaseError(f"downloaded asset hash mismatch: {name}")
        readback[name] = identity
    return readback


def publish(bundle: Path, github: GitHub | None = None) -> dict[str, Any]:
    required_environment = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_REF": "refs/heads/comphy",
        "PYOOMPH_RELEASE_APPROVED": "1",
    }
    wrong = [name for name, value in required_environment.items()
             if os.environ.get(name) != value]
    if wrong:
        raise ReleaseError("publishing is restricted to the approved comphy release environment; "
                           "invalid: " + ", ".join(wrong))
    bundle = Path(bundle).absolute()
    manifest = _validate_bundle(bundle)
    gh = github or GitHub()
    _revalidate_remote_evidence(gh, manifest, bundle)
    eligibility = release_eligibility(gh, manifest["candidate_sha"])
    if not eligibility["promotable"]:
        raise ReleaseError("candidate is stale: it is neither contained in comphy nor its fresh child")
    notes = (bundle / "RELEASE-NOTES.md").read_text(encoding="utf-8")
    manifest_hash = sha256_file(bundle / "manifest.json")
    marker = f"CoMPhy release bundle: {manifest_hash}"
    ref, release = _existing_retry(gh, manifest, marker, notes)
    asset_names = set(manifest["files"]) | {"manifest.json", "SHA256SUMS"}
    if release is not None and release.get("draft") is False:
        _check_assets(gh, release["id"], bundle, asset_names)
        target, _ = _tag_target(gh, ref)
        if target != manifest["candidate_sha"]:
            raise ReleaseError("immutable release tag target differs from the bundle")
        result = {"tag": manifest["tag"], "candidate_sha": manifest["candidate_sha"],
                  "release_id": release["id"], "url": release.get("html_url"),
                  "immutable": True, "recovered": True}
        print(json.dumps(result, sort_keys=True))
        return result
    if eligibility["state"] == "fresh":
        gh.api(f"repos/{REPOSITORY}/git/refs/heads/comphy", method="PATCH",
               fields={"sha": manifest["candidate_sha"], "force": False})
        if _ref_sha(gh, "comphy") != manifest["candidate_sha"]:
            raise ReleaseError("recoverable partial publication: comphy update was not confirmed")
    tag = manifest["tag"]
    if ref is None:
        tag_object = gh.api(f"repos/{REPOSITORY}/git/tags", method="POST", fields={
            "tag": tag, "message": f"{tag}\n\n{marker}",
            "object": manifest["candidate_sha"], "type": "commit",
        })
        gh.api(f"repos/{REPOSITORY}/git/refs", method="POST",
               fields={"ref": f"refs/tags/{tag}", "sha": tag_object["sha"]})
        ref = gh.api(f"repos/{REPOSITORY}/git/ref/tags/{tag}")
        target, message = _tag_target(gh, ref)
        if target != manifest["candidate_sha"] or marker not in message:
            raise ReleaseError("recoverable partial publication: annotated tag read-back failed")
    if release is None:
        release = gh.api(f"repos/{REPOSITORY}/releases", method="POST", fields={
            "tag_name": tag, "target_commitish": manifest["candidate_sha"],
            "name": tag, "body": notes, "draft": True, "prerelease": False,
        })
    asset_paths = [bundle / name for name in sorted(asset_names)]
    existing_names = {asset["name"] for asset in release.get("assets", [])}
    bundle_names = {path.name for path in asset_paths}
    if not existing_names <= bundle_names:
        raise ReleaseError("existing draft has unrelated assets; preserved unchanged")
    missing = [path for path in asset_paths if path.name not in existing_names]
    if missing:
        gh.upload_assets(tag, missing)
    verified_assets = _check_assets(gh, release["id"], bundle, bundle_names)
    gh.api(f"repos/{REPOSITORY}/releases/{release['id']}", method="PATCH",
           fields={"draft": False})
    final_ref = gh.api(f"repos/{REPOSITORY}/git/ref/tags/{tag}")
    target, _ = _tag_target(gh, final_ref)
    final = gh.api(f"repos/{REPOSITORY}/releases/{release['id']}")
    if target != manifest["candidate_sha"]:
        raise ReleaseError("published tag peeled target does not match the candidate")
    if final.get("draft") or final.get("tag_name") != tag or final.get("body") != notes:
        raise ReleaseError("published release read-back does not match the bundle")
    _check_assets(gh, final["id"], bundle, bundle_names, verified_assets)
    if final.get("immutable") is not True:
        raise ReleaseError("published release was not confirmed immutable")
    result = {"tag": tag, "candidate_sha": manifest["candidate_sha"],
              "release_id": final["id"], "url": final.get("html_url"),
              "immutable": True}
    print(json.dumps(result, sort_keys=True))
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--tag", required=True)
    prepare_parser.add_argument("--candidate-sha", required=True)
    prepare_parser.add_argument("--verification-run", required=True, type=int)
    prepare_parser.add_argument("--output", required=True, type=Path)
    publish_parser = subparsers.add_parser("publish")
    publish_parser.add_argument("--bundle", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            prepare(args.tag, args.candidate_sha, args.verification_run, args.output)
        else:
            publish(args.bundle)
    except ReleaseError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
