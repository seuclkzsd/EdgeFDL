# Security policy

## Supported versions

| Version | Security fixes |
|---|---|
| 0.1.x | Yes, in the latest 0.1 patch release (currently 0.1.2); please upgrade from earlier 0.1 releases |
| Earlier snapshots (before the first release) | No, please upgrade to the latest 0.1.x |

## Reporting a vulnerability

**Please do not disclose vulnerability details in public issues, pull requests or discussions.** Report them
privately:

1. **GitHub private vulnerability reporting**:
   <https://github.com/seuclkzsd/EdgeFDL/security/advisories/new> (repository page, Security, Report a
   vulnerability; only you and the maintainers can see the report). GitHub offers this form for public repositories
   only; the maintainers switch it on when the repository is made public, and until then the link does not open a
   form.
2. **If the form is not available**, open an issue titled "Security contact request" that contains no details of the
   problem, or use a contact method shown on the GitHub profile of the maintainer listed in
   [MAINTAINERS.md](MAINTAINERS.md), <https://github.com/seuclkzsd>. The maintainer replies within 3 business days
   with a private channel for the report.

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
log directory.

The server defends the browser of an operator against cross-site request forgery (CSRF) and DNS rebinding:
`POST` requests must carry `Content-Type: application/json` (HTTP 415 otherwise), which browsers send cross-site
only after a CORS preflight that the server does not answer; a `POST` whose `Origin` header differs from its `Host`
header, including `Origin: null`, is refused with 403; while the server listens on a loopback address, it answers
only requests whose `Host` names `localhost`, `127.0.0.1`, `::1` or the bound address (any port), so a page served
from an attacker's domain that resolves to 127.0.0.1 gets 403. With `--host` set to a non-loopback address the
`Host` check is off and the other checks remain. Request bodies are limited to 65,536 bytes, and fault values are
validated ([docs/api.md](docs/api.md#post-apiinject-and-post-apiclear)). In scope: requests that reach other files,
execute code, change engine state without the inject endpoints, stop the measurement loop, or get past these
cross-site checks from a browser.

**Model bundles.** A model bundle (`model.pt`) is loaded with `torch.load(..., weights_only=True)` (PyTorch 2.0 or
newer), for both inference engines (`--engine torch` and `--engine numpy`). In this mode PyTorch restores only
tensors and basic Python types; a bundle that contains other objects is refused with an error, so loading a bundle
does not execute code from it. A bundle still determines the topology, the baseline and the weights the engine
works with: deploy only bundles that you trained yourself or obtained from this repository or its GitHub Releases.
Sample sets (`.npz`) are read with `numpy.load(..., allow_pickle=False)`. In scope: any bundle or sample set that
executes code when loaded, and any way to make EdgeFDL load a bundle the operator did not choose.

**Parsing of operator input.** Topology files and intake forms are read with `yaml.safe_load`; device
configurations and LLDP neighbour tables (CSV) are parsed as text with regular expressions and the `csv` module;
responses of the optional multimodal endpoint (`intake --llm`) are parsed as JSON only. When a topology is loaded,
device names and link IDs are restricted to letters, digits, `_`, `.` and `-` (interface names also `/` and `:`),
addresses and link values are checked, and duplicate names, IDs, interface names and addresses are refused. In
scope: inputs that cause code execution, unbounded memory or CPU use, or that silently produce a topology different
from the file (for example a link attached to the wrong device).

**Command construction for `ip`, `tc`, `vtysh`, `lxc` and the probe tools.** All external commands are built as
argument lists and run with `subprocess` without a shell, so topology fields are passed as single arguments and
shell metacharacters are not interpreted. Device names, interface names and addresses from the topology also
appear in FRR configuration files, in `vtysh -c` lines and in directory names (`/etc/frr/efl-<device>/`,
`/var/run/frr/efl-<device>/` for `netns`; a temporary staging directory and `lxc file push` for `lxd`). Besides the
validation above, the FRR configuration generator refuses fields that contain line breaks or control characters,
the real-stack backends require valid Linux interface names, and the `lxd` backend requires valid LXD instance names
and keeps its staging files inside the staging directory. Treat topology and intake files as trusted operator input
and review files received from others before running the `netns` or `lxd` backends, which run with root or LXD
privileges. In scope: any topology field that leads to a command other than the documented ones, extra FRR or vtysh
configuration lines, or files written outside these directories.

**CSV and log injection.** `measure_log.csv`, `alarm_log.csv`, Syslog messages and Webhook payloads contain device
names, link identifiers and alarm text taken from the topology. CSV files are written with Python's `csv` module,
and text cells that start with `=`, `+`, `-`, `@`, a tab or a carriage return (and are not plain numbers) get a
leading single quote, so that spreadsheet programs show them as text. In scope: fields that break the documented
column schema, forge log records or Syslog fields, or are executed as formulas when the exported CSV is opened in a
spreadsheet program.

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
  (`--baseline`); with `intake --llm`, the topology images (PNG, JPEG) to be extracted. When psutil is installed,
  the process's own CPU and memory statistics.
- **Writes:** the log directory of `serve` (`--log-dir`, default `runs/live`): `measure_log.csv`, `alarm_log.csv`,
  their `*.schema.json` files and rotated archives (rotated at 200 MB); the output directories given with `--out`
  or `-o` to `intake`, `baseline`, `gen`, `train`, `eval`, `bench`, `robust` and `pipeline`. The `netns` backend
  writes FRR configuration and PID files under `/etc/frr/efl-<device>/` and `/var/run/frr/efl-<device>/`; the `lxd`
  backend writes FRR files to a temporary staging directory, pushes them into its containers and removes the
  directory (`python -m edgefdl.vnet.lxd --stage-dir DIR` keeps them in `DIR` instead).
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
  which receives the base64-encoded image. There is no telemetry and no update check.
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
| `lxd` backend | A member of the `lxd` group |
| `vnet up` / `vnet down` with `--dry-run` (`netns` or `lxd`) | An ordinary user: the command sequence is printed and nothing is executed (the `lxd` dry run generates the FRR files in a temporary directory and removes it). `python -m edgefdl.vnet.lxd` prints the complete LXD deployment sequence the same way |
