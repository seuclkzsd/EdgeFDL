# Security policy

## Supported versions

| Version | Security fixes |
|---|---|
| 0.1.x (latest release) | Yes |
| Earlier snapshots (before the first release) | No, please upgrade to 0.1.x |

## Reporting a vulnerability

**Please do not disclose vulnerability details in public issues, pull requests or discussions.** Report them
through GitHub's private vulnerability reporting:

<https://github.com/seuclkzsd/EdgeFDL/security/advisories/new>

(Repository page, Security, Report a vulnerability. Only you and the maintainers can see the report.)

Please include, as far as you can:

- the affected version or commit (`python -m edgefdl --version`) and how EdgeFDL was installed;
- the operating system, Python and PyTorch versions, and the backend in use (`sim`, `netns` or `lxd`);
- a minimal reproduction: the command line, the HTTP request, or the input file (topology, intake form, device
  configuration, LLDP table, model bundle) that triggers the problem, with real addresses and names replaced;
- the impact you observed: a command that should not have run, a file written outside the expected directories,
  configuration lines injected into FRR, data disclosed, a crash or resource exhaustion of the engine.

## Response times

| Stage | Target |
|---|---|
| Acknowledge the report | Within 3 business days |
| Initial assessment (confirmed or not, severity, plan) | Within 10 business days |
| Fix or mitigation released (high or critical severity) | Within 30 days; for harder problems we report progress and reasons in the advisory thread |

After a fix is released we publish a GitHub Security Advisory and, with the reporter's consent, credit the reporter.

## Scope

EdgeFDL runs on the network-management server (EMS) of an edge network. It reads files prepared by the operator,
sends measurement probes, runs network tools inside the equivalent virtual network, and serves a dashboard and a
REST API. The following are security issues and should be reported privately.

**HTTP API and dashboard (`python -m edgefdl serve`).** The server has no authentication and no TLS. It binds to
`127.0.0.1:8460` by default (`--host`, `--port`). Expose it beyond the local machine only behind a reverse proxy that
adds authentication and TLS, or on a management network restricted by a firewall. `POST /api/inject` and
`POST /api/clear` inject and clear faults in the equivalent virtual network for demonstrations. They are enabled by
default on all three backends (`sim`, `netns`, `lxd`), and `--no-inject` disables both (they then answer HTTP 403).
**Start every deployment that is connected to a real network, for example an `lxd` deployment with a bridged
uplink, with `--no-inject`.** `GET /api/export/...` serves only `measure_log.csv` and `alarm_log.csv` from the
log directory. In scope: requests that reach other files, execute code, change engine state without the inject
endpoints, or stop the measurement loop.

**Model bundles.** A model bundle (`model.pt`) is loaded with `torch.load(..., weights_only=False)`. **Loading a
bundle unpickles it, and unpickling can execute arbitrary code.** Both inference engines (`--engine torch` and
`--engine numpy`) read bundles this way. Load only bundles that you trained yourself or obtained from this
repository or its GitHub Releases, and never load a bundle received from an untrusted party. Sample sets (`.npz`)
are read with `numpy.load(..., allow_pickle=False)`. In scope: any way to make EdgeFDL load a bundle the operator
did not choose. Not in scope: code execution from a bundle that the operator chose to load (documented behaviour).

**Parsing of operator input.** Topology files and intake forms are read with `yaml.safe_load`; device
configurations and LLDP neighbour tables (CSV) are parsed as text with regular expressions and the `csv` module;
responses of the optional multimodal endpoint (`intake --llm`) are parsed as JSON only. In scope: inputs that
cause code execution, unbounded memory or CPU use, or that silently produce a topology different from the file
(for example a link attached to the wrong device).

**Command construction for `ip`, `tc`, `vtysh`, `lxc` and the probe tools.** All external commands are built as
argument lists and run with `subprocess` without a shell, so topology fields are passed as single arguments and
shell metacharacters are not interpreted. Device names, interface names and addresses from the topology also
appear in FRR configuration files, in `vtysh -c` lines and in directory names (`/etc/frr/efl-<device>/`,
`/var/run/frr/efl-<device>/` for `netns`; a staging directory and `lxc file push` for `lxd`). Treat topology and
intake files as trusted operator input and review files received from others before running the `netns` or `lxd`
backends, which run with root or LXD privileges. In scope: any topology field that leads to a command other than
the documented ones, extra FRR or vtysh configuration lines, or files written outside these directories.

**CSV and log injection.** `measure_log.csv`, `alarm_log.csv`, Syslog messages and Webhook payloads contain device
names, link identifiers and alarm text taken from the topology. CSV files are written with Python's `csv` module.
In scope: fields that break the documented column schema, forge log records or Syslog fields, or are executed as
formulas when the exported CSV is opened in a spreadsheet program.

**Out of scope** (please use a public issue): wrong, missed or late alarms and accuracy differences (use the bug
report template, or the field report template for real networks); the volume of the probe traffic of the
measurement plan (measured figures in [docs/results.md](docs/results.md)); the `--privileged` lab
container of `scripts/lab.sh`, which is a development tool and not a deployment mode; problems that require an
attacker who can already write to the EMS's files or run commands as the EdgeFDL user. Vulnerabilities in
third-party components (PyTorch, NumPy, PyYAML, FRRouting, iproute2, LXD and others listed in [NOTICE](NOTICE))
should be reported to their upstream projects; please tell us as well if EdgeFDL's use of them is affected.

## Data handling statement

EdgeFDL is read-only towards production devices: it never logs in to devices and never writes device
configuration. Vendor commands in alarms are advice for the operator and are not executed.

- **Reads:** the files given on the command line: topology (`--topo`), intake form (`--form`), device
  configurations (`--configs`), LLDP table (`--lldp`), model bundle (`--model`), sample sets (`--data`), baseline
  (`--baseline`); with `intake --llm`, the topology image or PDF to be extracted. When psutil is installed, the
  process's own CPU and memory statistics.
- **Writes:** the log directory of `serve` (`--log-dir`, default `runs/live`): `measure_log.csv`, `alarm_log.csv`,
  their `*.schema.json` files and rotated archives (rotated at 200 MB); the output directories given with `--out`
  or `-o` to `intake`, `baseline`, `gen`, `train`, `eval`, `bench`, `robust` and `pipeline`. The `netns` backend
  writes FRR configuration and PID files under `/etc/frr/efl-<device>/` and `/var/run/frr/efl-<device>/`; the `lxd`
  backend writes FRR files to a staging directory and pushes them into its containers.
- **Executes:** with the `sim` backend, no external programs. With `netns` or `lxd`: `ip`, `tc`, `sysctl` (only
  per-namespace keys), `ping`, `traceroute`, `iperf3`, `python3 -c` with the packet-train sender and receiver
  shipped in `edgefdl/measure/probes.py`, the FRR daemons `zebra` and `ospfd`, `vtysh`, and for `lxd` the `lxc`
  client (which runs `systemctl restart frr` inside the containers).
- **Network:** EdgeFDL sends only the measurement probes of its measurement plan to the targets of the configured
  topology: ICMP echo (5 packets per target per round), ICMP traceroute, short UDP packet trains, and, for a deep
  probe of a single link, 100 pings at 10 ms intervals plus a 1 s iperf3 run at no more than 30% of the link's
  bandwidth. It listens on the HTTP port of `serve`. It contacts other hosts only when the operator configures
  them: the Syslog (UDP) and Webhook (HTTP POST, sent with Python's `urllib`, which honours the standard proxy
  environment variables) sinks given with `--sinks`, and the OpenAI-compatible endpoint given with `intake --llm`,
  which receives the base64-encoded image or PDF. There is no telemetry and no update check.
- **What logs contain:** timestamps, target IP addresses, device and link names, RTT, loss, jitter and bandwidth
  values, hop-by-hop paths, OSPF adjacency state and cost, and alarms with their evidence. Together they describe
  the topology and addressing of the network: handle them as internal network documentation, and replace names
  and addresses before sharing them in issues or field reports.
- **Environment variables read:** `EDGEFDL_LLM_MODEL` and `EDGEFDL_LLM_KEY` (`intake --llm`),
  `EDGEFDL_ROBUST_SEED` (`robust`); `scripts/lab.sh` reads `EDGEFDL_LAB_IMAGE` and `EDGEFDL_APT_MIRROR`. EdgeFDL
  does not modify the environment of the calling shell.

### Privileges

| Mode | Privileges needed |
|---|---|
| `sim` backend: training, evaluation, `bench`, `robust`, `serve` in simulation | An ordinary user; no root, no system service |
| `netns` backend | Root (`CAP_NET_ADMIN`, `CAP_SYS_ADMIN`) to create namespaces, configure `tc` and run FRR; `scripts/lab.sh` runs it inside a privileged container started with `--network none` |
| `lxd` backend | A member of the `lxd` group; `--dry-run` only prints the command sequence |
