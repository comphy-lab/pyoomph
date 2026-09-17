# `test_the_event_remesh_reproduces_the_serial_one[coalescence-4]` was red

Status: **fixed 2026-09-07 (§7).** Written 2026-09-06 (§1-3, the failure), extended 2026-09-07
(§4, the cause; §7, the fix), all against `e7408619`. `axisymmetric_topological_changes.md` owns the feature; this document owns only this
failure.

## 1. What fails

    tests/test_mpi_axisymm_reconnection.py::test_the_event_remesh_reproduces_the_serial_one[coalescence-4]
    AssertionError: the coalescence remesh/bulk/usum is 379.682382, serially 380.677356
    assert 379.682382 == 380.677356 ± 7.6e-01

One quantity, `bulk/usum`, a **sum over a transferred field** after the coalescence event remesh, at
**4 ranks only**. Relative deviation **2.61e-3** against `_SUM_TOL = 2e-3`
(`tests/test_mpi_axisymm_reconnection.py:354`). Everything else in the digest passes, and that is
the important half of the finding:

- the `_EXACT` structural keys (`n_fragments`, `npoints`, `nnode`, `nelem`, `n_nonfinite`, `field`)
  are **equal**, so the mesh is the same mesh and no node landed on the wrong side of the event;
- `umin`/`umax`/`fmin`/`fmax` agree to **1e-6**, so the field extremes are unaffected.

Only the sum moves. That is consistent with the rank-local nearest-node blend the test's own
`_SUM_TOL` comment describes, and inconsistent with a wrong result.

## 2. It is deterministic, and it is not from this session's work

Reproduced identically — the same 379.682382, digit for digit — under all of:

- three consecutive runs of the current build;
- a build with `src/expressions.cpp`, `src/codegen.cpp`, `src/codegen.hpp` **reverted** (`git stash`
  + full `./build_for_develop.sh`), ruling out the codegen changes of `e7408619`;
- `pyoomph/__init__.py` checked out at `f31956c7`, ruling out the solver autoselection of
  `04974e3e`. That one was worth checking rather than assuming: the test pins no linear solver, and
  the standalone MUMPS **is** importable on this machine, so the new autoselection genuinely could
  have changed which solver these runs use. It did not change the number.

So: pre-existing, not a flake, and not sensitive to the linear solver.

## 3. The measurement that makes this more than a tolerance nit

Setting `_SUM_TOL = 1e-12` temporarily and reading off all four parametrisations gives the deviation
of each — this is the useful table, and it is what argues against simply widening the tolerance:

| case | quantity | mpirun | serial | relative |
|---|---|---|---|---|
| `pinch-2` | — | — | — | **bit-identical** |
| `pinch-4` | `bulk/uysum` | 203.721528 | 203.727361 | 2.9e-5 |
| `coalescence-2` | `bulk/usum` | 380.602006 | 380.677356 | 2.0e-4 |
| `coalescence-4` | `bulk/usum` | 379.682382 | 380.677356 | **2.6e-3** |

Two things follow.

**The tolerance was fitted to `coalescence-2`.** The `_SUM_TOL` comment says the blend fallback was
"Measured here: 3 of ~400 nodes, moving the sums by up to 2e-4 relative" — which is exactly the
`coalescence-2` row. The 2e-3 tolerance is that measurement with a 10× margin. `coalescence-4` is
13× the measured value, so it is outside what the comment claims to bound, not merely outside the
number that was written down.

**The growth from 2 to 4 ranks is a factor of 13**, which looked at the time like a trend worth
extrapolating to 8 ranks. §4 shows it is not a trend: the same four nodes fail to be located at every
rank count, and what changes is only which rank's blend of them survives. There is no rank count at
which this is bounded by anything but the spread of the field.

## 4. The cause, measured

Established 2026-09-07 against `e7408619`, by re-running the `coalescence` case standalone at 1, 2
and 4 ranks and diffing the merged `(x, y, u)` table node by node. Not 8 ranks: this machine's cap
is 4 (`-np 4`, no `--oversubscribe`).

**The fallback node count does not grow with the rank count.** The warning that
`nodal_interpolate_from()` prints is *character for character the same* at 1, 2 and 4 ranks:

    WARNING: interpolating liquid: 3 of 294 node(s) could not be located in the old mesh ...
      Nodes at: (0.0484819, 8.48998e-09), (0.0725599, -0.0139471), (0.0725599, 0.0139471)
    WARNING: interpolating liquid/axis: 1 of 40 node(s) could not be located in the old mesh ...
      Nodes at: (0, 9.09376e-10)

Three bulk nodes plus the one axis node, at identical positions, in every run. So §3's growth from
2e-4 to 2.6e-3 is **not** more nodes falling back. It is the same four nodes being blended worse.

**The whole gap sits on those four nodes.** The merged tables have the same 383 nodes with the same
coordinates in both runs; nine values differ at all, and four of them carry the deficit:

| node (r, z) | serial | 4 ranks | difference |
|---|---|---|---|
| (0.072560, 0.013947) | 1.012086 | 0.758316 | −0.254 |
| (0.000000, 0.000000) | 1.000000 | 0.747320 | −0.253 |
| (0.048482, 0.000000) | 1.000000 | 0.758278 | −0.242 |
| (0.072560, −0.013947) | 0.987914 | 0.758320 | −0.230 |
| five others, |d| ≤ 0.017 | | | −0.016 total |
| | | | **−0.99497** |

Those four coordinates are exactly the four the warning names. The five small ones are their
neighbours: `u` is a transient diffusion field and the digest is read after the Newton step that
follows the remesh, so a wrong nodal value bleeds one element outwards. −0.995 is the entire gap
between 379.682382 and 380.677356.

**Why the blend gets worse.** The four nodes are the fresh bridge; no rank can locate them because
they lie outside the old geometry *everywhere*, so `share_interpolation_across_ranks()` cannot
rescue them either - pooling only redistributes what some rank did place. Each rank then runs the
two-nearest-node blend over `source_nodes`, which is **its own share of the old mesh**
(`src/mesh.cpp:4520`), and the re-distribution keeps whichever rank ends up owning the node. Serially
the two nearest old nodes really are the nearest and the blend returns 1.000000, which is the exact
value of `u` there. At 4 ranks the winning rank's share does not reach the bridge at all: its nearest
old node sits down at z ≈ −0.24 and the blend returns 0.7583. The 2-rank run is not better in kind,
only in luck - the rank that won there happened to hold old mesh near the bridge.

So the mechanism is understood, and it is the one §5.1 describes. The deviation is not a function of
the rank count in any smooth way; it is a function of *which* rank wins the ownership of those four
nodes, which is why 2 → 4 ranks jumped by 13x and why extrapolating to 8 would prove nothing. It is
bounded by the spread of `u` over the old mesh, i.e. O(1) - the same order the test claims to catch.

## 5. Options, in the order they were considered

Option 1 is what was done; see §7.

1. **Make the blend rank-independent.** The drift exists because the handful of nodes that no rank
   can locate in the old mesh fall back to a nearest-node blend that is computed from each rank's
   own share of the old mesh. If that fallback were made deterministic — every rank blending from
   the same data, or the owner's answer being the only one computed — all four rows above would go
   to bit-identical and the tolerance could go away entirely rather than being raised. This is the
   right fix and the one that closes the item.
   Concretely: every rank already visits the same replicated destination node list in the same order
   (`share_interpolation_across_ranks`, `src/mesh.cpp:3846`). Have each rank report its best local
   candidate's squared distance for each still-missing node and take a global minimum over the ranks
   - an `MPI_Allreduce` with `MPI_MINLOC` on the same list, then a broadcast of the two blended
   values from the winner - and the answer is the serial one by construction, for both nearest nodes.
2. Widen `_SUM_TOL` and **rewrite its comment**, which would otherwise be left asserting a measured
   2e-4 next to a tolerance chosen to admit 2.6e-3. §4 argues against this: the error is not small
   and shrinking with resolution, it is one rank's arbitrary far-away node standing in for a value,
   so any tolerance that admits it is fitted to the partition that happened to be produced.
3. Do **not** mark it `xfail` without doing (1) or (2). The test is currently the only thing
   watching this quantity, and the structural keys it also checks are the guard against a node
   landing on the wrong side of the event.

## 6. Reproducing

    PYTHONPATH=<complex petsc arch>/lib \
      python3 -m pytest "tests/test_mpi_axisymm_reconnection.py::test_the_event_remesh_reproduces_the_serial_one" -q --full

About 10 s for all four parametrisations. `--full` is required — the MPI suites are marked `slow`
and are skipped without it. Complex PETSc on `PYTHONPATH` is required for the file's other tests;
this particular one does not need it, but running the file without it produces unrelated failures
elsewhere. Runs at 4 ranks, within the 4-core cap.

## 7. The fix

`Mesh::nodal_interpolate_from()` now resolves the fallback blend **globally** when the source mesh is
distributed and the destination replicated (`shared_across_ranks`), in `src/mesh.cpp`:

- halo copies are dropped from `source_nodes` first, so each physical old node is offered by exactly
  one rank and cannot be picked as both the first and the second nearest;
- the still-missing nodes are walked in the deterministic order
  `share_interpolation_across_ranks()` uses - `missing_nodes` is a set of *pointers*, ordered by the
  addresses the allocator handed out, so it differs from rank to rank and cannot index a buffer;
- two `MPI_Allreduce(MPI_MINLOC)` rounds pick the globally nearest and second-nearest source node.
  The globally nearest is somebody's local nearest, and the globally second nearest is then either
  that rank's local second or another rank's local nearest, so offering exactly that in round two is
  sufficient. `MINLOC` breaks a tie by the lower rank, which makes the winner unique;
- the winners' data - values through `field_map`, the interface dofs, the position history and the
  Lagrangian coordinates - is packed into one flat buffer per winner and summed across the ranks, so
  every rank applies the same arithmetic the serial branch applies to two node pointers.

Serial and non-distributed runs take the old path unchanged.

**Result.** The four bridge nodes now carry their exact serial values. `bulk/usum`:

| ranks | before | after | relative to serial |
|---|---|---|---|
| serial | 380.677356 | 380.677356 | — |
| 2 | 380.602006 | 380.660147 | 4.5e-5 |
| 4 | 379.682382 | 380.660147 | 4.5e-5 |

2 and 4 ranks now agree with each other to 1e-11 - the partition no longer enters the answer, which
is the property that was missing. The residual 4.5e-5 is five nodes around the bridge, none of them
in the fallback list: `u` is a transient diffusion field and this is the ordinary interpolation
difference of a *located* node near the event, the same effect as `pinch-4`'s pre-existing 2.9e-5.
`_SUM_TOL` was therefore tightened from `2e-3` to `5e-4` (that residual with a 10x margin) and its
comment rewritten to describe what it now bounds.

Verified: `tests/test_mpi_axisymm_reconnection.py` 18 passed, and 169 passed across
`test_mpi_remeshing`, `test_mesh_point_locator`, `test_boundary_interpolation_fixes`,
`test_mpi_facet_fields`, `test_mpi_adaptivity` and the three serial `test_axisymm_*_remesh` suites.
