# Deployment

This page takes EdgeFDL from the demo network to a network of your own: describing the network, building its
equivalent virtual network, training the per-network model, running the online service on the network-management
server (EMS), and connecting its alarms to the systems you already use. For a first look at the system, start with
the [quick start](quickstart.md).

**Scope of 0.1.2.** `serve` measures the equivalent virtual network of the site through one of the three backends
(`sim`, `netns`, `lxd`); measuring the production network itself is the first step of the
[roadmap](../ROADMAP.md) ([section 8](#8-scope-of-v01-and-next-steps)). With this release you describe the network,
train and evaluate its model, and run the online service, its alarms and its integrations against the equivalent
network, which prepares the operators and the receiving systems for the connection to the physical network.

1. [Requirements](#1-requirements)
2. [Describe the network](#2-describe-the-network)
3. [Build the equivalent network and train the model](#3-build-the-equivalent-network-and-train-the-model)
4. [Run the online service](#4-run-the-online-service)
5. [Integrations](#5-integrations)
6. [Security](#6-security)
7. [Checklist before going live](#7-checklist-before-going-live)
8. [Scope of v0.1 and next steps](#8-scope-of-v01-and-next-steps)

## How a deployment is organised

| Where | What runs | Produces |
|---|---|---|
| Offline, once per network (on the EMS or on a separate training server) | `intake` → `baseline` → `gen` → `train` → `eval` / `bench`, or all of them through `pipeline`, against an equivalent virtual network | One model bundle, `model.pt`: weights, the topology, the normal baseline and the measurement plan |
| Online, on the EMS | `serve --model model.pt`: measurement every 5 s, detector, agent, alarms, dashboard and REST API | CSV logs, alarms on the dashboard, the API, Syslog, Webhook and Prometheus |

The bundle is the only file the online service needs. The equivalent virtual network is also where remediation
actions are rehearsed before they are recommended, so the same topology model serves training, diagnosis and advice.

## 1. Requirements

### Online service on the EMS

Measured with the released model on the 12-device demo topology (simulation backend, 5 s rounds; 2 × Intel Xeon Gold
6336Y, Python 3.13, PyTorch 2.9.1, a CUDA 12.8 build run on the CPU; [`results/bench.json`](../results/bench.json)):

| Resource | Measured | Notes |
|---|---|---|
| CPU | 4.338 ms of computation per 5 s round = 0.0868% of one core | Inference runs on one thread: p99 0.575 ms per forward pass with PyTorch, 0.302 ms with `--engine numpy`; a full diagnosis including features p99 2.512 ms |
| Memory | RSS 528.6 MB | Mostly the PyTorch runtime, here the CUDA build of PyTorch 2.9.1 (no GPU used); the model has 36,041 parameters |
| Probe traffic | 222.0 kbit/s by the plan's accounting; about 290 KB per round (≈465 kbit/s) measured on the real protocol stack | 0.222% (plan) and at most 0.47% (real stack) of the narrowest 100 Mbit/s link even if all probe traffic crossed it; 0.022% of the 1 Gbit/s management uplink (plan) |
| Disk | Logs rotate at 200 MB (MiB) with 20 archives kept, so each of the two logs takes at most about 4.1 GiB | Plus the model bundle and the topology file |

The probe traffic of one round is 5 pings to every interface, loopback and host address, a traceroute per target and
a packet train on 3 links. A deep probe, which the agent requests only when the light evidence is insufficient, sends
100 pings at 10 ms intervals and runs iperf3 for 1 s at no more than 30% of the link's bandwidth (0.061 deep probes
per sample on the test set).

### Model building

Sample generation and training use only the CPU. On the hardware above, generating 60,000 samples took 87.8 s with
16 processes, training (15,000 iterations, 512 parallel episodes) about 4.4 minutes on 8 threads, and the whole
`pipeline` including evaluation and self-test about 15 minutes. `--workers` and `--threads` scale this to smaller
machines.

### Software and privileges

| Component | Needed for |
|---|---|
| Linux, Python ≥ 3.8 | Everything. Tested on Ubuntu 20.04 (x86-64) with Python 3.8 and 3.13 and, for the real-stack lab, on Debian 12 |
| `numpy`, `pyyaml`, `torch` (the CPU build is enough) | Everything; `--engine numpy` speeds up inference but still loads the bundle with PyTorch |
| `psutil` (optional) | Process CPU and memory in `/api/status` and on the dashboard |
| `iproute2` (`ip`, `tc`), `iputils-ping`, `traceroute`, `iperf3`, FRR (`zebra`, `ospfd`, `vtysh`), `python3` | The `netns` backend (inside each LXD container for `lxd`) |
| LXD | The `lxd` backend |

| Mode | Privileges |
|---|---|
| `sim` backend: intake, training, evaluation, self-test, `serve` | An ordinary user |
| [Container image](#container-image) (`sim` backend, `serve`) | None inside the container; access to Docker to start it |
| `netns` backend | Root (`CAP_NET_ADMIN`, `CAP_SYS_ADMIN`), or the privileged lab container of [`scripts/lab.sh`](../scripts/lab.sh) |
| `lxd` backend | Membership of the `lxd` group |

Install as in the [quick start](quickstart.md#1-install), in a virtual environment owned by the user that will run
the service.

## 2. Describe the network

Everything EdgeFDL does derives from one topology file. `intake` builds it from what is usually at hand on site: a
short requirements form, the devices' configuration dumps and, optionally, an LLDP neighbour table or a topology
drawing.

**The requirements form** (YAML) lists the devices and where the EMS is; links can be left to the configurations.
[`configs/intake_demo/form.yaml`](../configs/intake_demo/form.yaml), with comments added:

```yaml
name: intake-demo
vantage: ems                       # the device name of the EMS
nodes:                             # role: router | switch | host | ems
  - {name: core, role: router, site: 中心机房}
  - {name: agg1, role: router, site: 东区}
  - {name: agg2, role: router, site: 西区}
  - {name: ems, role: ems, site: 中心机房}
links:                             # optional: links not visible in any configuration, e.g. to the EMS
  - {a: core, b: ems, bw: 1000, delay: 0.1}
link_perf:                         # optional: measured one-way delay (ms), bandwidth (Mbit/s), jitter, loss per link
  core-agg1: {delay: 1.0}
  core-agg2: {delay: 1.2}
```

Node entries may also give `vendor`, `model`, `loopback` and `agent` (whether the device can host a probe endpoint
for deep probes); link entries may give `id`, interface names (`a_if`, `b_if`), addresses (`a_ip`, `b_ip` with
prefix length), `cost`, `jitter` and `loss`. `ip_plan` (`p2p_pool`, `loopback_pool`) sets the pools used for
anything left blank; automatic planning skips names and addresses that the form already uses.

The topology is checked when it is loaded. Device names and link IDs start with a letter or digit and contain only
letters, digits, `_`, `.` and `-` (at most 32 characters); interface names may also contain `/` and `:`; `lo` is
reserved. Duplicate device names, link IDs, interface names of one device and addresses, links from a device to
itself, unknown devices and out-of-range values (bandwidth, delay, jitter, loss, OSPF cost 1–65535) stop the command
with a message that names the field.

**Device configurations** go into one directory, one text file per device: Huawei VRP or H3C Comware
`display current-configuration`, Cisco IOS `show running-config`. EdgeFDL reads the device name (`sysname` or
`hostname`), interfaces with their addresses, OSPF costs, `bandwidth` statements and descriptions; the interface
name gives the speed (GigabitEthernet → 1000 Mbit/s and so on). Interfaces of two devices in the same subnet become
a link; an interface whose description reads `to-<device>` is linked to a device that has no configuration file,
such as the EMS.

**An LLDP table** (`--lldp`) is a CSV file with the columns `local_device,local_if,remote_device,remote_if`; it
adds links between devices of the form that the configurations do not reveal.

**Drawings and scanned documents** (`.png`, `.jpg`, `.jpeg` in the configuration directory) are sent to an
OpenAI-compatible multimodal endpoint when `--llm <base URL>` is given (for example a locally hosted
vision-language model; `EDGEFDL_LLM_MODEL` and `EDGEFDL_LLM_KEY` set the model name and key). The model is asked
to return devices and links as JSON and to leave unreadable fields empty. Without `--llm` these files are skipped
with a note. A PDF is not sent, because the image input of these endpoints accepts images only: convert its pages
to PNG first, for example `pdftoppm -png -r 150 topology.pdf topology`; a `.pdf` in the directory is skipped with a
warning. A drawing whose extraction fails (endpoint unreachable, request refused, unusable answer) is skipped with a
warning and the intake continues. Review what was extracted before training.

```bash
python -m edgefdl intake --form configs/intake_demo/form.yaml --configs configs/intake_demo/configs \
    -o runs/site/topology.yaml          # add --lldp neighbours.csv and --llm http://<host>:<port>/v1 as available
```

```text
拓扑 intake-demo: 4 台设备, 4 条链路, 网管=ems
  L1   core(10.20.9.1/30) <-> ems(10.20.9.2/30)  1000Mbps 0.1ms cost=10
  L2   agg1(10.20.0.2/30) <-> core(10.20.0.1/30)  1000Mbps 1ms cost=10
  L3   agg2(10.20.0.6/30) <-> core(10.20.0.5/30)  1000Mbps 1.2ms cost=10
  L4   agg1(10.20.0.9/30) <-> agg2(10.20.0.10/30)  100Mbps 0.5ms cost=15
已生成拓扑: runs/site/topology.yaml
```

(Topology intake-demo: 4 devices, 4 links, EMS = ems; then one line per link; "topology written".) Then check the
monitoring coverage:

```bash
python -m edgefdl topo runs/site/topology.yaml
```

The last line, `监测目标 11 个；盲区链路（已规划 NQA 探测目标）: L4`, reads "11 monitored targets; blind links (NQA
probe targets planned): L4". A blind link is one that no path from the EMS crosses, so probes from the EMS
cannot see a fault on it. Coverage planning adds a device-native probe target for each such link (Huawei and H3C NQA,
Cisco IP SLA) from one end device to the other. The virtual backends run it themselves; on the physical network the
operator configures it once on the end device and EdgeFDL only reads its results. On the demo topology the backup link L3 is the blind link.

Read the topology file once more before training. It is the single source of truth for the virtual network, the
features, the alarms and the remediation advice, and the format is described in
[architecture, topology file](architecture.md#topology-file). Parallel links between the same two devices are not
supported in 0.1.2.

## 3. Build the equivalent network and train the model

### Backends

The equivalent virtual network reproduces the devices, links, addresses, bandwidths, delays and OSPF costs of the
topology, and injects faults with the same semantics in every backend (`tc netem` for delay, loss and jitter, `tbf`
for bandwidth, interfaces down for link down, OSPF cost changes for route drift).

| Backend | What it is | Use it for | Verified by |
|---|---|---|---|
| `sim` | Analytic simulator: OSPF shortest paths and per-link delay, jitter, loss and bandwidth; parallelises over processes (60,000 samples in 87.8 s with 16 processes) | Baselines, sample generation, training, evaluation, self-test, robustness suite, `serve` for demonstrations | All accuracy and robustness results in [Results](results.md) |
| `netns` | One Linux network namespace per device, a veth pair per link, FRR (zebra + ospfd) per router, `tc` on both link ends; real kernel forwarding and OSPF | Checking probes and fault semantics on a real protocol stack; real-stack baselines and samples | The 26-check smoke test ([`results/smoke/`](../results/smoke/)); baseline RTT of each of the 37 targets within 0.74 ms of the simulator, median 0.39 ms ([`results/sim_vs_stack.json`](../results/sim_vs_stack.json)); OSPF converges in about 11 s for 12 devices |
| `lxd` | One LXD system container per device (FRR and the probe tools inside), one address-less LXD bridge per link, optional macvlan or bridged uplinks from chosen devices to physical ports of the host | The deployment form on the EMS or a training server, including interconnection of the virtual network with a physical network | Generation of the complete command sequence (dry run) |

The real-stack backends use the interface names of the topology as Linux interface names, which allow at most 15
characters, only letters, digits, `_`, `.` and `-`. A topology built from vendor configurations carries names such
as `GigabitEthernet0/0/1`; it works with `sim` as it is, while `netns` and `lxd` stop at once with
`ValueError: 以下接口名不是合法的 Linux 接口名…` ("these interface names are not valid Linux interface names") and
list the offending names. Give these backends a copy of the topology with empty interface names, which EdgeFDL
fills with `eth1`, `eth2`, …:

```bash
python - <<'EOF'
import yaml
with open("runs/site/topology.yaml", encoding="utf-8") as f:
    d = yaml.safe_load(f)
for l in d["links"]:
    l["a_if"] = l["b_if"] = ""          # empty names: EdgeFDL assigns eth1, eth2, ...
with open("runs/site/topology-stack.yaml", "w", encoding="utf-8") as f:
    yaml.safe_dump(d, f, allow_unicode=True, sort_keys=False)
EOF
```

Manage the real-stack backends with `vnet`:

```bash
sudo .venv/bin/python -m edgefdl vnet up --backend netns --topo runs/site/topology-stack.yaml     # root required
sudo .venv/bin/python -m edgefdl vnet status --backend netns --topo runs/site/topology-stack.yaml
sudo .venv/bin/python -m edgefdl vnet down --backend netns --topo runs/site/topology-stack.yaml
python -m edgefdl vnet up --backend netns --dry-run --topo runs/site/topology-stack.yaml   # prints the commands, runs nothing
python -m edgefdl vnet up --backend lxd --dry-run --topo runs/site/topology-stack.yaml     # the same for LXD
```

`--dry-run` prints the command sequence of `up` or `down` and changes nothing on the machine, for both backends; on
a machine without the `lxc` command, `vnet --backend lxd` always runs as a dry run. Namespaces are named
`efl-<device>`; FRR configuration and PID files go to `/etc/frr/efl-<device>/` and `/var/run/frr/efl-<device>/`.
Without root on the host, run the `netns` backend in the lab container
([quick start, section 6](quickstart.md#6-real-protocol-stack-in-docker)), with outputs inside the repository,
because the container is removed when the command ends:
`scripts/lab.sh run python3 -m edgefdl baseline --backend netns --topo runs/site/topology-stack.yaml -o runs/lab/baseline.json`.

For LXD, print and review the whole command sequence first:

```bash
python -m edgefdl.vnet.lxd runs/site/topology-stack.yaml --uplink ems=enp3s0 > runs/site/lxd-plan.txt
```

The plan has four parts: (1) build the node image `edgefdl-node` once from `images:ubuntu/22.04` with FRR, iperf3,
traceroute, iproute2, iputils-ping and python3 (this step needs network access; for an offline EMS build the image
on a connected machine and move it with LXD's image export and import); (2) create the profile `efl-node`, one
bridge `efl-<link>` per link, one container `efl-<device>` per device with its interfaces, the uplink
(`--uplink <device>=<host port>`, macvlan, may be repeated), addresses and FRR configuration; (3) an example fault
injection and recovery; (4) teardown. The FRR files are generated in a temporary directory that is removed
afterwards; `--stage-dir runs/site/frr` keeps them for review. LXD network names are limited to 15 characters, so
link IDs should stay short, and the containers `efl-<device>` must be valid LXD instance names, so device names
may contain only letters, digits and `-` and must not end with `-`.

### Train the per-network model

```bash
python -m edgefdl pipeline --topo runs/site/topology.yaml --out runs/site --workers 16
```

This learns the baseline on the simulated network, generates the training, validation and test sets, trains the
agent, evaluates it and runs the self-test, exactly as for the demo network
([quick start, section 4](quickstart.md#4-train-a-model-with-the-pipeline)). The network size does not change the
model's size (36,041 parameters); the action space grows with the number of links (7 actions per link plus
"normal"). The same steps one by one, for example to use a real-stack baseline or more samples:

```bash
python -m edgefdl baseline --topo runs/site/topology.yaml -o runs/site/baseline.json        # --backend netns|lxd for a real-stack baseline
python -m edgefdl gen --topo runs/site/topology.yaml --baseline runs/site/baseline.json \
    --n 60000 --out runs/site/train.npz --workers 16                                         # domain-randomized training set
python -m edgefdl gen --topo runs/site/topology.yaml --baseline runs/site/baseline.json \
    --n 10000 --seed 3 --plain --out runs/site/test.npz --workers 16                         # independent test set, standard conditions
python -m edgefdl train --data runs/site/train.npz --topo runs/site/topology.yaml \
    --baseline runs/site/baseline.json --out runs/site/model.pt --threads 8 --version site-v1
python -m edgefdl eval --model runs/site/model.pt --data runs/site/test.npz --json runs/site/eval_test.json
python -m edgefdl bench --model runs/site/model.pt --out runs/site/bench
```

On a real-stack backend, `baseline` first waits for OSPF to converge, and `gen --backend netns|lxd` runs in real
time, one sample after another, which suits calibration sets rather than the full training set. The virtual network
stays up after `baseline`; remove it with `vnet down`. `train --val <file>` adds a validation set; `gen --raw-csv
<file>` (single process) also writes every measurement round in the measurement-log format. On the `sim` backend a
seed gives the same sample set for any `--workers` value, with or without `--raw-csv`.

Before deploying, compare `eval_test.json` and `bench/bench.json` of your model with the released results of the
demo model ([Results](results.md)): accuracy per fault type, false alarms in the long run, time to alarm in the
online scenarios, inference latency and probe overhead.

## 4. Run the online service

In 0.1.2 the online service measures the site's equivalent virtual network. Run the site model on it with the
alarm sinks that the site uses, inject faults from the dashboard or through `POST /api/inject`, and check that the
alarms, the advice and the integrations behave as expected:

```bash
python -m edgefdl serve --model runs/site/model.pt --backend sim --engine numpy --log-dir runs/site/logs \
    --sinks '[{"type": "syslog", "host": "10.0.0.5"}, {"type": "webhook", "url": "https://hooks.example.org/edgefdl"}]'
```

This prints `边缘网管已启动：http://127.0.0.1:8460  后端=sim  日志=runs/site/logs` ("EMS service started").
`--backend netns` (root) or `--backend lxd` (lxd group) runs the model on a real protocol stack built from the
topology in the bundle; for these backends, train the model from the copy with Linux interface names (section 3,
`pipeline --topo runs/site/topology-stack.yaml`), whose alarms then name those interfaces. All options below apply
to every backend. A deployment whose backend is connected to a real network, for example an `lxd` deployment with a
bridged uplink, adds `--learn-rounds 120` (the service then first prints `上线基线学习：120 轮（约 10 分钟）…`,
"on-site baseline learning: 120 rounds, about 10 minutes") and `--no-inject`.

| Option | Default | Meaning |
|---|---|---|
| `--model` | (required) | Model bundle |
| `--backend` | `sim` | `sim`, `netns` or `lxd` |
| `--host`, `--port` | `127.0.0.1`, `8460` | Address of the dashboard and API |
| `--interval` | from the bundle (5 s) | Measurement interval in seconds |
| `--log-dir` | `runs/live` | Directory of the CSV logs, relative to the working directory |
| `--learn-rounds` | `0` | Learn the baseline on site for this many rounds before starting (0 keeps the bundle's baseline) |
| `--no-inject` | off | Disable `POST /api/inject` and `/api/clear` (they then return 403) |
| `--single-alarm` | off | At most one active alarm at a time |
| `--confirm` | `2` | Consistent diagnoses required before the first alarm |
| `--engine` | `torch` | `numpy` runs the forward pass in NumPy (p99 0.302 ms instead of 0.575 ms); the bundle is still loaded with PyTorch |
| `--sinks` | none | JSON list (or a single object) of alarm sinks, checked at start ([section 5](#5-integrations)) |
| `--noise`, `--seed` | `0.1`, `0` | `sim` only: probability of a one-round transient per round, random seed |

`serve` runs in the foreground. Exceptions in the measurement loop are caught and counted (`errors` in
`/api/status`), so one bad round does not stop the service; a failing log write, alarm sink or advice generation is
counted the same way and does not stop the alarm. Ctrl-C stops the engine and closes the logs.

### Container image

[`compose.yaml`](../compose.yaml) runs the dashboard on the `sim` backend with the bundled model, so that the system
can be tried and tested without a Python environment:

```bash
docker compose up                # builds the image on the first run; dashboard on http://127.0.0.1:8460
docker compose down --volumes    # removes the container and its logs
```

The image is built on your machine from the root [`Dockerfile`](../Dockerfile): the Python 3.12 base image pinned by
digest, the CPU build of PyTorch and the other packages of
[`requirements-lock.txt`](../requirements-lock.txt), installed with their hashes. Measured size, memory and the
end-to-end result are in [`results/docker_check.json`](../results/docker_check.json); evaluating
`results/model.pt` on the test set inside the image gives a file byte-identical to
[`results/eval_test.json`](../results/eval_test.json).

The container runs as UID 10001 with a read-only root file system, no capabilities, `no-new-privileges`, 2 CPUs and
2 GB of memory. The CSV logs are in the `logs` volume, mounted at `/data`, and the REST API works as on a bare-metal
`serve` ([api.md](api.md)). The dashboard is published on the host's `127.0.0.1` only, because the service has no
authentication ([section 6](#6-security)). The image contains no network tools: the `netns` and `lxd` backends do not
run in it, and they need the privileges in [section 1](#software-and-privileges). `linux/amd64` is the only platform of
the lock file.

| Variable | Default | Meaning |
|---|---|---|
| `EDGEFDL_PORT` | `8460` | Port on the host's loopback address |
| `EDGEFDL_INTERVAL` | `2` | Measurement interval in seconds (the demo's 2 s, not the production 5 s) |
| `EDGEFDL_IMAGE` | `edgefdl:local` | Name of the image |
| `EDGEFDL_REGISTRY` | `docker.io/library` | Registry prefix of the base image, for a mirror |
| `EDGEFDL_PIP_INDEX` | `https://pypi.org/simple` | PyPI index for the build, for a mirror |
| `EDGEFDL_PIP_FIND_LINKS` | empty | Additional source of wheels for the build, for a mirror of the PyTorch CPU wheels |

Mirrors do not change what is installed: the base image is pinned by digest and pip checks every file against the hash
in the lock file. For example, in mainland China:

```bash
export EDGEFDL_REGISTRY=docker.1ms.run/library
export EDGEFDL_PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
export EDGEFDL_PIP_FIND_LINKS=https://mirrors.aliyun.com/pytorch-wheels/cpu/
docker compose up --build
```

`tools/dev/ci.sh image` builds the image, starts it, checks these settings and repeats the end-to-end check through the
published port: inject a fault through the REST API, await the alarm, clear the fault, await the recovery, stop.

### On-site baseline learning

The bundle's baseline was learned on the backend used for training, usually `sim`. A real protocol stack, and later
the physical network, differs in small ways: device response times, background loss (for example routers
rate-limiting ICMP replies), jitter. `--learn-rounds N` measures the network for N rounds before the engine starts,
runs three deep probes on every link, fits a new baseline (per-target RTT, deviation, background loss rate and path;
per-link bandwidth, cost and deep-probe baseline) and uses it instead of the bundle's. On the real-stack backends it first waits until every target answers, that is until OSPF has
converged, and stops with an error if that takes longer than 60 s. The dashboard
and the API start when learning is complete. The CLI recommends at least 120 rounds and prints an estimate from the
interval (120 rounds × 5 s = 10 minutes); the learning rounds are measured back to back, so the actual duration
depends on how long one round of probes takes on your network. Learn while the network is healthy: a fault present
during learning becomes part of the baseline.

After the start the baseline keeps adapting on its own:

- the RTT baseline of each target follows slow drift with an EWMA (30 minute time constant), which absorbs effects
  such as busy-hour load; targets affected by an active alarm, deviating beyond the threshold or missing follow the
  network-wide per-hop common-mode drift instead, and the deep-probe baseline shifts with it;
- the background loss is relearned (bursts excluded, capped at 5%) when the detector stays busy for 30 minutes
  without a consistent conclusion, when loss conclusions rotate over three or more links while the alarmed link shows
  no significant excess loss, or when router targets that cross no alarmed link broadly show excess loss.

In the robustness suite (simulation, 24 h runs, [Results](results.md)), routers dropping 5% of ICMP replies already at
baseline time caused 0 false alarms, with 98.8% joint accuracy over the 84 scenarios; the same rate appearing after
deployment caused 2 false alarms in 24 h (96.4%); per-link background jitter of 1 ms with the baseline relearned on
site caused 0 false alarms (100.0%); and a busy-hour rise of up to 1 ms one-way delay on every link caused 0 false
alarms.

Retrain when the network changes: new or removed devices or links, readdressing, or changed OSPF costs. Run `intake`
and the pipeline again and deploy the new bundle; the bundle carries its own topology, so an old bundle keeps
describing the old network.

### Multi-alarm and single-alarm modes

By default the engine reports concurrent faults separately. The first alarm is raised on the original baseline and
must show direct evidence of its fault type on its own link. Four rounds after the set of alarms changes, the engine
freezes a residual baseline that explains the alarmed faults; the agent then diagnoses the residual with the alarmed
links masked, and a second fault needs direct evidence on its own link and three consistent confirmations. Every
alarm clears on its own link's evidence (3 rounds) while the others stay. A device whose adjacencies to all
neighbouring routers are down and whose loopback is unreachable gets one device-down alarm for all its links, which
becomes a link-down alarm again if the device returns while one of its links is still down.

From the robustness suite (simulation, seed 21; [`results/robust.json`](../results/robust.json)):

| Trial | Result |
|---|---|
| Two simultaneous link faults, 300 trials | First alarm correct in 100% of trials, both faults reported in 97.67%, no wrong alarm in 99.67%; after one fault is repaired its alarm clears while the other stays in 100% of the trials in which both were active; mean time to alarm 34.0 s |
| One transit device down, 60 trials | One device-level alarm in every trial, mean time to alarm 29.6 s |
| Device down plus another link fault, 60 trials | Both reported in 98.33%, no wrong alarm in 100% |
| Device recovers while one of its links stays down, 22 trials | Downgraded to a link-down alarm in every trial, after 40.0 s on average |
| 84 single faults held for 40 rounds | Exactly one alarm in every scenario |

`--single-alarm` restores the one-alarm-at-a-time behaviour: a new, different conclusion replaces the active alarm
(cleared with the reason 研判结论变化, "diagnosis changed"), and the alarm clears when the whole network is back to
its baseline. Use it when a downstream system expects a single open incident from EdgeFDL.
`robust --single-alarm` and [`scripts/compare_robust.py`](../scripts/compare_robust.py) compare the two modes.

### Timing

In the self-test (5 s rounds, [`results/bench.json`](../results/bench.json)) the mean time from fault to alarm over 84
scenarios is 37.9 s (link down 30.0 s, delay 34.7 s, loss 37.3 s, jitter 34.3 s, bandwidth 58.0 s, route drift
30.0 s; maximum 70 s), and the mean time to clear after repair is 19.3 s. Bandwidth is slowest because the
bandwidth of 3 links is estimated per round. A shorter `--interval` shortens these times proportionally and raises
the probe traffic in the same proportion. `--confirm 3` trades a slower first alarm for one more consistent
diagnosis.

## 5. Integrations

### Alarm sinks

`--sinks` takes a JSON list of sinks (a single object is accepted too); several sinks may be combined:

| Sink | Configuration | Delivery |
|---|---|---|
| Syslog | `{"type": "syslog", "host": "10.0.0.5", "port": 514}` (`port` optional) | RFC 5424 over UDP, facility local0, one message per alarm `raise` and `clear`; severity critical / major / minor, clear as informational |
| Webhook | `{"type": "webhook", "url": "https://hooks.example.org/edgefdl"}` | HTTP POST of a JSON object per `raise` and `clear`, 3 s timeout, in a background thread, no retries |

The configuration is checked when `serve` starts, before the network backend comes up, and an error stops it with
exit code 1 and the line `edgefdl: --sinks 配置有误：<reason>` (invalid JSON gives the parser's message): a Webhook needs a
complete `http://` or `https://` URL (`Webhook 地址须为以 http:// 或 https:// 开头的完整 URL，收到 '…'`), a Syslog
sink needs a `host` (`syslog 告警通道缺少 host`) and a port from 1 to 65535, every entry must be a JSON object, and
other types are refused (`未知告警通道类型 'snmp'（支持 webhook、syslog）`). The message and payload formats, with
examples, are in [api.md](api.md#syslog). The Webhook sink uses Python's `urllib`, which honours `http_proxy`,
`https_proxy` and `no_proxy`: if the EMS has a proxy configured, add internal receivers to `no_proxy` in the
environment of `serve`.

Sinks are best effort. Every alarm is registered (dashboard, API, `/metrics`) before it is written and sent, so a
failing sink never stops an alarm from being raised or cleared: a failed write to `alarm_log.csv` or a sink that
raises an error adds 1 to `errors` in `/api/status` and prints
`告警 <id> raise|clear 写入/外发失败（<sink>）：<error>` to standard error, and the other sinks still receive the
record. A Webhook POST that fails in the background thread prints `Webhook 投递失败（<url>）：<error>`.
`alarm_log.csv` is the complete record of every alarm.

### Prometheus

```yaml
scrape_configs:
  - job_name: edgefdl
    static_configs:
      - targets: ["127.0.0.1:8460"]
```

The metrics are served at `/metrics`, Prometheus's default path. `edgefdl_alarms_active` counts the active alarms,
`edgefdl_alarm{id,type,link,severity}` has one series per active alarm, and `edgefdl_link_health{link}` gives the
health of every link; all metrics are listed in [api.md](api.md#prometheus-metrics). If Prometheus runs on another
host, see [section 6](#6-security) before binding `serve` to a non-local address.

### CSV logs

`serve` writes `measure_log.csv` (one row per probe result, bandwidth estimate, LSDB entry and deep probe) and
`alarm_log.csv` (one row per alarm raise and clear) into `--log-dir`, each with a `*.schema.json` column definition
and a `schema_version` column, so other systems can import them without reading the code. Rows are flushed line by
line; the measurement log is fsynced every 5 s and the alarm log on every write. At 200 MB a file is renamed with a
timestamp suffix and a new one started; the 20 most recent archives are kept. The columns are documented in
[api.md](api.md#csv-logs), and both current files can be downloaded from `/api/export/`.

## 6. Security

- **Read-only towards devices.** EdgeFDL never logs in to a production device and never writes device configuration.
  The vendor commands in an alarm are advice for the operator and are not executed. Commands such as `tc`, `ip`,
  `vtysh` and `lxc` act only on the equivalent virtual network.
- **Disable fault injection.** Start every deployment that faces a real network with `--no-inject`; `POST
  /api/inject` and `/api/clear` then return 403.
- **Local by default.** The dashboard and API have no authentication and no TLS, and `serve` binds to 127.0.0.1
  unless `--host` says otherwise. To reach them from elsewhere, use an SSH tunnel or a reverse proxy that adds
  authentication and TLS, or bind to an address of a management network that a firewall restricts.
- **Cross-site requests.** A web page open in an operator's browser cannot drive the API: `POST` requests must carry
  `Content-Type: application/json` (otherwise 415), which a browser sends cross-site only after a preflight that
  the server does not answer, and a `POST` with an `Origin` header that differs from the `Host` header is refused
  (403). While `serve` listens on a loopback address, it answers only requests whose `Host` is `localhost`,
  `127.0.0.1`, `::1` or the bound address, with any port (403 otherwise), which blocks DNS rebinding. With `--host`
  set to another address (including `0.0.0.0`) the `Host` header is not checked; the `Origin` and content-type rules
  still apply. A reverse proxy in front of a loopback-bound `serve` must pass the upstream `Host` (nginx's default
  `proxy_set_header Host $proxy_host`, that is `127.0.0.1:8460`); otherwise every request is refused. A browser
  `POST` through such a proxy carries the proxy's public origin and is refused too, so fault injection is available
  only on the EMS itself or through an SSH tunnel.
- **Model bundles.** Bundles are loaded with `torch.load(..., weights_only=True)`, which accepts only tensors and
  basic Python types, so loading a bundle does not run code from it; a bundle with other objects is refused with an
  error. A bundle still defines the topology, baseline and weights the engine works with, so deploy only bundles you
  trained or obtained from this repository's releases.
- **Trusted operator input.** Topology files, intake forms and device configurations shape the commands run by the
  `netns` and `lxd` backends, which run with root or LXD privileges. Names, IDs and addresses are validated when a
  topology is loaded (section 2); still review files received from others before use.
- **Logs describe the network.** The logs contain addresses, device names, paths and alarm evidence. Treat them as
  internal network documentation and anonymise them before sharing them in an issue.

What the process reads, writes, executes and sends:

| | `sim` backend | `netns` / `lxd` backends |
|---|---|---|
| Reads | The model bundle (`serve`), topology, intake files, baselines and sample sets given on the command line | The same |
| Writes | `--log-dir` (`serve`), the `--out` / `-o` paths of the offline commands | Also FRR files under `/etc/frr/efl-*` and `/var/run/frr/efl-*` (`netns`), or a temporary staging directory that is pushed into the containers and then removed (`lxd`) |
| Executes | No external programs | `ip`, `tc`, `sysctl` (per-namespace keys only), `ping`, `traceroute`, `iperf3`, `python3 -c` with the packet-train probe of [`edgefdl/measure/probes.py`](../edgefdl/measure/probes.py), FRR `zebra`, `ospfd`, `vtysh`; `lxc` for LXD |
| Network | The HTTP port of `serve`; the configured Syslog and Webhook receivers; the `intake --llm` endpoint if given | Also the probe traffic of the measurement plan |

There is no telemetry and no update check. The full statement, with the environment variables read, is in
[SECURITY.md](../SECURITY.md#data-handling-statement), which also explains how to report a vulnerability.

## 7. Checklist before going live

In 0.1.2 the checklist applies to the service running on the site's equivalent network, and it is the starting
point for the connection to the physical network on the roadmap.

- [ ] `topo` shows the expected devices and links and lists the blind links. The virtual backends emulate their NQA
      / IP SLA probes; on the physical network these probes are to be configured on the devices, and reading their
      results over SNMP is planned for 0.3.
- [ ] `eval_test.json` and `bench/bench.json` of the site model have been reviewed against [Results](results.md).
- [ ] `serve` starts with `--no-inject`, `--learn-rounds` of at least 120, and a `--log-dir` on a disk with room for
      two logs of up to about 4.1 GiB each.
- [ ] The dashboard and API are reachable only through a tunnel, an authenticating reverse proxy (passing the
      upstream `Host`) or a restricted management network.
- [ ] A test alarm reached the Syslog or Webhook receiver (for example on a `sim` instance of the same model with
      injection enabled), and Prometheus scrapes `/metrics`.
- [ ] The people on call know how to read an alarm: the link and its interfaces, the evidence, the remediation steps
      and the rehearsal result ([quick start, section 3](quickstart.md#3-inject-a-fault-and-read-the-alarm)).

## 8. Scope of v0.1 and next steps

- In 0.1.2, `serve` takes its measurements from one of the three backends, which all realise the equivalent
  virtual network. The measurement data model and the CSV schema are shared by all backends, and the ping and
  traceroute commands with their parsers run on the EMS, so they carry over to production devices unchanged;
  packet trains and deep probes run between the two ends of a link and need a probe endpoint there. Running the
  engine against a production network is planned in steps on the [roadmap](../ROADMAP.md): the LXD backend on hardware,
  interconnected with a physical network through the EMS uplink (0.2); passive OSPF listening and reading NQA / IP SLA
  results over SNMP (0.3); a field deployment on a production edge network (1.0).
- Accuracy and robustness figures come from the simulation backend on the demo topology; the `netns` backend is
  validated by smoke tests; for the `lxd` backend the generated commands are verified (dry run) and have not yet
  been run on hardware.
- A 72-hour real-time run of `serve` is planned; the 72-hour figure in the self-test is simulated time. Kylin OS and
  openEuler support is planned.
- Light probes do not detect one-way loss below about 3% or bandwidth still above about 70% of the baseline. With
  routers dropping 5% of ICMP replies, some 15% loss faults in the online scenarios were missed on L8 and L10 (each
  crossed by a single monitored path) or attributed to a neighbouring link (L2 reported as L7). A fault behind the
  EMS's own uplink cannot be told apart from a fault of the uplink itself.
- Heavy loss (≥30%) makes real OSPF adjacencies flap, which the simulator does not model yet.
- One model per topology; transfer across topologies is planned, not validated.
- The real-stack backends need Linux-compatible interface names in the topology (section 3), so a topology built
  from vendor configurations needs a copy with short names for them.
- Alarms are sent as Syslog, Webhook and Prometheus metrics; SNMP traps are not implemented. The dashboard, CLI
  messages and alarm texts are in Chinese.
