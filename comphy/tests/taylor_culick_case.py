"""Reduced planar Taylor--Culick case used by the CoMPhy regression suite.

Adapted from ``simulationCases/planar_comoving.py`` at commit
``bb3695d12626bcb9996768aa727b93e0e5e964ed`` of
https://github.com/comphy-lab/taylor-culick-fem, authored by Vatsal Sanjay.
The geometry, equations and boundary conditions are retained with the author's
permission for this GPL-3.0-or-later pyoomph test suite; expensive output and
campaign-scale resolution are omitted for this software regression.
"""

import os

os.environ.setdefault("HWLOC_COMPONENTS", "-gl")
os.environ.setdefault("MPLBACKEND", "Agg")

from pyoomph import (  # noqa: E402
    DirichletBC,
    GmshTemplate,
    IntegralObservables,
    Problem,
    WeakContribution,
)
from pyoomph.equations.ALE import HyperelasticSmoothedMesh  # noqa: E402
from pyoomph.equations.navier_stokes import (  # noqa: E402
    NavierStokesEquations,
    NavierStokesFreeSurface,
)
from pyoomph.expressions import partial_t, var, vector  # noqa: E402
from pyoomph.equations.generic import EnforcedDirichlet  # noqa: E402
from pyoomph.meshes.zeta import (  # noqa: E402
    AssignZetaCoordinatesByArclength,
    AssignZetaCoordinatesByEulerianCoordinate,
)

HALF_THICKNESS = 0.5


class PlanarSheetMesh(GmshTemplate):
    def define_geometry(self):
        problem = self.get_problem()
        self.mesh_mode = "tris"
        self.default_resolution = problem.resolution

        tip = self.point(0.0, 0.0)
        shoulder = self.point(HALF_THICKNESS, HALF_THICKNESS)
        far_top = self.point(problem.domain_length, HALF_THICKNESS)
        self.circle_arc(tip, shoulder, center=(HALF_THICKNESS, 0.0), name="interface")
        self.line(shoulder, far_top, name="interface")
        far_bottom = self.point(problem.domain_length, 0.0)
        self.line(far_top, far_bottom, name="far")
        self.line(far_bottom, tip, name="symmetry")
        self.plane_surface("interface", "far", "symmetry", name="liquid")


class PlanarTaylorCulickProblem(Problem):
    def __init__(self):
        super().__init__()
        self.Oh = 0.05
        self.domain_length = 2.0
        self.resolution = 0.25
        self.write_states = False

    def define_problem(self):
        self.set_coordinate_system("cartesian")
        self += PlanarSheetMesh()
        frame_velocity, frame_test = self.add_global_dof("U", initial_condition=0.0)
        bulk_force = -vector(1, 0) * partial_t(frame_velocity)
        equations = NavierStokesEquations(
            mass_density=1, dynamic_viscosity=self.Oh, bulkforce=bulk_force
        )
        equations += HyperelasticSmoothedMesh()
        equations += NavierStokesFreeSurface(surface_tension=1) @ "interface"
        equations += DirichletBC(velocity_y=0, mesh_y=0) @ "symmetry"
        equations += DirichletBC(
            mesh_x=self.domain_length, mesh_y=True, velocity_y=0
        ) @ "far"
        equations += EnforcedDirichlet(velocity_x=-frame_velocity) @ "far"
        equations += WeakContribution(var("mesh_x"), frame_test) @ "interface/symmetry"
        equations += AssignZetaCoordinatesByArclength(sort_along_axis="x+") @ "interface"
        equations += AssignZetaCoordinatesByEulerianCoordinate("x") @ "symmetry"
        equations += IntegralObservables(area=1)
        self += equations @ "liquid"

    def control_velocity(self) -> float:
        return self.get_ode("globals").get_value("U", as_float=True)
