<!-- SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
# Getting help

Support is given in public on GitHub by the maintainers ([MAINTAINERS.md](MAINTAINERS.md)) and the community, so that the
next person finds the answer.

| You want to | Go to |
|---|---|
| Try it, without Python on your machine | `docker compose up`, then <http://127.0.0.1:8460>; [quick start](docs/quickstart.md) |
| Look something up | [Documentation index](docs/index.md); [troubleshooting table](docs/quickstart.md#7-troubleshooting) |
| Report something that does not work | [Bug report](https://github.com/seuclkzsd/EdgeFDL/issues/new?template=bug_report.yml) |
| Ask a usage question or propose a change | [Feature request](https://github.com/seuclkzsd/EdgeFDL/issues/new?template=feature_request.yml); say that it is a question in the title |
| Tell us how it behaved on your network | [Report from a real network](https://github.com/seuclkzsd/EdgeFDL/issues/new?template=field_report.yml) |
| Report a security problem | Privately, as described in [SECURITY.md](SECURITY.md); not in a public issue |
| Contribute | [CONTRIBUTING.md](CONTRIBUTING.md), section "Your first contribution" |

Issues may be written in English or Chinese. Maintainers give a first response to an issue within 7 days and to a pull
request within 14 days ([GOVERNANCE.md](GOVERNANCE.md#response-targets)).

A useful report contains:

- the version (`python -m edgefdl --version`) and how it was installed (pip, source, Docker);
- the operating system, the Python and PyTorch versions, and the backend (`sim`, `netns`, `lxd`);
- the exact command or HTTP request, what you expected and what happened, with the console output;
- for wrong, missed or late alarms: the alarm from `GET /api/alarms` and the `alarm_log.csv` and `measure_log.csv`
  lines around the event.

Replace real device names, site names and addresses before posting ([CONTRIBUTING.md](CONTRIBUTING.md#field-reports-and-topologies-from-real-networks)).
