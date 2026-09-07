"""Bounded resident-memory regression for repeated fixed-mesh replacement."""

import gc
import os
from pathlib import Path

import gmsh  # noqa: F401 -- a missing required remeshing dependency is a hard failure
import pytest

from pyoomph import ElementSpace, Equations, GmshTemplate, Problem


def _rss_mib() -> float:
    """Return the current Linux resident set, rather than the peak RSS."""
    status = Path("/proc/self/status")
    if not status.is_file():
        pytest.fail("the CoMPhy RSS regression requires Linux /proc")
    for line in status.read_text(encoding="ascii").splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024.0
    pytest.fail("VmRSS is absent from /proc/self/status")


def _least_squares_slope(values: list[float]) -> float:
    centre = (len(values) - 1) / 2.0
    denominator = sum((i - centre) ** 2 for i in range(len(values)))
    return sum((i - centre) * value for i, value in enumerate(values)) / denominator


class FixedSquare(GmshTemplate):
    """A deterministic mesh whose size and topology do not change on rebuild."""

    def define_geometry(self):
        self.default_resolution = 0.045
        p00 = self.point(0, 0)
        p10 = self.point(1, 0)
        p11 = self.point(1, 1)
        p01 = self.point(0, 1)
        self.create_lines(p00, "bottom", p10, "right", p11, "top", p01, "left", p00)
        self.plane_surface("bottom", "right", "top", "left", name="domain")


def test_repeated_fixed_mesh_rebuild_has_bounded_rss_slope(tmp_path):
    """A replaced mesh must not remain live after each ``force_remesh`` call.

    Four warm-up rebuilds absorb JIT, gmsh and allocator startup.  The measured
    sequence is long enough that retaining one roughly 1,000-element C2 mesh per
    iteration is visible as a sustained RSS slope, while a one-off allocation
    remains below the trend and endpoint limits.
    """
    os.environ.setdefault("HWLOC_COMPONENTS", "-gl")

    with Problem() as problem:
        problem.set_output_directory(str(tmp_path / "rss"))
        problem.quiet()
        problem += FixedSquare()
        problem += ElementSpace("C2") @ "domain"
        problem.initialise()

        expected_elements = problem.get_mesh("domain").nelement()
        assert expected_elements >= 500, "mesh is too small to expose retained rebuilds"

        for _ in range(4):
            problem.force_remesh()
        gc.collect()

        samples = []
        for _ in range(12):
            problem.force_remesh()
            # Re-numbering fills each element's local-coordinate buffer. Repeating
            # the fill makes the historical dim*nnode allocation leak large enough
            # to distinguish from allocator granularity on a hosted runner.
            for _ in range(10):
                problem.reapply_boundary_conditions()
            gc.collect()
            assert problem.get_mesh("domain").nelement() == expected_elements
            samples.append(_rss_mib())

    slope = _least_squares_slope(samples)
    growth = samples[-1] - samples[0]
    print("PYOOMPH_COMPHY_RSS samples_mib=" + ",".join(f"{value:.3f}" for value in samples))
    print(f"PYOOMPH_COMPHY_RSS slope_mib_per_rebuild={slope:.4f} growth_mib={growth:.3f}")
    assert slope <= 0.75, f"RSS grows by {slope:.3f} MiB per fixed-mesh rebuild"
    assert growth <= 8.0, f"RSS grew by {growth:.3f} MiB over the measured rebuilds"
