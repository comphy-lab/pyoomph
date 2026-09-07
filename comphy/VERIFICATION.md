# CoMPhy fork verification

This contract supplies bounded software-regression evidence for a candidate
`comphy-lab/pyoomph` wheel. It establishes that the installed wheel imports
without checkout shadowing, includes MPI support, replaces a fixed finite-element
mesh without sustained resident-memory growth, reproduces an upstream state-file
continuation, and checkpoint/restarts a short planar Taylor--Culick construction.
It does not establish numerical convergence, physical validation, MPI-distributed
correctness, or the accuracy of the Taylor--Culick campaign.

## Prerequisites and doctor

The intended environment is a fresh GitHub-hosted Ubuntu runner with Python 3.13,
OpenMPI, Git, a candidate wheel built from the pinned candidate checkout with
`PYOOMPH_USE_MPI=ON`, and network access for Python dependencies. The wheel declares
its numerical and meshing dependencies; the runner additionally installs
`pytest>=8,<9` and `mpi4py>=4,<5` into a run-owned virtual environment.

The runner imports `pyoomph` with isolated Python mode, records its installed
version and resolved path, and calls `pyoomph.generic.mpi.has_mpi()`. It fails if
the import resolves under either checkout or if MPI support is absent.

## Run

From the GitHub Actions workspace containing trusted control code in `control/`
and the pinned fork checkout in `candidate/`:

```bash
python control/comphy/verify.py \
  --wheel wheelhouse/pyoomph-*.whl \
  --candidate-sha "$CANDIDATE_SHA" \
  --source candidate \
  --report receipts/verification.json
```

The wheel glob must resolve to exactly one path. The runner verifies that
`candidate/` has the supplied exact Git HEAD, hashes the wheel, creates its work
directory below `receipts/`, and installs the wheel into a temporary virtual
environment. `PYTHONPATH` is removed and all Python probes use isolated mode.

## Drive and assertions

The selected suite has four gates and a ten-minute execution timeout:

1. `comphy/tests/test_remeshing_rss.py` rebuilds the same roughly 1,000-element
   C2 mesh four warm-up times and twelve measured times. Each measured rebuild
   repeats boundary reapplication ten times, amplifying the historical
   `dimension * node-count` local-coordinate allocation leak to about 3.7 MiB
   per sample at this mesh size. The test asserts constant element count, an RSS
   least-squares slope no greater than 0.75 MiB per sample, and endpoint growth
   no greater than 8 MiB. RSS is current `VmRSS` from Linux `/proc`, not a source
   inspection or peak-only proxy.
2. The candidate's upstream `test_remeshing_leaks.py` runs its
   `via_recreation` regression, comparing ordinary shutdown with explicit
   `Problem.release()`.
3. The candidate's upstream `test_state_file_restart.py` runs the deterministic
   SuperLU transient case and compares the loaded state and continued solution
   with the uninterrupted run.
4. `comphy/tests/test_taylor_culick_restart.py` drives a coarse, short version of
   the planar co-moving case through a checkpoint. The independent reader must
   restore the control velocity exactly and reproduce the uninterrupted velocity
   and degrees of freedom within `1e-10` and `1e-9`, respectively. The case is
   adapted with attribution from `comphy-lab/taylor-culick-fem` commit
   `bb3695d12626bcb9996768aa727b93e0e5e964ed`.

Any failed or skipped gate fails the contract. The tests run serially; MPI support
is a build prerequisite checked by the doctor, rather than a claim that an MPI
simulation was exercised.

## Receipt and cleanup

`receipts/verification.json` is the durable machine-readable receipt. It contains
the candidate commit, wheel SHA-256, installed package path and version, MPI flag,
exact commands, exit statuses, full captured output, durations, overall result,
and UTC timestamps. The report is written on failure as well as success.

The temporary environment, staged tests, generated meshes, state dumps and solver
output are all below one run-owned directory. Python's temporary-directory guard
removes only that directory. The receipt survives outside it and records whether
cleanup completed.

## Failure triage

- A candidate/source SHA mismatch means the wheel provenance is not established;
  rebuild from the pinned checkout rather than weakening the check.
- A resolved import path under `candidate/` or `control/` means source shadowing;
  retain isolated mode and inspect the workflow's working directory and command.
- A missing MPI flag means the candidate build did not honour
  `PYOOMPH_USE_MPI=ON`.
- An RSS failure prints every sample, fitted slope and endpoint growth. Re-run the
  same candidate once to distinguish allocator noise; do not raise the bounds
  without demonstrating a non-leaking reference distribution on hosted Ubuntu.
- Restart failures retain the selected pytest output in the receipt. Diagnose the
  candidate code or wheel build; the contract must not redefine the observed
  difference as expected behaviour.
- A timeout is inconclusive evidence. Keep the receipt and reduce only redundant
  setup after identifying which recorded stage exceeded its bound.
