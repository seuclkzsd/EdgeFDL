# Comparison with existing tools

EdgeFDL answers one operational question for an edge network: **which link is faulty, and what kind of fault is it**,
from the network-management server (EMS). This page places it next to the open-source tools an edge-network
operator typically runs, shows where it adds a capability, where those tools are stronger, and how EdgeFDL feeds its
alarms into them.

The comparison is at **capability level**, based on the public documentation of each project as of October 2026
(links in [References](#references)); we did not run these tools against EdgeFDL. EdgeFDL's numbers come from
[results.md](results.md) and hold for its test conditions: the analytic simulation backend, the 12-device demo
topology and the released model `results/model.pt`.

## Capability comparison

| | General monitoring and alerting<br>Zabbix · Prometheus + Alertmanager · Nightingale | Network management and alarm correlation<br>LibreNMS · OpenNMS ALEC | Active performance measurement<br>perfSONAR · SmokePing | **EdgeFDL** |
|---|---|---|---|---|
| Data source | Monitored items: host agents, exporters, SNMP, ICMP checks | Device events and counters: SNMP polling, traps, syslog | End-to-end measurements between measurement hosts, or from one host to targets | Active probes from the single EMS (ping, traceroute, packet trains, on-demand deep probes) and the read-only OSPF LSDB |
| Deployment requirements | Agents on managed hosts, or SNMP access to devices | SNMP and syslog enabled on devices | perfSONAR needs a measurement host at each end of a measured path | Runs on the EMS, read-only towards devices: pings and traceroutes start at the EMS; packet-train bandwidth estimates and on-demand deep probes run between the two ends of a link; device-native NQA / IP SLA covers links that no EMS probe crosses |
| Link down | Alert on the SNMP interface status; with ICMP checks only, the alert says which targets became unreachable | Interface status and traps report it directly; ALEC can group the resulting alarms by topology | The broken path is visible; the failed segment is found by hand | Names the failed link and both interfaces, and checks whether the backup path took over |
| Performance degradation | Per-item threshold alerts; the faulty link is not inferred | Mainly reachability, interface traffic and error counters; link-level delay and jitter are not inferred | Path-level delay, loss and throughput; the link is located by comparing paths by hand | Recognises delay, loss, jitter and bandwidth degradation and names the link |
| Route drift | No built-in detection; needs custom items | OSPF neighbour state is visible; a cost change that moves traffic is not judged | Path changes are visible in traceroute and judged by hand | Detected and located from the LSDB cost and the observed path changes |
| Alarm convergence and noise robustness | Dependencies and suppression configured by hand (Zabbix trigger dependencies; Alertmanager grouping, inhibition and silences; Nightingale muting and event pipelines) | Dependencies configured by hand; ALEC clusters alarms by time and topology distance into "situations" | Alert conditions set by hand | One alarm per fault; a device outage becomes one device-level alarm; concurrent faults are alarmed and cleared separately; k-of-n detection, agent judgement and confirmation against transients (0 false alarms in a 72-hour-equivalent run with transients in 15% of rounds) |
| Remediation advice | Predefined action scripts can run on alerts (Zabbix actions; Nightingale with ibex); Nightingale's built-in AI agent can investigate an alert over the connected monitoring data with a model the operator configures | ALEC can optionally ask an LLM for probable root causes and resolutions of a situation | None built in | Vendor-specific troubleshooting and repair commands (Huawei VRP, H3C Comware, Linux/FRR); temporary actions are rehearsed on the equivalent virtual network and only feasible ones are recommended |
| Rule or model construction | Templates and thresholds written by hand | Hand-written rules, or clustering of the alarms the network produces in operation | Measurement tasks configured by hand | Fault samples generated automatically on an equivalent virtual network of the operator's topology; no production fault data needed |

## What EdgeFDL adds

### 1. The alarm names the faulty link

Threshold monitoring and path measurement alert on targets or paths; one faulty link shows up as every target whose
path crosses it. On the demo topology, 36 targets are probed from the EMS (the 37th target is a device-native NQA
probe). Counting the targets whose baseline round trip crosses each link:

| Degraded link | Role | EMS-side targets whose round trip crosses it | Per-target threshold alerts | EdgeFDL |
|---|---|---|---|---|
| L1 | core – aggregation (`core`–`agg1`) | 16 of 36 | 16 at once, no link named | 1 alarm naming L1 and the fault type |
| L2 | core – aggregation (`core`–`agg2`) | 17 of 36 | 17 at once, no link named | 1 alarm naming L2 and the fault type |
| L15 | management uplink (`core`–`ems`) | 36 of 36 | all 36 at once | 1 alarm naming L15 |
| L3 | backup link (`agg1`–`agg2`) | 0 of 36 | none: a blind spot | coverage planning adds an NQA / IP SLA probe for L3; 1 alarm naming L3 |

The counts follow from the topology's OSPF routes and can be checked with the topology module:

```python
from edgefdl.topology import load

t = load("configs/demo_topology.yaml")
ems = [x for x in t.targets() if x.kind != "nqa"]          # the 36 EMS-side targets
def crossing(lid):
    return sum(lid in t.path_links(f) + t.path_links(r)
               for f, r in (t.route_to_target(t.vantage, x) for x in ems))
print({lid: crossing(lid) for lid in ("L1", "L2", "L15", "L3")})   # {'L1': 16, 'L2': 17, 'L15': 36, 'L3': 0}
```

EdgeFDL turns the same measurements into link-level evidence (far-end minus near-end round trip of each link,
contrast between targets that cross the link and those that do not, path changes, LSDB state) and lets the agent name
the link; see [algorithms.md](algorithms.md).

### 2. All fault types under one judgement, route drift included

The alarm types of existing tools follow the items an operator configured. Route drift is the hardest case for
thresholds: every target stays reachable and only the path changes. EdgeFDL recognises six fault types: link down,
delay, loss, jitter, bandwidth degradation and route drift. On the independent test set (10,000 samples, end-to-end)
the type + location accuracy is 100.00% for link down, 98.73% for performance degradation and 100.00% for route drift,
and a device outage is aggregated into one device-level alarm (60 of 60 trials).

### 3. No hand-written rules and no production fault data

Thresholds, dependencies and correlation rules are normally tuned by experienced staff, and data-driven correlation
needs a history of alarms from the production network. EdgeFDL builds an equivalent virtual network of the operator's
topology, enumerates the fault space (231 fault primitives on the demo topology), generates trigger-aligned samples
and trains a per-network model, so it can localize faults from the first day of operation.

### 4. Read-only, low overhead

EdgeFDL never writes device configuration. Its light measurements start at the EMS; only the packet-train bandwidth
estimate (3 links per round) and on-demand deep probes use a probe endpoint at a link end. On the demo topology the
measurement traffic is 0.222% of the narrowest 100 Mbps link even if all of it crossed that link, computation takes
0.0868% of one core, and one inference has a p99 of 0.575 ms, which fits a small EMS at the edge.

### 5. Closed-loop remediation with explainable conclusions

Each alarm carries its evidence, the alternatives the agent considered and the deep-probe results. The advice lists
vendor-specific commands, and temporary actions (take over on the backup path, raise the OSPF cost to steer traffic
away, restore the baseline cost) are rehearsed on the equivalent virtual network before they are recommended.

## Research context

Network tomography infers link-level performance from end-to-end measurements [[Coates 2002](#ref-coates),
[Duffield 2006](#ref-duffield)], usually with many sources or receivers and statistical models of the paths. Fault
localization systems in production networks follow the same idea at scale: black-hole detection in an ISP backbone
uses active measurements between edge routers and spatial correlation [[Kompella 2007](#ref-kompella)]; NetBouncer
probes data-center paths with IP-in-IP from servers to localize device and link failures
[[Tan 2019](#ref-netbouncer)]; 007 runs on every end host and finds the links responsible for TCP packet drops
[[Arzani 2018](#ref-007)]. These systems rely on many vantage points or on agents on servers, and they target outages
and packet loss.

An edge network offers one vantage point, the EMS, and needs more fault types. EdgeFDL works within that setting: it
probes the interface addresses at both ends of every link so that each link has its own differential evidence, adds
the routing-matrix contrast and the control plane (OSPF LSDB), and uses a DDQN agent that requests a deep probe of a
single link only when the evidence is insufficient (0.061 deep probes per sample on the independent test set).

## Where existing tools are stronger

The tools above are mature, broad and widely deployed, and EdgeFDL does not replace them.

- **Breadth and scale.** They monitor servers, applications, databases and network devices, with long-term storage,
  dashboards, permissions, high availability and large user communities. EdgeFDL answers one question for one routed
  domain.
- **Device telemetry.** SNMP polling, traps and syslog report interface state, error counters, CPU, memory,
  temperature and hardware events directly from the device; an interface going down is known as soon as the device
  reports it. EdgeFDL infers link state from probes and the LSDB and does not collect device health.
- **Measurement across domains.** perfSONAR's dedicated measurement hosts measure throughput and one-way delay
  between sites and across administrative domains. EdgeFDL measures round trips from one EMS inside one OSPF domain.
- **General correlation.** ALEC correlates alarms of any kind from any source with tunable clustering, and
  Nightingale's AI agent can investigate alerts over all connected data; EdgeFDL's model covers the network fault
  types it was trained on.
- **Verification before changes.** Batfish analyses configurations before they are deployed and finds errors that
  have not happened yet; EdgeFDL observes the running network.
- **Setup.** General tools work on any topology once configured; EdgeFDL needs a topology model and one model trained
  per network (the full pipeline takes about 15 minutes on a CPU for the demo topology). Interconnection with a
  physical network through the LXD backend, reading device-native NQA results over SNMP and passive OSPF listening
  are on the [roadmap](../ROADMAP.md).

## Using EdgeFDL with existing tools

EdgeFDL is designed to sit next to an existing monitoring platform and send it localized alarms. `python -m edgefdl
serve` provides three integration paths; [deployment.md](deployment.md) and [api.md](api.md) give the full details.

| Path | What is sent | Typical receiver |
|---|---|---|
| Syslog (RFC 5424, UDP, facility local0) | One message per alarm raise and clear; severity critical for link down and device down, major for route drift, loss and bandwidth, minor for delay and jitter | OpenNMS (Syslogd), LibreNMS (syslog extension), a SIEM or log collector |
| Webhook (HTTP POST, JSON) | For each raise, the alarm record: id, severity, fault type, link, endpoints with interfaces and addresses, confidence, evidence, repair steps; a shorter record for each clear (`event`: `raise` or `clear`) | An incident or on-call platform, chat-ops, custom automation |
| Prometheus (`GET /metrics`) | `edgefdl_alarms_active`, one `edgefdl_alarm{id,type,link,severity}` series per active alarm, `edgefdl_link_health{link}`, totals, inference latency, probe bandwidth share | Prometheus + Alertmanager, Nightingale, VictoriaMetrics, Grafana, Zabbix (HTTP agent item with a Prometheus check) |

Example: send alarms to a syslog collector and a webhook receiver, and listen on all interfaces so that a Prometheus
server can scrape `/metrics` (addresses are documentation examples; `--no-inject` disables the demonstration
fault-injection endpoints when the page is reachable by others):

```bash
python -m edgefdl serve --model results/model.pt --backend sim --host 0.0.0.0 \
    --sinks '[{"type":"syslog","host":"192.0.2.10"},{"type":"webhook","url":"http://192.0.2.20:8080/edgefdl"}]'
```

```yaml
# prometheus.yml
scrape_configs:
  - job_name: edgefdl
    static_configs:
      - targets: ["192.0.2.30:8460"]          # the EMS running edgefdl serve (default port 8460)

# rules/edgefdl.yml
groups:
  - name: edgefdl
    rules:
      - alert: EdgeFDLLinkFault
        expr: edgefdl_alarm == 1            # carries the labels id, type, link and severity
        annotations:
          summary: "EdgeFDL: {{ $labels.type }} on link {{ $labels.link }}"
```

Alertmanager then routes EdgeFDL alarms by their `severity` label like any other alert, and the alarm already names the
link, so the receiver does not have to correlate per-target alerts. Some text fields of the alarm record (the fault
type label, repair steps and clear reasons) are in Chinese, matching the dashboard; the field names and the
`fault_type` values are English identifiers.

## References

Projects (licenses as stated by each project):

| Project | Category | License | Documentation |
|---|---|---|---|
| Zabbix | General monitoring | AGPL-3.0 (since 7.0) | <https://www.zabbix.com/>, trigger dependencies: <https://www.zabbix.com/documentation/current/en/manual/config/triggers/dependencies> |
| Prometheus Alertmanager | General monitoring, alert routing | Apache-2.0 | <https://prometheus.io/docs/alerting/latest/alertmanager/> |
| Nightingale | General monitoring and alerting | Apache-2.0 | <https://github.com/ccfos/nightingale> |
| LibreNMS | Network management (SNMP; LLDP/CDP, OSPF and BGP discovery) | GPL-3.0-or-later | <https://www.librenms.org/>, <https://docs.librenms.org/Extensions/Auto-Discovery/> |
| OpenNMS ALEC | Alarm correlation into situations | AGPL-3.0 | <https://github.com/OpenNMS-Plugins/alec>, <https://docs.opennms.com/alec/latest/> |
| perfSONAR | Active performance measurement (pScheduler) | Apache-2.0 | <https://www.perfsonar.net/>, <https://docs.perfsonar.net/> |
| SmokePing | Latency and loss measurement | GPL-2.0 | <https://oss.oetiker.ch/smokeping/> |
| Batfish | Network configuration analysis | Apache-2.0 | <https://batfish.org/>, <https://github.com/batfish/batfish> |

Papers:

- <a id="ref-coates"></a>M. Coates, A. Hero, R. Nowak, B. Yu. Internet tomography. *IEEE Signal Processing Magazine*
  19(3):47–65, 2002. <https://doi.org/10.1109/79.998081>
- <a id="ref-duffield"></a>N. Duffield. Network tomography of binary network performance characteristics. *IEEE
  Transactions on Information Theory* 52(12):5373–5388, 2006. <https://doi.org/10.1109/TIT.2006.885460>
- <a id="ref-kompella"></a>R. R. Kompella, J. Yates, A. Greenberg, A. C. Snoeren. Detection and localization of network
  black holes. *IEEE INFOCOM 2007*, 2180–2188. <https://doi.org/10.1109/INFCOM.2007.252>
- <a id="ref-netbouncer"></a>C. Tan, Z. Jin, C. Guo, et al. NetBouncer: active device and link failure localization in
  data center networks. *USENIX NSDI 2019*, 599–614. <https://www.usenix.org/conference/nsdi19/presentation/tan>
- <a id="ref-007"></a>B. Arzani, S. Ciraci, L. Chamon, et al. 007: democratically finding the cause of packet drops.
  *USENIX NSDI 2018*. <https://www.usenix.org/conference/nsdi18/presentation/arzani>
- <a id="ref-batfish"></a>A. Fogel, S. Fung, L. Pedrosa, et al. A general approach to network configuration analysis.
  *USENIX NSDI 2015*. <https://www.usenix.org/conference/nsdi15/technical-sessions/presentation/fogel>
