"""Representative Taylor--Culick checkpoint/restart software regression."""

import math

import numpy

from taylor_culick_case import PlanarTaylorCulickProblem


def _advance(problem, end_time):
    problem.run(end_time, startstep=2.0e-5, maxstep=1.0e-3,
                temporal_error=1.0e-3, outstep=False)


def test_short_planar_run_restarts_to_the_same_continuation(tmp_path):
    """A short production-shaped run must continue reproducibly from its dump.

    This is a software regression of the case construction and state machinery.
    Its coarse mesh and very short time horizon do not validate Taylor--Culick
    physics or accuracy.
    """
    output = tmp_path / "case"
    checkpoint = tmp_path / "taylor-culick.dump"

    with PlanarTaylorCulickProblem() as uninterrupted:
        uninterrupted.set_output_directory(str(output))
        uninterrupted.quiet()
        uninterrupted.initialise()
        _advance(uninterrupted, 0.002)
        velocity_at_checkpoint = uninterrupted.control_velocity()
        uninterrupted.save_state(str(checkpoint), quiet=True)
        _advance(uninterrupted, 0.004)
        expected_velocity = uninterrupted.control_velocity()
        expected_dofs = numpy.asarray(uninterrupted.get_history_dofs(0))

    assert math.isfinite(velocity_at_checkpoint) and velocity_at_checkpoint > 0
    assert math.isfinite(expected_velocity) and expected_velocity > velocity_at_checkpoint

    with PlanarTaylorCulickProblem() as restarted:
        restarted.set_output_directory(str(output))
        restarted._runmode = "overwrite"
        restarted.quiet()
        restarted.initialise()
        restarted.load_state(str(checkpoint), ignore_outstep=True)
        assert restarted.control_velocity() == velocity_at_checkpoint
        _advance(restarted, 0.004)
        actual_velocity = restarted.control_velocity()
        actual_dofs = numpy.asarray(restarted.get_history_dofs(0))

    assert abs(actual_velocity - expected_velocity) <= 1.0e-10
    assert actual_dofs.shape == expected_dofs.shape
    assert numpy.max(numpy.abs(actual_dofs - expected_dofs)) <= 1.0e-9
