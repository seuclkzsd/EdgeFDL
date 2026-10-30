# Maintainers

Roles, decision rules and response targets are described in [GOVERNANCE.md](GOVERNANCE.md).

| GitHub | Role | Responsibilities | Contact |
|---|---|---|---|
| [@seuclkzsd](https://github.com/seuclkzsd) | Maintainer, project lead | All modules, releases, review of detection, diagnosis and alarm logic and of the published results | GitHub issues; security problems as described in [SECURITY.md](SECURITY.md) |

## Open roles

| Role | Responsibilities | How to get involved |
|---|---|---|
| Second maintainer | CI and releases, the `netns` and `lxd` backends, packaging | Review and fix issues, and meet the conditions for maintainers in [GOVERNANCE.md](GOVERNANCE.md) |
| Domain reviewer (network operations) | Fault semantics, measurement plan and probe load, vendor command templates, interpretation of field reports from real networks | Contact the maintainers in an issue, or start with a field report or a vendor command template |
| Domain reviewer (reinforcement learning) | Agent design, rewards and training, sample generation and domain randomization, evaluation methodology | Contact the maintainers in an issue |

## Successor contacts

| Maintainer | Successor contact |
|---|---|
| @seuclkzsd | Not yet designated: the project has a single maintainer; the contact is added here within 30 days after a second maintainer joins |

Automatic review assignment is configured in [.github/CODEOWNERS](.github/CODEOWNERS).
