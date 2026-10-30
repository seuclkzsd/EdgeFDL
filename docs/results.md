# Results

This page reports every measured number for EdgeFDL with its test conditions, defines the metrics, and gives
the command that reproduces each table. All numbers come from the files in [`results/`](../results/) (described in
[`results/README.md`](../results/README.md)) and were produced with the released model bundle
[`results/model.pt`](../results/model.pt).

- [At a glance](#at-a-glance)
- [Test conditions](#test-conditions)
- [Metric definitions](#metric-definitions)
- [1. Independent test set](#1-independent-test-set)
- [2. Self-test: latency, overhead, long run, online scenarios](#2-self-test-latency-overhead-long-run-online-scenarios)
- [3. Real protocol stack smoke test](#3-real-protocol-stack-smoke-test)
- [4. Robustness](#4-robustness)
- [5. How to reproduce](#5-how-to-reproduce)
- [6. Raw data](#6-raw-data)
- [Limitations and next steps](#limitations-and-next-steps)

## At a glance

| Area | Result | Section |
|---|---|---|
| Accuracy (type + location) | Link down 100.00%, performance degradation 98.73%, route drift 100.00%; false-alarm rate 0.07% on normal samples, transient noise included (10,000 samples, end-to-end) | [1](#1-independent-test-set) |
| Inference | One forward pass p99 0.575 ms (PyTorch, 1 thread), NumPy engine p99 0.302 ms; full diagnosis including feature computation p99 2.512 ms | [2](#2-self-test-latency-overhead-long-run-online-scenarios) |
| Overhead | Measurement traffic 0.222% of the narrowest 100 Mbps link (worst case); compute 0.0868% of one core; RSS 528.6 MB | [2](#2-self-test-latency-overhead-long-run-online-scenarios) |
| Noise immunity | 72-hour-equivalent run with a transient in 15% of rounds: 214 detector triggers, 0 false alarms, 0 engine errors | [2](#2-self-test-latency-overhead-long-run-online-scenarios) |
| Online scenarios | 84/84 correct alarms (type and link); mean time to alarm 37.9 s, mean time to clear after repair 19.3 s | [2](#2-self-test-latency-overhead-long-run-online-scenarios) |
| Real protocol stack | Linux namespaces + FRR OSPF + tc: 26/26 checks in each of 3 runs; baseline RTT of all 37 targets within 0.74 ms of the simulator | [3](#3-real-protocol-stack-smoke-test) |
| Harsh conditions | 16 long-run conditions of 24 h each: 0 false alarms in 12 of them, at most 9 in 24 h (transients lasting 15 s) | [4.1](#41-long-runs-and-scenarios-under-harsh-conditions) |
| Concurrent faults | Two simultaneous link faults (300 trials): first alarm correct 100%, both reported 97.67%; device down (60 trials): one device-level alarm every time | [4.4](#44-multiple-faults) |

## Test conditions

These conditions apply to every table on this page unless a section states otherwise.

| Item | Value |
|---|---|
| Version | Model bundle `results/model.pt` (version string `ddqn-20261030-0116`, 36,041 parameters), trained and measured with EdgeFDL 0.1.0. Release 0.1.1 reproduces the test-set evaluations and the baseline byte for byte, the self-test's long run and 84 scenarios with identical outcomes (latency, CPU and memory vary with the machine's load) and the robustness suite apart from its run time |
| Topology | [`configs/demo_topology.yaml`](../configs/demo_topology.yaml): 12 devices (routers `core`, `agg1`, `agg2`, `acc1`–`acc4`; hosts `h1`–`h4`; the network-management server `ems`), 15 links L1–L15 (1000 and 100 Mbps), Huawei/H3C vendor mix |
| Monitored targets | 37: 29 interface addresses (25 router interfaces, 4 host addresses), 7 router loopbacks, and 1 NQA target that coverage planning adds for the backup link L3, which no EMS-side probe crosses |
| Measurement plan | One round every 5 s: 5 pings to every target, traceroute paths, packet-train bandwidth estimate on 3 links per round, OSPF LSDB; detection window of 6 rounds |
| Backend | `sim`, the analytic simulator of the equivalent virtual network, for the independent test set, the self-test and the robustness suite. These run in **simulated time**: each 5 s round is computed without waiting, so "72 hours" means 51,840 rounds. The real protocol stack (`netns` backend) is exercised by the smoke test in section 3 |
| Seeds | Baseline 1; training samples 1, validation samples 2, independent test set 3, domain-randomized test set 4; training 0; self-test 7; robustness 21 |
| Machine | 2 × Intel Xeon Gold 6336Y (96 logical CPUs), Linux; Python 3.13.5; PyTorch 2.9.1, a CUDA 12.8 build run on the CPU (no GPU used) |
| Training | 60,000 trigger-aligned, domain-randomized samples generated in 87.8 s with 16 processes; 15,000 iterations with 512 parallel episodes in about 4.4 minutes on 8 CPU threads |
| Targets | The "Target" columns are EdgeFDL's design targets |

## Metric definitions

**End-to-end accuracy.** Each test sample starts from a normal network, then a fault (or no fault) is injected and the
k-of-n detector runs exactly as it does online. If the detector never wakes the agent, the sample is scored as
"normal": a missed fault for fault samples, a correct "no alarm" for normal samples. Undetected faults therefore count
as errors in every accuracy figure.

| Metric | Definition |
|---|---|
| Type accuracy | The predicted fault type equals the true type. For the performance-degradation category the specific type (delay, loss, jitter or bandwidth) must match |
| Location accuracy | The predicted link equals the faulty link |
| Type + location | Both are correct. This is the headline accuracy |
| Trigger rate | Share of samples on which the detector woke the agent. On normal samples this is a false trigger caused by transient noise, which the agent then judges |
| Detection delay | Simulated time from fault onset until the agent is woken (detector trigger plus the 2 observation rounds) |
| False-alarm rate | Share of normal samples on which the agent named a fault |
| Miss rate | Share of fault samples reported as normal |
| Deep probes per sample | Mean number of on-demand single-link deep probes (100 pings at 10 ms plus a 1 s iperf3 at ≤ 30% of the link bandwidth) the agent requested per test sample; samples on which the detector never woke the agent count as 0 |
| Time to alarm / to clear | Online engine only: from fault injection to the alarm (including confirmation: two consistent diagnoses two rounds apart), and from repair to the alarm clearing |
| False alarm (long run) | An alarm raised while no fault is injected |
| False-alarm exposure | Share of rounds during which a false alarm is active. A single false alarm that stayed active all day would count once but give 100% exposure |

The three categories are **link down**, **performance degradation** (delay, loss, jitter, bandwidth) and **route
drift** (an OSPF cost change that moves a monitored path).

## 1. Independent test set

10,000 samples generated with seed 3, which differs from the training and validation seeds, under **standard
conditions**: 15% normal samples; in every round a new one-round transient (a delay spike of 10–150 ms or a loss burst
of 20–80% on a random link) appears with probability 25%; no ICMP rate limiting; default background jitter. Fault values
are drawn from the ranges of the fault model, applied one way at both ends of the link: delay +5–100 ms, loss 5–90%
and jitter 5–50 ms (log-uniform, so light faults are more frequent), remaining bandwidth 5–50% (uniform), link down,
and route drift by OSPF cost × 0.1/0.3/3/10/30 restricted to changes that move a monitored path. Each sample is diagnosed once by the agent (the online engine adds
confirmation and re-checks on top, sections 2 and 4.1). Raw data: [`results/eval_test.json`](../results/eval_test.json).

**Per category**

| Category | Samples | Target | Type | Location | Type + location |
|---|---|---|---|---|---|
| Link down | 1380 | ≥ 98% | 100.00% | 100.00% | **100.00%** |
| Performance degradation | 5668 | ≥ 95% | 98.80% | 98.76% | **98.73%** |
| Route drift | 1430 | ≥ 95% | 100.00% | 100.00% | **100.00%** |
| Normal (transient noise included) | 1522 | no invalid alarms | false-alarm rate **0.07%** | – | – |

Overall: type accuracy 99.31%, type + location 99.27%, miss rate 0.75%, 0.061 deep probes per sample on average.

**Per fault type**

| Fault type | Samples | Trigger rate | Type | Location | Type + location | Mean detection delay |
|---|---|---|---|---|---|---|
| Link down | 1380 | 100.00% | 100.00% | 100.00% | 100.00% | 19.7 s |
| Delay | 1477 | 100.00% | 100.00% | 100.00% | 100.00% | 22.9 s |
| Loss | 1400 | 99.79% | 96.14% | 95.86% | 95.86% | 26.8 s |
| Jitter | 1390 | 100.00% | 99.86% | 100.00% | 99.86% | 23.1 s |
| Bandwidth | 1401 | 100.00% | 99.14% | 99.14% | 99.14% | 48.0 s |
| Route drift | 1430 | 100.00% | 100.00% | 100.00% | 100.00% | 19.7 s |
| Normal | 1522 | 27.60% | 99.93% (judged normal) | – | – | – |

The detector wakes the agent on 27.60% of the normal samples because of the injected transients; the agent judges
99.93% of the normal samples as normal, which is how the false-alarm rate stays at 0.07%. Bandwidth faults take longest
to detect because the packet-train estimate covers 3 links per round and the detector waits for two estimates below 70%
of the baseline.

A second, harder test set generated under the training-time domain randomization (seed 4) is kept in
[`results/eval_test_rand.json`](../results/eval_test_rand.json); the robustness suite evaluates the same kind of
condition on fresh samples as "domain randomization" in [section 4.2](#42-offline-single-shot-diagnosis).

## 2. Self-test: latency, overhead, long run, online scenarios

`python -m edgefdl bench --model results/model.pt --out runs/bench --rounds 51840` drives the complete online engine
(detector, agent, deep probes, confirmation, alarms, baseline tracking) on the simulation backend with seed 7. Raw data:
[`results/bench.json`](../results/bench.json).

| Metric | Target | Result | Conditions |
|---|---|---|---|
| Inference, one forward pass | ≤ 100 ms | p99 **0.575 ms**, mean 0.553 ms | PyTorch, 1 thread; NumPy engine p99 0.302 ms |
| Full diagnosis | – | p99 2.512 ms | Feature computation plus the agent's forward passes |
| Measurement traffic | < 5% | **0.222%** of the narrowest 100 Mbps link (222.0 kbps); 0.022% of the 1 Gbps management uplink | Worst case: all probe traffic, deep probes included, assumed to cross the narrowest link |
| Server resources | ≤ 20% | **0.0868%** of one core (4.338 ms of computation per 5 s round); RSS 528.6 MB | RSS is mostly the PyTorch runtime |
| Transient noise | no invalid alarms | **0 false alarms**, 0 engine errors, false-alarm exposure 0.0%; 214 detector triggers, the agent judged normal 202 times (including periodic re-checks) | 72-hour equivalent: 51,840 rounds × 5 s; in each round a 15% chance of a one-round delay spike (10–150 ms) or loss burst (20–80%) on a random link |
| Online scenarios | all scenarios | **84/84** correct alarms (type and link); mean time to alarm 37.9 s (max 70 s); mean time to clear after repair 19.3 s | See below |

**Online scenarios.** One scenario per link and fault type: 15 links × 5 fault types, plus route drift on the 9
router–router links where a cost change moves a monitored path, 84 in total. Values: link down; delay +20 ms; loss 15%;
jitter 15 ms; bandwidth reduced to 20%; route drift with the largest cost factor that moves a monitored path. Each
scenario starts from a normal window, injects the fault, steps the engine until an alarm (at most 60 rounds), repairs
the fault and steps until the alarm clears.

| Fault type | Link down | Delay | Loss | Jitter | Bandwidth | Route drift |
|---|---|---|---|---|---|---|
| Mean time to alarm | 30.0 s | 34.7 s | 37.3 s | 34.3 s | 58.0 s | 30.0 s |

Time to alarm includes the detector's k-of-n rule (at least 3 of the last 6 rounds anomalous for a target, 2 rounds for
a control-plane change, two bandwidth estimates below 70%), 2 observation rounds and the confirmation step. Bandwidth is
the slowest because 3 links are estimated per round.

## 3. Real protocol stack smoke test

`scripts/lab.sh smoke` builds the equivalent virtual network of the demo topology from Linux network namespaces, veth
pairs, FRR OSPF (hello 1 s, dead 4 s) and tc inside a privileged Debian 12 container
([`scripts/lab/Dockerfile`](../scripts/lab/Dockerfile)), then injects the six fault types one after another on link L4
(`agg1`–`acc1`, which has a redundant path) and checks that the measurements show what each fault should cause. The
probe target is the loopback of `acc1`, whose baseline path crosses L4. Script:
[`scripts/netns_smoke.py`](../scripts/netns_smoke.py); logs (in Chinese): [`results/smoke/`](../results/smoke/).

| Step | What the checks assert | Checks |
|---|---|---|
| Baseline | All 37 targets reachable; hop-by-hop paths complete (hops dropped by ICMP rate limiting are re-measured); the probe target's path crosses L4; deep-probe bandwidth within 30% of nominal; forwarding-table path equals the traceroute path | 5 |
| Link down | Both interface addresses of L4 unreachable; the probe target stays reachable over a path that avoids L4 (forwarding table and traceroute); LSDB shows the adjacency down | 3 |
| Delay +20 ms (one way) | OSPF adjacency stays Full throughout; monitored path unchanged; round trip rises by about 2 × 20 ms (± 5 ms) | 3 |
| Loss 10% | Adjacency Full; path unchanged; deep-probe loss ≈ 1 − (1 − p)² (± 0.15) | 3 |
| Jitter 10 ms | Adjacency Full; path unchanged; deep-probe RTT deviation above 5 ms and above 4 × its baseline; deep-probe bandwidth within 30% of nominal, so jitter is not mistaken for bandwidth loss | 4 |
| Bandwidth to 20% | Adjacency Full; path unchanged; deep-probe bandwidth ≈ 20% of nominal (± 30%) | 3 |
| Route drift × 10 | All targets reachable; path changes and avoids L4; LSDB cost equals 10 × the baseline cost | 3 |
| After clearing | All targets reachable, path and RTT back to baseline (within 1 ms); no namespace left after `down` | 2 |

**Results: 26/26 checks passed in each of three consecutive runs** (`run1.log`–`run3.log`). Measured values: link down
makes both interface addresses unreachable while traffic reroutes and the LSDB shows the adjacency down; +20 ms one-way
delay raises the RTT by 40.0 ms; reducing bandwidth to 20% gives a deep-probe estimate of 200 Mbps; route drift × 10
keeps every target reachable, moves the path off the link and shows LSDB cost 100; the OSPF adjacency stays Full during
the four degradation steps; the baseline returns after clearing. OSPF converges in about 11 s for the 12 devices.

The measured probe traffic is about 290 KB per round (≈ 465 kbps), at most 0.47% of the narrowest 100 Mbps link even
if all probe traffic crossed it. The smoke test sends 10 pings per target per round, twice the online default, so this
is an upper estimate for the default plan.

**Simulator against real stack.** [`scripts/sim_vs_stack.py`](../scripts/sim_vs_stack.py) measures 30 rounds of the
healthy demo network on the `netns` backend (FRR OSPF, `tc` netem and tbf) and 30 rounds on the simulator with the same
topology and measurement plan (5 pings per target, 0.2 s apart), and compares for each target the median of the
per-round mean RTT, the estimate the baseline uses. All 37 targets are reachable in every round on both backends; the
absolute RTT difference is at most 0.74 ms (target `L3.agg1`), with a median of 0.39 ms and a 90th percentile of
0.52 ms (kernel 5.15, FRR 8.4.4, OSPF converged in 10.8 s). Raw data:
[`results/sim_vs_stack.json`](../results/sim_vs_stack.json).

## 4. Robustness

`python -m edgefdl robust --model results/model.pt --out runs/robust --workers 40` runs the same model with the same
engine defaults under conditions harsher than the standard test conditions: stronger and longer interference, missing
measurements, ICMP rate limiting, larger background jitter, busy-hour drift, fault sizes outside the training range,
and concurrent faults. Simulation backend, simulated time, seed 21. Raw data:
[`results/robust.json`](../results/robust.json); the Chinese condition keys used there are translated in
[`results/README.md`](../results/README.md#condition-names).

### 4.1 Long runs and scenarios under harsh conditions

Each condition is a 24-hour-equivalent run (17,280 rounds) of the online engine without any fault; conditions marked
in the last column also repeat the 84 online scenarios of section 2. Every condition keeps the base interference (a
one-round transient in 15% of rounds) unless it changes it.

| Condition | What changes | False alarms in 24 h | False-alarm exposure | 84 scenarios, type + location |
|---|---|---|---|---|
| Base | None (15% of rounds with a one-round transient) | 0 | 0.0% | 100.0% |
| Transients 30% | A new transient in 30% of rounds | 0 | 0.0% | not run |
| Transients 50% | A new transient in 50% of rounds | 2 | 0.064% | 100.0% |
| Transients last 2 rounds | Each transient lasts 10 s | 0 | 0.0% | not run |
| Transients last 3 rounds | Each transient lasts 15 s | 9 | 0.197% | 100.0% |
| Missing targets 10% | 10% of target results missing in each round | 0 | 0.0% | 100.0% |
| Missing rounds 5% | 5% of rounds missing entirely | 0 | 0.0% | 100.0% |
| ICMP rate limiting 2%, in baseline | Routers drop 2% of ICMP replies, already present while the baseline was learned | 0 | 0.0% | not run |
| ICMP rate limiting 5%, in baseline | As above, 5% | 0 | 0.0% | 98.8% |
| ICMP rate limiting 2%, after deployment | Routers start dropping 2% of ICMP replies after deployment | 0 | 0.0% | not run |
| ICMP rate limiting 5%, after deployment | As above, 5% | 2 | 0.058% | 96.4% |
| Background jitter 0.5 ms | Per-link background jitter 0.5 ms (10 × default), baseline re-learned on site | 0 | 0.0% | not run |
| Background jitter 1 ms | Per-link background jitter 1 ms (20 × default), baseline re-learned on site | 0 | 0.0% | 100.0% |
| Busy-hour drift 0.25 ms | Every link's one-way delay rises by up to 0.25 ms over 24 h (sin² profile) | 1 | 0.012% | not run |
| Busy-hour drift 0.5 ms | As above, up to 0.5 ms | 0 | 0.0% | not run |
| Busy-hour drift 1 ms | As above, up to 1 ms | 0 | 0.0% | not run |

The adaptive baseline (EWMA tracking of slow RTT drift, network-wide common-mode drift, background-loss re-learning)
and the confirmation step hold the busy-hour and ICMP rate-limiting conditions to 0–2 false alarms per 24 h; the
mechanisms are described in [algorithms.md](algorithms.md).

### 4.2 Offline single-shot diagnosis

4,000 fresh trigger-aligned samples per condition, each diagnosed **once** by the agent, without the online engine's
confirmation, re-checks or baseline adaptation. This isolates the agent itself. Values are type + location accuracy
per category; the false-alarm rate is the share of normal samples on which a fault was named. The offline standard
condition uses the transient rate of the independent test set (25% of rounds).

| Condition | What changes | Link down | Performance degradation | Route drift | False-alarm rate |
|---|---|---|---|---|---|
| Standard conditions | As the independent test set | 100.0% | 98.5% | 100.0% | 0.2% |
| Domain randomization (training conditions) | Transients lasting 1/2/3 rounds (60/25/15%); ICMP rate limiting of 0.5–3% on router targets in 25% of samples, half of them in the baseline; larger background jitter in 45% of samples | 100.0% | 95.0% | 100.0% | 4.2% |
| Transients 50% | A new transient in 50% of rounds | 100.0% | 97.1% | 100.0% | 0.7% |
| Transients 75% | A new transient in 75% of rounds | 100.0% | 93.8% | 100.0% | 2.4% |
| Transients last 2 rounds | Each transient lasts 10 s | 100.0% | 95.8% | 100.0% | 7.0% |
| Transients last 3 rounds | Each transient lasts 15 s | 100.0% | 89.2% | 100.0% | 11.0% |
| Missing targets 5% | 5% of target results missing per round | 100.0% | 97.9% | 100.0% | 0.7% |
| Missing targets 10% | As above, 10% | 99.8% | 95.5% | 100.0% | 0.0% |
| Missing targets 20% | As above, 20% | 100.0% | 91.0% | 100.0% | 0.0% |
| Missing rounds 5% | 5% of rounds missing entirely | 100.0% | 97.1% | 100.0% | 0.7% |
| Missing rounds 10% | As above, 10% | 100.0% | 94.7% | 100.0% | 0.2% |
| ICMP rate limiting 2%, in baseline | Present while the baseline was learned | 100.0% | 96.5% | 100.0% | 0.2% |
| ICMP rate limiting 5%, in baseline | As above, 5% | 100.0% | 94.2% | 100.0% | 2.6% |
| ICMP rate limiting 1%, after deployment | Starts after deployment | 100.0% | 96.6% | 100.0% | 1.1% |
| ICMP rate limiting 2%, after deployment | As above, 2% | 100.0% | 93.5% | 100.0% | 1.2% |
| ICMP rate limiting 5%, after deployment | As above, 5% | 100.0% | 91.1% | 100.0% | 43.9% |
| Background jitter 0.2 ms | Baseline re-learned on site | 100.0% | 98.6% | 100.0% | 0.0% |
| Background jitter 0.5 ms | As above | 100.0% | 98.3% | 100.0% | 0.0% |
| Background jitter 1 ms | As above | 100.0% | 97.4% | 100.0% | 0.0% |

Link down and route drift stay at 100.0% under every condition (99.8% with 10% of targets missing). The online engine
adds confirmation, re-checks and the adaptive baseline on top of the single-shot agent: for 5% ICMP rate limiting that
starts after deployment, the single-shot false-alarm rate on normal samples is 43.9%, while the online 24-hour run under
the same condition raises 2 false alarms (section 4.1).

### 4.3 Fault-size sensitivity

Faults smaller than the lower end and larger than the upper end of the training range, 15 links × 20 samples per value
(300 per value), single-shot diagnosis. Delay and jitter are added per direction; loss is the one-way loss rate;
bandwidth is the remaining share of the link bandwidth.

| Fault type | Value | Relation to the training range | Trigger rate | Type + location |
|---|---|---|---|---|
| Delay | 1 ms | below | 85.0% | 49.7% |
| Delay | 2 ms | below | 100.0% | 95.7% |
| Delay | 3 ms | below | 100.0% | 100.0% |
| Delay | 5 ms | inside (lower end) | 100.0% | 100.0% |
| Delay | 100 ms | inside (upper end) | 100.0% | 100.0% |
| Delay | 200 ms | above | 100.0% | 99.0% |
| Delay | 500 ms | above | 100.0% | 96.7% |
| Loss | 1% | below | 59.3% | 1.3% |
| Loss | 2% | below | 83.3% | 12.3% |
| Loss | 3% | below | 90.3% | 32.3% |
| Loss | 5% | inside (lower end) | 95.3% | 75.7% |
| Loss | 50% | inside | 100.0% | 99.3% |
| Loss | 70% | inside | 100.0% | 100.0% |
| Loss | 90% | inside (upper end) | 100.0% | 100.0% |
| Jitter | 1 ms | below | 86.3% | 72.7% |
| Jitter | 2 ms | below | 100.0% | 97.7% |
| Jitter | 3 ms | below | 100.0% | 100.0% |
| Jitter | 5 ms | inside (lower end) | 100.0% | 99.7% |
| Jitter | 50 ms | inside (upper end) | 100.0% | 99.7% |
| Jitter | 100 ms | above | 100.0% | 100.0% |
| Bandwidth | 90% remaining | milder | 29.3% | 0.0% |
| Bandwidth | 80% remaining | milder | 30.7% | 9.3% |
| Bandwidth | 70% remaining | milder | 73.7% | 68.0% |
| Bandwidth | 50% remaining | inside (mildest) | 100.0% | 99.7% |
| Bandwidth | 5% remaining | inside (most severe) | 100.0% | 99.3% |
| Bandwidth | 1% remaining | more severe | 100.0% | 99.0% |

Delay and jitter are recognised from about 2–3 ms per direction, below the 5 ms lower end of the training range, and
recognition holds far above the upper end (500 ms delay, 100 ms jitter, 1% remaining bandwidth). Loss at the 5% lower
end is detected in 95.3% of the samples and named with its link in 75.7%; from 50% loss upwards type + location is
99.3–100%. Bandwidth degradation is recognised at or below 50% remaining.

### 4.4 Multiple faults

The online engine raises one alarm per fault by default (concurrent alarms with residual re-baselining; `--single-alarm`
reverts to one alarm at a time). Each trial starts from a normal window, injects the faults together and records every
alarm raised in the next 40 rounds (200 s). Fault values are those of the online scenarios.

- **Two link faults**: two random links with random fault types, 300 trials.
- **Device down**: every link of one transit router goes down; each of the 6 routers other than `core` (the EMS's only
  upstream device, whose loss disconnects the EMS) × 10 repetitions, 60 trials.
- **Device down + link fault**: a device down plus a fault on another link (not attached to that device, not the
  management uplink, not route drift), 60 trials.
- **Single fault held**: the 84 online scenarios, each held for 40 rounds; exactly one alarm is expected.

| Metric | Two link faults | Device down | Device down + link fault | Single fault held |
|---|---|---|---|---|
| Trials | 300 | 60 | 60 | 84 |
| At least one alarm | 100% | 100% | 100% | 100% |
| First alarm correct (type and link or device) | 100% | 100% | 100% | 100% |
| First alarm at a faulty location | 100% | 100% | 100% | 100% |
| No wrong alarm | 99.67% | 100% | 100% | 100% |
| All faults reported | 97.67% | 100% | 98.33% | 100% |
| Mean time to first alarm | 34.0 s | 29.6 s | 30.4 s | 38.0 s |
| Mean alarms per trial | 1.98 | 1.0 | 1.9833 | 1.0 |
| Trials with more alarms than faults | 0.33% | 0% | 0% | 0% |
| Wrong alarms per trial | 0.0033 | 0 | 0 | 0 |
| Cross-wired alarms (type of one fault, link of the other) | 0% | 0% | 0% | 0% |
| Deep probes per round while the faults are present | 0.0788 | 0.0013 | 0.0258 | 0.0098 |
| Deep probes for alarm-clearing re-checks, total over all trials | 61 | 0 | 4 | 5 |

**Repairing one of two faults.** After the 40 rounds, one of the two faults is repaired (alternating between trials)
and the engine runs 60 more rounds. A trial passes when the repaired alarm clears and stays cleared, the other alarm
stays active with the same alarm id, and no new alarm is raised.

| Metric | Two link faults | Device down + link fault |
|---|---|---|
| Both alarms active before the repair | 97.33% | 98.33% |
| Repair handled correctly, all trials | 97.33% | 98.33% |
| Repair handled correctly, trials with both alarms active | 100% | 100% |
| Other alarm kept | 100% | 100% |
| Mean time to clear the repaired alarm | 20.0 s | 20.2 s |
| New alarms during the repair window | 0 | 0 |
| Deep probes per round during the repair window | 0.0239 | 0.009 |

**Device recovery** (22 trials: each transit router other than `core` × each of its links): the device comes back while
one of its links stays down. The device-down alarm was raised in 100% of the trials, it was downgraded to a link-down
alarm on the remaining link in 100%, no stale device alarm remained (0%), and the mean time to downgrade was 40.0 s.

## 5. How to reproduce

Run from the repository root after `pip install -r requirements.txt`. The `runs/` directory is git-ignored. Times are
wall-clock times on the machine of the [test conditions](#test-conditions).

| Result | Command | Output | Time |
|---|---|---|---|
| Independent test set with the released model | `python -m edgefdl gen --baseline results/baseline.json --n 10000 --seed 3 --plain --workers 8 --out runs/check/test.npz`<br>`python -m edgefdl eval --model results/model.pt --data runs/check/test.npz --json runs/check/eval_test.json` | `runs/check/eval_test.json`, identical to `results/eval_test.json` byte for byte | about 40 s with 8 workers, plus a few seconds |
| Domain-randomized test set | as above with `--seed 4` and without `--plain` | identical to `results/eval_test_rand.json` | about 40 s |
| Normal baseline | `python -m edgefdl baseline --rounds 40 --seed 1 -o runs/check/baseline.json` | identical to `results/baseline.json` | seconds |
| Full pipeline: baseline, samples, training, evaluation, self-test | `python -m edgefdl pipeline --out runs/v6 --workers 16` | `runs/v6/` with `baseline.json`, `train/val/test/test_rand.npz`, `model.pt`, `eval_test.json`, `eval_test_rand.json`, `bench/bench.json` | about 15 minutes |
| Self-test (72-hour equivalent) | `python -m edgefdl bench --model results/model.pt --out runs/bench --rounds 51840` | `runs/bench/bench.json` | about 5 minutes, single process |
| Robustness suite | `python -m edgefdl robust --model results/model.pt --out runs/robust --workers 40` | `runs/robust/robust.json` | about 3.5 minutes with 40 workers (`wall_s` 205.1) |
| Real protocol stack | `scripts/lab.sh build`<br>`mkdir -p runs/smoke && scripts/lab.sh smoke \| tee runs/smoke/run1.log` | console log as in `results/smoke/` | a few minutes after the image is built; needs Docker with `--privileged` on Linux |
| Simulator against real stack | `scripts/lab.sh run python3 scripts/sim_vs_stack.py --out runs/lab/sim_vs_stack.json` | `runs/lab/sim_vs_stack.json`, as `results/sim_vs_stack.json` (RTT values vary slightly between runs) | about 1 minute after the image is built |
| Unit tests | `python -m unittest discover -s tests` | 71 tests | about 20 s |

Notes:

- Baseline learning, sample generation and evaluation are deterministic for a given seed. Sample sets are generated
  in chunks with their own seeds, so they do not depend on the number of workers (one worker and `gen --raw-csv`
  included); the first three rows were re-run with 8 workers and match the published files byte for byte.
- `pipeline` runs its built-in self-test with 17,280 rounds (24 h) by default; the published `bench.json` uses the
  separate 51,840-round run shown in the table (`pipeline --no-bench` skips the built-in one).
- Training draws all its random numbers from generators seeded with the configured seed, so the same training set,
  seed and thread count give the same model. `results/model.pt` was trained with EdgeFDL 0.1.0, whose training left
  weight initialisation, exploration and replay sampling unseeded, so `pipeline` trains a different model whose
  figures differ from the published ones. Evaluate the released bundle to reproduce the published numbers exactly.
- Latency, CPU share and RSS depend on the machine and the PyTorch build; the published RSS was measured with a
  CUDA build of PyTorch, whose libraries make up most of it.
- `robust` options: `--only online|offline`, `--kinds long,scen,dual,node,single,node_dual,node_back` to run a subset,
  `--seed` (or the environment variable `EDGEFDL_ROBUST_SEED`, default 21), `--single-alarm` for the one-alarm engine.
  `scripts/compare_robust.py` compares the pass criteria of two `robust.json` files, for example a concurrent-alarm run
  against a `--single-alarm` run. With fewer CPU cores, pass a smaller `--workers` value; the suite then takes longer.
- `EDGEFDL_APT_MIRROR=<host>` makes `scripts/lab.sh build` use a local Debian mirror.

## 6. Raw data

| File | Content |
|---|---|
| [`results/model.pt`](../results/model.pt) | The model bundle evaluated on this page |
| [`results/eval_test.json`](../results/eval_test.json) | Section 1 |
| [`results/eval_test_rand.json`](../results/eval_test_rand.json) | Domain-randomized test set, single-shot |
| [`results/bench.json`](../results/bench.json) | Section 2, including all 84 scenario rows |
| [`results/robust.json`](../results/robust.json) | Section 4, including every multi-fault trial |
| [`results/baseline.json`](../results/baseline.json) | Normal baseline learned on the virtual network |
| [`results/pipeline.log`](../results/pipeline.log) | Console log of sample generation, training and evaluation |
| [`results/smoke/`](../results/smoke/) | Section 3, three runs |
| [`results/sim_vs_stack.json`](../results/sim_vs_stack.json) | Section 3, per-target RTT of the real stack and the simulator |

The file formats and the JSON keys, including an English glossary of the Chinese keys and condition names, are
documented in [`results/README.md`](../results/README.md).

## Limitations and next steps

- All accuracy and robustness numbers come from the analytic simulation backend on the 12-device demo topology. The real
  protocol stack (netns + FRR + tc) is validated by the smoke test, not yet by accuracy runs; for the LXD backend the
  command generation is verified (dry run) and has not yet run on hardware.
- The harsh conditions of section 4 (ICMP rate limiting, busy-hour drift, long transients) are modelled by us, not
  measured on production networks. The 72-hour figure is simulated time; a 72-hour real-time stress test is planned.
- The light probes (5 packets per target per round) do not resolve one-way loss below about 3% or bandwidth still
  above about 70% of the baseline (section 4.3). With routers dropping 5% of ICMP replies, some 15% loss faults of
  the online scenarios are missed or attributed to a neighbouring link: the scenario errors of section 4.1 are all of
  this kind, missed on the access-ring links L8 and L10 (each crossed by a single monitored path) or, once, reported
  on L7 instead of L2.
- Heavy loss (≥ 30%) makes real OSPF adjacencies flap; the simulator does not model this yet, which is why the smoke
  test uses 10% loss. Samples from the real stack will cover it.
- Faults behind the management uplink L15 cannot be told apart from a fault of the uplink itself: when L15 is down the
  EMS sees no data plane. Most of the two-fault trials in which the second fault was not reported have one fault on
  L15.
- One model is trained per topology by design. The shared link encoder could transfer across topologies; this is not
  validated yet.

Planned work with measurable criteria (real-stack fine-tuning, a lower loss sensitivity floor, the 72-hour real-time
test, larger topologies) is listed in [ROADMAP.md](../ROADMAP.md).
