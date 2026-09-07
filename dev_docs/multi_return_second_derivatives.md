# Second derivatives of multi-return callbacks

**State:** implemented and measured. `tests/test_multiret_hessian.py` (28 tests) covers the
machinery and every `CustomMultiReturnExpression` pyoomph ships. Everything below is measured on
this machine, not estimated.

Companion to [code_generation.md](code_generation.md) §9 (the Hessian generator) and to
[aiomfac_electrolytes.md](aiomfac_electrolytes.md) (the activity model whose Jacobian this turned
from finite differences into exact code).

## 1. Why: an activity model could not appear in a stability analysis

A `CustomMultiReturnExpression` is a black box that returns several values at once and, at runtime,
their Jacobian with respect to its arguments. The code generator wraps a chain rule around that.
Until this change the chain rule stopped after one differentiation:

> `Multi-Return Callbacks can only be derived to the first order at the moment!`
> — the old `codegen.cpp:11301`

An analytic Hessian differentiates the residual twice, so any residual containing a multi-return
callback could not have one — which put UNIFAC and AIOMFAC activity coefficients, the
log-conformation decompositions, `InvertMatrix` and the Cahn-Hilliard potentials out of reach of
every eigenvalue, bifurcation, Floquet and orbit workflow.
`set_activity_coefficients_by_unifac(..., use_multi_return=False)` was the documented escape.

## 2. Two live bugs found on the way

**The Hessian passed a mode where a flag was expected.** In `HessianVectorProduct<N>` the local
`flag` is the Hessian *mode* (0..5), not "compute derivatives", but `write_code_multi_ret_call`
handed it straight to the callback. Mode 0 is the ordinary Hessian-vector product, reached from
`fill_in_contribution_to_hessian_vector_products` (`elements_assembly.cpp`), which is the hook
`MyFoldHandler` / `MyHopfHandler` / `MyPitchForkHandler` use. So a bifurcation tracker over a
multi-return callback read an uninitialised `dmulti_ret_*` array. The Hessian pass now emits a
literal flag instead, which also keeps modes 2..5 out of user callbacks that branch on `flag == 1`.

**A throw during Hessian differentiation corrupted every later element.**
`__derive_shapes_by_second_index` was set and cleared by hand, on the reasoning that the region is
"set and cleared inside a single pass". But `GiNaC::diff()` in the middle of it is exactly where a
residual that cannot be differentiated twice throws, and the flag then stayed latched for the rest
of the process. The symptom was a *later, unrelated* element's ordinary Jacobian coming out with
`l_shape2` in it — a loop variable that only exists in a Hessian — i.e. a C file the compiler
rejects, pointing nowhere near the cause. It is now an `AmbientCodegenScope`, restored on every
exit including the throwing one, and the comment that dismissed the risk is corrected.

Two further pre-existing defects turned up in the shipped callbacks themselves; see §7.

## 3. The symbolic side

`MultiRetCallback` (`codegen.hpp`) gains `derived_by_arg2`. With it `>= 0` the node stands for
`d2 result[retindex] / d arg[derived_by_arg] d arg[derived_by_arg2]`.

`GiNaCMultiRetCallback::derivative` now has three cases: a plain node differentiates as before; a
once-derived node produces `sum_k (d arg_k / ds) * node(retindex, derived_by_arg, k)`; a
twice-derived one throws, since the ABI carries no third-order tensor. Only that one half of the
second-order chain rule appears here — the other half,
`sum_k (d2 arg_k / ds dt) * dF/d arg_k`, is GiNaC's own product rule differentiating the
`(d arg_j / ds)` factor in front of the node.

**The index pair is canonicalised** (sorted) by the constructor. `d2f/da_j da_k` is symmetric, so an
unsorted pair would give two distinct GiNaC atoms reading two distinct slots for one value, which
defeats common-subexpression elimination and roughly doubles the Hessian body. Callbacks are
correspondingly only ever asked for the `j <= k` half, and the tensor they fill **must** be
symmetric.

A callback that answers `use_symbolic_derivative` with a closed form never reaches any of this: its
first derivative is an ordinary expression, which GiNaC differentiates as many times as it likes.

## 4. The runtime side

`invoke_multi_ret` and `multi_ret_ccode_<k>` are **unchanged**. The second derivatives travel
through siblings: `invoke_multi_ret_hessian` and `multi_ret_ccode_d2_<k>`, the latter emitted only
when `generate_hessian` is set. That is what makes the property in §6 provable rather than
arguable, and it means every existing hand-written `generate_c_code()` body keeps being pasted into
the signature it was written for.

Flag bits (`jitbridge.h`), replacing the magic 128:

    PYOOMPH_MULTIRET_FLAG_DERIVATIVES        1
    PYOOMPH_MULTIRET_FLAG_DEBUG_PYTHON_VS_C  128
    PYOOMPH_MULTIRET_FLAG_SECOND_DERIVATIVES 256

Array layout, extending `derivative_matrix[i_res*nargs + j_arg]` one index deeper:

    second_derivative_tensor[(i_res*nargs + j_arg)*nargs + k_arg]

printed by the node as `d2multi_ret_<i>[<nargs*nargs>*<retindex> + <nargs>*<d1> + <d2>]`.

The array is `nret*nargs*nargs` doubles and is declared **above** the integration-point loop, not
inside it: `PYOOMPH_AQUIRE_ARRAY` is `_alloca` on Windows, which a loop never releases, so 6.8 kB
per integration point would accumulate over the whole quadrature rule.

Sizes, for the largest callbacks that ship:

| callback | nargs -> nret | `d2` array |
|---|---|---|
| `PiecewiseNSCHPotential` | 1 -> 2 | 2 doubles |
| `SymmetricMatrixExponential` | 3 -> 3 | 27 doubles |
| AIOMFAC, 3 species + salt | 5 -> 4 | 100 doubles |
| `InvertMatrix` (3x3 general) | 9 -> 9 | 729 doubles, 5.8 kB |
| `LogConfTensorDecompositionAxisymmetric` | 11 -> 7 | 847 doubles, 6.8 kB |

### The ordering hazard

The subexpression-derivative cache (`d_subexpr_N_d_<field>`) is filled *after* the multi-return
calls are emitted, but a nested Hessian subexpression body already holds a once-derived node, so
differentiating it there is what creates the twice-derived one — and hence what decides whether the
`d2` array is needed at all. Hoisting the emission instead is not possible: a call's arguments may
reference earlier `subexpr_*` variables, which is why the two are interleaved. So in the Hessian
pass the fill loop's differentiations are computed first and only printed later, in unchanged
order. Restricted to `hessian == true`, so the residual/Jacobian emission order is untouched.

## 5. Getting the numbers: three routes, in order of preference

**`use_symbolic_second_derivative`** — a closed form, substituted into the expression. Best when it
exists, since it also lets the code generator simplify.

**Analytic code**, `eval_second_derivatives` in Python and `generate_c_code_second_derivatives` in
C. Used by `MultiSafeDivide`, both Cahn-Hilliard potentials and `CSplineInterpolator`, whose second
derivatives are three lines each.

**Forward-mode AD**, `second_derivative_mode = "ad"`. `numpy.log`/`exp`/`sqrt`/`cosh` on a Python
object dispatch to that object's own `log()`/`exp()`/… method, so a `HyperDual` number
(`pyoomph/expressions/cb.py`) carrying `(value, gradient, hessian)` can be pushed straight through
an `eval()` that was only ever written to compute values. That is how all six tensor callbacks get
exact second derivatives with nothing written out by hand; they agree with their own analytic
Jacobians to 1e-16.

An `eval()` qualifies if it uses arithmetic and `numpy` calls only. It does **not** qualify if it
calls `math.*` or `float()` on its arguments. `HyperDual` deliberately defines **no** `__float__`:
with one, `math.sqrt(x)` would coerce silently and return a plain number, i.e. the right value with
*no derivatives at all* — a silently wrong Jacobian, which is the failure the class exists to
prevent. Without it those calls raise a `TypeError` naming the operation.
`SymmetricMatrixExponential` was switched from `math.*` to `numpy.*` for exactly this reason.

**Finite differences**, the default when nothing else is supplied — `FILL_MULTI_RET_HESSIAN_BY_FD`
in the generated C, `fill_python_second_derivatives_by_FD` in Python. Both **central**-difference
the derivative matrix and symmetrise afterwards. Nothing has to be done to get this: it is what
keeps a `generate_c_code()` written before this change working under a Hessian.

### The warning that matters

Differencing a Jacobian that is *itself* finite-differenced leaves nothing. Measured on AIOMFAC,
the second derivatives came out with a **relative error of 2.2 — no correct digits at all**. A
callback that is going to appear in an analytic Hessian therefore needs a real Jacobian; an FD one
is not merely less accurate but useless. This is why AIOMFAC and UNIFAC had to get exact Jacobians
before their second derivatives meant anything (§7).

## 6. Residual and Jacobian are untouched

The requirement was that none of this may cost anything outside a Hessian, and it is checked rather
than argued: `Scratchpad/multiret_hessian/capture_generated_code.py` emits the element C for seven
problems using multi-return callbacks, with and without `analytic_hessian`, before and after.

Across all eight non-Hessian files the diff is **one line**: the function-table size assertion,
2288 -> 2296 bytes, from the single new function pointer. Not one line of residual, Jacobian or
mass-matrix arithmetic changes. `tests/test_multiret_hessian.py::test_residual_and_jacobian_code_is_untouched`
locks that in on the emitted text.

It holds by construction: the existing signatures are untouched, the new
`write_code_multi_ret_call` parameter defaults to false so the four non-Hessian call sites emit
identical text, `operator<` gains a tiebreaker only for a field that is -1 on every node a residual
can contain, `calchash()` ignores struct content, and the `write_code_subexpressions` restructure is
gated on `hessian == true`.

## 6.1 End to end

`u^2 = lambda` with the square supplied by a callback has a fold at `lambda = 0`, and
`activate_bifurcation_tracking("lam", "fold")` now converges to it exactly, through the generated-C
and the Python dispatch alike. That is worth having as a test rather than only the FD checks: the
fold handler is what reaches `HessianVectorProduct` with Hessian mode 0, i.e. the path the flag bug
in §2 corrupted, and on a problem this small a wrong Hessian shows up as a Newton failure rather
than as a slightly wrong number.

## 7. AIOMFAC: from a finite-difference Jacobian to exact code

AIOMFAC's model is written against an expression-generator interface (`ln`, `exp`, `pow`,
`subexpression`, …) precisely so the same code can serve several back-ends. Two were added:
`DualExpressionGenerator` (values are `HyperDual`s) for Python and `CDualExpressionGenerator`
(values are `CDual`s, whose components are C expressions) for the generated code. `subexpression`
is what keeps the emitted code a sensible size: it names the value **and each nonzero gradient
component** as its own `const double`.

Measured on water + glycerol + NaCl (5 arguments, 4 results), `-O3 -march=native`:

|  | lines | bytes | compile | Jacobian error | Hessian error |
|---|---|---|---|---|---|
| FD Jacobian (before) | 272 | 47.6 kB | 0.25 s | 2.4e-6 | **2.2 — useless** |
| analytic Jacobian | 794 | 482 kB | 1.03 s | **8.7e-16** | 5.0e-9 |

| time per callback | value | + Jacobian | + Hessian |
|---|---|---|---|
| FD Jacobian | 0.42 us | 2.59 us | 29.2 us |
| analytic | 0.42 us | **0.79 us** | **9.1 us** |

So the analytic Jacobian costs 10x the C text and 4x the compile, and buys a 3.3x faster and exact
Jacobian — and, more importantly, a Hessian that has any correct digits at all. It is on by
default (`AIOMFACElectrolyteMultiReturnExpression.analytic_c_jacobian`).

The 8.7e-16 is also a genuine cross-check: `CDual` in C and `HyperDual` in Python are independent
implementations of the same chain rule, agreeing to machine precision.

Switching the generated FD Hessian from forward to central differences moved its error from 1e-5 to
5e-9 for 1.8x the cost, which is why that is the default.

In Python the exact Jacobian is about 3x *slower* than the finite-difference one (5.6 ms against
1.9 ms), because every elementary operation allocates a small numpy array. That path is not the hot
one — AIOMFAC always ships C code — and 3x is the price of five extra orders of magnitude. Carrying
second derivatives through as well would make it 16x, so `HyperDual` seeds them only when they are
asked for.

## 8. Defects found in the shipped callbacks

Neither is related to second derivatives; both were found because AD gives an independent second
opinion on a Jacobian that had never had one.

* **`InvertMatrix(3, "symmetric")` had a wrong Jacobian.** One index typo: `dC01/dd` was written
  where `dC01/de` belongs, `C01 = c*e - b*g` not containing `d` at all. Present in both the Python
  and the generated C. Its own central differences disagreed by 1.3e-2; now 6.4e-11.
  `pyoomph/equations/solid.py` uses this class.
* **A stray `print(Alist, B)` in `MultiSafeDivide.eval`**, firing on every evaluation.

## 9. Open

* No analytic **second**-derivative C is generated for AIOMFAC/UNIFAC. A second-order `CDual` would
  multiply the emitted code by roughly `nargs` again (~5 MB for this mixture), and the central-difference
  Hessian on top of the now-exact Jacobian is already at 5e-9. Revisit only if that shows up.
* The activity models' Python `eval` is 3x slower than it was; see §7 for why that is accepted.
* `multi_return_calls` is still not cleared before the Hessian pass (`codegen.cpp`), so the tail
  loop can emit calls the Hessian body never reads. Pre-existing; harmless, because the `d2` array
  is gated on the registered-invocation set rather than on `hessian` alone.
* Untested under MPI and under azimuthal/normal-mode expansions with a callback whose arguments
  carry the perturbation — a complex argument into a real-valued black box was already unsupported
  at first order, and this changes neither way.
