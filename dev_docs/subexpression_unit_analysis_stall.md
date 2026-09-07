# Code generation stalls on a deeply nested subexpression under azimuthal stability

Status: **fixed.** The reported droplet builds and solves. Three walls were found, one behind the
other, and all three are gone: `add_residual` walking the residual DAG as a tree (§4), the azimuthal
real/imaginary split multiplying products out until a marker argument held 14806 terms (§5.1), and
the code writer having the same DAG-as-tree problem `add_residual` had (§5.3). Written 2026-09-06
against `e7408619`, rewritten 2026-09-07 against `6cec0346` and again against `7297d5e9`. This
document owns the reasoning; `code_generation.md` owns the unit-check pipeline it happens in.

## 1. The symptom, and what it is not

A water/1,2-hexanediol evaporating droplet (UNIFAC activity coefficients, multi-return callbacks)
with

    p.setup_for_stability_analysis(azimuthal_stability=True, analytic_hessian=False)

never returned from `p.initialise()` (until `451bd795`; §5.1). It was **not** a deadlock, not the solver, and not MPI: the
process sat at 100 % CPU in `FiniteElementCode::add_residual` for the `liquid_gas` interface's
mass-transfer residual, built at `pyoomph/materials/mass_transfer.py:194`
(`ieqs.add_residual(weak(j - rhs, jtest))`).

The same script **without** `azimuthal_stability=True` completed throughout (about a minute). That
difference was the whole of the diagnosis: azimuthal stability is what routes the residual through
`split_subexpressions_in_real_and_imaginary_parts`, which rebuilds every `subexpression()` in it.

## 2. Why this class of bug exists at all

A pyoomph residual is a **DAG**, not a tree, and `subexpression()` is precisely the marker that
creates the sharing — that is its purpose. Every GiNaC operation pyoomph reaches for here
(`ex::map`, `expand()`, `has()`, `subs()`, `const_preorder_iterator`, `evalm()`) is a **tree**
operation: it visits a shared subtree once per parent and, when it rebuilds, hands back separate
copies. On a DAG of depth *d* that is exponential.

Every one of the defects fixed in `e7408619` is an instance of this, and so is everything in §4
below. Any future mapper added to this pipeline will be too unless it memoises.

## 3. The nesting is real, and capping it at the source was rejected

Symbolic UNIFAC (`pyoomph/materials/activity.py:182-331`, wrapped through
`UNIFACPyoomphExpressionGenerator.subexpression`, `pyoomph/materials/generic.py:2683`) is a genuinely
nested composition: depth about 7, fan-out about 6, of the order of 440 markers counted as a tree —
and every one of them is a reused intermediate, which is the whole point of the marker. The azimuthal
split (`SubExpressionsToRealAndImag`, `src/expressions.cpp`) then emits
`subexpression(real_part(m)) + I*subexpression(imag_part(m))` per complex marker, roughly doubling
that per level. The "9610 nested markers" read off a gdb frame in the first version of this document
is a **tree** count of a much smaller DAG.

Capping the nesting at the source was considered and rejected: the markers carry the common
subexpression elimination, and removing them makes the generated code worse. The two-component
mixture here is below the `use_multi_return=3` threshold (`generic.py:2793`), so its UNIFAC stays
symbolic; `use_multi_return=True` remains a user-level workaround that sidesteps the whole problem
by turning the activity coefficients into one opaque callback.

## 4. What was fixed: the markers are masked behind placeholder symbols

`DrawUnitsOutOfSubexpressions` works strictly bottom-up, so by the time it analyses a marker's
argument every nested marker in it has already been rewritten to `factor*unit*subexpression(rest)`
with `rest` proven free of base units by `collect_base_units()`'s own tail check
(`src/expressions.cpp`). `pyoomph::expressions::SubexpressionMasker` (`src/expressions.hpp`) replaces
each such marker by a plain placeholder symbol before the analysis and splices the marker `ex`
objects back afterwards — **by pointer**, so nothing is substituted and no interior is rebuilt. That
is what sank the two attempts recorded in the first version of this document (a `GiNaC::subs` with a
9610-entry map, and a division that blew RSS past 7 GB): the idea was right, the *materialisation* of
the result was what cost. Both mappers memoise.

Then (`972d96a5`) the same masker is kept for the rest of `add_residual` — the base-unit prescan,
`expand().normal()`, the surviving-unit scan, the `bu -> 1` substitution and the matrix scan — since
each of those is another tree walk over the same DAG. `PYOOMPH_DISABLE_UNIT_MASK=1` restores the old
path and is part of the codegen fingerprint (`sw_no_unit_mask`).

Two smaller items came out of the same trace: `subexpression()` is now registered with a static
return type (`dc3325fe`), so `function::return_type()` stops rediscovering it by walking the
first-operand chain; and `collect_base_units()`'s add branch builds its normalised sum in one
`GiNaC::add` instead of `+=` per term (`0cef1c7a`), which was quadratic.

### 4.1 The one thing that must not be masked

`GiNaCMultiRetCallback` is the only pyginacstruct whose `subs()` descends into what it wraps; neither
`ex::map` nor `const_preorder_iterator` does. A base unit inside a callback's invocation is therefore
invisible to the tail check *and* to the masker, but is still reached by the `bu -> 1` substitution at
the end of `add_residual`. Masking such a marker hid the unit from the one pass that would have
removed it, and `mol` and `kilogram` were emitted into the generated C, where gcc rejected them
(`tests/test_diffusivity_estimates.py`, the finite-difference thermodynamic factor). So a marker whose
argument contains a callback node is not maskable (`6cec0346`); the rule is inductive, because a
nested marker that hides one is not maskable either and therefore stays visible to the scan.

### 4.2 Measured

Synthetic residual, fan-out-2 chain of nested dimensional markers, axisymmetric,
`setup_for_stability_analysis(azimuthal_stability=True)`; `PYOOMPH_TIME_ADD_RESIDUAL=1`, per
contribution:

| depth | `DrawUnits` | prescan | `subs`+matrix scan | `add_residual` total |
|-------|-------------|---------|--------------------|----------------------|
| 4     | 0.44 → 0.014 s | 0.038 → 0 s | 0.184 → 0.001 s | 0.68 → 0.011 s |
| 5     | 5.11 → 0.019 s | 0.453 → 0 s | 2.23  → 0.001 s | 2.74 → 0.061 s |
| 6     | 61.5 → 0.024 s | 5.26  → 0 s | 25.4  → 0.001 s | 30.7 → 0.088 s |

i.e. flat instead of about a factor of ten per nesting level. The same case without
`azimuthal_stability` builds in 0.8 s at depth 6, which is what says the split is the trigger.
`tests/test_azimuthal_codegen.py::test_deeply_nested_dimensional_subexpressions_build_under_azimuthal_stability`
pins the depth-4 case.

## 5. What was found underneath it, and what fixed it

Both walls of the first version of this section are gone, and only one of the two candidate
remedies is what removed them. Measured 2026-09-07 against `7297d5e9`.

### 5.1 The droplet: the split was multiplying products out, and now does not

The 14806-term sum was not something `collect_base_units` had to be made to survive - it was
something the azimuthal split had no business producing. `SubExpressionsToRealAndImag` rewrites each
complex marker into `subexpression(real_part(m)) + I*subexpression(imag_part(m))`, so its markers are
real **by construction**; GiNaC could not see that, because `subexpression()` had no `real_part_func`
and therefore fell back to `basic::real_part`, i.e. a held `real_part_function(marker)`. With every
already-split factor looking fully complex, `mul::find_real_imag` ends in `rp.expand()`/`ip.expand()`
and turns a product of *n* such factors into `2*4^(n-1)` terms inside the next marker's argument -
measured `n=7 -> 8192`, `n=8 -> 32768`. That is where the droplet's chain of 32, 322, 752, 14806 came
from, and the answer to question 3 of the old list ("where does a 14806-term marker argument come
from?") is: from here.

`subexpression()` now carries a `real_part_func`/`imag_part_func` pair backed by a memoised
`subexpression_wrapped_is_real()` (`src/expressions.cpp`). Each factor's real and imaginary parts
become single markers rather than held pairs, i.e. `2^(n-1)` terms, and GiNaC's own
`real_part_function` already reports `imag_part == 0`, so the property propagates up the nesting by
induction and every level of the split stays short. The memo is not optional: the question is
answered per marker by asking it of every nested marker, over a DAG.
`PYOOMPH_DISABLE_REIM_FOLD=1` restores the old, unknowing behaviour and is the A/B lever.

It uncovered a genuine GiNaC bug on the way. `power::real_part()`/`imag_part()` build their binomial
expansion with `pow(a, NN-n)`, and a binomial expansion needs the `0^0 == 1` convention for its end
terms, which GiNaC's `pow()` refuses - so as soon as a basis has an *exactly* zero real or imaginary
part (which is what the fold newly makes possible) the whole expression dies with
`power::eval(): pow(0,0) is undefined`. `pow(I*x,2).real_part()` reproduces it without pyoomph.
`citools/patches/ginac-binomial-real-imag-pow00.patch` uses the convention for the end terms only;
it is worth sending upstream.

Measured, droplet with `--quick-test` and the complex PETSc: it reaches **and completes** the first
Newton solve, in 42 s wall and 308 MB peak RSS, with `add_residual` 20.8 s over 88 contributions and
`write_code` 3.9 s. The largest sum reaching `collect_base_units` in the whole run is **69** terms,
against 14806, and no split is announced as large (>200 operands) at all.

### 5.2 The `collect_common_factors` gate: implemented, and it never fires

Item 1 of the old list was done anyway - `collect_base_units` skips
`GiNaC::collect_common_factors` above `PYOOMPH_UNIT_CCF_MAX_TERMS` terms (default 2000), and once
tripped the decision holds for the whole subtree, because the per-term recursive calls are where the
time actually went. The rationale is sound (the `add` branch handles a sum term by term and only the
*choice* of hoisted factor changes) and 6/6 gdb samples of the never-returning split were inside
`collect_common_factors`.

But with 5.1 in place nothing reaches the threshold: the largest sum in the whole reference set has
**48** terms and the droplet's largest has 69, so the generated C is byte-identical to the ungated
build (`PYOOMPH_UNIT_CCF_MAX_TERMS=100000`) on all 28 files of the six cases. Forcing the gate on
everything (`=1`) still builds, loads and assembles every case and moves the C by 0.05-2 % in size,
which is what says the skip is a factoring choice and not a semantic one. It is kept as a bound on a
pathology of the same class, not as the fix for this one. Item 2 of the old list - hashing GiNaC's
`replace_with_symbol` lookup - was therefore **not** done and is not needed by anything measured.

### 5.3 The synthetic case: flat, and it was the code writer

The order-of-magnitude-per-level growth of `initialise()` was the emission half, exactly as
suspected, and nine commits (`e5685a7c`..`373b8fa0`) took it apart: every remaining tree walk over
the residual DAG in `write_code` - the two collectors, `MakeResidualSteady`, the marker derivative,
the subexpression-to-struct mapper, three preorder scans and the `dResidual/dParameter` substitution
- is now a DAG walk or memoised, and a residual is no longer differentiated by a parameter it does
not contain. Their commit bodies carry the per-phase numbers.

The result, `PYOOMPH_TIME_ADD_RESIDUAL=1 PYOOMPH_TIME_WRITE_CODE=1`, fresh directory per run:

| depth | `initialise()` | wall | peak RSS | `add_residual` (all) | `write_code` (all) | `domain.c` |
|-------|---------------|------|----------|----------------------|--------------------|------------|
| 3 | 0.79 s | 1.89 s | 162 MB | 0.052 s | 0.019 s | 49.0 kB |
| 4 | 0.91 s | 1.96 s | 162 MB | 0.082 s | 0.021 s | 50.4 kB |
| 5 | 0.90 s | 1.91 s | 162 MB | 0.088 s | 0.011 s | 51.8 kB |
| 6 | 0.83 s | 1.87 s | 162 MB | 0.113 s | 0.013 s | 53.1 kB |
| 7 | 1.02 s | 2.07 s | 162 MB | 0.312 s | 0.014 s | 54.5 kB |

against 0.66 / 6.7 / 76.7 / 887.6 s and 2.3 GB at depths 3 to 6 in round 1, i.e. a factor of about
11 per level. Most of what is left is the C compiler. The Jacobian agrees with a central difference
to 2.4e-9 at depth 7.

### 5.4 Smaller, known, not done

- `expand_all_and_ensure_nondimensional` (`src/codegen.cpp`) runs `repl.expand().evalm().normal()` on
  the **unmasked** result of its own `DrawUnitsOutOfSubexpressions`. It is the same hazard class. It
  has phase timers now (`ph:eaen_DrawUnits`, `ph:eaen_expand_normal`) and did not register above a
  millisecond on anything tried, which is why it was left alone: it is used for scales and single
  components, not for whole residuals.
- `warn_on_large_numerical_factor`'s `expand()` at the end of `add_residual` is unmasked too, and is
  off by default.
- The GiNaC patch is local to the vendored build and has not been sent upstream.

## 6. Reproducing, and the two traps

The script is `~/docs/mypapers/nonmonotonic_final_rebuttal/codes/run_droplet_evap.py`. Copy it to a
fresh directory (it writes state/dump files and takes a shortcut when it finds them) and add
`--quick-test`. It used to reach the killer residual in about 80 s and stop there; it now runs the
whole `--quick-test` in 42 s, so it is a regression check rather than a reproduction.

The synthetic case is a fan-out-2 chain of dimensional markers; it is written out in
`tests/test_azimuthal_codegen.py::test_deeply_nested_dimensional_subexpressions_build_under_azimuthal_stability`,
with `DEPTH` as the only knob.

- **Complex PETSc is required and its absence is silent-ish.** `PYTHONPATH` is unset in a non-login
  shell, so petsc4py is not importable and pyoomph falls back to scipy. Look the arch up rather than
  pasting a path; on this machine `packages/petsc_pyoomph/pyoomph_petsc_opt/lib` is complex and
  `pyoomph_petsc_real_opt` is not.
- **gdb cannot attach here.** `/proc/sys/kernel/yama/ptrace_scope` is `1`, so a running process
  cannot be attached to. Launch the script *under* gdb and send `SIGINT` to the **gdb** process to
  take a sample:

      gdb -batch -x cmds.gdb --args python3 -u script.py &
      GPID=$!; sleep 120; kill -INT $GPID     # cmds.gdb: run / bt / py-bt / continue / bt

  `py-bt` works and gives the Python frame, which is how the residual was identified. In practice the
  `PYOOMPH_TIME_ADD_RESIDUAL` split report of §5.1 answered the same question faster: it announces a
  large split before the call, so one that never returns is still identifiable.

## 7. Verification standard for a fix

`tests/ -q --full` is green apart from one unrelated pre-existing red — see
`axisymm_reconnection_coalescence_4.md`. Run it **with the complex PETSc on `PYTHONPATH`**: without
it, 19 tests fail and 7 error with "Your PETSc/SLEPc installation cannot handle a complex eigenvalue
problem", which looks alarming and means nothing.

Expect last-bit movement in generated code from any change here, and do not treat it as a regression
on its own: skipping or reordering these passes renumbers the CSE temporaries and can reassociate
products. `code_generation.md` documents the same effect for the unit prescan; the masking adds to it
by allocating placeholder symbols, which shifts GiNaC's symbol serials and hence its canonical factor
order. Measured for the masking with `PYOOMPH_DISABLE_UNIT_MASK=1` against the same build, over five
cases (the two azimuthal codegen tests, `falling_droplet`, `custom_math_dimensional_tennis`,
`stokes_nonnewtonian`, and the synthetic case): same line counts and identical numeric-constant
multisets everywhere; four of the five also have an identical token multiset, i.e. they are pure
factor permutations; the azimuthal case additionally renumbers `_jc` temporaries. The static return
type of `subexpression()` on its own left the generated C byte-identical.

The no-azimuthal droplet run converged to `1.13894e-12` after `e7408619` against `1.1425e-12` before,
with an identical step count, and to `1.14952e-12` after `7297d5e9`, still in the same 8 steps. The
**azimuthal** run, which is the one that never got there, converges to `1.12467e-12` in those same 8
steps and matches the non-azimuthal intermediate residuals exactly down to step 7 (`2.35951e-08`).
Movement in the last digits of a quantity at the round-off floor is not a regression signal.
