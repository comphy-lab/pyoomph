#  @file
#  @author Christian Diddens <c.diddens@utwente.nl>
#  @author Duarte Rocha <d.rocha@utwente.nl>
#  @author Maxim de Wildt <m.dewildt@utwente.nl>
#
#  @section LICENSE
#
#  pyoomph - a multi-physics finite element framework based on oomph-lib and GiNaC
#  Copyright (C) 2021-2026  Christian Diddens, Duarte Rocha & Maxim de Wildt
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
#  The main author may be contacted at c.diddens@utwente.nl
#
# ========================================================================

# First spatial derivatives of the surface normal, i.e. grad(normal) and div(normal). The latter is
# the mean curvature, which until now could only be obtained by projecting the normal onto an
# auxiliary continuous field first (see pyoomph/utils/dropgeom.py).
#
# dn_i/dx_j = -M_i^(c) M_j^(b) n_k X_{k,bc} follows from n.t_a = 0 and |n| = 1 alone, so it inherits
# whichever orientation the normal already had. pyoomph's interface normal is the OUTER one, so a
# droplet gives a POSITIVE divergence: div(n) = +1/R on a circle, +2/R on a sphere, +1/R on a
# cylinder. The tutorial's curvature is kappa = -div(n).
#
# ---------------------------------------------------------------------------------------------
# A trap worth knowing before writing any test here: an observable of the form (a-b)**2 is USELESS
# as an error measure. GiNaC expands it into a*a - 2*a*b + b*b, so the cancellation that should
# happen at 1e-16 happens instead between terms of order |a|^2, and the result is round-off - in
# practice a small NEGATIVE number where a square was expected. Wrap the difference in
# subexpression() to keep it opaque, or compare the two integrals separately.
# ---------------------------------------------------------------------------------------------

import math

import numpy
import pytest

from pyoomph import *
from pyoomph.expressions import *
from pyoomph.meshes.simplemeshes import RectangularQuadMesh, CuboidBrickMesh, CircularMesh
from pyoomph.meshes.mesh import MeshTemplate


class _Bulk(Equations):
    """Something to give the bulk domain equations; the interface is what matters."""

    def define_fields(self):
        self.define_scalar_field("d", "C2")

    def define_residuals(self):
        d, q = var_and_test("d")
        self.add_residual(weak(d, q))


def _normal_observables(dim):
    n = var("normal")
    G = grad(n)
    obs = {"div": div(n), "area": 1}
    # Structural identities, exact on any mesh: grad(n) is symmetric (it is minus the Weingarten
    # map) and tangential in both slots (n.grad(n) = grad(n).n = 0).
    sym = 0
    for i in range(dim):
        for j in range(i + 1, dim):
            sym = sym + subexpression(G[i, j] - G[j, i]) ** 2
    tang = 0
    for i in range(dim):
        tang = tang + subexpression(dot(n, G)[i]) ** 2 + subexpression(dot(G, n)[i]) ** 2
    obs["sym"] = sym
    obs["tang"] = tang
    for i in range(dim):
        for j in range(dim):
            obs["g%d%d" % (i, j)] = G[i, j]
    return IntegralObservables(**obs)


def _measure(mesh_factory, interface, dim, move=None, coordsys=None, nrefine=0):
    class P(Problem):
        def define_problem(self):
            if coordsys is not None:
                self.set_coordinate_system(coordsys)
            self.add_mesh(mesh_factory())
            self.max_refinement_level = 8
            self.add_equations(_Bulk() @ "domain")
            self.add_equations(_normal_observables(dim) @ interface)

    with P() as p:
        p.initialise()
        for _ in range(nrefine):
            p.refine_uniformly()
        if move is not None:
            for n in p.get_mesh("domain").nodes():
                move(n)
        o = p.get_mesh(interface).evaluate_all_observables()
        a = float(o["area"])
        res = {k: float(o[k]) / a for k in o if k != "area"}
        # sym/tang are integrals of squares; report them as RMS values
        res["sym"] = math.sqrt(max(0.0, float(o["sym"])) / a)
        res["tang"] = math.sqrt(max(0.0, float(o["tang"])) / a)
        return res


def _assert_identities(res, tol=1e-11):
    assert res["sym"] < tol, "grad(normal) is not symmetric: %.3e" % res["sym"]
    assert res["tang"] < tol, "grad(normal) is not tangential: %.3e" % res["tang"]


class _ScaledLineMesh(MeshTemplate):
    """One exact C2 line embedded in 2D, scaled without changing its shape."""

    def __init__(self, scale, curvature=0.0):
        super().__init__()
        self.scale = scale
        self.curvature = curvature

    def define_geometry(self):
        domain = self.new_domain("line", nodal_dimension=2)

        def point(q):
            return self.add_node(self.scale * q,
                                 self.scale * (0.3 * q + self.curvature * q * q))

        domain.add_line_1d_C2(point(0.0), point(0.5), point(1.0))


class _ScaledQuadMesh(MeshTemplate):
    """One explicit Q2 square whose nodes are never coordinate-deduplicated."""

    def __init__(self, scale):
        super().__init__()
        self.scale = scale

    def define_geometry(self):
        domain = self.new_domain("domain", nodal_dimension=2)
        nodes = [
            self.add_node(self.scale * i / 2, self.scale * j / 2)
            for j in range(3) for i in range(3)
        ]
        domain.add_quad_2d_C2(*nodes)
        self.add_facet_to_boundary("top", nodes[6:9], [nodes[6], nodes[8]])


_NORMAL_SCALES = [1.0, 1e-6, 1e-10, 1e-12, 1e-18]


@pytest.mark.parametrize("scale", _NORMAL_SCALES)
def test_line_normals_are_scale_safe_at_current_and_history_positions(scale):
    from pyoomph.equations.ALE import LaplaceSmoothedMesh

    current = numpy.array([-0.3, 1.0])
    current /= numpy.linalg.norm(current)
    previous = numpy.array([0.25, 1.0])
    previous /= numpy.linalg.norm(previous)

    class P(Problem):
        def define_problem(self):
            self.add_mesh(_ScaledLineMesh(scale))
            n = var("normal")
            n_old = evaluate_in_past(n, 1, apply_on_others=True)
            equations = LaplaceSmoothedMesh()
            equations += ElementSpace("C2")
            equations += IntegralObservables(
                length=1,
                nx=n[0], ny=n[1], norm2=dot(n, n),
                old_nx=n_old[0], old_ny=n_old[1], old_norm2=dot(n_old, n_old),
            )
            self.add_equations(equations @ "line")

    with P() as problem:
        problem.initialise()
        for node in problem.get_mesh("line").nodes():
            q = node.x(0) / scale
            node.set_x_at_t(1, 0, scale * q)
            node.set_x_at_t(1, 1, scale * (-0.25 * q))
        values = problem.get_mesh("line").evaluate_all_observables()

    length = float(values["length"])
    got = numpy.array([float(values["nx"]), float(values["ny"])]) / length
    got_old = numpy.array([float(values["old_nx"]), float(values["old_ny"])]) / length
    assert numpy.allclose(got, current, rtol=0.0, atol=2e-14)
    assert numpy.allclose(got_old, previous, rtol=0.0, atol=2e-14)
    assert abs(float(values["norm2"]) / length - 1.0) < 2e-14
    assert abs(float(values["old_norm2"]) / length - 1.0) < 2e-14


@pytest.mark.parametrize("scale", _NORMAL_SCALES)
def test_line_normal_position_jacobian_matches_scale_relative_finite_difference(scale):
    from pyoomph.equations.ALE import LaplaceSmoothedMesh

    class NormalResidual(Equations):
        def define_fields(self):
            self.define_scalar_field("u", "C2")

        def define_residuals(self):
            u, u_test = var_and_test("u")
            self.add_residual(weak(u + dot(var("normal"), vector(0.37, -0.61)),
                                   u_test))

    class P(Problem):
        def define_problem(self):
            self.add_mesh(_ScaledLineMesh(scale, curvature=0.35))
            self.add_equations(
                (LaplaceSmoothedMesh() + ElementSpace("C2") + NormalResidual()) @ "line"
            )

    with P() as problem:
        problem.initialise()
        mesh = problem.get_mesh("line")
        nodes = sorted(mesh.nodes(), key=lambda node: node.x(0))
        moved = nodes[1]
        column = moved.variable_position_pt().eqn_number(1)
        assert column >= 0
        field_index = mesh.element_pt(0).get_jit_code().get_nodal_field_indices()["u"]
        rows = [node.eqn_number(field_index) for node in nodes]
        assert all(row >= 0 for row in rows)

        _, jacobian = problem.assemble_jacobian(with_residual=True)
        analytical = numpy.asarray(jacobian[rows, column].todense()).reshape(-1)

        original = moved.x(1)
        epsilon = 2e-6 * scale
        moved.set_x(1, original + epsilon)
        residual_plus, _ = problem.assemble_jacobian(with_residual=True)
        moved.set_x(1, original - epsilon)
        residual_minus, _ = problem.assemble_jacobian(with_residual=True)
        moved.set_x(1, original)
        finite_difference = (numpy.asarray(residual_plus)[rows] -
                             numpy.asarray(residual_minus)[rows]) / (2 * epsilon)

    assert numpy.allclose(analytical, finite_difference, rtol=2e-6, atol=2e-8), \
        "scale=%g analytic=%r finite_difference=%r" % (scale, analytical, finite_difference)


@pytest.mark.parametrize("scale", [1.0, 1e-6, 1e-12, 1e-18])
def test_bulk_boundary_normal_position_jacobian_is_scale_safe(scale):
    """Exercise InterfaceElementBase, as used by a 2-D free surface."""
    from pyoomph.equations.ALE import LaplaceSmoothedMesh

    class NormalResidual(Equations):
        def define_fields(self):
            self.define_scalar_field("u", "C2")

        def define_residuals(self):
            u, u_test = var_and_test("u")
            self.add_residual(weak(u + dot(var("normal"), vector(0.37, -0.61)),
                                   u_test))

    class P(Problem):
        def __init__(self):
            super().__init__()
            self.initial_adaption_steps = 0

        def define_problem(self):
            self.add_mesh(_ScaledQuadMesh(scale))
            self.add_equations(
                (LaplaceSmoothedMesh() + ElementSpace("C2")) @ "domain"
            )
            self.add_equations(NormalResidual() @ "domain/top")

    with P() as problem:
        problem.initialise()
        boundary = problem.get_mesh("domain/top")
        nodes = sorted(boundary.nodes(), key=lambda node: node.x(0))
        moved = nodes[1]
        column = moved.variable_position_pt().eqn_number(1)
        assert column >= 0
        field_index = boundary.element_pt(0).get_jit_code().get_nodal_field_indices()["u"]
        rows = [node.eqn_number(field_index) for node in nodes]
        assert all(row >= 0 for row in rows)

        _, jacobian = problem.assemble_jacobian(with_residual=True)
        analytical = numpy.asarray(jacobian[rows, column].todense()).reshape(-1)

        original = moved.x(1)
        epsilon = 2e-6 * scale
        moved.set_x(1, original + epsilon)
        residual_plus, _ = problem.assemble_jacobian(with_residual=True)
        moved.set_x(1, original - epsilon)
        residual_minus, _ = problem.assemble_jacobian(with_residual=True)
        moved.set_x(1, original)
        finite_difference = (numpy.asarray(residual_plus)[rows] -
                             numpy.asarray(residual_minus)[rows]) / (2 * epsilon)

    assert numpy.allclose(analytical, finite_difference, rtol=2e-6, atol=2e-8), \
        "scale=%g analytic=%r finite_difference=%r" % (scale, analytical, finite_difference)


# ---------------------------------------------------------------------------------------------
# Flat interfaces: everything must vanish exactly. This is the sharp test of the X_{k,bc} term,
# in the same sense as "a linear function has a vanishing discrete Hessian".
# ---------------------------------------------------------------------------------------------

def test_flat_edge_in_2d():
    res = _measure(lambda: RectangularQuadMesh(N=4), "domain/bottom", 2)
    for i in range(2):
        for j in range(2):
            assert abs(res["g%d%d" % (i, j)]) < 1e-12
    assert abs(res["div"]) < 1e-12


def test_flat_face_in_3d():
    res = _measure(lambda: CuboidBrickMesh(N=2), "domain/top", 3)
    assert abs(res["div"]) < 1e-12
    _assert_identities(res)


# ---------------------------------------------------------------------------------------------
# A parabola, which a C2 line element represents EXACTLY: x is linear in the local coordinate, so
# y = 1 + a x^2 is quadratic in it. For a curve (x, f(x)) with the outward (upward) normal,
#     div(n) = -f'' / (1 + f'^2)^{3/2}
# and comparing at the same Gauss points removes quadrature error, so this is exact rather than a
# convergence test.
# ---------------------------------------------------------------------------------------------

_A = 0.35


def _parabola_curvature():
    x = var("coordinate_x")
    return -2 * _A * (1 + 4 * _A ** 2 * x ** 2) ** (-1.5)


def test_parabola_in_2d_is_exact():
    class P(Problem):
        def define_problem(self):
            self.add_mesh(RectangularQuadMesh(N=4))
            self.add_equations(_Bulk() @ "domain")
            n = var("normal")
            self.add_equations(IntegralObservables(
                diff=subexpression(div(n) - _parabola_curvature()) ** 2,
                ref=_parabola_curvature() ** 2, length=1) @ "domain/top")

    with P() as p:
        p.initialise()
        for nd in p.get_mesh("domain").nodes():
            if abs(nd.x(1) - 1.0) < 1e-12:
                nd.set_x(1, 1.0 + _A * nd.x(0) ** 2)
        o = p.get_mesh("domain/top").evaluate_all_observables()
        rel = math.sqrt(max(0.0, float(o["diff"])) / float(o["ref"]))
        assert rel < 1e-12, "div(normal) differs from the exact parabola curvature: %.3e" % rel


def test_parabolic_cylinder_in_3d_is_exact():
    """The same parabola extruded in z. Only one principal curvature is nonzero, and "top" of a
    CuboidBrickMesh is the y-max face."""

    class P(Problem):
        def define_problem(self):
            self.add_mesh(CuboidBrickMesh(N=3))
            self.add_equations(_Bulk() @ "domain")
            n = var("normal")
            G = grad(n)
            sym = sum((subexpression(G[i, j] - G[j, i]) ** 2 for i in range(3) for j in range(i + 1, 3)), 0)
            self.add_equations(IntegralObservables(
                diff=subexpression(div(n) - _parabola_curvature()) ** 2,
                ref=_parabola_curvature() ** 2, sym=sym, area=1) @ "domain/top")

    with P() as p:
        p.initialise()
        for nd in p.get_mesh("domain").nodes():
            if abs(nd.x(1) - 1.0) < 1e-12:
                nd.set_x(1, 1.0 + _A * nd.x(0) ** 2)
        o = p.get_mesh("domain/top").evaluate_all_observables()
        rel = math.sqrt(max(0.0, float(o["diff"])) / float(o["ref"]))
        assert rel < 1e-12, "div(normal) differs from the exact curvature: %.3e" % rel
        assert math.sqrt(max(0.0, float(o["sym"])) / float(o["area"])) < 1e-12


# ---------------------------------------------------------------------------------------------
# Curved geometries the space cannot represent exactly: convergence instead.
# ---------------------------------------------------------------------------------------------

def test_circle_curvature_converges_to_one_over_R():
    R = 1.0
    errs = []
    for k in range(3):
        res = _measure(lambda: CircularMesh(radius=R), "domain/circumference", 2, nrefine=k)
        _assert_identities(res)
        errs.append(abs(res["div"] - 1.0 / R))
    assert errs[-1] < 5e-3, "curvature did not converge: %r" % (errs,)
    for a, b in zip(errs, errs[1:]):
        assert math.log(a / b, 2) > 1.7, "expected ~2nd order, got %r" % (errs,)


# ---------------------------------------------------------------------------------------------
# Axisymmetry. div(n) picks up the hoop term n_r/r from vector_divergence, so the result is the
# sum of both principal curvatures. No coordinate-system code had to change for this - the metric
# factors are applied before the second diff, and the product rule does the rest.
# ---------------------------------------------------------------------------------------------

def test_axisymmetric_cylinder_is_exact():
    """A straight meridian: the Cartesian part vanishes and div(n) is purely the hoop term 1/R."""
    R = 0.7

    class P(Problem):
        def define_problem(self):
            self.set_coordinate_system("axisymmetric")
            self.add_mesh(RectangularQuadMesh(N=4, size=[R, 1.0]))
            self.add_equations(_Bulk() @ "domain")
            n = var("normal")
            self.add_equations(IntegralObservables(div=div(n), area=1) @ "domain/right")

    with P() as p:
        p.initialise()
        o = p.get_mesh("domain/right").evaluate_all_observables()
        got = float(o["div"]) / float(o["area"])
        assert abs(got - 1.0 / R) < 1e-11, "axisymmetric cylinder: %.16g, expected %.16g" % (got, 1.0 / R)


def test_axisymmetric_sphere_converges_to_two_over_R():
    R = 1.0
    errs = []
    for k in range(3):
        class P(Problem):
            def define_problem(self):
                self.set_coordinate_system("axisymmetric")
                self.add_mesh(CircularMesh(radius=R, segments=["NE", "SE"]))
                self.max_refinement_level = 8
                self.add_equations(_Bulk() @ "domain")
                n = var("normal")
                self.add_equations(IntegralObservables(div=div(n), area=1) @ "domain/circumference")

        with P() as p:
            p.initialise()
            for _ in range(k):
                p.refine_uniformly()
            o = p.get_mesh("domain/circumference").evaluate_all_observables()
            errs.append(abs(float(o["div"]) / float(o["area"]) - 2.0 / R))
    assert errs[-1] < 5e-3, "axisymmetric sphere did not converge: %r" % (errs,)
    for a, b in zip(errs, errs[1:]):
        assert math.log(a / b, 2) > 1.7, "expected ~2nd order, got %r" % (errs,)


# ---------------------------------------------------------------------------------------------
# Moving mesh: the position columns of the Jacobian and the Hessian come from
# d_dnormal_dx_dcoord and d2_dnormal_dx_d2coord. Both were checked to be sensitive by zeroing the
# fill on purpose - the Jacobian check then reports differences of order 4-18, and the Hessian
# check reports ~2.0 instead of ~1e-8.
# ---------------------------------------------------------------------------------------------

def _moving_curvature_problem(hessian):
    from pyoomph.equations.ALE import LaplaceSmoothedMesh

    class Bulk(Equations):
        def define_fields(self):
            self.define_scalar_field("d", "C2")

        def define_residuals(self):
            d, q = var_and_test("d")
            self.add_residual(weak(grad(d), grad(q)) + weak(1, q))

    class Surf(Equations):
        def define_fields(self):
            self.define_scalar_field("u", "C2")

        def define_residuals(self):
            u, v = var_and_test("u")
            # nonlinear in u AND containing the curvature, so the Hessian is not trivially zero
            self.add_residual(weak((1 + u ** 2) * div(var("normal")), v) + weak(1, v))

    class P(Problem):
        def define_problem(self):
            self.add_mesh(RectangularQuadMesh(N=3))
            eqs = Bulk() + LaplaceSmoothedMesh()
            # Only the top is pinned, so the whole interface is free in both directions and the
            # position dofs the normal's sensitivities touch are genuine unknowns.
            eqs += DirichletBC(mesh_x=True, mesh_y=True) @ "top"
            eqs += DirichletBC(d=0) @ "top"
            self.add_equations(eqs @ "domain")
            self.add_equations(Surf() @ "domain/bottom")
            if not hessian:
                self.equation_compilation_flags.debug_jacobian_epsilon = 1e-5

    p = P()
    if hessian:
        p.setup_for_stability_analysis(analytic_hessian=True)
    p.initialise()
    for n in p.get_mesh("domain").nodes():
        a, b = n.x(0), n.x(1)
        n.set_x(0, a + 0.30 * a * b)
        n.set_x(1, b - 0.20 * a * b + 0.15 * a * (1 - a))   # curves the interface itself
    im = p.get_mesh("domain/bottom")
    idx = im.element_pt(0).get_jit_code().get_nodal_field_indices()["u"]
    for n in im.nodes():
        n.set_value(idx, 0.3 + 0.5 * n.x(0))
    return p


def test_moving_mesh_curvature_jacobian():
    import io
    import contextlib
    p = _moving_curvature_problem(hessian=False)
    buf = io.StringIO()
    with p:
        with contextlib.redirect_stdout(buf):
            p.assemble_jacobian(with_residual=False)
    assert "DIFFERENCES IN JACOBIAN" not in buf.getvalue(), buf.getvalue()[:4000]


def test_moving_mesh_curvature_hessian():
    p = _moving_curvature_problem(hessian=True)
    with p:
        p.debug_analytic_hessian_by_fd(epsilon=1e-4)


# ---------------------------------------------------------------------------------------------
# Things that must be refused rather than answered wrongly
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad_coordinate", [0.0, float("nan")])
def test_degenerate_line_tangent_is_reported_after_assembly(bad_coordinate, capfd):
    """The failure path must not leak NaNs through the generated element loop."""
    from pyoomph.equations.ALE import LaplaceSmoothedMesh

    class NormalResidual(Equations):
        def define_fields(self):
            self.define_scalar_field("u", "C2")

        def define_residuals(self):
            u, u_test = var_and_test("u")
            self.add_residual(
                weak(u + dot(var("normal"), vector(0.37, -0.61)), u_test)
            )

    class P(Problem):
        def define_problem(self):
            self.add_mesh(_ScaledLineMesh(1.0))
            self.add_equations(
                (LaplaceSmoothedMesh() + ElementSpace("C2") + NormalResidual()) @ "line"
            )

    with P() as problem:
        problem.initialise()
        for node in problem.get_mesh("line").nodes():
            node.set_x(0, bad_coordinate)
            node.set_x(1, 0.0)
        with pytest.raises(RuntimeError, match="OomphException"):
            problem.assemble_jacobian(with_residual=True)
        assert "cannot normalise" in capfd.readouterr().err


def test_second_spatial_derivative_of_the_normal_is_refused():
    class Eq(Equations):
        def define_fields(self):
            self.define_scalar_field("u", "C2")

        def define_residuals(self):
            v = testfunction("u")
            self.add_residual(weak(div(grad(var("normal"))), v))

    class P(Problem):
        def define_problem(self):
            self.add_mesh(RectangularQuadMesh(N=2))
            self.add_equations(_Bulk() @ "domain")
            self.add_equations(Eq() @ "domain/bottom")

    with pytest.raises(Exception, match="[Ss]econd spatial derivative"):
        with P() as p:
            p.initialise()


def test_lagrangian_derivative_of_the_normal_is_refused():
    class Eq(Equations):
        def define_fields(self):
            self.define_scalar_field("u", "C2")

        def define_residuals(self):
            v = testfunction("u")
            self.add_residual(weak(div(var("normal"), lagrangian=True), v))

    class P(Problem):
        def define_problem(self):
            self.add_mesh(RectangularQuadMesh(N=2))
            self.add_equations(_Bulk() @ "domain")
            self.add_equations(Eq() @ "domain/bottom")

    with pytest.raises(Exception, match="Lagrangian"):
        with P() as p:
            p.initialise()
