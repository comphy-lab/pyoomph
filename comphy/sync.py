#!/usr/bin/env python3
"""Mirror upstream and prepare an unpromoted merge; never write upstream."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile

FORK = "https://github.com/comphy-lab/pyoomph.git"
UPSTREAM = "https://github.com/cdiddens/pyoomph.git"


def git(root: Path, *args: str, check: bool = True, env=None):
    return subprocess.run(
        ["git", "-C", str(root), *args], text=True, capture_output=True,
        check=check, env=env, timeout=180,
    )


def revision(root: Path, ref: str) -> str:
    return git(root, "rev-parse", "--verify", ref).stdout.strip()


def ancestor(root: Path, old: str, new: str) -> bool:
    result = git(root, "merge-base", "--is-ancestor", old, new, check=False)
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr)
    return result.returncode == 0


def anonymous_environment():
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith("GIT_CONFIG")
        and key not in {"GIT_ASKPASS", "SSH_ASKPASS", "GH_TOKEN", "GITHUB_TOKEN"}
    }
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="/bin/false")
    return environment


def prepare(root: Path, fork: str, upstream: str, *, publish=False, push_env=None, report=None):
    """Use a fresh disposable repository. URLs are injectable only for offline tests."""
    git(root, "init", "--quiet")
    git(root, "config", "user.name", "CoMPhy maintenance")
    git(root, "config", "user.email", "noreply@comphy-lab.org")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "config", "core.hooksPath", str(root / "disabled-hooks"))
    git(root, "remote", "add", "fork", fork)
    git(root, "remote", "add", "upstream", upstream)
    git(root, "remote", "set-url", "--push", "upstream", "disabled://upstream")
    git(root, "fetch", "--no-tags", "fork", "+refs/heads/*:refs/remotes/fork/*")
    git(root, "fetch", "--no-tags", "upstream",
        "refs/heads/main:refs/remotes/upstream/main",
        "refs/heads/develop:refs/remotes/upstream/develop", env=anonymous_environment())
    base = revision(root, "refs/remotes/fork/comphy")
    if report is None:
        report = {}
    report.update({"schema_version": 1, "checked_at": datetime.now(timezone.utc).isoformat(),
              "fork": fork, "upstream": upstream,
              "comphy_sha": base, "mirrors": {}, "candidate_sha": base,
              "candidate_ref": "comphy", "candidate_created": False,
              "status": "current", "published": False, "publication": "not-requested"})
    refspecs = []
    for branch in ("main", "develop"):
        old = revision(root, f"refs/remotes/fork/{branch}")
        new = revision(root, f"refs/remotes/upstream/{branch}")
        if not ancestor(root, old, new):
            raise RuntimeError(f"{branch}: upstream diverged from mirrored {old}; no refs published")
        report["mirrors"][branch] = {"before": old, "after": new}
        if old != new:
            refspecs.append(f"{new}:refs/heads/{branch}")

    development = report["mirrors"]["develop"]["after"]
    if not ancestor(root, development, base):
        ref = f"sync/upstream-{base[:12]}-{development[:12]}"
        git(root, "checkout", "--detach", base)
        result = git(root, "merge", "--no-ff", "--no-edit", development,
                     "-m", f"Merge upstream develop {development[:12]} for review", check=False)
        if result.returncode:
            conflicts = git(root, "diff", "--name-only", "--diff-filter=U").stdout.splitlines()
            if not conflicts:
                raise RuntimeError(result.stderr or result.stdout)
            report.update(status="conflict", conflicts=conflicts, candidate_sha="", candidate_ref="")
        else:
            candidate = revision(root, "HEAD")
            previous = git(root, "rev-parse", "--verify", f"refs/remotes/fork/{ref}", check=False)
            if previous.returncode == 0:
                existing = previous.stdout.strip()
                parents = git(root, "show", "-s", "--format=%P", existing).stdout.strip().split()
                if parents != [base, development] or revision(root, existing + "^{tree}") != revision(root, candidate + "^{tree}"):
                    raise RuntimeError(f"Existing {ref} differs from proposed merge; no refs published")
                candidate = existing
            else:
                refspecs.append(f"{candidate}:refs/heads/{ref}")
                report["candidate_created"] = True
            report.update(status="candidate", candidate_sha=candidate, candidate_ref=ref)

    report["intended_refspecs"] = refspecs
    if publish and refspecs:
        # Only fast-forward refspecs, atomically, to the fork. Never --force/--mirror.
        report.update(publication="push-started", published=None)
        git(root, "push", "--atomic", "fork", *refspecs, env=push_env)
        report.update(publication="push-confirmed", published=True)
        actual = dict(line.split()[::-1] for line in git(root, "ls-remote", "fork", "refs/heads/*").stdout.splitlines())
        for spec in refspecs:
            sha, ref = spec.split(":", 1)
            if actual.get(ref) != sha:
                raise RuntimeError(f"Readback mismatch for {ref}")
        report["publication"] = "readback-verified"
    elif publish:
        report["publication"] = "no-changes"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    env = os.environ.copy()
    if args.publish:
        if os.environ.get("GITHUB_REPOSITORY") != "comphy-lab/pyoomph":
            parser.error("Publishing is restricted to comphy-lab/pyoomph Actions")
        token = os.environ.get("GH_TOKEN", "")
        if not token:
            parser.error("GH_TOKEN is required")
        auth = base64.b64encode(("x-access-token:" + token).encode()).decode()
        env.update(GIT_CONFIG_COUNT="1",
                   GIT_CONFIG_KEY_0="http.https://github.com/comphy-lab/pyoomph.git.extraheader",
                   GIT_CONFIG_VALUE_0="AUTHORIZATION: basic " + auth)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    report = {"status": "error", "published": False}
    try:
        with tempfile.TemporaryDirectory(prefix="comphy-sync-", dir=args.report.parent) as directory:
            prepare(Path(directory), FORK, UPSTREAM, publish=args.publish, push_env=env, report=report)
    except (RuntimeError, subprocess.SubprocessError) as exc:
        report["error"] = str(exc)
        report["status"] = "error"
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if report["status"] in ("error", "conflict"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
