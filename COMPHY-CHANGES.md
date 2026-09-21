# CoMPhy changes

This file tracks the maintained difference from upstream. It contains no
private simulation records. Full history and authorship remain in Git.

| Change | Purpose | Evidence | Upstream status |
| --- | --- | --- | --- |
| Fork maintenance | Pristine mirrors, explicit integration candidates, bounded checks and reproducible releases. | Offline Git integration tests and CoMPhy maintenance run artifacts. | Fork-specific; no upstream submission requested. |
| Release publishing | Prepare a versioned proposal from tested assets; publish after explicit approval with immutable tags and assets. | Offline release guard/recovery tests and the CoMPhy release preparation workflow. | Fork-specific; no upstream submission requested. |
| Scale-safe line normals | Preserve unit normals and analytic coordinate derivatives for physical interface scales below `1e-10`; report genuinely collapsed or non-finite tangents through the collective inverted-element path. | `tests/test_normal_derivatives.py`, including current/history positions, Q2 bulk boundaries, coordinate Jacobians and degenerate tangents down to `1e-18`. | CoMPhy fix; no upstream submission requested. |
| Restarted nodal histories | Preserve loaded nodal value and position histories when boundary-condition lifecycle hooks rebuild interfaces and reapply slot-zero conditions. | `tests/test_state_file_restart.py::test_pinned_nodal_histories_survive_state_roundtrip`. | CoMPhy fix; no upstream submission requested. |
| Catmull-Rom spline inversion | Stop the Gauss-Newton projection in `CurvedEntityCatmullRomSpline::position_to_parametric` when the step along the curve is at coordinate roundoff, not only when the parameter step is; interfaces graded over many decades (coalescence necks) otherwise fail every remesh with `Cannot invert spline`. The error now reports the offending position and step. | `tests/test_curved_boundaries.py::test_catmull_rom_inversion_survives_eleven_decades_of_grading`; branch `fix/spline-inversion-position-tolerance`. | Generally useful; upstream submission to be requested after CoMPhy review. |

Bootstrap base: upstream `main` at
`4f286f7317b06a6e8bf5b068532300d5790d5968` (1 September 2026).
No solver modifications or backports are introduced by the maintenance setup.
Future integrations record their exact upstream parent in the merge commit and
sync receipt. Add each CoMPhy solver change here with its focused commit,
regression test and contribution status; distinguish a submitted PR from an
accepted upstream fix.
