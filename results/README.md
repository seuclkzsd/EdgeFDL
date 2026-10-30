# Published results

This directory holds the released model bundle and the raw data behind every number in
[`docs/results.md`](../docs/results.md), on the demo topology
[`configs/demo_topology.yaml`](../configs/demo_topology.yaml). The test conditions (simulation backend, simulated time,
seeds, machine, the EdgeFDL version that produced the files) are listed in
[docs/results.md § Test conditions](../docs/results.md#test-conditions).

The files of the training run were produced in one run directory (`runs/v6/`, git-ignored) and copied here
unchanged; `robust.json` still records the model path of that run (`runs/v6/model.pt`), which is byte-identical to
`results/model.pt`. Some keys, labels and both log formats are in Chinese, the language of the CLI and the
dashboard; the [glossary](#glossary) below translates every one of them.

## Files

| File | What it is | Produced by |
|---|---|---|
| `model.pt` | The model bundle for the demo topology: DDQN weights plus the topology, the normal baseline and the measurement plan, in one file. `serve`, `eval`, `bench` and `robust` load it directly | `pipeline` (training step) |
| `eval_test.json` | Independent test set: 10,000 samples, standard conditions, seed 3; end-to-end, one diagnosis per sample | `pipeline`, or `gen --plain` + `eval` |
| `eval_test_rand.json` | Domain-randomized test set: 10,000 samples generated under the training-time randomization, seed 4; end-to-end, one diagnosis per sample (single-shot) | `pipeline`, or `gen` + `eval` |
| `bench.json` | Self-test: inference latency, 72-hour-equivalent noise run with overhead and resource use, 84 online scenarios | `bench --rounds 51840` |
| `robust.json` | Robustness suite: offline single-shot conditions, fault-size sweep, online long runs and scenarios under harsh conditions, multi-fault trials | `robust` |
| `baseline.json` | Normal baseline learned on the simulation backend (40 rounds, seed 1); the same baseline is embedded in `model.pt` | `baseline`, or the first step of `pipeline` |
| `pipeline.log` | Console log (Chinese) of the run: baseline, sample generation, training progress, evaluation tables of both test sets | `pipeline` |
| `smoke/run1.log` … `run3.log` | Console logs (Chinese) of three consecutive real-protocol-stack smoke tests, 26/26 checks each | `scripts/lab.sh smoke` |
| `sim_vs_stack.json` | Per-target baseline RTT of the healthy demo network on the real protocol stack (`netns`, FRR OSPF, `tc`) and on the simulator, 30 rounds each | `scripts/lab.sh run python3 scripts/sim_vs_stack.py --out results/sim_vs_stack.json` |

## Commands

Run from the repository root. The first block regenerates the published files of the training run; the second
reproduces the evaluation files from the released model without retraining (byte-identical output).

```bash
# Training run: baseline -> samples -> training -> evaluation (pipeline.log, model.pt, baseline.json, eval_*.json)
python -m edgefdl pipeline --out runs/v6 --workers 16 --no-bench
python -m edgefdl bench  --model runs/v6/model.pt --out runs/v6/bench  --rounds 51840     # bench.json
python -m edgefdl robust --model runs/v6/model.pt --out runs/v6/robust --workers 40       # robust.json
mkdir -p runs/smoke && scripts/lab.sh build && scripts/lab.sh smoke | tee runs/smoke/run1.log   # smoke/run*.log
scripts/lab.sh run python3 scripts/sim_vs_stack.py --out runs/lab/sim_vs_stack.json             # sim_vs_stack.json

# Evaluation files from the released model
python -m edgefdl baseline --rounds 40 --seed 1 -o runs/check/baseline.json
python -m edgefdl gen --baseline results/baseline.json --n 10000 --seed 3 --plain --out runs/check/test.npz
python -m edgefdl eval --model results/model.pt --data runs/check/test.npz --json runs/check/eval_test.json
python -m edgefdl gen --baseline results/baseline.json --n 10000 --seed 4 --out runs/check/test_rand.npz
python -m edgefdl eval --model results/model.pt --data runs/check/test_rand.npz --json runs/check/eval_test_rand.json
```

Without `--no-bench`, `pipeline` also runs a 24-hour (17,280-round) self-test into `runs/v6/bench/`; the published
`bench.json` is the separate 72-hour run. Baseline, sample generation and evaluation are deterministic for a given
seed, for any number of workers. Training is seeded as well (same training set, seed and thread count give the same
model), but `model.pt` was trained with EdgeFDL 0.1.0, whose training was not fully seeded, so the training run
produces a different model; the second block reproduces the evaluation files of the released one. The real-stack
files vary slightly from run to run. Runtimes are listed in
[docs/results.md § How to reproduce](../docs/results.md#5-how-to-reproduce).

## Reading the JSON files

Conventions used in all files:

- Accuracies and rates are fractions between 0 and 1, except keys ending in `_pct`, which are percentages.
- `_s` keys are seconds of simulated time (one measurement round is 5 s); `_ms` keys are wall-clock milliseconds.
- Fault types use the identifiers `link_down`, `delay`, `loss`, `jitter`, `bandwidth`, `route_drift`, plus `normal`
  (no fault) and `node_down` (device down, produced by the engine's aggregation of link-down conclusions).
- An alarm or a truth item is written as `"<link> <type>"` (for example `"L1 link_down"`) or as a pair
  `["<type>", "<link or device>"]`.

### `eval_test.json`, `eval_test_rand.json`

Output of `python -m edgefdl eval` (`Agent.evaluate`). End-to-end: samples on which the detector never woke the agent
are scored as "normal".

| Key | Meaning |
|---|---|
| `per_type.<type>.n` | Number of samples of this fault type |
| `per_type.<type>.detect_rate` | Share of samples on which the detector woke the agent (for `normal`: false-trigger rate) |
| `per_type.<type>.type_acc` | Predicted type equals the true type (for `normal`: judged normal) |
| `per_type.<type>.loc_acc` | Predicted link equals the faulty link |
| `per_type.<type>.joint_acc` | Type and link both correct |
| `per_type.<type>.detect_delay_s` | Mean time from fault onset until the agent is woken (detector trigger plus 2 observation rounds), over detected samples |
| `per_type.<type>.avg_probes` | Mean number of deep probes the agent requested |
| `per_category.<category>` | The same metrics per category (Chinese keys, see [glossary](#categories-fault-types-and-labels)); `category_acc` = predicted category equals the true category, `type_acc` still requires the exact type |
| `overall.type_acc`, `overall.joint_acc` | Over all samples; a normal sample counts as correct when it is judged normal |
| `overall.false_alarm_rate` | Share of normal samples on which a fault was named |
| `overall.false_alarm_rate_noisy` | The same, restricted to normal samples that contained a transient |
| `overall.false_trigger_rate` | Share of normal samples on which the detector woke the agent |
| `overall.miss_rate` | Share of fault samples judged normal |
| `overall.avg_probes` | Mean deep probes per sample |

### `bench.json`

Output of `python -m edgefdl bench` (`edgefdl/bench.py`).

| Key | Meaning |
|---|---|
| `latency.torch`, `latency.numpy` | Inference engine. `forward_mean_ms`, `forward_p99_ms`, `forward_max_ms`: one forward pass of the network; `diagnose_mean_ms`, `diagnose_p99_ms`: one complete diagnosis including feature computation; 2000 repetitions on a normal window |
| `latency.params` | Number of model parameters |
| `noise.rounds`, `noise.sim_hours`, `noise.noise_prob` | Length of the noise run and the per-round transient probability |
| `noise.detector_triggers` | Times the detector became active (rising edges) |
| `noise.filtered_by_agent` | Diagnoses that concluded "normal", including periodic re-checks while the detector stays active |
| `noise.false_alarms`, `noise.false_alarms_per_day` | Alarms raised although no fault was injected |
| `noise.false_alarm_exposure_pct` | Percentage of rounds with a false alarm active |
| `noise.engine_errors` | Exceptions caught by the engine's isolation |
| `noise.wall_s` | Wall-clock seconds of the noise run |
| `noise.compute_ms_per_round`, `noise.cpu_pct_of_one_core` | Process CPU time per round, and as a percentage of one core over the 5 s round |
| `noise.rss_mb` | Resident memory of the process |
| `noise.overhead.kbps` | Measurement traffic including deep probes |
| `noise.overhead.pct_of_min_link`, `noise.overhead.pct_of_uplink` | That traffic as a percentage of the narrowest link (assuming all of it crosses that link) and of the management uplink |
| `scenarios.rows[]` | One row per online scenario: `link`, `type`, `value` (fault size), `alarmed`, `type_ok`, `loc_ok`, `pred` (alarm raised, or `无告警` = no alarm), `time_to_alarm_s`, `time_to_clear_s`, `probes` (deep probes used by the alarm's diagnosis), `n_alarms` (alarms raised in the scenario, including any raised after the repair) |
| `scenarios.summary.<label>` | Per fault type and per category (Chinese labels): `n`, `type_acc`, `joint_acc`, `mean_time_to_alarm_s`, `max_time_to_alarm_s` |
| `summary` | The headline figures with Chinese keys, translated in the [glossary](#bench-summary-keys) |

### `robust.json`

Output of `python -m edgefdl robust` (`edgefdl/robust.py`). Condition names are Chinese keys, translated in the
[glossary](#condition-names).

| Key | Meaning |
|---|---|
| `config` | `model` path, model `version`, `n` (offline samples per condition), `sweep_reps` (samples per link and value in the sweep), `rounds` (rounds per long run), `dual_n`, `node_reps`, `node_dual_reps`, `seed`, `base_noise` (per-round transient probability of the online runs), `engine` (engine overrides given with `--engine`; empty = defaults) |
| `offline.<condition>` | Single-shot evaluation of fresh samples: `链路中断`, `性能劣化`, `路由漂移` = type + location accuracy of the link-down, performance-degradation and route-drift categories; `误报率` = false-alarm rate; `漏报率` = miss rate; `平均精测次数` = mean deep probes per sample; `n` |
| `sweep[]` | Fault-size sensitivity: `type`, `value` (ms for delay and jitter per direction, loss rate, remaining bandwidth share), `n`, `detect_rate`, `type_acc`, `joint_acc` |
| `online.long.<condition>` | 24-hour-equivalent run without faults: `rounds`, `sim_hours`, `detector_triggers`, `filtered_by_agent`, `false_alarms`, `false_alarms_per_day`, `false_alarm_exposure_pct` and `engine_errors` (as in `bench.json` `noise`), plus `alarms` (false alarms counted by `"<link> <type>"`) and `wall_s` |
| `online.scen.<condition>` | The 84 online scenarios: `n`, `alarmed`, `joint_acc`, `wrong` (each error as `"<link> <true type> -> <alarm>"`), `mean_time_to_alarm_s`, `max_time_to_alarm_s`, `mean_time_to_clear_s`, `time_to_clear_by_type`, `not_cleared`, `extra_alarms`, `rows` (`[link, true type, alarm, time_to_alarm_s, time_to_clear_s, n_alarms]`) |
| `online.dual`, `online.node`, `online.node_dual`, `online.single` | Multi-fault trials, each `{summary, rows, wall_s}` under one Chinese key (see below) |
| `online.node_back` | Device-recovery trials, `{summary, rows, wall_s}` |
| `wall_s` | Wall-clock seconds of the whole suite |

Multi-fault `summary` keys (fractions over all trials unless noted):

| Key | Meaning |
|---|---|
| `n` | Number of trials |
| `alarmed` | At least one alarm was raised |
| `first_ok` | The first alarm is one of the true faults (type and link or device) |
| `first_loc_ok` | The first alarm points at a faulty link or device |
| `no_wrong_alarm` | Every alarm raised is a true fault (or an accepted intermediate link-down before a device-down aggregation) |
| `all_reported` | Every true fault was alarmed within the 40-round fault window |
| `mean_time_to_alarm_s` | Mean time to the first alarm |
| `mean_alarms_per_case` | Mean number of alarms per trial |
| `extra_alarm_rate` | Share of trials with more alarms than faults |
| `wrong_per_case` | Mean number of wrong alarms per trial |
| `cross_wired` | Share of trials with an alarm that combines the type of one fault with the link of another |
| `probes_per_round` | Deep probes per round during the fault window |
| `extra_probes` | Engine-wide count of deep probes used for alarm-clearing re-checks, over all trials |
| `both_active` | Two-fault trials: both alarms active at the end of the fault window |
| `partial_clear_ok` | Two-fault trials: after repairing one fault, its alarm cleared and stayed cleared, the other alarm kept its id, and no new alarm appeared (over all trials) |
| `partial_clear_ok_given_both` | The same, over trials with `both_active` |
| `partial_kept_other` | The other alarm stayed active with the same id |
| `mean_partial_clear_s` | Mean time to clear the repaired alarm |
| `repair_new_alarms` | Alarms raised during the repair windows (count) |
| `repair_probes_per_round` | Deep probes per round during the repair window |

Multi-fault `rows[]`: `truth`, `alarms` (in the order raised), `t_first` (rounds to the first alarm), `first_ok`,
`first_loc_ok`, `no_wrong_alarm`, `all_reported`, `n_wrong`, `cross_wired`, `probes`, and for two-fault trials
`both_active`, `repaired` (the fault that was repaired), `t_partial_clear` (rounds), `stayed_clear`, `kept_other`,
`repair_new_alarms`, `repair_probes`, `repair_clears` (`[alarm, clear reason, round]`), `partial_ok`.

`node_back` summary: `n`, `node_reported` (the device-down alarm was active during the outage), `downgrade_ok` (after
the device returned, the only active alarm is the link-down of the link that stayed down, and no other alarm was
raised), `stale_node` (a device-down alarm was still active at the end), `mean_time_to_downgrade_s`. Rows: `node`,
`kept` (the link left down), `before` (alarms during the outage), `node_ok`, `new` (alarms after recovery), `end`
(active alarms at the end), `t_downgrade` (rounds), `ok`.

### `baseline.json`

The normal baseline (`Baseline` in `edgefdl/agent/features.py`). Per-target arrays follow the target order of the
measurement plan (link-interface targets in link order, then router loopbacks, then the NQA target), printed by
`python -c "from edgefdl.topology import load; print([t.id for t in load('configs/demo_topology.yaml').targets()])"`.
Per-link arrays follow the link order L1–L15.

| Key | Meaning |
|---|---|
| `rtt`, `mdev` | Median round-trip time and its deviation per target, ms |
| `loss` | Background loss rate per target (rounds with bursts excluded, capped at 5%) |
| `paths` | Most frequent router path per target as seen by traceroute (empty for the NQA target, which is probed by a device, not by the EMS) |
| `bw` | Median light packet-train bandwidth estimate per link, Mbit/s |
| `cost` | OSPF cost per link from the LSDB |
| `deep_rtt`, `deep_mdev`, `deep_bw` | Deep-probe baseline per link: RTT and deviation (ms), bandwidth (Mbit/s) |

### `model.pt`

A PyTorch checkpoint with the keys `state` (network weights), `cfg` (agent configuration), `dims` (light features per
link, deep-probe features per link, global features, number of links: `[21, 6, 8, 15]`), `drift_links` (indices of the
links on which route drift is in the action space) and `meta`: `version`, `created`, `topology`, `baseline`, `plan`
(the measurement plan), `train` (training history every 1,000 iterations) and `val` (validation-set metrics in the
`eval` format). To inspect it:

```python
import torch
ck = torch.load("results/model.pt", map_location="cpu", weights_only=True)
print(ck["meta"]["version"], ck["dims"], len(ck["meta"]["topology"]["links"]))   # ddqn-20261030-0116 [21, 6, 8, 15] 15
```

The bundle contains only tensors and basic Python types (dictionaries, lists, strings, numbers), so it loads with
`weights_only=True`, the mode in which PyTorch refuses any other object; EdgeFDL loads every bundle this way.

### `sim_vs_stack.json`

Output of [`scripts/sim_vs_stack.py`](../scripts/sim_vs_stack.py), run in the lab container.

| Key | Meaning |
|---|---|
| `description` | What the file compares (English) |
| `topology`, `topology_file`, `devices`, `links` | The topology measured |
| `rounds`, `ping_count`, `ping_interval_s`, `sim_seed` | Rounds per backend, pings per target and round, their spacing in seconds, seed of the simulator |
| `stack` | `routing` (`frr-ospf`), `ospf_convergence_s`, `kernel`, `frr` (version) of the real-stack run |
| `created` | Time of the run |
| `summary` | `targets`, `compared`, `max_abs_diff_ms`, `median_abs_diff_ms`, `mean_abs_diff_ms`, `p90_abs_diff_ms`, `worst_target`, `reachability_agree` (targets for which both backends agree on whether every round got a reply) and `reachability_agree_all` |
| `per_target[]` | `id`, `ip`, `node`, `kind` (`if`, `host`, `lo`, `nqa`), `link`, `src` (probe source: the EMS, or the device that runs the NQA probe), `stack_rtt_ms` and `sim_rtt_ms` (median over rounds of the round's mean RTT, the estimate of `Baseline.fit`), `abs_diff_ms`, `stack_rounds_reachable`, `sim_rounds_reachable`, `reachability_agrees` |

## Glossary

### Condition names

`online.long`, `online.scen` and `offline` in `robust.json` use these keys. The English names are the ones used in
[docs/results.md § Robustness](../docs/results.md#4-robustness). Unless a condition changes it, every online condition
keeps the base interference of a one-round transient in 15% of rounds; offline conditions start from the standard
conditions of the independent test set (transient probability 25% per round).

| Key | English name | Meaning |
|---|---|---|
| `基准（噪声15%）` | Base | Online base condition: a one-round transient in 15% of rounds |
| `标准条件` | Standard conditions | Offline: as the independent test set |
| `域随机化（训练条件）` | Domain randomization (training conditions) | Transients lasting 1/2/3 rounds (60/25/15%), ICMP rate limiting of 0.5–3% on router targets in 25% of samples (half of them in the baseline), larger background jitter in 45% of samples |
| `噪声30%`, `噪声50%`, `噪声75%` | Transients 30% / 50% / 75% | A new transient appears in 30/50/75% of rounds |
| `干扰持续2轮`, `干扰持续3轮` | Transients last 2 / 3 rounds | Each transient lasts 10 / 15 s |
| `目标缺测5%`, `目标缺测10%`, `目标缺测20%` | Missing targets 5% / 10% / 20% | That share of target results is missing in each round |
| `整轮缺测5%`, `整轮缺测10%` | Missing rounds 5% / 10% | That share of rounds is missing entirely |
| `ICMP限速x%（基线已含）` | ICMP rate limiting x%, in baseline | Routers drop x% of ICMP replies, already present while the baseline was learned |
| `ICMP限速x%（上线后出现）` | ICMP rate limiting x%, after deployment | Routers start dropping x% of ICMP replies after deployment |
| `背景抖动0.2ms`, `背景抖动0.5ms`, `背景抖动1ms` | Background jitter 0.2 / 0.5 / 1 ms | Per-link background jitter of that size; the baseline is re-learned on site |
| `忙时抬升0.25ms`, `忙时抬升0.5ms`, `忙时抬升1ms` | Busy-hour drift 0.25 / 0.5 / 1 ms | Every link's one-way delay rises by up to that amount over 24 h (sin² profile) |
| `双链路同时故障` (`online.dual`) | Two link faults | Two random links fail at the same time, 300 trials |
| `设备整机宕机` (`online.node`) | Device down | All links of one transit router go down (every router except `core`, the EMS's only upstream device) |
| `设备宕机+链路故障` (`online.node_dual`) | Device down + link fault | A device down plus a fault on another link |
| `单故障持续观察` (`online.single`) | Single fault held | The 84 single faults, each held for 40 rounds; exactly one alarm expected |
| `设备恢复仍有链路中断` (`online.node_back`) | Device recovery | The device returns while one of its links stays down |

### Categories, fault types and labels

| Chinese | English | Where |
|---|---|---|
| `链路中断` | Link down | category and type label |
| `性能劣化` | Performance degradation (delay, loss, jitter, bandwidth) | category |
| `路由漂移` | Route drift | category and type label |
| `正常` | Normal (no fault) | category |
| `时延劣化(RTT异常)` | Delay degradation (RTT anomaly) | type label |
| `丢包劣化` | Loss degradation | type label |
| `抖动劣化` | Jitter degradation | type label |
| `带宽劣化` | Bandwidth degradation | type label |
| `设备宕机` | Device down | type label |
| `无告警` | No alarm | predicted value in scenario rows |
| `误报率` / `漏报率` / `平均精测次数` | False-alarm rate / miss rate / mean deep probes | `offline` keys |
| `告警链路自身证据恢复至基线` | Cleared: the alarmed link's own evidence returned to baseline | clear reason |
| `精测复核未见该故障，告警恢复` | Cleared: a deep-probe re-check no longer shows the fault | clear reason |

### `bench` summary keys

| Key | English |
|---|---|
| `单轮推理时延 p99 (ms, 目标≤100)` | Inference latency p99 (ms, target ≤ 100), measured per forward pass as `latency.torch.forward_p99_ms` |
| `完整研判时延 p99 (ms, 含特征与按需精测调用)` | Full diagnosis latency p99 (ms, including features and on-demand deep-probe calls) |
| `模型参数量` | Model parameters |
| `抗干扰长跑 72.0h 无效告警数` | False alarms in the 72.0-hour noise run |
| `判决器触发后被智能体过滤次数` | Detector triggers the agent judged normal |
| `引擎异常次数` | Engine errors |
| `测量带宽占用 % (最窄链路, 目标<5%)` | Measurement traffic, % of the narrowest link (target < 5%) |
| `测量带宽占用 % (网管上联)` | Measurement traffic, % of the management uplink |
| `单核 CPU 占用 % (计算部分)` | CPU, % of one core (computation) |
| `常驻内存 MB` | Resident memory, MB |
| `场景[<label>] 类型/类型+定位准确率, 平均发现时延 s` | Scenarios [label]: type / type + location accuracy, mean time to alarm (s) |

### `pipeline.log`

| Chinese | English |
|---|---|
| `基线已保存` | Baseline saved |
| `生成 N 个样本 -> <file> (<s>)，触发率 <r>` | Generated N samples into the file (seconds); share of samples on which the detector triggered |
| `训练集 N（可训练 M），链路 L，动作数 A，参数量 P` | Training set N (M usable for training), L links, A actions, P parameters |
| `{'iter': …}` | Training progress every 1,000 iterations: exploration `eps`, `loss`, mean episode return `ep_return`, elapsed `sec`, validation `val_joint` (type + location), `val_type`, `val_probes` (deep probes per sample) |
| `模型已保存` | Model saved |
| `指标类别`, `样本数`, `类别识别准确率`, `故障类型准确率`, `定位准确率`, `类型+定位`, `设计指标`, `结论`, `达标` | Category, samples, category accuracy, fault-type accuracy, location accuracy, type + location, target, verdict, target met |
| `故障类型`, `判决器触发率`, `类型准确率`, `平均精测次数`, `平均发现时延 s` | Fault type, detector trigger rate, type accuracy, mean deep probes, mean detection delay (s) |
| `总体：…漏报率…正常样本误报率…（其中含瞬时噪声样本…）…判决器误触发率…平均精测…次/样本` | Overall: … miss rate … false-alarm rate on normal samples (of which on samples with transients) … detector false-trigger rate … mean deep probes per sample |

The log contains two pairs of evaluation tables: the first pair is the independent test set (`eval_test.json`), the
second the domain-randomized test set (`eval_test_rand.json`).

### `smoke/run*.log`

Each log walks through the steps of [`scripts/netns_smoke.py`](../scripts/netns_smoke.py) on link L4 with the probe
target `lo.acc1`. Lines starting with `==` are steps, lines with `[PASS]` or `[FAIL]` are checks.

| Chinese | English |
|---|---|
| `拓扑 … 设备 / … 链路 / … 监测目标；路由=FRR-OSPF` | Topology: devices / links / monitored targets; routing = FRR OSPF |
| `虚拟网络创建 …s，收敛 …s` | Virtual network created in … s, OSPF converged in … s |
| `基线：可达 …，… RTT=… 路径=…；本轮测量字节 …（… kbps @ 5s 周期）` | Baseline: reachable targets, RTT and path of the probe target; bytes sent in this round (kbps at a 5 s period) |
| `轻量带宽估计` / `精测` / `标称` / `单次包列估计` | Light bandwidth estimate / deep probe / nominal / single packet-train estimate |
| `注入 L4 <fault>（注入前稳态等待 …s）` | Inject the fault on L4 (waited … s for a steady state before injecting) |
| `不可达` / `转发表路径` / `邻接 Full 持续 …s（注入后 …s）` | Unreachable targets / forwarding-table path / adjacency Full for … s (… s since injection) |
| `清除后稳态等待 …s` | Waited … s for a steady state after clearing |
| `轻量探测 RTT 增量 …ms（2J=20），mdev …ms` | Light-probe RTT increase (for reference, 2J = 20 ms) and deviation |
| `冒烟结果 N/M 通过` | Smoke result: N of M checks passed |
| `链路中断`, `时延劣化(RTT异常) 20ms`, `丢包劣化 10%`, `抖动劣化 10ms`, `带宽劣化 剩余20%`, `路由漂移 10x` | Link down, delay +20 ms, loss 10%, jitter 10 ms, bandwidth with 20% remaining, route drift × 10 |

The 26 checks:

| Check | English |
|---|---|
| `基线全部可达` | All targets reachable at baseline |
| `逐跳路径完整（ICMP 限速丢跳已重测）` | Hop-by-hop paths complete (hops dropped by ICMP rate limiting re-measured) |
| `基线路径经过被测链路` | The probe target's baseline path crosses the link under test |
| `精测带宽接近标称` | Deep-probe bandwidth close to nominal |
| `转发表路径与 traceroute 一致` | Forwarding-table path matches traceroute |
| `两端接口不可达` | Both interface addresses of the link unreachable |
| `目标经冗余路径仍可达且绕开该链路` | The target stays reachable over a redundant path that avoids the link |
| `LSDB 邻接断开` | LSDB shows the adjacency down |
| `邻接全程保持 Full` | Adjacency stays Full throughout (each degradation step) |
| `被测路径不变` | Monitored path unchanged (each degradation step) |
| `RTT 上升约 2v` | RTT rises by about twice the one-way delay |
| `精测丢包≈1-(1-p)^2` | Deep-probe loss ≈ 1 − (1 − p)² |
| `精测抖动显著上升` | Deep-probe jitter rises clearly |
| `抖动不被误测成带宽下降` | Jitter is not mistaken for bandwidth loss |
| `精测带宽≈r×bw` | Deep-probe bandwidth ≈ remaining share × nominal bandwidth |
| `全部可达` | All targets reachable (route drift) |
| `路径改变且绕开该链路` | Path changes and avoids the link |
| `LSDB 开销变化` | LSDB cost changed to the drifted value |
| `恢复后回到基线` | Back to baseline after clearing |
| `down 后无残留命名空间` | No namespaces left after `down` |
