# CoMPhy pyoomph

This fork maintains a tested CoMPhy integration of
[Christian Diddens' development repository](https://github.com/cdiddens/pyoomph).
The original project is [pyoomph/pyoomph](https://github.com/pyoomph/pyoomph).
Upstream authorship, licence and Git history are preserved.

## Branches and releases

| Ref | Contract |
| --- | --- |
| `main` | Exact mirror of `cdiddens/pyoomph:main`; no CoMPhy commits. |
| `develop` | Exact mirror of `cdiddens/pyoomph:develop`; no CoMPhy commits. |
| `comphy` | Default branch; reviewed upstream integrations and CoMPhy changes. |
| `sync/upstream-<base>-<upstream>` | Unpromoted merge candidate for two exact commits. |
| `fix/<topic>` | Focused development; keep generally useful changes easy to contribute. |
| `comphy-YYYY.MM.DD.N` | Immutable tested snapshot, with linked verification evidence. |

There is no tested CoMPhy release at bootstrap. The first successful candidate
check is evidence for review, not an automatic release. See the
[maintenance runs](https://github.com/comphy-lab/pyoomph/actions/workflows/comphy-maintenance.yml)
for current status; the latest green run may refer to an unpromoted candidate.

## Daily tracking

The **CoMPhy upstream maintenance** Action runs daily at 05:23 UTC and can be
started manually. It fetches upstream, checks ancestry, and publishes mirror
updates using fast-forward-only pushes. An upstream history rewrite fails
without publishing any refs. A merge conflict advances the pristine mirrors
but leaves `comphy` unchanged and reports the conflicting paths.

New upstream work is merged into a uniquely named candidate branch, retaining
both parent commits. The Action builds an MPI-enabled Linux/Python 3.13 wheel
and runs [the bounded verification procedure](comphy/VERIFICATION.md). A
scheduled run reuses an existing candidate SHA and checks it again until it is
promoted, so an interrupted build cannot leave a candidate permanently untested.
Manual dispatch also checks a current `comphy` with no pending update.
The maintenance Action never promotes candidates, tags releases, updates
downstream lockfiles, installs on compute hosts or opens upstream PRs. The
separate **CoMPhy release** Action can promote and publish an explicitly
approved snapshot, as described below.

The `comphy-sync` artifact records source SHAs, mirror changes and candidate
identity. `comphy-verification` retains test evidence and the built wheel for
90 days. Link the exact run and preserve its evidence with each accepted
release; an expired Actions artifact is not a permanent release record.

Scheduled workflows can be delayed or disabled by GitHub after prolonged
repository inactivity. Check the last successful run time, not only its green
badge. Run **CoMPhy upstream maintenance** manually after an interruption.

## Accepting an update

1. Inspect the candidate diff and upstream commit list. Resolve any merge
   conflicts explicitly; never use automatic `ours`/`theirs` conflict choices.
2. Require a successful maintenance run whose `verification.json` names the
   candidate's exact SHA. Inspect the assertions and limitations, including
   whether any required test was skipped. Linux serial checks do not establish
   MPI scaling, other-platform support or campaign-level scientific validity.
3. Check that the candidate's first parent is still the current `comphy` head.
   If `comphy` moved, prepare and test a new candidate; do not reuse old results.
4. Fast-forward `comphy` to the reviewed candidate. Do not squash, rebase
   published history, force-push, or merge the entire CoMPhy branch upstream.
5. For a release, create a new annotated `comphy-YYYY.MM.DD.N` tag on that exact
   tested commit. Record the upstream SHA, candidate SHA, build configuration,
   test run URL, wheel checksum and CoMPhy change summary in its release notes.
   Attach the verification receipt and wheel; never move an existing tag.

## Release workflow

Open [CoMPhy release](https://github.com/comphy-lab/pyoomph/actions/workflows/comphy-release.yml)
on the `comphy` branch. Supply a new `comphy-YYYY.MM.DD.N` tag, the full tested
candidate SHA, and the successful **CoMPhy upstream maintenance** run ID.

Use **prepare** first. This mode reads the original run and downloads its
artifacts, verifies the candidate identity and wheel checksum, and produces a
`comphy-release-proposal` artifact. It creates no Git tag or GitHub Release and
does not move `comphy`. Review its `RELEASE-NOTES.md`, `manifest.json`,
`SHA256SUMS`, verification receipt and exact wheel. A stale candidate can be
inspected, but cannot be promoted by the publisher; obtain a fresh maintenance
result after intervening changes to `comphy`.

To release the reviewed version, run the same workflow with **publish** and
the exact tag/SHA/run ID. The preparation job presents the complete proposal
again before the publishing job waits for `VatsalSy` to approve the protected
`comphy-release` environment. Approve only after checking that run's proposal.
The approval applies to that specific version and its notes, not future releases.

After approval the publisher rechecks the original evidence and current branch,
fast-forwards `comphy` if the candidate is still eligible, creates an annotated
tag and draft release, uploads and verifies the exact tested assets, and only
then publishes. Repository release immutability freezes both assets and tag.
Keep that repository setting enabled: the workflow token cannot read admin
settings, so the publisher confirms immutability on the final release readback.
The release retains provenance and verification evidence beyond the Actions
artifact retention period. No source rebuild takes place during publication.

If publication is interrupted, retain the existing tag/draft and the exact
proposal bundle. Use **Re-run failed jobs** on that same publishing run: the
successful preparation job and its proposal artifact are reused. A fresh
dispatch or rerun of all jobs correctly refuses an existing tag. The publisher
accepts only a matching retry; unrelated tags,
different notes or checksums, and changed targets are refused. Never delete or
move a release tag to repair a failed attempt. For a changed release, choose a
new tag and prepare a new proposal.

The CI wheel is built on Ubuntu 24.04 for Python 3.13 with OpenMPI 4. It is a
tested build artifact, not a promise of binary compatibility with every Linux
host. Source installations must preserve their own MPI/build settings.
These release checks establish bounded serial regressions and provenance;
they do not establish distributed-MPI correctness or campaign validation.

The implementation is `comphy/release.py`; its offline guard and recovery
tests are run with `python3 -m unittest discover -s comphy -p test_release.py -v`.
This repository-specific publisher supports the calendar tags above and never
calls or publishes through upstream's release workflows.

Review inherited workflows during integration. Upstream publishing/build
workflows are disabled in this fork at bootstrap; newly introduced upstream
workflows need explicit review before enabling. CoMPhy publishes its own
release assets only; it does not publish the upstream `pyoomph` PyPI package.

## Installation and reproducibility

Use a chosen immutable tag or full commit in each downstream project's
`[tool.uv.sources]`, then commit the resulting `uv.lock`. For example, the
source entry has the form `pyoomph = { git =
"https://github.com/comphy-lab/pyoomph", rev = "<full-tested-commit>" }`.
Keep the project's existing MPI/build settings. A moving `develop` branch is
for tracking; it is not an automatic production upgrade channel.

Build and test upgrades in a separate environment. Existing running processes
and their environments must not be rebuilt or replaced as part of mirroring.
The plain Python version string does not identify our fork snapshot: retain
the Git SHA and build flags alongside it.

## Contributions and write boundary

Track local bugs in [this fork's Issues](https://github.com/comphy-lab/pyoomph/issues)
and maintained changes in [COMPHY-CHANGES.md](COMPHY-CHANGES.md).
Preserve attribution and add a regression test for each solver fix. Use
`git cherry-pick -x` for deliberate backports so their original SHAs remain
visible; ordinary upstream updates use merges.

**No upstream PR, issue, comment, push or other upstream write is permitted
unless Vatsal explicitly requests that action.** Merely implementing or
publishing a change in this fork is not that request.

When a PR is explicitly requested, branch from the intended upstream target,
include only the relevant fix and tests, and push that contribution branch to
this fork. Confirm the named upstream repository and target branch before
opening the PR. Do not include CoMPhy administration or unrelated changes.

Local checkouts use `origin` for `comphy-lab/pyoomph` and `upstream` for
anonymous fetching from `cdiddens/pyoomph`. Configure `remote.pushDefault` as
`origin` and the upstream push URL as `disabled://upstream`. These are local
accident guards; CI's repository-scoped `GITHUB_TOKEN` is the write boundary.
Do not supply an upstream-capable personal token to maintenance automation.
