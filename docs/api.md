# REST API and integrations

`python -m edgefdl serve` exposes everything the dashboard shows through a small HTTP API, and hands every alarm to
other systems through Syslog, a Webhook, Prometheus metrics and CSV logs. This page is the reference for all of
them. The examples are real responses, captured from `serve --backend sim --interval 1` with the released model while
link L6 was down and link L1 had 20 ms of added delay (and, for the device-down examples, while all links of agg2
were down). Long lists are abridged where marked; alarm IDs and the rehearsal text are shown as release 0.1.1 writes
them.

- [Conventions](#conventions)
- [Endpoints](#endpoints)
- [GET /api/status](#get-apistatus)
- [The alarm object](#the-alarm-object)
- [GET /api/alarms](#get-apialarms) · [GET /api/topology](#get-apitopology) · [GET /api/series](#get-apiseries) ·
  [CSV export](#get-apiexportnamecsv) · [POST /api/inject and /api/clear](#post-apiinject-and-post-apiclear)
- [Prometheus metrics](#prometheus-metrics)
- [Syslog](#syslog) · [Webhook](#webhook)
- [CSV logs](#csv-logs)
- [Codes and labels](#codes-and-labels)

## Conventions

- **Base URL.** `http://127.0.0.1:8460` by default (`serve --host`, `--port`). There is no authentication and no
  TLS; see [Deployment, security](deployment.md#6-security) before exposing the port.
- **Format.** Responses are UTF-8 JSON (`application/json; charset=utf-8`) with `Cache-Control: no-store`, except
  `/metrics` (Prometheus text format), the CSV downloads and the dashboard (`/`). JSON never contains `NaN` or
  `Infinity`: a value that was not measured, or is not finite, is `null`. The same holds for Webhook payloads and
  the `evidence` column of `alarm_log.csv`.
- **Errors.** `{"error": "<message>"}` with status 400 (bad request body or parameter), 403 (fault injection
  disabled, or a request refused by the cross-site checks below), 404 (unknown path or file), 413 (request body
  larger than 65,536 bytes), 415 (`POST` without `Content-Type: application/json`) or 500 (unexpected error; the
  measurement loop is not affected).
- **Cross-site checks.** The API has no authentication, so it refuses requests that a web page in an operator's
  browser could forge. `POST` requests must carry `Content-Type: application/json` (parameters such as
  `charset=utf-8` are allowed); otherwise 415 `{"error": "POST 请求须为 Content-Type: application/json"}`. A `POST`
  with an `Origin` header must come from the same origin as the `Host` header (scheme `http` or `https`, equal host
  and port, default ports 80 and 443); otherwise 403 `{"error": "拒绝跨站请求：Origin '<origin>' 与本服务地址不一致"}`,
  also for `Origin: null`. Clients that send no `Origin`, such as `curl` and scripts, are not affected. While `serve`
  listens on a loopback address (the default `--host 127.0.0.1`), every request must name `localhost`,
  `127.0.0.1`, `::1` or the bound address in its `Host` header, with any port (so SSH tunnels to another local port
  work); otherwise 403 `{"error": "拒绝 Host 为 '<host>' 的请求：服务监听回环地址，只接受经 localhost / 127.0.0.1 访问（防 DNS 重绑定；需从其他主机访问时用 --host 指定监听地址）"}`.
  With `--host` set to any other address, including `0.0.0.0`, the `Host` header is not checked; the `Origin` and
  content-type rules still apply. A reverse proxy must pass the upstream `Host` ([Deployment, security](deployment.md#6-security)).
- **Language.** Identifiers and codes are English and stable: `fault_type`, `severity`, `event`, link and target
  IDs, field names. Display strings are Chinese: `fault_type_cn`, `category`, evidence items, remediation steps,
  event messages and detector reasons. Translations of the fixed labels are in [Codes and labels](#codes-and-labels).
- **Time.** In the API, `ts` fields are Unix time in seconds (float). In the CSV logs, `ts` is ISO 8601 local time
  with milliseconds and `epoch` is Unix time.
- **IDs.** Links are `L1`, `L2`, … as in the topology file. Targets are `<link>.<device>` for an interface address
  (`L6.agg2`), `lo.<device>` for a loopback and `nqa.<link>` for an NQA / IP SLA probe on a link that no EMS path
  crosses. Alarm IDs are `A<start time>-<instance>-<sequence>`, for example `A1793319160-3f9c1a-0001`: the Unix time
  in seconds at which the engine started, 6 random hexadecimal digits drawn at start, and a 4-digit sequence number
  counting the alarms of that run. Runs started in the same second get different instance digits, so IDs stay
  unique across restarts and across engines writing to the same log directory.

## Endpoints

| Method | Path | Returns |
|---|---|---|
| GET | `/` | The dashboard (HTML, no external resources) |
| GET | `/api/status` | Engine snapshot: active alarms, link health, adjacency state, inference latency, probe overhead, resources, events |
| GET | `/api/alarms?n=50` | The last `n` alarms, newest first, active and cleared (the engine keeps 200) |
| GET | `/api/topology` | The topology of the model bundle, layout coordinates and the fault-type dictionary |
| GET | `/api/series?link=L1` | RTT and loss of the targets on one link over the last 120 rounds |
| GET | `/api/export/measure_log.csv`, `/api/export/alarm_log.csv` | The current CSV log as a download |
| GET | `/metrics` | Prometheus metrics |
| POST | `/api/inject` | Inject a fault into the virtual network (demo and testing) |
| POST | `/api/clear` | Clear injected faults |

## GET /api/status

```bash
curl -s http://127.0.0.1:8460/api/status
```

```jsonc
{
  "round": 103,
  "uptime_s": 103,
  "backend": "sim",
  "model": "ddqn-20261030-0116",
  "interval_s": 1.0,
  "detector": {
    "active": true,
    "reason": [
      "数据面持续异常: L1.agg1,L3.agg1,L4.agg1,L4.acc1,L5.agg1",
      "控制面变化: L6",
      "带宽持续下降: L6"
    ]
  },
  "active_alarm": { /* alarm A1793319160-3f9c1a-0001, see "The alarm object" */ },
  "active_alarms": [ { /* A1793319160-3f9c1a-0001 */ }, { /* A1793319160-3f9c1a-0002 */ } ],
  "multi_alarm": true,
  "link_health": { "L1": 1.0, "L2": 0.0, "L6": 1.0 /* … one entry per link */ },
  "link_state": {
    "L1": { "adj_up": true, "cost": 10.0 },
    "L2": { "adj_up": true, "cost": 10.0 },
    "L6": { "adj_up": false, "cost": 10.0 }
    /* … one entry per link */
  },
  "unreachable": ["L6.agg2", "L6.acc3"],
  "ground_truth": [
    { "type": "link_down", "link": "L6", "value": 1.0, "label": "L6 链路中断" },
    { "type": "delay", "link": "L1", "value": 20.0, "label": "L1 时延劣化(RTT异常) 20ms" }
  ],
  "infer": { "n": 9, "mean_ms": 0.926, "p99_ms": 1.608, "max_ms": 1.662 },
  "overhead": { "kbps": 1109.9, "pct_of_min_link": 1.11, "pct_of_uplink": 0.111 },
  "resource": { "cpu_pct": 0.011458333333333334, "rss_mb": 529.0703125, "sys_mem_total_mb": 128537.31640625 },
  "round_ms": 16.7,
  "errors": 0,
  "suppressed": 0,
  "alarms_total": 2,
  "events": [
    { "ts": 1793319253.721767, "round": 94, "kind": "alarm",
      "msg": "告警 A1793319160-3f9c1a-0002：L1 时延劣化(RTT异常)（置信度 0.97），扣除已有告警后发现" },
    { "ts": 1793319251.7178967, "round": 92, "kind": "pending",
      "msg": "扣除已有告警后仍疑似 L1 时延劣化(RTT异常)，等待复核" }
    /* … up to 30 events, newest first */
  ]
}
```

| Field | Meaning |
|---|---|
| `round`, `uptime_s` | Measurement rounds completed, seconds since start |
| `backend`, `model`, `interval_s` | Backend (`sim`, `netns`, `lxd`), model version from the bundle, measurement interval in seconds |
| `detector.active` | The k-of-n detector currently sees a persistent anomaly |
| `detector.reason` | Why: 数据面持续异常 (persistent data-plane anomaly, first 5 targets listed), 控制面变化 (control-plane change: adjacency or cost), 带宽持续下降 (bandwidth persistently low), 残差持续异常 (persistent anomaly on the residual, while alarms are active) |
| `active_alarm` | The most severe active alarm (the newest among equals), or `null`; kept for clients that handle one alarm |
| `active_alarms` | All active alarms, most severe and newest first ([alarm object](#the-alarm-object)) |
| `multi_alarm` | `false` when the engine runs with `--single-alarm` |
| `link_health` | Per link, 0 (normal) to 1 (anomalous), from the light features of the last rounds |
| `link_state` | Per link, OSPF adjacency up and cost from the last LSDB reading |
| `unreachable` | Targets that answered no probe in the last round |
| `ground_truth` | Faults currently injected through `/api/inject` (empty when none) |
| `infer` | Time of one forward pass of the agent since start: count, mean, p99, maximum in ms (`{}` before the first diagnosis). A diagnosis runs 1 to 4 forward passes, each counted |
| `overhead` | Probe traffic over the last up to 120 rounds: kbit/s, and as a percentage of the narrowest link and of the EMS uplink. The example ran at `--interval 1`, five times the default probe rate |
| `resource` | Process CPU (percent of all CPUs), resident memory and total system memory in MB; `{}` without psutil |
| `round_ms` | Mean wall time of a measurement round, ms |
| `errors` | Exceptions caught and counted: in the measurement loop, failed writes to `alarm_log.csv`, alarm sinks that raised an error, and failures to build the remediation advice |
| `suppressed` | Anomalies the agent judged normal (shown as 已过滤, filtered) |
| `alarms_total` | Alarms raised since start (equal to the sequence number of the newest alarm) |
| `events` | Last 30 events, newest first: `kind` is `pending` (suspected, awaiting confirmation), `alarm`, `clear` or `filtered` (dismissed as a transient or as a background-loss change); `filtered` events from the detector also carry `reason` |

## The alarm object

`active_alarm`, the entries of `active_alarms` and the entries of `/api/alarms` share this structure. A link alarm:

```json
{
  "alarm_id": "A1793319160-3f9c1a-0001",
  "event": "raise",
  "severity": "critical",
  "category": "链路中断",
  "fault_type": "link_down",
  "fault_type_cn": "链路中断",
  "link_id": "L6",
  "endpoints": "agg2:eth3(10.10.0.21) <-> acc3:eth1(10.10.0.22)",
  "site": "三号楼/西区汇聚",
  "confidence": 0.98,
  "evidence": {
    "items": [
      "链路两端接口地址 10.10.0.21 / 10.10.0.22 均不可达",
      "OSPF 邻接 agg2↔acc3 断开",
      "基线经过该链路的监测路径中 100% 已改道"
    ],
    "trigger": ["数据面持续异常: L1.agg1,L3.agg1,L4.agg1,L4.acc1,L5.agg1", "控制面变化: L6"],
    "candidates": [
      { "type": "link_down", "link": "L6", "q": 0.954 },
      { "type": "route_drift", "link": "L6", "q": 0.425 },
      { "type": "bandwidth", "link": "L6", "q": 0.366 }
    ],
    "probes": []
  },
  "repair": "查看两端端口物理/协议状态，确认是否 down 或被误 shutdown | 检查光模块收发光功率 / 网线，必要时更换跳线或光模块 | 如为误关闭，恢复端口 | 确认 OSPF 邻接恢复",
  "probes_used": 0,
  "infer_ms": 0.986,
  "round_id": 86,
  "model": "ddqn-20261030-0116",
  "advice": {
    "summary": "L6（agg2:eth3 ↔ acc3:eth1）链路中断",
    "type_cn": "链路中断",
    "steps": [
      { "step": "查看两端端口物理/协议状态，确认是否 down 或被误 shutdown",
        "cmds": ["[agg2] display interface eth3", "[acc3] display interface eth1"] },
      { "step": "检查光模块收发光功率 / 网线，必要时更换跳线或光模块",
        "cmds": ["[agg2] display transceiver interface eth3", "[acc3] display transceiver interface eth1"] },
      { "step": "如为误关闭，恢复端口",
        "cmds": ["[agg2] system-view ; interface eth3 ; undo shutdown", "[acc3] system-view ; interface eth1 ; undo shutdown"] },
      { "step": "确认 OSPF 邻接恢复", "cmds": ["[agg2] display ospf peer"] }
    ],
    "rehearsal": {
      "action": "依赖 OSPF 自动切换至备份路径",
      "feasible": true,
      "detail": "7 个监测目标已切换到备份路径；备份路径经过同样告警中的 L1（时延劣化(RTT异常)）；业务无单点中断（已计入其他活动告警：L1 时延劣化(RTT异常)）"
    }
  },
  "ts": 1793319245.7239912,
  "first_round": 86,
  "last_round": 98,
  "ground_truth": ["L6 链路中断", "L1 时延劣化(RTT异常) 20ms"],
  "secondary": false,
  "evident": true
}
```

A device-down alarm replaces the link-down alarms of all links of a device whose adjacencies to every neighbouring
router are down and whose loopback is unreachable. It adds `node` and `links`; `link_id` joins the links with `/`
and `endpoints` names the device and its model:

```jsonc
{
  "alarm_id": "A1793319334-a41f07-0001",
  "event": "raise",
  "severity": "critical",
  "category": "链路中断",
  "fault_type": "node_down",
  "fault_type_cn": "设备宕机",
  "link_id": "L2/L3/L6/L7",
  "node": "agg2",
  "links": ["L2", "L3", "L6", "L7"],
  "endpoints": "agg2（h3c MSR3620）",
  "site": "西区汇聚",
  "confidence": 0.873,
  "evidence": {
    "items": [
      "设备 agg2 与相邻路由设备的全部 4 条 OSPF 邻接（L2、L3、L6、L7）均已断开",
      "设备环回地址 10.255.0.3 不可达",
      "链路两端接口地址 10.10.0.9 / 10.10.0.10 均不可达",
      "OSPF 邻接 agg1↔agg2 断开"
    ],
    "trigger": ["数据面持续异常: L2.core,L2.agg2,L3.agg1,L3.agg2,L6.agg2", "控制面变化: L2,L3,L6,L7", "带宽持续下降: L7"],
    "candidates": [
      { "type": "link_down", "link": "L3", "q": 0.751 },
      { "type": "link_down", "link": "L2", "q": 0.719 },
      { "type": "link_down", "link": "L7", "q": 0.694 }
    ],
    "probes": []
  },
  "repair": "确认设备 agg2 供电、主控板与风扇状态，经 Console 登录查看是否重启或死机 | 在相邻设备上核对对端端口状态与 OSPF 邻居 | 设备恢复后确认全部邻接重建、路径回到基线",
  "probes_used": 0,
  "infer_ms": 0.562,
  "round_id": 39,
  "model": "ddqn-20261030-0116",
  "advice": { /* summary, steps with commands for agg2 and its neighbours, rehearsal */ },
  "ts": 1793319372.1204865,
  "first_round": 39,
  "last_round": 43,
  "ground_truth": ["L2 链路中断", "L3 链路中断", "L6 链路中断", "L7 链路中断"],
  "secondary": false,
  "evident": true
}
```

| Field | Meaning |
|---|---|
| `alarm_id` | Unique alarm ID |
| `event` | `raise` for an active alarm; history entries keep `raise` and add the `cleared_*` fields when cleared |
| `severity` | `critical`, `major` or `minor` (see [Codes and labels](#codes-and-labels)) |
| `category`, `fault_type`, `fault_type_cn` | Fault category (Chinese), fault type code, fault type label (Chinese) |
| `link_id` | The faulty link; for `node_down` all links of the device joined with `/` |
| `node`, `links` | `node_down` only: the device and its links |
| `endpoints` | `device:interface(address) <-> device:interface(address)`; for `node_down` the device with vendor and model |
| `site` | Sites of the two link ends (or of the device), joined with `/` |
| `confidence` | 0 to 1, derived from the agent's Q-value; refreshed by later diagnoses that reach the same conclusion |
| `evidence.items` | Human-readable evidence for the conclusion (Chinese) |
| `evidence.trigger` | The detector reasons when the diagnosis ran |
| `evidence.candidates` | The top 3 conclusions with their scores `q` (`type`, `link`) |
| `evidence.probes` | Deep probes made for this diagnosis: `link`, `loss` (0–1), `rtt` and `mdev` (ms), `bw` (Mbit/s); a value that was not measured is `null` (for example the RTT of a deep probe without replies) |
| `repair` | The remediation steps joined with ` \| ` |
| `probes_used`, `infer_ms` | Number of deep probes; time of the slowest single forward pass of the diagnosis (ms) |
| `round_id`, `first_round`, `last_round` | Round of the alarm, first round, last round the alarm was confirmed active |
| `model` | Model version |
| `advice.summary`, `advice.steps[].step`, `advice.steps[].cmds` | Summary and steps; commands are prefixed with the device in brackets and use the device's vendor syntax (Huawei VRP, H3C Comware or Linux / FRR) |
| `advice.rehearsal` | The remediation rehearsed on the equivalent topology: `action`, `feasible`, `detail` (for example how many targets move to a backup path). Only feasible actions are recommended. The rehearsal runs on the current state of the network: links of the other active alarms count as down (a device-down alarm takes all its links), a drifted link keeps its current LSDB cost, and a detour over another alarmed, degraded link is reported; `detail` then ends with `（已计入其他活动告警：…）` ("other active alarms taken into account: …"). The advice and `repair` of the active alarms are recomputed when the set of alarms changes; this shows in the API and the dashboard, while `alarm_log.csv` and the sinks keep the text of the raise. If the advice cannot be built, the alarm is still raised with `advice` = `{"summary": "处置建议生成失败", "steps": [], "rehearsal": null}` and `errors` grows by 1 |
| `ts` | Time of the alarm (Unix seconds) |
| `ground_truth` | Labels of the faults injected when the alarm was raised (`sim` and `netns` backends), otherwise `null` |
| `secondary` | `true` if the alarm was found on the residual after subtracting the alarms already active |
| `evident` | `false` if the conclusion lacked direct evidence on its own link when raised. Such an alarm is not tracked round by round; it clears on network-wide recovery or, for link down, bandwidth, delay, loss and jitter, when a recheck deep probe, issued after 9 rounds (3 × `clear_n`) without evidence, finds no fault (`精测复核未见该故障，告警恢复`); it can also be cleared by a background-loss relearn or replaced |
| `cleared_ts`, `cleared_round`, `cleared_why` | History entries only, once cleared: time, round and reason (Chinese, see [Codes and labels](#codes-and-labels)) |

## GET /api/alarms

```bash
curl -s 'http://127.0.0.1:8460/api/alarms?n=5'
```

Returns a JSON array of [alarm objects](#the-alarm-object), newest first, active and cleared alike; `n` defaults to
50 and the engine keeps the last 200 alarms in memory. A value of `n` that is not a non-negative integer returns 400
`{"error": "参数 n 须为非负整数"}`. The full record of every alarm since the log directory was
created is in `alarm_log.csv`.

## GET /api/topology

```bash
curl -s http://127.0.0.1:8460/api/topology
```

```jsonc
{
  "name": "demo-edge",
  "description": "主干接入 + 双汇聚 + 环形接入的典型边缘网络（15 条链路）",
  "vantage": "ems",
  "nodes": [
    { "name": "core", "role": "router", "vendor": "huawei", "model": "AR6280", "loopback": "10.255.0.1/32", "agent": true, "site": "中心机房" },
    { "name": "ems", "role": "ems", "vendor": "generic", "model": "", "loopback": "", "agent": true, "site": "中心机房" }
    /* … all 12 devices */
  ],
  "links": [
    { "id": "L1", "a": "core", "b": "agg1", "a_if": "eth1", "b_if": "eth1", "a_ip": "10.10.0.1/30", "b_ip": "10.10.0.2/30",
      "bw_mbps": 1000, "delay_ms": 1.0, "jitter_ms": 0.05, "loss": 0.0, "cost": 10 }
    /* … all 15 links */
  ],
  "layout": { "ems": [0.5, 0.08], "core": [0.5, 0.29], "agg1": [0.333, 0.5] /* … */ },
  "fault_types": [
    { "id": "link_down", "label": "链路中断" }, { "id": "delay", "label": "时延劣化(RTT异常)" },
    { "id": "loss", "label": "丢包劣化" }, { "id": "jitter", "label": "抖动劣化" },
    { "id": "bandwidth", "label": "带宽劣化" }, { "id": "route_drift", "label": "路由漂移" }
  ]
}
```

`nodes` and `links` are the complete topology from the model bundle, including the interfaces and addresses planned
automatically ([architecture, topology file](architecture.md#topology-file)). `layout` gives each device a position
in the unit square (by hop count from the EMS). `fault_types` lists the types accepted by `/api/inject`.

## GET /api/series

```bash
curl -s 'http://127.0.0.1:8460/api/series?link=L1'
```

```jsonc
{
  "L1.core": [ { "round": 22, "rtt": 0.38, "loss": 0.0 }, { "round": 23, "rtt": 0.36, "loss": 0.0 } /* … */ ],
  "L1.agg1": [ /* … */ ]
}
```

The targets whose address belongs to the link (its two interface addresses, plus `nqa.<link>` if the link has an NQA
probe), each with up to the last 120 rounds: `rtt` is the average RTT in ms (`null` when no reply), `loss` is
0 to 1. An unknown or missing `link` returns `{}`.

## GET /api/export/{name}.csv

```bash
curl -s -O http://127.0.0.1:8460/api/export/alarm_log.csv
curl -s -O http://127.0.0.1:8460/api/export/measure_log.csv
```

Downloads the current file from the log directory (`text/csv`, with `Content-Disposition: attachment`). Only these
two names are served; anything else returns 404. Rotated archives are read from the log directory directly
([CSV logs](#csv-logs)).

## POST /api/inject and POST /api/clear

For demonstrations and tests on the virtual-network backends (`sim`, `netns`, `lxd`). `serve --no-inject` disables
both endpoints: every POST then returns 403 `{"error": "当前后端不允许注入故障"}` ("this backend does not allow
fault injection"). The body is a JSON object sent with `Content-Type: application/json` (see
[Conventions](#conventions)); a plain `curl -d` without that header gets 415.

```bash
curl -s -X POST -H 'Content-Type: application/json' \
     -d '{"type": "link_down", "link": "L6"}' http://127.0.0.1:8460/api/inject
# {"ok": true, "fault": {"type": "link_down", "link": "L6", "value": 1.0}, "label": "L6 链路中断"}

curl -s -X POST -H 'Content-Type: application/json' \
     -d '{"type": "delay", "link": "L1", "value": 20}' http://127.0.0.1:8460/api/inject
# {"ok": true, "fault": {"type": "delay", "link": "L1", "value": 20.0}, "label": "L1 时延劣化(RTT异常) 20ms"}

curl -s -X POST -H 'Content-Type: application/json' \
     -d '{"link": "L6"}' http://127.0.0.1:8460/api/clear      # clear the fault on L6 only
curl -s -X POST -H 'Content-Type: application/json' \
     -d '{}' http://127.0.0.1:8460/api/clear                  # clear all injected faults
# {"ok": true}
```

| Body field | Meaning |
|---|---|
| `type` | `link_down`, `delay`, `loss`, `jitter`, `bandwidth` or `route_drift` |
| `link` | Link ID |
| `value` | `delay`: added one-way delay in ms (applied at both ends, so the RTT rises by twice the value); `loss`: loss rate; `jitter`: ms; `bandwidth`: remaining fraction of the link bandwidth; `route_drift`: multiplier of the OSPF cost; ignored for `link_down` (stored as 1.0). A number, or a string holding a number such as `"20"`. Default 1.0 |

Accepted values:

| `type` | Accepted `value` |
|---|---|
| `delay`, `jitter` | greater than 0 and at most 10,000 (ms) |
| `loss` | greater than 0 and at most 1 |
| `bandwidth` | greater than 0 and at most 1 |
| `route_drift` | greater than 0, and the new cost `max(1, round(cost × value))` at most 65,535 (for a link of cost 10, a multiplier of at most 6553) |

One fault per link: injecting on a link that already has a fault replaces it; faults on different links coexist.
The model was trained on delay 5–100 ms, loss 0.05–0.9, jitter 5–50 ms, remaining bandwidth 0.05–0.5 and cost
multipliers 0.1, 0.3, 3, 10 and 30.

`/api/clear` takes a JSON object; an absent, `null` or empty `link` clears every injected fault (an empty body is
read as `{}`). Errors of both endpoints, all with status 400 unless noted:

| Case | `error` |
|---|---|
| Body larger than 65,536 bytes | `请求体过大（上限 65536 字节）` (413) |
| Invalid `Content-Length` | `Content-Length 不合法` |
| Body is not JSON | `请求体不是合法的 JSON：<detail>` |
| `NaN`, `Infinity` or `-Infinity` in the body | `请求体含 NaN，不是合法的 JSON 数值` (or `Infinity` / `-Infinity`) |
| Body is not a JSON object | `请求体须为 JSON 对象` |
| `type` or `link` missing (`/api/inject`) | `缺少字段 type`, `缺少字段 link` |
| Unknown fault type | `未知故障类型 foo` |
| Unknown link | `未知链路 L99` |
| `value` is `true`, `false`, `null` or not a number | `value 须为数值，收到 <value>` |
| `value` is not finite, for example `"nan"` or `1e400` | `value 须为有限数值，收到 <value>` |
| `value` out of range | for example `delay 的 value 须大于 0 且不超过 10000ms，收到 -40`, `loss 的 value 须大于 0 且不超过 1（丢包率），收到 5`, `route_drift 的 value（开销倍率）须大于 0，且使 L1 的开销（基线 10）不超过 65535，即倍率不超过 6553，收到 1000000.0` |

The checks run in this order: request size, `Host`, `--no-inject` (403), `Origin` (403), `Content-Type` (415), JSON
syntax, then the fields.

## Prometheus metrics

```bash
curl -s http://127.0.0.1:8460/metrics
```

```text
# HELP edgefdl_link_health 链路健康度 0 正常 1 异常
# TYPE edgefdl_link_health gauge
edgefdl_link_health{link="L1"} 1.0
edgefdl_link_health{link="L2"} 0.0
edgefdl_link_health{link="L6"} 1.0
# (L3 … L15 omitted here)
# TYPE edgefdl_alarm_active gauge
edgefdl_alarm_active{type="link_down",link="L6"} 1
# TYPE edgefdl_alarms_active gauge
edgefdl_alarms_active 2
# TYPE edgefdl_alarms_total counter
edgefdl_alarms_total 2
# TYPE edgefdl_filtered_total counter
edgefdl_filtered_total 0
# TYPE edgefdl_round counter
edgefdl_round 103
# TYPE edgefdl_alarm gauge
edgefdl_alarm{id="A1793319160-3f9c1a-0001",type="link_down",link="L6",severity="critical"} 1
edgefdl_alarm{id="A1793319160-3f9c1a-0002",type="delay",link="L1",severity="minor"} 1
# TYPE edgefdl_infer_ms gauge
edgefdl_infer_ms{q="p99"} 1.608
# TYPE edgefdl_probe_bw_pct gauge
edgefdl_probe_bw_pct 1.11
```

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `edgefdl_link_health` | gauge | `link` | Link health, 0 normal to 1 anomalous (as `link_health` in `/api/status`) |
| `edgefdl_alarm_active` | gauge | `type`, `link` | 1 with the type and link of the most severe active alarm; 0 with empty labels when there is none |
| `edgefdl_alarms_active` | gauge | | Number of active alarms |
| `edgefdl_alarm` | gauge | `id`, `type`, `link`, `severity` | One series with value 1 per active alarm; absent when no alarm is active. For `node_down`, `link` is the `/`-joined link list |
| `edgefdl_alarms_total` | counter | | Alarms raised since start |
| `edgefdl_filtered_total` | counter | | Anomalies the agent judged normal |
| `edgefdl_round` | counter | | Measurement rounds completed |
| `edgefdl_infer_ms` | gauge | `q="p99"` | p99 time of one forward pass of the agent in ms, as `infer.p99_ms` in `/api/status` (after the first diagnosis) |
| `edgefdl_probe_bw_pct` | gauge | | Probe traffic as a percentage of the narrowest link (after the first round) |

The output parses with the Prometheus text-format parser of `prometheus_client`. A scrape configuration is in
[Deployment, Prometheus](deployment.md#prometheus).

## Syslog

Each alarm `raise` and `clear` is sent as one RFC 5424 message over UDP (default port 514), facility local0 (the
host name in these captured messages is replaced by `ems-host`):

```text
<130>1 2026-10-30T08:14:05.721857+08:00 ems-host edgefdl - raise - 链路中断 L6 agg2:eth3(10.10.0.21) <-> acc3:eth1(10.10.0.22) conf=0.977
<132>1 2026-10-30T08:14:13.720621+08:00 ems-host edgefdl - raise - 时延劣化(RTT异常) L1 core:eth1(10.10.0.1) <-> agg1:eth1(10.10.0.2) conf=0.974
<134>1 2026-10-30T08:14:51.739434+08:00 ems-host edgefdl - clear - 链路中断 L6 agg2:eth3(10.10.0.21) <-> acc3:eth1(10.10.0.22) conf=0.979
<130>1 2026-10-30T08:16:12.118147+08:00 ems-host edgefdl - raise - 设备宕机 L2/L3/L6/L7 agg2（h3c MSR3620） conf=0.876
```

| RFC 5424 field | Value |
|---|---|
| PRI | facility local0 (16) × 8 + severity: `critical` → 130, `major` → 131, `minor` → 132, `clear` → 134 |
| VERSION, TIMESTAMP, HOSTNAME | `1`, local time with UTC offset, host name of the EMS |
| APP-NAME, PROCID | `edgefdl`, `-` |
| MSGID | `raise` or `clear` |
| STRUCTURED-DATA | `-` |
| MSG | `<fault_type_cn> <link_id> <endpoints> conf=<confidence>` (UTF-8, Chinese fault label) |

Configure the sink with `--sinks '[{"type": "syslog", "host": "10.0.0.5", "port": 514}]'` (`port` is optional, 1 to
65535). The sink configuration is checked when `serve` starts ([Deployment, alarm sinks](deployment.md#alarm-sinks)).

## Webhook

Each alarm `raise` and `clear` is POSTed as one JSON object (`Content-Type: application/json`, UTF-8) to the
configured URL. Delivery runs in a background thread with a 3 s timeout and does not block the measurement loop;
a failed delivery is printed to standard error as `Webhook 投递失败（<url>）：<error>` and not retried, so use
`alarm_log.csv` as the authoritative record. A `raise` payload carries the
fields of `alarm_log.csv` (with `evidence` as a JSON object and without `ts`, `epoch` and `schema_version`), plus
`node` and `links` for device-down alarms:

```json
{"alarm_id": "A1793319334-a41f07-0001", "event": "raise", "severity": "critical", "category": "链路中断", "fault_type": "node_down", "fault_type_cn": "设备宕机", "link_id": "L2/L3/L6/L7", "node": "agg2", "links": ["L2", "L3", "L6", "L7"], "endpoints": "agg2（h3c MSR3620）", "site": "西区汇聚", "confidence": 0.876, "evidence": {"items": ["设备 agg2 与相邻路由设备的全部 4 条 OSPF 邻接（L2、L3、L6、L7）均已断开", "设备环回地址 10.255.0.3 不可达", "链路两端接口地址 10.10.0.9 / 10.10.0.10 均不可达", "OSPF 邻接 agg1↔agg2 断开"], "trigger": ["数据面持续异常: L2.core,L2.agg2,L3.agg1,L3.agg2,L6.agg2", "控制面变化: L2,L3,L6,L7", "带宽持续下降: L7"], "candidates": [{"type": "link_down", "link": "L3", "q": 0.751}, {"type": "link_down", "link": "L2", "q": 0.719}, {"type": "link_down", "link": "L7", "q": 0.694}], "probes": []}, "repair": "确认设备 agg2 供电、主控板与风扇状态，经 Console 登录查看是否重启或死机 | 在相邻设备上核对对端端口状态与 OSPF 邻居 | 设备恢复后确认全部邻接重建、路径回到基线", "probes_used": 0, "infer_ms": 0.562, "round_id": 39, "model": "ddqn-20261030-0116"}
```

A `clear` payload identifies the alarm and puts the reason for clearing in `repair`:

```json
{"alarm_id": "A1793319334-a41f07-0001", "severity": "critical", "category": "链路中断", "fault_type": "node_down", "fault_type_cn": "设备宕机", "link_id": "L2/L3/L6/L7", "endpoints": "agg2（h3c MSR3620）", "site": "西区汇聚", "confidence": 0.875, "model": "ddqn-20261030-0116", "event": "clear", "repair": "告警链路自身证据恢复至基线", "round_id": 58}
```

The payloads carry no timestamp; use the time of receipt, or join on `alarm_id` with `alarm_log.csv`. Configure the
sink with `--sinks '[{"type": "webhook", "url": "https://hooks.example.org/edgefdl"}]'`; the URL must start with
`http://` or `https://` and name a host, or `serve` does not start. The request goes through Python's `urllib`,
which honours the `http_proxy`, `https_proxy` and `no_proxy` environment variables.

## CSV logs

`serve` writes two logs into `--log-dir` (default `runs/live`): `measure_log.csv` and `alarm_log.csv`. Every row
starts with `schema_version` (currently `1.0`), and each log has a column definition next to it
(`measure_log.schema.json`, `alarm_log.schema.json`):

```json
{"schema_version": "1.0", "columns": [{"name": "schema_version", "desc": "日志格式版本"}, {"name": "alarm_id", "desc": "告警编号"}, …]}
```

Rows are flushed line by line. The measurement log is fsynced every 5 s and the alarm log on every write. A file is
rotated when it exceeds 200 MB (MiB) and renamed to `<name>.<YYYYmmddHHMMSSffffff>`; the 20 most recent archives are
kept, so each log occupies at most about 4.1 GiB. An existing log is appended to when `serve` restarts, and the
`*.schema.json` files are rewritten at every start.

Text cells that a spreadsheet program would read as a formula (starting with `=`, `+`, `-`, `@`, a tab or a carriage
return) are written with a leading single quote (`'`), so that the exported files open as text; numbers and text
that is a plain number, such as `-1.5`, are written unchanged.

### measure_log.csv

One row per target probe, bandwidth estimate, LSDB entry and deep probe:

```text
schema_version,ts,epoch,round_id,sample_id,vantage,kind,target_id,target_ip,target_node,link_id,sent,recv,loss,rtt_min_ms,rtt_avg_ms,rtt_max_ms,rtt_mdev_ms,bw_mbps,path,adj_up,cost,fault_label,probe_bytes
1.0,2026-10-30T08:12:40.763,1793319160.764,0,,ems,ping,L1.agg1,10.10.0.2,agg1,L1,5,5,0.0,2.423,2.547,2.683,0.091,,core>agg1,,,,3749
1.0,2026-10-30T08:12:40.763,1793319160.764,0,,ems,bw,,,,L13,,,,,,,,1050.0,,,,,
1.0,2026-10-30T08:12:40.763,1793319160.764,0,,ems,lsdb,,,,L1,,,,,,,,,,1,10.0,,
1.0,2026-10-30T08:28:38.294,1793320118.295,37,,ems,deep,,,,L7,100,83,0.17,,5.137,,0.043,100.0,,,,,3769600
```

| Column | `kind` | Meaning |
|---|---|---|
| `schema_version` | all | Log format version |
| `ts`, `epoch` | all | ISO 8601 local time with milliseconds; Unix time in seconds |
| `round_id` | all | Measurement round |
| `sample_id`, `fault_label` | all | Filled only when samples are generated (`gen --raw-csv`): sample number and injected fault (`normal` for normal samples) |
| `vantage` | all | Measurement source (the EMS) |
| `kind` | all | `ping` (light probe of a target), `bw` (packet-train bandwidth estimate), `lsdb` (control plane), `deep` (deep probe of one link) |
| `target_id`, `target_ip`, `target_node` | ping | Target ID, address and the device that owns it |
| `link_id` | all | The link the target address belongs to (empty for loopbacks); the link measured for `bw`, `lsdb`, `deep` |
| `sent`, `recv`, `loss` | ping, deep | Packets sent and received, loss rate 0–1 (5 packets per target per round; 100 per deep probe) |
| `rtt_min_ms`, `rtt_avg_ms`, `rtt_max_ms`, `rtt_mdev_ms` | ping (avg and mdev also deep) | RTT statistics in ms; `rtt_mdev_ms` is the standard deviation (jitter) |
| `bw_mbps` | bw, deep | Bandwidth in Mbit/s |
| `path` | ping | Hop-by-hop path from traceroute as device names joined with `>`, without the source |
| `adj_up`, `cost` | lsdb | OSPF adjacency up (1) or down (0); OSPF interface cost |
| `probe_bytes` | ping, deep | Bytes injected: for `ping` rows the round's probe traffic divided evenly over its ping rows; for `deep` the bytes of that probe |

Empty fields are values that do not apply to the row's `kind` or were not measured.

### alarm_log.csv

One row per alarm `raise` and per `clear`:

```text
schema_version,alarm_id,ts,epoch,event,severity,category,fault_type,fault_type_cn,link_id,endpoints,site,confidence,evidence,repair,probes_used,infer_ms,round_id,model
1.0,A1793319160-3f9c1a-0001,2026-10-30T08:14:05.707,1793319245.707,raise,critical,链路中断,link_down,链路中断,L6,agg2:eth3(10.10.0.21) <-> acc3:eth1(10.10.0.22),三号楼/西区汇聚,0.977,"{""items"": [""链路两端接口地址 10.10.0.21 / 10.10.0.22 均不可达"", ""OSPF 邻接 agg2↔acc3 断开"", ""基线经过该链路的监测路径中 100% 已改道""], ""trigger"": [""数据面持续异常: L1.agg1,L3.agg1,L4.agg1,L4.acc1,L5.agg1"", ""控制面变化: L6""], ""candidates"": [{""type"": ""link_down"", ""link"": ""L6"", ""q"": 0.954}, {""type"": ""route_drift"", ""link"": ""L6"", ""q"": 0.425}, {""type"": ""bandwidth"", ""link"": ""L6"", ""q"": 0.366}], ""probes"": []}",查看两端端口物理/协议状态，确认是否 down 或被误 shutdown | 检查光模块收发光功率 / 网线，必要时更换跳线或光模块 | 如为误关闭，恢复端口 | 确认 OSPF 邻接恢复,0,0.986,86,ddqn-20261030-0116
1.0,A1793319160-3f9c1a-0001,2026-10-30T08:14:51.713,1793319291.714,clear,critical,链路中断,link_down,链路中断,L6,agg2:eth3(10.10.0.21) <-> acc3:eth1(10.10.0.22),三号楼/西区汇聚,0.979,,告警链路自身证据恢复至基线,,,132,ddqn-20261030-0116
```

| Column | Meaning |
|---|---|
| `schema_version`, `ts`, `epoch` | As in the measurement log |
| `alarm_id` | Alarm ID; a `clear` row repeats the ID of the alarm it clears |
| `event` | `raise` or `clear` (the schema file also reserves `update`, which release 0.1.1 does not write) |
| `severity`, `category`, `fault_type`, `fault_type_cn` | As in the [alarm object](#the-alarm-object) |
| `link_id`, `endpoints`, `site` | As in the alarm object (`/`-joined links and the device for `node_down`) |
| `confidence` | Confidence 0–1 at the time of the row |
| `evidence` | JSON text with `items`, `trigger`, `candidates`, `probes` (empty in `clear` rows); values that were not measured are `null` |
| `repair` | `raise`: remediation steps joined with ` \| `; `clear`: the reason for clearing |
| `probes_used`, `infer_ms` | Deep probes and the time of the slowest forward pass of the diagnosis (empty in `clear` rows) |
| `round_id` | Round of the raise or the clear |
| `model` | Model version |

## Codes and labels

| `fault_type` | `fault_type_cn` (page label) | `category` | `severity` |
|---|---|---|---|
| `link_down` | 链路中断 (link down) | 链路中断 (link down) | `critical` |
| `node_down` | 设备宕机 (device down) | 链路中断 (link down) | `critical` |
| `route_drift` | 路由漂移 (route drift) | 路由漂移 (route drift) | `major` |
| `loss` | 丢包劣化 (loss) | 性能劣化 (performance degradation) | `major` |
| `bandwidth` | 带宽劣化 (bandwidth degradation) | 性能劣化 | `major` |
| `delay` | 时延劣化(RTT异常) (delay, RTT anomaly) | 性能劣化 | `minor` |
| `jitter` | 抖动劣化 (jitter) | 性能劣化 | `minor` |

The dashboard shows `critical` as 紧急, `major` as 重要 and `minor` as 一般. Reasons for clearing an alarm
(`cleared_why` in the API, `repair` in `clear` rows and Webhook payloads):

| Reason | Meaning |
|---|---|
| 告警链路自身证据恢复至基线 | The alarmed link's own evidence is back to the baseline |
| 测量指标恢复至基线 | The whole network's measurements are back to the baseline |
| 精测复核未见该故障，告警恢复 | A recheck deep probe no longer shows the fault |
| 升级为设备宕机告警 | Replaced by a device-down alarm |
| 设备已恢复运行，降为链路中断告警 | The device is back; replaced by a link-down alarm for a link that is still down |
| 设备已恢复运行，剩余链路中断另行研判 | The device is back; remaining link outages are diagnosed separately |
| 研判结论变化 | The diagnosis changed (single-alarm mode replaces the alarm) |
| …，判定为背景丢包变化并已重学丢包基线 | Loss conclusions rotated over several links, or router targets broadly show excess loss: judged a change of background loss, and the loss baseline was relearned |
