# Governance

This document describes who makes decisions in EdgeFDL, how decisions are made, what the maintainers commit to, and
how the project continues when a maintainer leaves. The list of maintainers is in [MAINTAINERS.md](MAINTAINERS.md);
the contribution workflow is in [CONTRIBUTING.md](CONTRIBUTING.md).

## Roles

| Role | Who | Rights and duties |
|---|---|---|
| Contributor | Anyone who has opened an issue or pull request, or contributed a field report, topology, vendor template, translation or documentation | Proposes and discusses changes; pull requests are merged after review |
| Maintainer | The people listed in [MAINTAINERS.md](MAINTAINERS.md) | Review and merge, triage issues, publish releases; follow the response targets below |
| Domain reviewer | Experts in network operations or reinforcement learning invited by the maintainers | Review changes to fault semantics, detection and alarm logic, the agent and its training and evaluation; may veto such changes |
| Project lead | Currently @seuclkzsd | Takes the final decision when maintainers disagree and no consensus can be reached, and publishes the reasons |

**Becoming a maintainer.** At least five merged substantial pull requests over at least three months, or sustained
participation in reviews. A maintainer nominates the candidate in an issue; the nomination takes effect if no other
maintainer objects within 7 days. Nominations, approvals and changes of permissions are recorded in that issue.

## Decision rules

| Type of change | Condition for merging |
|---|---|
| Documentation, tests, refactoring and performance work that leave alarms and published numbers unchanged | CI passes and one maintainer approves |
| User-visible interfaces: CLI subcommands and options, REST endpoints and their JSON fields, Prometheus metric names and labels, CSV log columns and `schema_version`, topology and intake file formats, the model bundle format | An issue open for discussion for at least 7 days; semantic versioning (see "Releases") |
| **Detection, diagnosis and alarm logic**: the detector (`edgefdl/core/denoise.py`), the engine's confirmation, multi-alarm, recovery and adaptive-baseline rules (`edgefdl/core/orchestrator.py`), the agent's features, actions, rewards and inference (`edgefdl/agent/`), fault semantics and sample generation (`edgefdl/faults/`) | All four rules below |
| New dependencies | OSI-approved open-source license; recorded in `requirements.txt`, `pyproject.toml` and [NOTICE](NOTICE) |
| This file and MAINTAINERS.md | Pull request open for at least 14 days, approved by the project lead |

Changes to detection, diagnosis and alarm logic:

1. **A test is required**: a test in `tests/` that fails before the change and passes after it; for a change that
   should not alter behaviour, a test that shows it does not.
2. **The robustness criteria are re-run**: the robustness suite with the default engine and with `--single-alarm`
   on seeds 21, 33 and 47, compared with `scripts/compare_robust.py`, which must print `ALL PASS`. The output is
   attached to the pull request (commands in [CONTRIBUTING.md](CONTRIBUTING.md#changes-that-affect-accuracy-or-robustness)).
   When the change alters any published number, the pipeline and the robustness suite are re-run and their raw
   outputs are committed to `results/` in the same pull request.
3. **A review is required**: besides the author, at least one maintainer or domain reviewer approves. While the
   project has a single maintainer, that maintainer's own changes of this kind are announced in an issue at least
   7 days before merging, together with the comparison output.
4. **Documentation is updated**: [docs/algorithms.md](docs/algorithms.md), [docs/results.md](docs/results.md) and
   the README tables are updated with the change, and the change is recorded in [CHANGELOG.md](CHANGELOG.md).

**Claims about accuracy, robustness and performance.** Every number in the README, the documentation and the release
notes comes from raw data committed to `results/` (JSON result files and logs) and produced by a documented command.
It is stated with its conditions: backend, topology, sample count, seed, CPU, and Python and PyTorch versions.
Numbers that cannot be reproduced from committed data do not appear in these places. Results measured on real
networks are reported separately from simulation results and say so.

**Disagreements** are first discussed in the issue with the aim of reaching consensus. If none is reached, the
maintainers decide by simple majority; on a tie, or while there is a single maintainer, the project lead decides and
records the reasons in the issue.

## Response targets

| Item | Target |
|---|---|
| First response to a new issue | Within 7 days |
| First review of a pull request | Within 14 days |
| Acknowledgement of a security report | Within 3 business days ([SECURITY.md](SECURITY.md)) |
| Response to a Code of Conduct report | Within 7 days ([CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)) |
| Releases | At least one minor or patch release per month while there are user-visible changes |

How well these targets are met (median time to first response for issues and pull requests) is recorded at the end of
each quarter in the progress table of [ROADMAP.md](ROADMAP.md).

## Releases

- Version numbers follow [Semantic Versioning](https://semver.org/). Before 1.0.0, a minor version may contain
  incompatible interface changes; they are announced in [CHANGELOG.md](CHANGELOG.md) and, where possible, the old
  behaviour is kept as deprecated for one minor version. Changes that alter alarms or published numbers are listed
  separately in the release notes.
- Before a release: CI passes; the version in `edgefdl/__init__.py` (read by `pyproject.toml`), `CITATION.cff` and
  `CHANGELOG.md` agree; the model bundle and result files in `results/` were produced by the code being released.
- Every release is tagged `vX.Y.Z` and published as a GitHub Release whose notes come from CHANGELOG.md.

## Succession

- A maintainer with no activity (commits, reviews or issue replies) for 6 consecutive months is asked in an issue to
  state whether they continue, pause or step down. Without an answer within 30 days of the reminder they are
  considered to have stepped down, and the other maintainers revoke their permissions.
- Every maintainer names a successor contact in [MAINTAINERS.md](MAINTAINERS.md). When the project lead steps down,
  the remaining maintainers elect a new lead within 30 days; if there are no other maintainers, the successor
  contact takes over the administration of the repository.
- If no maintainer has been active for 12 consecutive months, anyone may continue the project under the Apache
  License 2.0, and the README will point to a successor repository recognized by the community.
