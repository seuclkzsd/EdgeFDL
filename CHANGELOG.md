# Changelog

All user-visible changes to EdgeFDL are recorded in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-10-30

First release of EdgeFDL: autonomous link-level fault detection and localization for edge networks, running on the
network-management server (EMS).

### Added

- **Requirements intake** (`python -m edgefdl intake`): a form template
  ([configs/intake_demo/form.yaml](configs/intake_demo/form.yaml)), extraction of interfaces, addresses, OSPF costs
  and speeds from Huawei, H3C and Cisco configuration text, LLDP neighbour tables, and an optional OpenAI-compatible
  multimodal endpoint (`--llm`) for topology images and PDFs.
- **Topology model** (`edgefdl/topology.py`): automatic IP and loopback planning, OSPF shortest paths, monitored
  targets, and coverage planning that adds a device-native NQA / IP SLA probe for links that no monitored path
  crosses. Demo topology [configs/demo_topology.yaml](configs/demo_topology.yaml): 12 devices, 15 links.
- **Equivalent virtual network** with three interchangeable backends and the same fault semantics: `sim` (analytic
  simulator), `netns` (Linux network namespaces, FRR OSPF, `tc`) and `lxd` (one container per device, optional
  bridged uplink; `python -m edgefdl.vnet.lxd ... --uplink` prints the deployment command sequence).
  `python -m edgefdl vnet up|down|status` manages the real-stack backends.
- **Fault model and injector**: link down, delay, loss, jitter, bandwidth degradation (`tbf`) and route drift (OSPF
  cost change that moves a monitored path), applied at both link ends; 231 fault primitives on the demo topology.
- **Sample generation** (`python -m edgefdl baseline`, `gen`): baseline learning, trigger-aligned samples cut where
  the online detector wakes the agent, domain randomization (transient length, ICMP rate limiting, background
  jitter), `--plain` for standard-condition test sets, multi-process generation.
- **DDQN agent** (`python -m edgefdl train`, `eval`): Double DQN with a dueling head, a link-shared encoder,
  prioritized replay, action masking and a deep-probe action with a per-probe cost in the reward; 36,041
  parameters; PyTorch and NumPy inference engines (`serve --engine numpy`). Model bundles are single files
  containing weights, topology, baseline and measurement plan.
- **Online engine** (`python -m edgefdl serve`): 5 s light measurement rounds (ping, traceroute, packet-train
  bandwidth estimates, OSPF LSDB), k-of-n detector, diagnosis with on-demand deep probes, confirmation, one alarm
  per fault with evidence, device-down aggregation, concurrent multi-fault alarms through residual re-baselining
  (`--single-alarm` keeps one alarm at a time), per-alarm recovery, on-site baseline learning
  (`--learn-rounds`), RTT baseline tracking and background-loss re-learning.
- **Remediation advice**: troubleshooting and remediation commands for Huawei VRP, H3C Comware and Linux/FRR, with
  temporary actions rehearsed on the equivalent topology so that only feasible actions are recommended.
- **Integrations**: Web dashboard (Chinese UI) at `http://127.0.0.1:8460`, REST API (`/api/topology`, `/api/status`,
  `/api/alarms`, `/api/series`, `/api/export/*.csv`, `POST /api/inject` and `/api/clear` for virtual backends,
  `--no-inject` to disable them), Prometheus `/metrics`, Syslog (RFC 5424) and Webhook (JSON) sinks (`--sinks`).
- **Logs**: `measure_log.csv` and `alarm_log.csv` with `schema_version` and `*.schema.json`, flushed per line,
  synced to disk every 5 s, rotated at 200 MB.
- **Evaluation tools**: `python -m edgefdl pipeline` (baseline, samples, training, evaluation and self-test in one
  command), `bench` (inference latency, overhead, long-run false alarms, online scenarios), `robust` (harsh
  conditions, measurement defects, fault-size sweep, concurrent faults and device failures; `--seed`, `--kinds`,
  `--only`, `--single-alarm`), and [scripts/compare_robust.py](scripts/compare_robust.py) for pass criteria of the
  multi-alarm engine against the single-alarm mode.
- **Real-stack lab**: [scripts/lab.sh](scripts/lab.sh) and [scripts/lab/Dockerfile](scripts/lab/Dockerfile)
  (Debian 12 with FRR, iperf3, traceroute, iproute2) and [scripts/netns_smoke.py](scripts/netns_smoke.py) with 26
  checks of fault semantics on the real protocol stack.
- **Released model and results** in [results/](results/README.md): `model.pt`, `baseline.json`, `eval_test.json`,
  `eval_test_rand.json`, `bench.json`, `robust.json`, `pipeline.log` and real-stack smoke logs. On the independent
  test set (10,000 samples, standard conditions, simulation backend, demo topology), type + location accuracy is
  100.00% for link down, 98.73% for performance degradation and 100.00% for route drift; one forward pass takes
  0.575 ms at p99 (PyTorch, 1 thread). Details and conditions: [docs/results.md](docs/results.md).
- **Documentation**: [README.md](README.md) (English) and [README.zh-CN.md](README.zh-CN.md), quick start,
  deployment, API, architecture, algorithms, results and comparison pages under [docs/](docs/index.md).
- **Project infrastructure**: Apache-2.0 [LICENSE](LICENSE) and [NOTICE](NOTICE), SPDX headers checked by
  `tools/dev/spdx.py`, [CONTRIBUTING.md](CONTRIBUTING.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) (Contributor
  Covenant 2.1), [SECURITY.md](SECURITY.md) with a data handling statement, [GOVERNANCE.md](GOVERNANCE.md),
  [MAINTAINERS.md](MAINTAINERS.md), [ROADMAP.md](ROADMAP.md), [CITATION.cff](CITATION.cff), issue forms (bug report,
  feature request, report from a real network) and a pull request template, a CI workflow running
  `tools/dev/ci.sh`, a release workflow, and 34 unit tests.
- **Demo video** `EdgeFDL-demo-zh.mp4` (Chinese narration, 6 min 19 s) attached to the
  [v0.1.0 release](https://github.com/seuclkzsd/EdgeFDL/releases/tag/v0.1.0).

[Unreleased]: https://github.com/seuclkzsd/EdgeFDL/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/seuclkzsd/EdgeFDL/releases/tag/v0.1.0
