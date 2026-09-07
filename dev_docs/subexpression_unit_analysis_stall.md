# Code generation stalls on a deeply nested subexpression under azimuthal stability

Status: **the unit analysis is fixed; the reported case still does not finish.** `add_residual` was
the wall and no longer is — it is flat in the nesting depth now, measured over three orders of
magnitude. Two further walls were found underneath it and are *not* fixed: GiNaC's own
`collect_common_factors` on a marker argument with 14806 terms (the droplet), and the code writer,
which has the same DAG-as-tree problem `add_residual` had (the synthetic case). Written 2026-09-06
against `e7408619`, rewritten 2026-09-07 against `6cec0346`. This document owns the reasoning;
`code_generation.md` owns the unit-check pipeline it happens in.

## 1. The symptom, and what it is not

A water/1,2-hexanediol evaporating droplet (UNIFAC activity coefficients, multi-return callbacks)
with

    p.setup_for_stability_analysis(azimuthal_stability=True, analytic_hessian=False)

never returns from `p.initialise()`. It is **not** a deadlock, not the solver, and not MPI: the
process is at 100 % CPU in `FiniteElementCode::add_residual` for the `liquid_gas` interface's
mass-transfer residual, built at `pyoomph/materials/mass_transfer.py:194`
(`ieqs.add_residual(weak(j - rhs, jtest))`).

The same script **without** `azimuthal_stability=True` completes in about two minutes. That
difference is the whole of the diagnosis: azimuthal stability is what routes the residual through
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

## 5. What is still open, in the order it was met

### 5.1 The droplet: one `collect_base_units` call on a 14806-term sum

With the masking in place the droplet's mass-transfer residual now spends its time in a **single**
`collect_base_units()` call. `PYOOMPH_TIME_ADD_RESIDUAL=1` announces any split whose argument has more
than 200 operands *before* the call and reports any split over half a second when it returns; the
sequence for that residual is

    ph:units slow split  #22  1.03 s  add nops 322    masked_total 19
    ph:units slow split  #29  1.29 s  add nops 752    masked_total 24
    ...
    ph:units large split #37  entering add nops 14806 masked_total 32   <- never returned (16 min)

Each term is a product of 10-14 atoms, and the term counts of the marker chain go 32, 322, 752,
14806. At 752 terms the split costs 1.3 s, i.e. 1.7 ms per term; a linear extrapolation would give
25 s for 14806, and it ran for more than sixteen minutes, so the cost is at least quadratic in the
number of terms. The remaining quadratic is inside GiNaC: `collect_base_units()` opens with
`collect_common_factors(expand(arg))`, and `find_common_factor -> to_polynomial ->
replace_with_symbol` (`ginac/normal.cpp`) scans its `repl` vector **linearly** for every node it has
to replace. Masking does not help there, because the terms still contain non-symbol atoms (field
placeholders, functions) that `to_polynomial` must replace, so `repl` grows with the number of terms.

What has *not* been tried, in the order it is worth trying:

1. Find out whether `collect_common_factors` is needed at all in `collect_base_units`. The `add`
   branch already handles a sum term by term and insists the terms agree on their unit, which is the
   correct semantics; the common-factor collection only changes *which* factor is pulled out. Skipping
   it (or skipping it above some term count) would change the split wherever a common factor exists,
   so it needs the same generated-C audit as §7.
2. Failing that, patch the vendored GiNaC so `replace_with_symbol` looks its `repl` entries up by
   hash rather than by a linear `is_equal` scan. It is a self-contained change and would help every
   caller of `normal()`/`collect_common_factors`, but it is a change to vendored third-party code
   (`src/thirdparty/INFO_oomph-lib` conventions apply).
3. Ask where a 14806-term marker argument comes from in the first place. It is an *expanded* sum, and
   nothing in `add_residual` expands it — it arrives that way from `expand_placeholders` and the
   azimuthal split. If the split can keep it factored, none of the above is needed.

### 5.2 The synthetic case: the code writer has the same DAG-as-tree problem

Once `add_residual` is flat, what is left of `initialise()` for the synthetic case is
`FiniteElementCode::write_code` — the "Generating equation C code" step — and it still grows by an
order of magnitude per nesting level:

| depth | `add_residual` (both contributions) | `initialise()` | generated `domain.c` |
|-------|-------------------------------------|----------------|----------------------|
| 4     | 0.02 s | 7.6 s   | 67 kB |
| 5     | 0.12 s | 76.5 s  | 73 kB |
| 6     | 0.18 s | 887.6 s | 78 kB |

The *output* grows linearly, so this is not the emitted code getting bigger; it is the derivative and
printing passes walking the DAG as a tree. Nobody has instrumented that half yet. Peak RSS at depth 6
is 2.3 GB.

### 5.3 Smaller, known, not done

- `expand_all_and_ensure_nondimensional` (`src/codegen.cpp`) runs `repl.expand().evalm().normal()` on
  the **unmasked** result of its own `DrawUnitsOutOfSubexpressions`. It is the same hazard class. It
  has phase timers now (`ph:eaen_DrawUnits`, `ph:eaen_expand_normal`) and did not register above a
  millisecond on anything tried, which is why it was left alone: it is used for scales and single
  components, not for whole residuals.
- `warn_on_large_numerical_factor`'s `expand()` at the end of `add_residual` is unmasked too, and is
  off by default.

## 6. Reproducing, and the two traps

The script is `~/docs/mypapers/nonmonotonic_final_rebuttal/codes/run_droplet_evap.py`. Copy it to a
fresh directory (it writes state/dump files and takes a shortcut when it finds them) and add
`--quick-test`. It reaches the killer residual in about 80 s.

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
with an identical step count.
