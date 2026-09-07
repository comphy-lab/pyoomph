# CoMPhy changes

This file tracks the maintained difference from upstream. It contains no
private simulation records. Full history and authorship remain in Git.

| Change | Purpose | Evidence | Upstream status |
| --- | --- | --- | --- |
| Fork maintenance | Pristine mirrors, explicit integration candidates, bounded checks and reproducible releases. | Offline Git integration tests and CoMPhy maintenance run artifacts. | Fork-specific; no upstream submission requested. |

Bootstrap base: upstream `main` at
`4f286f7317b06a6e8bf5b068532300d5790d5968` (1 September 2026).
No solver modifications or backports are introduced by the maintenance setup.
Future integrations record their exact upstream parent in the merge commit and
sync receipt. Add each CoMPhy solver change here with its focused commit,
regression test and contribution status; distinguish a submitted PR from an
accepted upstream fix.
