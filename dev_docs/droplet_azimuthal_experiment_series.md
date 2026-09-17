# Handover: the azimuthal m-scan over the measured instability data

Status: **still blocked, no results; the symbolic wall is gone.** Written 2026-09-07 against
`3ac5316e`, §3 and §5 rewritten the same day once wall one was fixed. All 14 parameter sets of
`experimental_instability.txt` were run and none produced an eigenvalue. Two independent walls, one
symbolic and one physical, are described in §3 and §4; **§3 is fixed**, §4 is not; §5 says what to
do next. This document owns
the campaign; `subexpression_unit_analysis_stall.md` owns the codegen pipeline that §3 lives in, and
that document's "fixed" status is **not** contradicted here - the stall it fixed is genuinely gone,
what remains is a different failure in the same unit check.

The scripts live outside this repository, in
`~/docs/mypapers/nonmonotonic_final_rebuttal/codes/` (the rebuttal paper for the non-monotonic
water/1,2-hexanediol droplet work).

## 1. What was built

`run_droplet_evap_with_eigen.py` grew a module-level

    find_dominant_azimuthal_mode(p, m_presample=None, neigen=20, shift=10)

which presamples `m = 10 ... 1000`, brackets the coarse maximum by its neighbours in the presample
list and refines with a golden-section search on the integers, caching every `m` it has evaluated. It
returns `(m_crit, lambda_crit, lambda_m)` and writes `azimuthal_growth_rates.txt` into the output
directory. `__main__` is unchanged in behaviour.

Two corrections went in with it. The loop it replaced passed a hard-wired `azimuthal_m=100` for every
`m` in the presample list, so all eleven presamples solved the same eigenproblem; and it read
`numpy.real(get_last_eigenvalues()[0])`, which is only the largest real part on the multi-`m` path
(`problem.py:8679` sorts by `argsort(-alleigenvals)`), not through the plain solver sort. It is
`numpy.amax(numpy.real(...))` now.

`run_experimental_series.py` drives one case per data row. Each case is **its own subprocess** - a
second `Problem` in one process segfaults in the JIT loader - and writes `result.json` into
`run_droplet_evap_with_eigen_series/case_<num>/`. It resumes: a case that already has a `result.json`
is skipped unless `--force`. `--collect` re-assembles the output table from whatever finished, so a
crash costs only the case in flight. The output is `experimental_vs_simulation_instab.txt`: the input
table with `m_sim[-]` and `lambda_sim[1/s]` inserted before the `note` column, blank where the case
did not reach `t_instab`.

Each case sets that row's `V_0`, `theta_0`, `RH`, `T` and `t_instab`. The `R[mm]` column is **not**
set and does not need to be: `DropletGeometry(V_0, theta_0).base_radius` reproduces it to four
decimals for every pinned row, which is what the mesh already derives. The two rows the data header
marks as unpinned (76, 77) have `R_lab/R_geom = 1.25` and are simulated pinned at `R_geom`; row 70 is
skipped, being marked EXCLUDED with `nan` targets and a 2.16 ratio.

One deliberate deviation from the data file: its header prescribes a fixed reference `T = 21 degC`
"throughout the SI, not at each row's own measured T", and this series uses each row's own measured
`T` instead, on Christian's instruction. That matters for §3.

## 2. The result

    case t_reach   t_instab theta0    theta_end ndof     mode
    67   204.5     278.2     37.22    20.57     81782    stall
    69    79.2     100.7     26.83    14.19     64789    stall
    70       -         -     40.00        -         -    skipped (EXCLUDED)
    76    76.0     121.0     43.74    24.57     94320    stall
    77   125.3     222.5     41.70    23.33     91653    stall
    78   389.4     648.0     38.65    21.47     85335    stall
    79       -     107.4     11.97        -         -    UNITS
    80       -     126.2     13.17        -         -    UNITS
    84    89.8     134.0     14.38     6.98     42045    stall
    86       -     225.1      9.30        -         -    UNITS
    87       -     284.7     12.24        -         -    UNITS
    88       -     231.6     15.29        -         -    UNITS
    89    89.9     184.8     16.29     8.03     47337    stall
    90    75.7     119.7     11.87     5.89     37848    stall

`experimental_vs_simulation_instab.txt` exists with both new columns present and empty throughout.
Its structure is correct, so a re-run after the fixes below only has to populate it.

## 3. Wall one: a float exponent puts a dimensional argument inside atan2 - FIXED

**Status: fixed** (2026-09-07, on top of `3ac5316e`). All eight temperatures of the reproducer below
now pass. What follows is the diagnosis as it was found, with the outcome; the campaign itself still
has to be re-run (§5).

Five cases never got past `initialise()`:

    The added residual contribution is not dimensionless.
    It still carries the base units: kilogram, meter
    ...
    cos(-(2.0)*atan2(0, meter^(-3)*(-(46.511...)*<SHAPEEXP of massfrac_12hexanediol ...>*kilogram
                                    +(997.511...)*kilogram)))

raised from `src/codegen.cpp`'s dimensional-error path, on the `liquid_gas` interface residual
(`_kin_bc`, `masstrans_water`) rather than the bulk.

The proximate mechanism is GiNaC's `power::real_part`, whose guard is

    basis.is_equal(a) && exponent.is_equal(c) && (a.info(nonnegative) || c.info(integer))

Neither disjunct holds: the basis is a mixture mass density, an `add` with a negative term that
cannot be proved nonnegative, and the exponent is **inexact**, for which `info(integer)` is false. So
it falls through the integer-binomial branch to the polar form
`|X|^c * exp(-d*atan2(b,a)) * cos(c*atan2(b,a)+d*log|X|)`, and the dimensional basis lands inside
`atan2`, where the unit split cannot separate it.

### 3.1 Where the float came from

The exponent is born exact. `d/deps` of the interface residual's `(rho*(w-1))^(-1)` gives an exact
`-2`, and it is still exact when `ReplaceFieldsToNonDimFields` hands the contribution to the
azimuthal real/imaginary split. Measured, with a temporary trace in
`SubExpressionsToRealAndImag::operator()`:

    [POWEXP] exponent before=-2 mapper returns=-2.0 exponent after=-2
    [SPLIT_ENTRY] exponent=-2 pred=0
    [SPLIT_BORN] map
       IN [pred=1 class=power exponent=-2.0 crat=0]: (951*<SHAPEEXP of massfrac_12hexanediol ...>...)^(-2.0)

i.e. the split's own memo, asked for the exact `-2`, answered `-2.0` - and the power it was the
exponent of came out inexact even though nothing had rewritten it.

`SubExpressionsToRealAndImag` (`src/expressions.cpp`) memoises in a `GiNaC::exmap`, and it was the
one mapper of this pipeline that did not exempt numbers. Two GiNaC properties make that fatal:

* numbers are hashed and compared **by value, not by representation**. `numeric::calchash` says so
  itself - *"3 and 3.0 share the same hashvalue. That shouldn't really matter, though."* - and
  `numeric::compare` is a `cln::compare` of the values. So an exact `-2` and an inexact `-2.0` are
  literally the same key in an `exmap`.
* `ex::compare()` (`ginac/ex.h`) **unifies** two expressions it finds equal by rebinding one's
  pointer to the other's (`ex::share`). So the mere *lookup* of the exact `-2` against a stored
  `-2.0` rewrote the `exponent` member of the power **in place**, which is why the trace above shows
  `inp` changing under its own operator().

The corruption goes both ways - in the minimal reproducer below the buggy build also turns `-2.0*x`
into `-2*x` when an exact `-2` is met first - so it is not only exponents that were at risk.

**The regressing commit is `e7408619`** ("Stop the residual mappers from treating an expression DAG as
a tree", 2026-09-06), which introduced all three of these `exmap` memos at once and guarded none of
them against numbers - a day *before* the round-2/3 commits this document first suspected. The first
version of this section was wrong to blame `c024a2f9`, and wrong that "the float is new": what the
round-2/3 commits changed is only *which* of two equal numbers the memo meets first, which is
precisely why the failure was intermittent before `451bd795` and deterministic after it. Neither
`PYOOMPH_DISABLE_UNIT_MASK=1` nor `PYOOMPH_DISABLE_REIM_FOLD=1` makes the failure go away, which is
what ruled those out.

### 3.2 The fix

Two commits:

1. `SubExpressionsToRealAndImag` returns a `GiNaC::numeric` unchanged **before** touching its cache,
   which is the same rule `ReplaceFieldsToNonDimFields`, `SubexpressionMasker` and `MemoisedSubs`
   already follow. `DrawUnitsOutOfSubexpressions` and `MeshToCoordinateShapes`, the two other
   `exmap`-memoised mappers of this pipeline, get the same guard - they had the same hole.
   **This is the commit that cures the reported case**: all four failing temperatures pass with it
   alone, before the GiNaC patch below was linked in.
2. A defensive fifth vendored GiNaC patch,
   `citools/patches/ginac-inexact-whole-number-exponent.patch`: `power::real_part()`/`imag_part()`
   redo the call with the exact integer when the exponent is an inexact numeric of whole-number
   value, so the binomial branch is taken and the basis never reaches an `atan2`. It closes the class
   but it is not what fixed this bug - it was not in the binary for the passing runs above.

Minimal reproducer of the defect itself, no model needed:

    from pyoomph import _pyoomph_core as core
    x, y = core.GiNaC_new_symbol("x"), core.GiNaC_new_symbol("y")
    core.GiNaC_split_subexpressions_in_real_and_imaginary_parts(-2.0*x + (x+y)**(-2))
    # before:  -(2.0)*x+(y+x)**(-2.0)      after:  -(2.0)*x+(y+x)**(-2)

pinned by `tests/test_azimuthal_codegen.py::test_azimuthal_split_keeps_exact_and_inexact_numbers_apart`
and `::test_real_part_of_an_inexact_whole_number_power_stays_polynomial`.

### 3.3 Result

Reproducer, `Scratchpad/eigenseries_run/bT.py <T>`, `initialise()` only, default
`V_0`/`theta_0`/`RH`, varying only `p.temperature`:

    T [degC]  21.0  21.0001  22.0  22.5  22.75  23.0  23.24  25.0
    before    ok    ok       ok    FAIL  FAIL   ok    FAIL   FAIL
    after     ok    ok       ok    ok    ok     ok    ok     ok

It was never a threshold in `T`: 23.0 passed between two failing neighbours. `T` is only the input
that perturbs the numeric constants, and through them which numbers meet in the memo.

Generated C is **byte-identical** over the reference set - `falling_droplet`,
`custom_math_dimensional_tennis`, `stokes_nonnewtonian` and the two azimuthal codegen cases, 23 files
- against a build with both changes reverted. The deeply nested synthetic case stays flat in the
nesting depth (`initialise()` 0.094 / 0.124 / 0.139 / 0.195 / 0.315 s at depths 3 to 7, peak RSS
161 MB throughout), and `run_droplet_evap.py --quick-test` still reaches and completes the first
Newton solve in 8 steps, 1.11506e-12, peak RSS 315 MB, matching the recorded intermediate residuals
exactly (step 7: 2.35951e-08).

**`b1909a8c` was exonerated** on the way: `PYOOMPH_UNIT_CCF_MAX_TERMS=100000`, the
`collect_common_factors` gate fully disabled, still failed at both `T = 23.24` and `T = 22.5`.
`PYOOMPH_DISABLE_UNIT_MASK=1` and `PYOOMPH_DISABLE_REIM_FOLD=1` were ruled out the same way, which is
what moved the search into `ReplaceFieldsToNonDimFields`' own mapper pass.

**What the round-2 commits did change is the determinism.** Before, the failure was intermittent: it
first presented as a clean function of the temperature, which is a very convincing false signal, and
then the same commands passed ~15/15 in the same directories. That was heap/pointer-ordering
nondeterminism in GiNaC's term order, the class of `dof_ordering.md`'s hazard - and, in hindsight,
exactly what a defect that depends on *which of two equal numbers the memo met first* looks like. It
became deterministic per parameter value, which is what made it findable.

Every row of `experimental_instability.txt` sits at 22.6-23.6 degC, squarely in the failing band, so
this alone stopped five cases and would have stopped more if wall two did not reach them first.

## 4. Wall two: the transient does not reach t_instab

Eight cases stalled: Newton hits its ten-iteration cap, `adaptive_unsteady_newton_solve` halves the
step repeatedly and oomph-lib aborts at `problem.cc:11736` with `dt = 9.09e-13` below the `1e-12`
floor. They died between 63 % and 79 % of their `t_instab`, at contact angles from 29.8 down to 5.9
degrees, with 38k-94k dofs.

The likely cause is the contact-line resolution. `run_droplet_evap_with_eigen.py:17` asks for

    pr0 = self.point(geom.base_radius, 0, size=-0.0001)

against `run_droplet_evap.py:17`'s `size=-0.1`. A negative size is a **factor on
`default_resolution`** (`pyoomph/meshes/gmsh.py:453`), so with `default_resolution = 0.05` the eigen
script asks for `5e-6 * R` at the contact line where the other asks for `5e-3 * R` - a thousand times
finer. That is where the dof counts come from, and as `theta` collapses the contact-line elements
degenerate.

The `FEM` entries in `experimental_instability.txt`'s `note` column came from `run_droplet_evap.py`,
i.e. from `size=-0.1`, which is why the data file claims rows 67, 69, 79, 80, 84 and 90 were
FEM-reachable while this series reaches none of them. Christian chose to keep `size=-0.0001` and
leave stalled rows blank rather than change the mesh, since the mesh changes the eigenvalues and not
just the cost; the `size=-0.1` arm has therefore **not** been run and is the obvious next experiment.

An early read that this only affected steep `theta_0` was **wrong** and is recorded here so it is not
repeated: case 90 starts at 11.87 degrees, below the 12-degree default that runs clean, and still
stalls.

## 5. Next steps

1. ~~Fix the inexact exponent of §3.~~ **Done**, see §3.2. The five UNITS cases should now reach
   the transient, where wall two (§4) may or may not stop them - that is untested, the fix was
   verified on `initialise()` only.
2. Re-run the series. It resumes, so nothing already computed is lost:

       cd ~/docs/mypapers/nonmonotonic_final_rebuttal/codes
       PYTHONPATH=$PETSC_DIR/$PETSC_ARCH_COMPLEX/lib python3 -u run_experimental_series.py

   Complex PETSc is mandatory; without it the eigensolver falls back to scipy without saying so. On
   this machine the arch is `~/code/packages/petsc_pyoomph/pyoomph_petsc_opt/lib` - look it up rather
   than pasting that, a nonexistent `PYTHONPATH` entry is not an error.
3. Decide the mesh question of §4. If the point is to compare against the `N_t` column, the
   `size=-0.1` arm is the one that matches how the data file's own FEM columns were produced.
4. Do not rebuild while a series is running. The 15:32 rebuild swapped the module underneath case 78
   and split this campaign across two builds, which is most of why §3 took as long to pin down as it
   did.
