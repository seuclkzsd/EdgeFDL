# Quick start

From a fresh clone to a located fault in about ten minutes, on any Linux machine with Python 3.8 or newer. No
network hardware, GPU or root access is needed: the bundled model ([`results/model.pt`](../results/model.pt))
runs against a simulated copy of the 12-device demo network.

1. [Install](#1-install)
2. [Start the dashboard](#2-start-the-dashboard)
3. [Inject a fault and read the alarm](#3-inject-a-fault-and-read-the-alarm)
4. [Train a model with the pipeline](#4-train-a-model-with-the-pipeline)
5. [Evaluation, self-test and robustness suite](#5-evaluation-self-test-and-robustness-suite)
6. [Real protocol stack in Docker](#6-real-protocol-stack-in-docker)
7. [Troubleshooting](#7-troubleshooting)

The commands below run from the repository root. The terminal output, CLI help and dashboard are in Chinese; this
page translates what you will see.

## 1. Install

```bash
git clone https://github.com/seuclkzsd/EdgeFDL.git && cd EdgeFDL
python3 -m venv .venv && . .venv/bin/activate
pip install torch --index-url https://download.pytorch.org/whl/cpu    # CPU-only PyTorch (a much smaller download)
pip install -r requirements.txt                                       # numpy, pyyaml, torch, psutil
python -m edgefdl --version                                           # edgefdl 0.1.1
```

- The CPU-only PyTorch line is optional but recommended: EdgeFDL never uses a GPU, and the default PyTorch wheel for
  Linux pulls in CUDA libraries. Install it before `requirements.txt` so that pip keeps it.
- `psutil` is optional; without it the dashboard and `/api/status` show no process CPU and memory figures.
- `pip install -e .` additionally installs an `edgefdl` command (`edgefdl --version`). Keep working from the
  checkout: the demo topology (`configs/`) and the released model (`results/`) live in the repository, not in the
  Python package.
- Check the installation with the unit tests (71 tests, about 20 s):

  ```bash
  python -m unittest discover -s tests
  ```

These steps, the unit tests and section 2 were checked in new virtual environments on Ubuntu 20.04 (x86-64) with
Python 3.8 (PyTorch 2.4 CPU build) and Python 3.13 (PyTorch 2.15 CPU build).

## 2. Start the dashboard

```bash
python -m edgefdl serve --model results/model.pt --backend sim --interval 2
```

The process prints `边缘网管已启动：http://127.0.0.1:8460  后端=sim  日志=runs/live` ("EMS service started …
backend = sim, logs = runs/live") and keeps running until you press Ctrl-C. Open <http://127.0.0.1:8460>.

- `--backend sim` measures a simulated copy of the network described in the model bundle; the bundle carries the
  topology, the normal baseline and the measurement plan, so no other file is needed.
- `--interval 2` runs a measurement round every 2 s instead of the default 5 s, so the demo reacts faster. The
  probe-bandwidth tile reads correspondingly higher than at 5 s.
- The simulator adds a one-round transient (a delay spike or a loss burst on a random link) with probability 0.1 per
  round (`--noise`), so you will occasionally see the detector wake up and the agent dismiss the anomaly.
- The server listens on 127.0.0.1 only and answers only requests addressed to `localhost`, `127.0.0.1` or `::1`
  (any port). On a remote machine, forward the port with SSH (`ssh -L 8460:127.0.0.1:8460 <user>@<server>`) and
  open the same address locally; use `--port` if 8460 is taken.
- Measurement and alarm logs are written to `runs/live/` (`--log-dir`); see [API, CSV logs](api.md#csv-logs).

What the page shows:

| Area (label on the page) | Content |
|---|---|
| Header tiles: 运行时长 · 测量轮次 · 单次前向推理 p99 (≤100ms) · 测量带宽占用 · 进程 CPU / 内存 · 告警 / 已过滤 | Uptime · measurement rounds · p99 time of one forward pass of the agent (target ≤ 100 ms) · probe traffic as a share of the narrowest link (target < 5%) · process CPU and memory · alarms raised / anomalies filtered by the agent |
| 网络拓扑与链路健康 (topology and link health) | Links colored 正常 green (normal), 可疑 / 受影响 amber (suspect or affected), 异常 / 告警 red (anomalous or alarmed); a dashed line means the OSPF adjacency is down. Click a link to plot its measurements |
| 链路测量 (link measurements) | RTT and loss of the targets on the selected link over the last 120 rounds |
| 故障注入（演示 / 测试） (fault injection, demo and test) | Link, fault type and value; 注入故障 injects, 清除全部 clears every injected fault |
| 当前告警与处置建议 (current alarm and remediation) | The most severe active alarm, explained below |
| 事件流 (event stream) | Suspected faults, alarms, clearings and filtered anomalies, newest first |
| 告警历史 (alarm history) | The last 12 alarms with their state (活动 active, 已恢复 cleared); buttons export the alarm log and the measurement log as CSV |

## 3. Inject a fault and read the alarm

In the fault-injection panel pick a link, a fault type and a value, then press **注入故障**. Faults are injected into
the simulated network only.

| Fault type (label on the page) | Value field | Default value in the form | Range the model was trained on |
|---|---|---|---|
| Link down (链路中断) | none | | both interfaces down, the subnet is withdrawn |
| Delay (时延劣化(RTT异常)) | added one-way delay in ms, applied at both link ends | 30 | 5–100 ms |
| Loss (丢包劣化) | loss rate as a fraction, 0–1 | 0.1 | 0.05–0.9 |
| Jitter (抖动劣化) | jitter in ms | 15 | 5–50 ms |
| Bandwidth (带宽劣化) | remaining fraction of the link bandwidth, 0–1 | 0.2 | 0.05–0.5 |
| Route drift (路由漂移) | multiplier of the link's OSPF cost | 10 | ×0.1, 0.3, 3, 10, 30; router–router links only, and the change must move a monitored path |

Watch the event stream. A persistent fault is first reported as suspected, for example
`疑似 L6 链路中断，等待下一轮复核` ("suspected: L6 link down, waiting for the next check"), and becomes an alarm after a
second consistent diagnosis: `告警 A…-0001：L6 链路中断（置信度 0.98）` ("alarm …: L6 link down, confidence 0.98").
This takes about 6 to 8 measurement rounds; bandwidth faults take longer because the bandwidth of only 3 links is
estimated per round. In the self-test at the default 5 s interval the mean time to alarm over 84 scenarios is
37.9 s (maximum 70 s), see [Results](results.md).

![Dashboard while link L6 is down: the topology marks L6 red and dashed, the alarm panel names L6 and its interfaces, lists the evidence, the H3C Comware commands and the rehearsal result (the UI is in Chinese)](media/dash_linkdown.png)

The alarm panel contains, from top to bottom:

- the severity (紧急 critical, 重要 major, 一般 minor), the alarm ID, the category and the round of the alarm;
- the link (or, for a device-down alarm, the device) and the fault type, both interfaces with their addresses, and
  the site;
- the confidence, the inference time, the number of deep probes used, and the next-best candidate conclusions;
- 研判依据 (evidence), for example "both interface addresses 10.10.0.21 / 10.10.0.22 unreachable", "OSPF adjacency
  agg2↔acc3 down", "100% of the monitored paths that crossed the link have been rerouted";
- 处置建议 (remediation steps) with vendor commands for the two devices, here H3C Comware, the vendor of agg2 and
  acc3 (`[agg2] display interface eth3`, …);
- 等价网络预演 (rehearsal on the equivalent network): the remediation action, whether it is feasible (可行) and its
  effect, for example "7 monitored targets have switched to the backup path";
- 与注入真值比对 (comparison with the injected fault): shown only in the demo, ✓ when link and type both match.

Press **清除全部** to repair the fault. The alarm clears once its own link looks normal again for 3 rounds:
`告警 … 恢复：告警链路自身证据恢复至基线` ("alarm … cleared: the alarmed link's own evidence is back to baseline").

Things to try next:

| Try | What you will see |
|---|---|
| Link down on L6 and a 20 ms delay on L1 at the same time | Two alarms. The second is found on the residual after subtracting the first (`…，扣除已有告警后发现`), and the panel says 另有 1 条活动告警 (one more active alarm). Each alarm's rehearsal takes the other alarm into account (`已计入其他活动告警：…`). Clearing L6 alone clears only its alarm |
| Link down on L2, L3, L6 and L7, all links of agg2 | One device-level alarm, 设备 agg2 设备宕机 (device agg2 down), covering L2/L3/L6/L7 instead of four link alarms |
| Route drift ×10 on L4 | Paths move away from L4 and the OSPF cost of L4 becomes 100; one route-drift alarm |
| Bandwidth 0.9 on L8 | No alarm: a bandwidth that stays above about 70% of the baseline is below what the light probes detect |
| Loss 0.02 on L4 | Usually no alarm; sometimes an alarm, which may name a wrong link. One-way loss below about 3% is below what the light probes resolve: in the fault-size sweep, 2% loss is named with the right type and link in 12.3% of the samples ([Results, section 4.3](results.md#43-fault-size-sensitivity)) |

The same can be scripted through the REST API ([api.md](api.md)):

```bash
curl -s -X POST -H 'Content-Type: application/json' \
     -d '{"type": "delay", "link": "L1", "value": 20}' http://127.0.0.1:8460/api/inject
curl -s http://127.0.0.1:8460/api/status          # active_alarms, link health, events, ...
curl -s -X POST -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:8460/api/clear
```

## 4. Train a model with the pipeline

```bash
python -m edgefdl pipeline --out runs/v6 --workers 16
```

One command runs the whole offline chain on the demo topology: learn the normal baseline on the simulated network,
generate 60,000 training samples plus validation and test sets, train the DDQN agent for 15,000 iterations,
evaluate it, and run the self-test. It takes about 15 minutes on CPU (measured on 2 × Intel Xeon Gold 6336Y; sample
generation used 16 processes and training 8 threads). Use `--workers` for the number of sample-generation processes
and `--threads` for the training threads; `--topo` selects another topology ([Deployment](deployment.md)).

| Output in `--out` | Content |
|---|---|
| `baseline.json` | Normal baseline learned on the simulated network |
| `train.npz`, `val.npz` | Training and validation sets (domain-randomized), each with a `*_samples.csv` manifest |
| `test.npz` | Independent test set under standard conditions (`gen --plain`), 10,000 samples by default |
| `test_rand.npz` | A second test set with domain randomization |
| `model.pt` | The model bundle: weights, topology, baseline, measurement plan, training history |
| `eval_test.json`, `eval_test_rand.json` | Evaluation of the model on both test sets |
| `bench/bench.json`, `bench/logs/` | Self-test results and the engine logs of the self-test (skipped with `--no-bench`) |

To check the toolchain in a few minutes, run a deliberately small pipeline. Its model is too weakly trained to be
useful; the point is only that every stage runs:

```bash
python -m edgefdl pipeline --out runs/tiny --n 2000 --iters 300 --no-bench --workers 8 --threads 4
```

Serve a model you trained exactly like the bundled one: `python -m edgefdl serve --model runs/v6/model.pt --interval 2`.
The figures on the [Results](results.md) page were measured with `results/model.pt`; a model you train is a
different model, and its figures differ from the published ones.

## 5. Evaluation, self-test and robustness suite

| Command | What it does | Output |
|---|---|---|
| `python -m edgefdl eval --model results/model.pt --data runs/v6/test.npz --json runs/v6/eval_check.json` | Accuracy per fault category and type, end to end (a fault the detector never triggers on counts as missed) | Tables on the terminal; the JSON file with `--json` |
| `python -m edgefdl bench --model results/model.pt --out runs/bench --rounds 51840` | Self-test: inference latency (PyTorch and NumPy engines), a long run with transient noise (51,840 rounds × 5 s = 72 h of simulated time; the default is 17,280 rounds = 24 h), probe overhead, CPU and memory, and 84 online fault scenarios | `runs/bench/bench.json` |
| `python -m edgefdl robust --model results/model.pt --out runs/robust --workers 40` | Robustness suite: 24 h runs under harsher conditions (heavier and longer transients, missing measurements, ICMP rate limiting, background jitter, busy-hour drift), offline accuracy under the same conditions, fault-size sweep, two simultaneous faults, device down. About 3.5 minutes with 40 workers | `runs/robust/robust.json`, per-run engine logs in `runs/robust/logs/` |

`eval` needs a test set from the pipeline (`runs/v6/test.npz`). The released outputs of the same commands are in
[`results/`](../results/README.md) (`eval_test.json`, `bench.json`, `robust.json`) and are explained in
[Results](results.md).

Useful `robust` options: `--only online` or `--only offline`; `--kinds long,scen,dual,node,single,node_dual,node_back`
to run selected online items; `--seed` (default: the `EDGEFDL_ROBUST_SEED` environment variable, else 21);
`--single-alarm` to evaluate the one-alarm-at-a-time mode. [`scripts/compare_robust.py`](../scripts/compare_robust.py)
compares multi-alarm and single-alarm runs of the same seeds against fixed pass criteria and prints `PASS` or `FAIL`
per criterion:

```bash
python -m edgefdl robust --model results/model.pt --out runs/robust_single --workers 40 --single-alarm
python scripts/compare_robust.py --multi runs/robust/robust.json --single runs/robust_single/robust.json
```

Short versions that only check that the commands work:

```bash
python -m edgefdl bench --model results/model.pt --out runs/bench-quick --rounds 720
python -m edgefdl robust --model results/model.pt --out runs/robust-quick --workers 8 \
    --n 200 --sweep-reps 1 --rounds 200 --dual 4 --node-reps 1 --kinds long,dual,node
```

Their numbers are not comparable with the released ones: the samples are few, and the busy-hour profile of the
long runs is stretched over the run length, so 200 rounds compress a day of drift into 17 minutes of simulated time.

## 6. Real protocol stack in Docker

The `netns` backend builds the equivalent network from Linux network namespaces, veth pairs, FRR (OSPF) and `tc`, with
real kernel forwarding. `scripts/lab.sh` packages it in a privileged Debian 12 container with FRR, iperf3,
traceroute and iproute2, so the host needs only Docker (your user in the `docker` group, privileged containers
allowed) and no root shell:

```bash
scripts/lab.sh build        # build the image edgefdl-lab:latest (EDGEFDL_APT_MIRROR=<mirror host> to use a local Debian mirror)
scripts/lab.sh smoke        # build the demo network in namespaces and run the 26-check smoke test
```

The smoke test brings the 12-device network up, waits for OSPF to converge, checks that all 37 targets are
reachable, that traceroute agrees with the forwarding tables and that a deep probe measures the nominal bandwidth,
then injects each fault type on link L4 in turn (link down, +20 ms delay, 10% loss, 10 ms jitter, bandwidth cut to
20%, OSPF cost ×10) and checks what the probes see: unreachable interfaces and rerouting, the RTT increase, deep-probe
loss, jitter and bandwidth, the LSDB adjacency and cost, OSPF adjacencies staying Full during the degradations. It
finally checks the return to baseline and that no namespace is left after teardown. It prints `[PASS]` or `[FAIL]` per check and
ends with `== 冒烟结果 N/26 通过` (N of 26 checks passed); the exit code is non-zero if any check fails. Options:
`--link` (a link with a redundant path, default L4), `--loss` (loss for the loss step, default 0.1), `--ospf`
(hello and dead intervals, default `1,4`), `--static` (static routes instead of FRR). The logs of the release runs
are in [`results/smoke/`](../results/smoke/).

Other entry points:

```bash
scripts/lab.sh shell                                                            # interactive shell in the container
scripts/lab.sh run python3 -m edgefdl baseline --backend netns -o runs/lab/baseline.json   # learn a baseline on the real stack
```

The container mounts the repository at `/work`, runs with `--network none` and is removed when the command ends, so
write outputs into the repository (as `runs/lab/` above); `lab.sh run` warns about output paths under `/tmp`. Files
that a `run` command creates in the repository are handed back to your user afterwards (files created in `shell`
stay owned by root). The image carries the network tools but not PyTorch, so it runs the backend-level commands
(`baseline`, `gen`, `vnet`, the smoke test); training and `serve` run on the host. `EDGEFDL_LAB_IMAGE` overrides the
image name.

The third backend, `lxd`, runs every device in its own LXD system container. Print its complete command sequence
(node image, bridges, containers, FRR configuration, an uplink to a physical port, a fault injection and teardown)
without LXD installed:

```bash
python -m edgefdl.vnet.lxd configs/demo_topology.yaml --uplink ems=enp3s0
```

The FRR configuration files are generated in a temporary directory that is removed afterwards; add
`--stage-dir <directory>` to keep them for review.

[Deployment](deployment.md#3-build-the-equivalent-network-and-train-the-model) explains when to use which backend.

## 7. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `OSError: [Errno 98] Address already in use` when starting `serve` | Port 8460 is taken, perhaps by an earlier `serve`. Stop it or pass `--port 8470` |
| `curl` to `127.0.0.1` returns nothing or an error page from a proxy | `http_proxy` / `https_proxy` are set and curl sends local requests to the proxy. Use `curl --noproxy '*' …` or set `no_proxy=127.0.0.1,localhost` |
| Webhook receiver gets nothing | The Webhook sink uses Python's `urllib`, which honours `http_proxy` and `no_proxy`. Add the receiver's host to `no_proxy` in the environment of `serve` |
| `POST /api/inject` returns `{"error": "当前后端不允许注入故障"}` (HTTP 403) | `serve` was started with `--no-inject`, which disables fault injection |
| A `POST` returns `{"error": "POST 请求须为 Content-Type: application/json"}` (HTTP 415) | Send the JSON body with `-H 'Content-Type: application/json'`, as in [section 3](#3-inject-a-fault-and-read-the-alarm) |
| A request returns `{"error": "拒绝 Host 为 '…' 的请求：…"}` (HTTP 403) | The server listens on 127.0.0.1 and accepts only requests addressed to `localhost`, `127.0.0.1` or `::1`. Use one of these names (an SSH tunnel to any local port works); a reverse proxy must pass the upstream `Host` (`127.0.0.1:8460`, nginx's default). To serve other hosts directly, start `serve` with `--host <address>` ([Deployment, security](deployment.md#6-security)) |
| `POST /api/inject` returns HTTP 400, for example `{"error": "loss 的 value 须大于 0 且不超过 1（丢包率），收到 5"}` | The value is outside the accepted range; the ranges and messages are listed in [api.md](api.md#post-apiinject-and-post-apiclear) |
| `serve` stops at start with `edgefdl: --sinks 配置有误：Webhook 地址须为以 http:// 或 https:// 开头的完整 URL…` or another `--sinks 配置有误` message | `--sinks` is checked at start: a Webhook needs a complete `http://` or `https://` URL, a Syslog sink a `host` and a port from 1 to 65535, and only the types `webhook` and `syslog` exist ([Deployment, alarm sinks](deployment.md#alarm-sinks)) |
| No alarm after injecting a fault | The value is below what the light probes detect (one-way loss below about 3%, bandwidth above about 70% of the baseline), or a route drift does not move any monitored path (and route drift applies to router–router links only). Wait longer for bandwidth faults. Use values in the trained ranges of [section 3](#3-inject-a-fault-and-read-the-alarm) |
| `FileNotFoundError: …/configs/demo_topology.yaml` | EdgeFDL was installed with `pip install .` and run outside the checkout, so the default topology path does not exist. Run from the repository, install with `pip install -e .`, or pass `--topo` |
| The PyTorch download is very large | The default Linux wheel includes CUDA. Install the CPU build first (section 1) |
| `pipeline`, `robust` or `gen` uses too many CPUs | Lower `--workers` (sample generation, robustness jobs) and `--threads` (training). Sample sets do not depend on `--workers` |
| `scripts/lab.sh build` is slow or cannot reach the Debian mirror | Set `EDGEFDL_APT_MIRROR` to a reachable Debian mirror host, for example `EDGEFDL_APT_MIRROR=mirrors.example.org scripts/lab.sh build` |
| `scripts/lab.sh` reports `permission denied` on the Docker socket | Add your user to the `docker` group (and log in again), or run it as a user who may start privileged containers |
| `scripts/lab.sh smoke` reports a failed check (`[FAIL] …`) | The real-stack checks depend on packet timing inside the container; a heavily loaded host can delay OSPF convergence or probe packets. Re-run on an idle host; if a check keeps failing, open an issue with the full log (the `[FAIL]` line shows the measured value) |
| `ModuleNotFoundError: No module named 'torch'` inside the lab container | Expected: the lab image has no PyTorch. Run `serve`, `train`, `eval`, `bench` and `robust` on the host |

Still stuck? Open an issue with the bug report template and include `python -m edgefdl --version`, the command line and
its full output.
