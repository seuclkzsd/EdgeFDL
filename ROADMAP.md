# Roadmap

EdgeFDL's goal: an edge network with a single network-management server and a small operations team learns about a
fault from one alarm that names the faulty link and the fault type, with evidence and vendor-specific commands,
instead of from user complaints and hop-by-hop logins. The table below lists the plan by quarter with a
**measurable criterion** for each item. At the end of every quarter the progress table is updated; unfinished items
state the reason and are moved or dropped. To discuss an item or volunteer for it, open a feature request that names
the item.

## What 0.1 provides

- Requirements intake from a form, device configurations and LLDP tables, with an optional local multimodal model
  for topology images; a topology model with IP planning, OSPF shortest paths and coverage planning that adds
  device-native NQA / IP SLA probes for links no monitored path crosses.
- An equivalent virtual network with three interchangeable backends and the same fault semantics: the `sim`
  analytic simulator, `netns` (Linux namespaces, FRR OSPF, `tc`) and `lxd` (one container per device).
- Training per network without production fault data: enumeration of the fault space, trigger-aligned and
  domain-randomized samples, and a DDQN agent with a deep-probe action and a link-shared encoder, delivered as a
  single-file model bundle.
- Online operation on the EMS, measuring the equivalent virtual network through any of the three backends: 5 s light
  measurement rounds, k-of-n detection, diagnosis, confirmation,
  one alarm per fault with evidence and rehearsed remediation advice, device-down aggregation, concurrent alarms,
  adaptive baseline, per-alarm recovery.
- Integration through a Web dashboard, a REST API, Syslog (RFC 5424), Webhook (JSON), Prometheus `/metrics` and CSV
  logs with schema files.

Measured results and their conditions are in [docs/results.md](docs/results.md) and [results/](results/README.md).

## Plan

| Quarter | Version | Item | Measurable criterion |
|---|---|---|---|
| 2026Q4 | 0.2 | `lxd` backend on hardware, interconnected with a physical network through the EMS uplink | On a physical host, `vnet up --backend lxd` builds the demo topology; the 26 checks of the real-stack smoke test pass on the `lxd` backend; probes reach at least one device of a physical network through the bridged uplink; logs of the run are published in `results/` |
| 2026Q4 | 0.2 | 72-hour real-time stress test of `serve` on the real protocol stack | 72 h of wall-clock operation on the `netns` backend with background noise: 0 engine errors; false alarms, inference latency, probe bandwidth and CPU and memory use reported against the targets (single-round inference ≤ 100 ms, measurement bandwidth < 5%, server resources ≤ 20%); `measure_log.csv` and `alarm_log.csv` published with the release |
| 2026Q4 | 0.2 | Sample generation on the real stack and fine-tuning, including heavy loss (≥ 30%) with OSPF adjacency flaps | A test set generated on the `netns` backend covering all six fault types; the fine-tuned model reaches type + location accuracy ≥ 98% for link down, ≥ 95% for performance degradation and ≥ 95% for route drift on it, and keeps these levels on the simulation test set |
| 2027Q1 | 0.3 | Passive OSPF listening and readers for SNMP and device-native NQA / IP SLA results | Adjacency and cost changes are detected from a passive OSPF adjacency without `vtysh` access; NQA / IP SLA results read over SNMP enter the features; both are covered by tests on recorded protocol traces |
| 2027Q1 | 0.3 | Active probing for light loss | In the fault-size sweep of `python -m edgefdl robust`, 2% one-way loss is detected and located (type + link) in at least 90% of samples, with no additional false alarms in the 24 h runs and probe traffic below 1% of the narrowest link |
| 2027Q1 | 0.3 | Kylin OS and openEuler adaptation | Unit tests and the 26 real-stack smoke checks pass on Kylin OS V10 and openEuler 24.03 LTS, recorded in CI or in a dated validation report |
| 2027Q1 | 0.3 | PyPI package | In a fresh virtual environment, `pip install edgefdl` followed by the quick-start `serve` command works unchanged; the wheel is built and published by the release workflow |
| 2027Q1 | 0.3 | English and Chinese user interface | Dashboard and CLI help switch between English and Chinese; all strings live in one message catalogue, and a test checks that every key exists in both languages |
| 2027Q2 | 0.4 | Cross-topology transfer of the link-shared encoder | Trained on at least 5 generated topologies and evaluated on at least 3 unseen ones: type + location accuracy ≥ 95% for performance degradation without retraining, or after fine-tuning that takes at most 10% of the per-topology training time |
| 2027Q2 | 0.4 | Larger topologies (50–100 devices) | On a 100-device topology: single-round inference ≤ 100 ms, measurement bandwidth < 5% of the narrowest link, server resources ≤ 20%, and test-set accuracy at the targets (link down ≥ 98%, degradation ≥ 95%, route drift ≥ 95%) |
| 2027Q2 | — | Governance: a larger maintainer team | A second maintainer and two domain reviewers (network operations, reinforcement learning) listed in [MAINTAINERS.md](MAINTAINERS.md) |
| 2027Q3 | 1.0 | Field deployment on a production edge network; stable interfaces | At least one production edge network monitored read-only for at least 30 days; a published, anonymized field report compares every alarm with the operator's records of real faults; CLI, REST API, Prometheus metrics and CSV log schema declared stable under semantic versioning |

Release cadence: at least one minor or patch release per month while there are user-visible changes
([GOVERNANCE.md](GOVERNANCE.md)).

## Non-goals

| Not planned | Reason and alternative |
|---|---|
| Replacing general monitoring platforms (Zabbix, Prometheus, Nightingale, LibreNMS, OpenNMS and others) | They are mature and cover far more than link faults. EdgeFDL complements them and sends its alarms to them through Syslog, Webhook and Prometheus ([docs/comparison.md](docs/comparison.md)) |
| Writing configuration to production devices | EdgeFDL is read-only by design. Remediation is advisory: it is rehearsed on the equivalent virtual network and shown as vendor commands for the operator to apply |
| Routing protocols other than OSPF in 0.x | Route-drift detection and the equivalent virtual network model OSPF. IS-IS and BGP will be planned after 1.0 |

## Progress

| Quarter | Done | Not done, and why | Median first response to issues | Median first review of pull requests |
|---|---|---|---|---|
| 2026Q4 | — | — | — | — |
