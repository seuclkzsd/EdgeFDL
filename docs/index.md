# EdgeFDL documentation

EdgeFDL runs on the network-management server (EMS) of an edge network, measures the network with light probes and,
when an anomaly persists, raises one alarm per fault that names the faulty link and the fault type, with the evidence
and vendor-specific remediation steps. The model behind it is trained for each network on an equivalent virtual
network, without production fault data.

These pages are in English. The command-line messages, the CLI help and the web dashboard are in Chinese;
the pages translate the labels you will see.

## Where to start

| You want to | Read, in this order |
|---|---|
| See it work (about 10 minutes, no network hardware) | [Quick start](quickstart.md) → [Results](results.md) |
| Prepare it for your own network | [Quick start](quickstart.md) → [Deployment](deployment.md) → [REST API and integrations](api.md) |
| Connect it to a monitoring or ticketing system | [REST API and integrations](api.md) → [Deployment, section 5](deployment.md#5-integrations) |
| Understand how it works | [Architecture](architecture.md) → [Algorithms](algorithms.md) → [Results](results.md) |
| Compare it with other tools | [Comparison](comparison.md) → [Results](results.md) |
| Check a number | [Results](results.md) → [`results/`](../results/README.md) (raw JSON, logs and the released model) |
| Contribute | [CONTRIBUTING.md](../CONTRIBUTING.md) → [Architecture](architecture.md) → [ROADMAP.md](../ROADMAP.md) |

## Using EdgeFDL

| Page | Content |
|---|---|
| [Quick start](quickstart.md) | Install on Linux (CPU-only PyTorch is enough), start the dashboard with the bundled model, inject faults from the page and read the alarm, train a model with the pipeline, run the evaluation, self-test and robustness suite, the real-protocol-stack lab in Docker, troubleshooting |
| [Deployment](deployment.md) | Preparing EdgeFDL for a specific network on an EMS (in 0.1.2 `serve` measures the network's equivalent virtual network): requirements and measured footprint, describing the network with the intake form and device configurations, the three equivalent-network backends (`sim`, `netns`, `lxd`), training, on-site baseline learning, multi-alarm and single-alarm modes, alarm sinks, Prometheus, log rotation, security |
| [REST API and integrations](api.md) | Every HTTP endpoint with real example responses and error messages, the cross-site request checks, the alarm object (including device-down alarms), Prometheus metrics, Syslog and Webhook formats, the CSV log schemas |

## How it works

| Page | Content |
|---|---|
| [Architecture](architecture.md) | Components, the offline pipeline and the online loop, the module map, the files exchanged between stages (topology, model bundle, sample sets, logs) |
| [Algorithms](algorithms.md) | Link-level features from a single vantage point, the k-of-n detector, the DDQN agent with deep-probe actions, trigger-aligned and domain-randomized samples, adaptive baselines, device-down aggregation, concurrent alarms by residual re-baselining, remediation rehearsal |

## Evidence

| Page | Content |
|---|---|
| [Results](results.md) | Accuracy on the independent test set, the self-test (latency, overhead, 72-hour-equivalent run, 84 online scenarios), the robustness suite, multi-fault and device-down trials, fault-size sensitivity, the real-stack smoke test and the simulator vs real-stack RTT comparison; each figure with its test conditions and source file |
| [Comparison](comparison.md) | Measured head-to-head with re-implementations of the published methods on the same measurements, and a capability-level comparison with general monitoring (Zabbix, Prometheus, Nightingale), network management and alarm correlation (LibreNMS, OpenNMS ALEC), active measurement (perfSONAR, SmokePing), configuration analysis (Batfish) and research approaches |
| [`results/README.md`](../results/README.md) | What each file in `results/` contains and which command produced it |

The demo video (Chinese narration, 6 min 41 s) is attached to the
[latest release](https://github.com/seuclkzsd/EdgeFDL/releases/latest) as `EdgeFDL-demo-zh.mp4`. Dashboard
screenshots are in [`docs/media/`](media/).

## Project

| Document | Content |
|---|---|
| [README](../README.md) · [中文说明](../README.zh-CN.md) | Overview, key results, quick start (the English README is authoritative) |
| [CONTRIBUTING.md](../CONTRIBUTING.md) | Development setup, tests, commit and review rules, field reports from real networks |
| [GOVERNANCE.md](../GOVERNANCE.md) · [MAINTAINERS.md](../MAINTAINERS.md) | Roles, decision rules, maintainers |
| [SECURITY.md](../SECURITY.md) | Reporting vulnerabilities, response times, what EdgeFDL reads, writes, executes and sends |
| [CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md) | Community standards |
| [ROADMAP.md](../ROADMAP.md) · [CHANGELOG.md](../CHANGELOG.md) | Quarterly plan with success criteria and non-goals; version history |
| [LICENSE](../LICENSE) · [NOTICE](../NOTICE) · [CITATION.cff](../CITATION.cff) | Apache-2.0; attributions and third-party components; how to cite |
