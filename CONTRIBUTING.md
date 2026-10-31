# Contributing to EdgeFDL

Thank you for helping to improve EdgeFDL. This guide explains what you can contribute, how to set up a development
environment, which checks to run before a pull request, where tests go, and what extra steps apply to changes that
affect the published accuracy and robustness numbers. Decision rules and response targets are in
[GOVERNANCE.md](GOVERNANCE.md). All participants follow the [Code of Conduct](CODE_OF_CONDUCT.md). Please report
security problems privately as described in [SECURITY.md](SECURITY.md).

## What you can contribute

**Code.** The package is organized by pipeline stage ([docs/architecture.md](docs/architecture.md)):

| Module | Contents | Good places to start |
|---|---|---|
| `edgefdl/intake/` | Intake form, extraction of devices and links from device configurations and LLDP tables | Parsers for further vendors' configuration syntax, clearer error messages |
| `edgefdl/topology.py` | Topology model, input validation, IP plan, OSPF shortest paths, monitored targets, coverage planning | Parallel links between two devices, further IP-plan checks |
| `edgefdl/vnet/` | Equivalent virtual network: `sim`, `netns`, `lxd` backends, FRR configuration; the read-only `live` backend for real networks | Running the `lxd` backend on hardware, more real-stack checks, LSDB adapters for vendor routers, field reports from `live` |
| `edgefdl/faults/` | Fault model, injector (`tc`, `ip`, `vtysh` commands), sample generator | New fault types: **open an issue first** (see below) |
| `edgefdl/measure/` | Measurement data model, probe commands and parsers, CSV store | Readers for SNMP and device-native NQA / IP SLA results |
| `edgefdl/agent/` | Features, DDQN agent, inference engines, remediation knowledge | Vendor command templates in `knowledge.py`; changes to features, actions or rewards: **open an issue first** |
| `edgefdl/core/` | Detector, orchestration engine, alarm sinks, HTTP API, dashboard | New alarm sinks, dashboard improvements; changes to the detector or the engine: **open an issue first** |
| `edgefdl/bench.py`, `edgefdl/robust.py`, `scripts/` | Self-test, robustness harness, comparison script, real-stack smoke test | New robustness conditions, scenario coverage |

Issues suitable for first-time contributors carry the label `good first issue`; see also
[Your first contribution](#your-first-contribution).

**Contributions that need no code are just as valuable:**

- **Field reports from real edge networks.** Tell us how EdgeFDL handled a network you operate: whether `intake` and
  coverage planning described it correctly, how a model trained on its topology performed and, when you run it next
  to the network (`serve --backend live`, read-only), what it reported and what
  actually happened, whether it was right or wrong. Use the issue template "Report from a real network"; the
  requirements are below.
- **Topologies.** Anonymized topologies of real campus, branch or factory networks, in the format of
  [configs/demo_topology.yaml](configs/demo_topology.yaml). They let us test coverage planning, training and
  inference on structures other than the demo topology.
- **Vendor command templates.** Alarms carry troubleshooting and remediation commands per vendor, defined in the
  `CMDS` table of [edgefdl/agent/knowledge.py](edgefdl/agent/knowledge.py) (Huawei VRP, H3C Comware and a Linux/FRR
  fallback today). A new vendor is one entry with the same keys; see "Adding a vendor command template" below.
  Network engineers can submit these as a pull request or paste them into a feature request.
- **Translations.** The dashboard ([edgefdl/core/web/index.html](edgefdl/core/web/index.html)), the CLI help and the
  code comments are in Chinese. English translations of
  UI strings, help texts and comments are welcome; an English/Chinese UI is planned in [ROADMAP.md](ROADMAP.md).
- **Documentation**: corrections, clearer explanations and examples for [docs/](docs/index.md).

## Development environment

EdgeFDL needs Linux, Python 3.8 or later and PyTorch 2.0 or later (the published results were produced with Python
3.13 and PyTorch 2.9.1, a CUDA 12.8 build run on the CPU). No GPU is needed.

```bash
git clone https://github.com/seuclkzsd/EdgeFDL.git && cd EdgeFDL
python -m venv .venv && source .venv/bin/activate
pip install torch --index-url https://download.pytorch.org/whl/cpu   # optional: the smaller CPU-only PyTorch
pip install -r requirements.txt
pip install -r requirements-dev.txt                                  # optional, Python >= 3.11: ruff, coverage.py, build
python -m unittest discover -s tests
```

The unit tests use the `sim` backend only and need neither root, Docker nor network access. For the full offline
pipeline (`python -m edgefdl pipeline --out runs/<name> --workers 16`, about 15 minutes on CPU) and the robustness
suite (`python -m edgefdl robust --workers 40`, about 3.5 minutes with 40 workers) see
[docs/quickstart.md](docs/quickstart.md).
Everything these commands generate goes to `runs/`, which git ignores.

The real protocol stack (Linux network namespaces, FRR OSPF, `tc`) runs in a privileged Docker container, so the host
needs Docker and membership in the `docker` group, but no `sudo`:

```bash
scripts/lab.sh build     # Debian 12 image with FRR, iperf3, traceroute, iproute2;
                         # EDGEFDL_APT_MIRROR=<host> uses a local Debian mirror
scripts/lab.sh smoke     # builds the demo network, injects each fault type, 26 checks
```

## Your first contribution

Each of these is small, needs no knowledge of the agent and can be checked on a laptop without network hardware. The
[label set](.github/labels.yml) marks more of them with `good first issue` and `help wanted`; if none is open, pick one
from this list and say so in a new issue, so that nobody does the same work twice.

1. **Add the Cisco IOS commands to the alarm advice.** Intake already reads Cisco IOS configurations, but `CMDS` in
   [edgefdl/agent/knowledge.py](edgefdl/agent/knowledge.py) has no `cisco` entry, so such devices get the Linux/FRR
   fallback. Follow [Adding a vendor command template](#adding-a-vendor-command-template) and run the advice tests:

   ```bash
   python -m unittest tests.test_engine.RehearsalTest
   ```

2. **Try your own topology.** Copy the demo topology, change devices, links and speeds to resemble a network you
   know, and let EdgeFDL check it and print the monitored targets and the coverage plan. An error message that does not
   tell you what to fix is a good first issue in itself.

   ```bash
   mkdir -p runs && cp configs/demo_topology.yaml runs/my_topology.yaml
   python -m edgefdl topo runs/my_topology.yaml
   ```

3. **Translate one piece of the interface.** The help of a subcommand (`python -m edgefdl serve --help`) or a panel of
   the dashboard ([edgefdl/core/web/index.html](edgefdl/core/web/index.html)) is a good size. Show the Chinese and
   English strings in the pull request; the bilingual UI of the [roadmap](ROADMAP.md) will reuse them.
4. **Walk through the quick start and report what stopped you.** Run `docker compose up` or
   [docs/quickstart.md](docs/quickstart.md) on a clean machine and open an issue, or a pull request for the page, for
   each step that was wrong or unclear. Name your operating system and versions.
5. **Add a real-stack check.** Add one more assertion to [scripts/netns_smoke.py](scripts/netns_smoke.py), for example
   on an interface counter after a fault, with its `check(name, ok, detail)` helper
   ([Real-stack checks](#real-stack-checks)), and run `scripts/lab.sh smoke`.

Run `tools/dev/ci.sh quick` before you open the pull request; it takes about half a minute.

## Checks before a pull request

| Check | Command | When |
|---|---|---|
| Unit tests | `python -m unittest discover -s tests` | Always |
| SPDX license headers | `python tools/dev/spdx.py` (`--fix` adds missing headers) | Always |
| Quick CI suite: SPDX headers, dependency licenses, ruff, unit tests, online check | `tools/dev/ci.sh quick` | Always |
| Lint | `tools/dev/ci.sh lint` (`python -m ruff check .`, configured in `pyproject.toml`) | Always; part of the quick suite |
| Licenses of the runtime dependencies | `python tools/dev/licenses.py` ([docs/dependencies.md](docs/dependencies.md)) | Always; part of the quick suite |
| Test coverage | `tools/dev/ci.sh coverage` (reports the total, no threshold) | When adding code |
| Container image | `tools/dev/ci.sh image` (needs Docker with the compose plugin) | When changing `Dockerfile`, `compose.yaml`, `.dockerignore`, `requirements-lock.txt` or the `serve` command |
| Real-stack smoke test | `scripts/lab.sh smoke` | When changing `edgefdl/vnet/`, `edgefdl/faults/injector.py` or `edgefdl/measure/probes.py` |
| LXD command sequence | `python -m edgefdl.vnet.lxd configs/demo_topology.yaml --uplink ems=enp3s0` (dry run; review the printed commands) | When changing `edgefdl/vnet/lxd.py` or `edgefdl/vnet/stack.py` |
| Robustness criteria | See "Changes that affect accuracy or robustness" | When changing detection, diagnosis or alarm logic |

CI is defined in [.github/workflows/ci.yml](.github/workflows/ci.yml) and runs [tools/dev/ci.sh](tools/dev/ci.sh). If
this table and CI disagree, CI is authoritative; please open an issue so that this section can be corrected.

Every new Python or shell file starts with the two SPDX lines that `tools/dev/spdx.py` checks:

```python
# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
```

**Code style.** Follow the surrounding code: PEP 8 naming, 4-space indentation ([.editorconfig](.editorconfig)),
type hints on public functions, no new runtime dependencies without discussion. Library code uses seeded random
number generators so that every result can be reproduced. Comments and docstrings may be written in English or
Chinese; keep user-facing strings of the dashboard and the CLI consistent with the existing ones until the
bilingual UI lands.

## Commit messages

Use `<area>: <what and why>`:

```text
core: require own-link evidence before a secondary alarm so that one fault cannot raise two alarms
```

- `<area>` is the module, directory or file you changed, for example `intake`, `topology`, `vnet`, `faults`,
  `measure`, `agent`, `core`, `web`, `api`, `bench`, `robust`, `scripts`, `tests`, `docs`, `results`, `ci`, `deps`,
  `image`, `readme`, `changelog`.
- The first line says what changed and why in one sentence.
- The body explains the motivation and how you verified the change: the checks you ran and, for any number you
  quote, its conditions (backend, topology, seed, sample count, CPU, Python and PyTorch versions).
- One logical change per commit; every commit passes the unit tests.

## Changes that affect accuracy or robustness

The detector, the diagnosis and confirmation logic, the multi-alarm handling, the adaptive baseline, the features,
actions and rewards of the agent, the fault semantics and the sample generator together determine the numbers
published in [README.md](README.md), [docs/results.md](docs/results.md) and [results/](results/README.md). For
such changes:

1. **Open an issue first** describing the problem and the proposed approach, and wait for a maintainer to agree on
   the direction.
2. **Add a test** in `tests/` that fails before the change and passes after it. If the change should not alter
   behaviour, add a test showing that it does not.
3. **Re-run the robustness criteria.** Run the robustness suite with the default engine and with `--single-alarm`
   on seeds 21, 33 and 47, then compare:

   ```bash
   for s in 21 33 47; do
     python -m edgefdl robust --model results/model.pt --out runs/check/multi-$s --seed $s --only online
     python -m edgefdl robust --model results/model.pt --out runs/check/single-$s --seed $s --only online --single-alarm
   done
   python scripts/compare_robust.py --multi runs/check/multi-{21,33,47}/robust.json \
                                    --single runs/check/single-{21,33,47}/robust.json
   ```

   The comparison must print `ALL PASS`; paste its output into the pull request. Use the model you retrained in
   step 4 instead of `results/model.pt` when the change requires retraining.
4. **Regenerate the published results** when the change alters any published number. Re-run the pipeline if
   features, actions, rewards, fault semantics or the sample generator changed, then the robustness suite with its
   default seed:

   ```bash
   mkdir -p runs/<name>
   python -m edgefdl pipeline --out runs/<name> --workers 16 --no-bench | tee runs/<name>/pipeline.log
   python -m edgefdl bench --model runs/<name>/model.pt --out runs/<name>/bench --rounds 51840
   python -m edgefdl robust --model runs/<name>/model.pt --out runs/<name>/robust --workers 40
   ```

   The published self-test is the 72-hour-equivalent run (51,840 rounds); `pipeline` alone would run a 24-hour one.
   Copy the outputs into `results/` (`model.pt`, `baseline.json`, `eval_test.json`, `eval_test_rand.json`,
   `bench/bench.json` as `bench.json`, `robust/robust.json` as `robust.json`, and the console output of the
   pipeline, `runs/<name>/pipeline.log`), update the tables in README.md, README.zh-CN.md and docs/results.md from
   these files, and record the change in [CHANGELOG.md](CHANGELOG.md). Numbers that cannot be reproduced from committed raw data do
   not go into the documentation ([GOVERNANCE.md](GOVERNANCE.md)).
5. Changes to the measurement commands or fault injection also need `scripts/lab.sh smoke` to pass. Changes to the
   simulator or the real-stack backends also re-run
   `scripts/lab.sh run python3 scripts/sim_vs_stack.py --out results/sim_vs_stack.json`.

## Where to add things

### Unit tests

Tests use the standard library's `unittest` and are discovered from `tests/` (`test*.py`). The suite is grouped by
area: [tests/test_core.py](tests/test_core.py) has `TopologyTest`, `SimTest`, `StoreTest`, `AgentTest`,
`EngineTest`, `IntakeTest`, `ResidualTest` and `MultiAlarmEngineTest`; [tests/test_engine.py](tests/test_engine.py)
has `SinkTest`, `JsonSafeTest`, `ApiTest` and `RehearsalTest`; [tests/test_infra.py](tests/test_infra.py) has
`TopologyValidationTest`, `VnetDryRunTest`, `CsvLogTest`, `ReproducibilityTest`, `BundleLoadTest` and
`IntakeInfraTest`. Tests that need the released model read `results/model.pt`. Add a test to the matching class,
or create `tests/test_<area>.py` for a new area. Tests use the `sim` backend with fixed seeds, must not need root,
Docker or network access, and should finish in seconds. Run a single class with
`python -m unittest tests.test_core.EngineTest`.

### Real-stack checks

Checks against the real protocol stack go into [scripts/netns_smoke.py](scripts/netns_smoke.py) via its
`check(name, ok, detail)` helper and run with `scripts/lab.sh smoke`. Logs of reference runs are kept in
`results/smoke/`.

### Adding a vendor command template

1. Add an entry to `CMDS` in [edgefdl/agent/knowledge.py](edgefdl/agent/knowledge.py). The key is the `vendor` value
   used in topology files (for example `huawei`, `h3c`); the entry defines every key of the existing entries
   (`if_status`, `if_err`, `optic`, `peer`, `route`, `qos`, `car`, `nego`, `changes`, `device`, `log`, `set_cost`,
   `up`) with the placeholders `{ifn}`, `{ip}` and `{cost}`. Separate the steps of a configuration sequence with
   ` ; `, as the existing entries do.
2. Use only read-only display commands for diagnosis keys; the configuration keys (`set_cost`, `up`) are shown as
   advice and never executed by EdgeFDL.
3. Name the device models and software versions you verified the commands on in the pull request.
4. Add a test that builds the advice for a device of the new vendor and checks that every key is present.

### New fault types

A new fault type changes the action space of the agent and therefore requires retraining. Describe in the issue:
its semantics (what is applied to which interfaces), the injector commands for `netns` and `lxd`, its behaviour in
the `sim` backend, the value range for sample generation, and how the alarm and remediation advice should read. The
pull request then follows "Changes that affect accuracy or robustness" in full, including a real-stack check.

## Field reports and topologies from real networks

Use the issue template "Report from a real network". Useful reports state:

- the network: type (campus, branch, factory), number of routers, switches and hosts, link speeds, vendors and
  software versions, routing protocol;
- how EdgeFDL ran: version, backend, how the EMS is attached, whether on-site baseline learning was used
  (`serve --learn-rounds`), how long it ran;
- what happened in reality (ground truth: which link or device failed, when, and how you know) and what EdgeFDL
  reported (alarm type, link, time to alarm, time to clear), or that it stayed quiet;
- excerpts of `alarm_log.csv` and `measure_log.csv` around the event.

**Remove sensitive data before posting**: replace device names, site names and IP addresses consistently (the same
real value always becomes the same placeholder), and delete passwords, SNMP communities, keys and user names from
configurations. Post only data that the owner of the network allows you to share. Anonymized topologies you
contribute are published under CC-BY 4.0 or CC0 1.0, as you choose in the issue. Accepted reports and topologies are
credited in [CHANGELOG.md](CHANGELOG.md).

## Review and merge

- Maintainers respond to issues within 7 days and review pull requests within 14 days
  ([GOVERNANCE.md](GOVERNANCE.md)).
- A pull request is merged when CI passes and at least one maintainer approves. User-visible changes are recorded in
  the `[Unreleased]` section of [CHANGELOG.md](CHANGELOG.md). Changes to CLI options, REST endpoints and their JSON
  fields, Prometheus metrics, CSV log columns or the model bundle format update [docs/api.md](docs/api.md) and the
  other affected documents in the same pull request.
- Keep pull requests small and complete; discuss large changes in an issue first so that they can be split.

## Use of AI tools

You may use AI assistants to write code, tests or documentation. You remain responsible for everything you submit:

- you have read and understood every line, and you have run the checks above yourself;
- every number you quote comes from a run you performed, with its conditions;
- mention substantial AI assistance in the pull request description (one sentence is enough);
- do not add AI tools as commit authors or co-authors (no `Co-Authored-By` trailers for AI tools); commits are
  authored by the people who submit them;
- AI tools may draft reviews, but approval and merging are decisions of maintainers.

## License

By submitting a contribution you agree that it is licensed under the [Apache License 2.0](LICENSE) (section 5 of the
license). There is no contributor license agreement, and a DCO sign-off (`Signed-off-by`) is not required; you may
add one if your organization asks for it. New runtime dependencies must use an OSI-approved open-source license
and be added to `requirements.txt`, `pyproject.toml`, `requirements-lock.txt`, [NOTICE](NOTICE) and
[docs/dependencies.md](docs/dependencies.md).
