# Comparison with existing tools

EdgeFDL answers one operational question for an edge network: **which link is faulty, and what kind of fault is it**,
from the network-management server (EMS). This page measures it against the methods behind existing tools and
research, places it next to the open-source tools an edge-network operator typically runs, shows where it adds a
capability, where those tools are stronger, and how EdgeFDL feeds its alarms into them.

- [Measured comparison](#measured-comparison): re-implementations of the published methods, run on the same
  measurements as the unchanged EdgeFDL engine. These are not the products themselves; what the products offer beyond
  this is listed [below](#what-this-comparison-does-not-cover).
- [Capability comparison](#capability-comparison): based on the public documentation of each project as of October
  2026 (links in [References](#references)). The products themselves were not run against EdgeFDL.

EdgeFDL's numbers on this page hold for their test conditions: the analytic simulation backend, the 12-device demo
topology and the released model `results/model.pt`; [results.md](results.md) has the full set.

## Measured comparison

`python -m edgefdl compare` runs the real, unchanged online engine on the simulation backend while a read-only tap on
the backend records every `RoundResult` the engine sees. Each other method then replays exactly that stream round by
round, without look-ahead: the same seeds, the same transients, the same injected faults. The raw output is
[`results/compare.json`](../results/compare.json); [`edgefdl/compare.py`](../edgefdl/compare.py) is the code.

![Three panels with one row per method. Left: link and type correct by fault type under standard conditions. Middle: link and type correct under six test conditions. Right: false alarms per simulated day under the same conditions](images/compare.png)

**What every baseline gets.** The best information an EMS has, so that no result comes from a starved input:

- the same 37 targets as EdgeFDL: both interfaces of every link, loopbacks, hosts and the NQA probe on the backup link
  L3, with 5 pings per target every 5 s;
- the routing matrix recomputed from the LSDB every round, including return paths, and the expected RTT after a reroute
  computed from a per-link delay fit, so that a reroute alone does not make a path look bad;
- the same RTT baseline tracking as the engine (rate 1/360) and tracking of background loss;
- a path-state detector with EdgeFDL's own thresholds and its 3-of-6 persistence rule in the default configuration, and
  a parameter grid in the tuned configuration, including loss aggregated over a window;
- a device rule: all addresses of a router unreachable for 3 rounds (with EMS rules: for 2 rounds with all its router
  adjacencies down) is one device-level alarm;
- a type rule, since the originals have no classifier: both link addresses unreachable is link down; RTT excess under
  2 ms on the bad paths is loss; RTT spread at least 0.2 × RTT excess is jitter; otherwise delay. It can never claim
  bandwidth or route drift.

The rows marked **+ EMS rules** add three rules with EdgeFDL's detector thresholds: OSPF adjacency down for 2 rounds is
link down on that link, an LSDB cost change for 2 rounds is route drift, and 2 consecutive bandwidth estimates below
70% of baseline are bandwidth degradation. Any NMS that reads the LSDB could run these rules, so they are shown
separately from the methods as published. Per-target threshold alarms name no link, so link metrics are n/a for them
and they have no such row.

**Tuning.** Every baseline is reported with default parameters (typical configuration or paper values, cited
[below](#how-each-method-was-implemented)) and tuned: a grid search of 2,728 settings in total on validation streams
with a different seed, keeping the best F1, then scored once on the test streams. The validation streams cover six
conditions, each with the 84 single-fault scenarios, 30 two-fault and 6 device-down cases and a 6-hour normal run. One
setting is chosen per method and variant for all conditions, as EdgeFDL has one model. F1 counts a named true link as a true
positive, a missed one as a false negative, and an alarm naming another link or an alarm in the normal run (scaled to
24 h) as a false positive. A second operating point, **low-FA**, takes the best F1 among settings with at most 1 false
alarm per day on validation, and the fewest false alarms if none reaches that. All selected settings are in
`tuning` and `tuning_low_fa` in `compare.json`, with the five best per method.

**Test conditions.** Simulation backend, demo topology (15 links), released model, 5 test seeds fixed in the code, 15%
chance per round of a transient. Per seed and condition: the 84 single-fault scenarios of
[`results/bench.json`](../results/bench.json) (each with 6 calm rounds, a 6-minute fault, so that 5-minute windows can
fire, and 1 minute after repair) and a 24-hour normal run. Under standard conditions also 60 random two-fault cases
(drawn as in the robustness suite) and 12 device-down cases per seed. That makes 420 faults and 5 simulated days
per condition, 300 two-fault cases and 60 device-down cases.

| Condition | What changes | Parameters |
|---|---|---|
| Standard | Nothing | as above |
| ICMP 5% new | Routers drop 5% of ICMP replies, starting after the baseline was learned | `icmp_drop=0.05` |
| ICMP 5% known | The same limit is already present while the baseline is learned | `icmp_drop=0.05`, relearned |
| 15 s transients | Every transient lasts 3 rounds | `hold=3` |
| Jitter 1 ms | Background jitter of 1 ms on every link, baseline relearned on site | `sigma=1.0` |
| Busy-hour drift | Network-wide delay rises and falls by 1 ms per link over a day | `swing_ms=1.0` |

**Scoring.** As in `bench.json`, the first alarm decides: *link* means the first alarm within the fault names the
faulty link, *link + type* also the type. *Link in any alarm* is the more generous count. Alarms per fault counts every
alarm raised during the fault and the minute after it; a baseline alarm stays up for 1 minute after the entity was last
inferred, so flapping does not inflate it. False alarms are alarms in the 24-hour normal runs; per-target rules count
one per target.

### Results on the 84 single-fault scenarios, standard conditions

Tuned parameters unless stated, 84 scenarios × 5 seeds = 420 faults and 5 simulated days per row. Fewer than 1 alarm per
fault means that some faults raised no alarm. The low-FA point is listed where it differs from the tuned setting.

| Method | Detected % | Link % | Link + type % | Link in any alarm % | Alarms per fault, mean / max | Time to first alarm, s | False alarms per day |
|---|---|---|---|---|---|---|---|
| Threshold, Zabbix ICMP defaults | 33.1 | n/a | n/a | n/a | 1.87 / 36 | 100.2 | 0.00 |
| Threshold, tuned | 73.3 | n/a | n/a | n/a | 4.86 / 36 | 90.7 | 0.00 |
| Alarm grouping | 67.1 | 67.1 | 67.1 | 67.1 | 0.68 / 2 | 121.4 | 0.00 |
| Boolean tomography (SCFS) | 62.9 | 59.0 | 58.6 | 62.9 | 0.85 / 8 | 65.7 | 0.00 |
| Greedy set cover (SCORE) | 62.9 | 59.0 | 58.6 | 62.9 | 0.85 / 8 | 65.7 | 0.00 |
| NetBouncer-style regression | 67.9 | 67.9 | 67.9 | 68.1 | 0.72 / 2 | 270.0 | 0.00 |
| 007-style voting | 68.6 | 68.6 | 68.6 | 68.6 | 0.69 / 3 | 90.4 | 0.00 |
| Alarm grouping + EMS rules | 95.7 | 95.7 | 95.7 | 95.7 | 0.96 / 2 | 73.8 | 0.00 |
| SCFS + EMS rules | 82.1 | 81.0 | 80.5 | 82.1 | 0.89 / 4 | 39.1 | 0.00 |
| SCORE + EMS rules | 82.1 | 81.0 | 80.5 | 82.1 | 0.89 / 4 | 39.1 | 0.00 |
| NetBouncer + EMS rules | 96.4 | 96.4 | 96.4 | 96.7 | 1.00 / 2 | 150.4 | 0.00 |
| 007 + EMS rules | 97.1 | 97.1 | 97.1 | 97.1 | 0.98 / 3 | 57.6 | 0.00 |
| 007-style voting, low-FA point | 59.5 | 57.1 | 57.1 | 58.3 | 0.64 / 4 | 93.1 | 0.00 |
| 007 + EMS rules, low-FA point | 88.1 | 85.7 | 85.7 | 86.9 | 0.92 / 4 | 56.0 | 0.00 |
| EdgeFDL | 100.0 | 100.0 | 100.0 | 100.0 | 1.00 / 1 | 38.5 | 0.00 |
| EdgeFDL without deep probes | 100.0 | 100.0 | 100.0 | 100.0 | 1.00 / 2 | 39.2 | n/a |

With default parameters:

| Method, default parameters | Link % | Link + type % | Alarms per fault, mean / max | Time to first alarm, s | False alarms per day | 15 s transients: link + type % / false alarms per day |
|---|---|---|---|---|---|---|
| Alarm grouping | 69.0 | 69.0 | 0.79 / 3 | 48.5 | 52.2 | 19.3 / 188.8 |
| Boolean tomography (SCFS) | 65.2 | 64.5 | 1.76 / 25 | 25.1 | 69.2 | 41.0 / 2170.8 |
| Greedy set cover (SCORE) | 66.7 | 66.2 | 1.31 / 9 | 25.0 | 71.2 | 42.6 / 2210.6 |
| NetBouncer-style regression | 63.3 | 63.3 | 0.75 / 2 | 270.0 | 27.4 | 43.8 / 154.6 |
| 007-style voting | 40.2 | 37.9 | 7.15 / 12 | 39.9 | 1655.0 | 29.5 / 1642.6 |
| Alarm grouping + EMS rules | 98.3 | 98.3 | 1.08 / 3 | 34.5 | 52.2 | 58.1 / 184.0 |
| SCFS + EMS rules | 92.9 | 92.6 | 2.04 / 25 | 18.8 | 69.2 | 59.5 / 2166.0 |
| SCORE + EMS rules | 95.2 | 94.8 | 1.60 / 9 | 18.8 | 71.2 | 60.2 / 2205.8 |
| NetBouncer + EMS rules | 92.1 | 92.1 | 1.04 / 2 | 148.0 | 27.4 | 78.1 / 149.8 |
| 007 + EMS rules | 65.5 | 64.8 | 7.11 / 13 | 28.4 | 1655.0 | 61.2 / 1637.8 |

Reading these two tables:

- **EdgeFDL:** 420 of 420 correct (84 of 84 on each seed), one alarm per fault, no false alarm in 5 simulated days, 38.5 s
  to the first alarm on average. The same engine without deep probes is also at 100.0%.
- **No baseline reaches accuracy, speed and false alarms together.** At default settings with EdgeFDL's own detector
  thresholds and persistence, the best link + type figure is 98.3% for alarm grouping + EMS rules, in 34.5 s, at 52.2
  false alarms per day. On the same 24-hour runs that detector triggers about 60 times a day and EdgeFDL's agent turns
  none of the triggers into an alarm. Tuned to no false alarm on these streams, the best figures are 97.1% in 57.6 s
  (007-style voting + EMS rules), 96.4% in 150.4 s (regression + EMS rules) and 95.7% in 73.8 s (grouping + EMS rules).
  The selected settings use persistence or windows of 1 to 5 minutes.
- **Ties.** Link down, route drift and bandwidth degradation are at 100% for every method with EMS rules: they are short
  rules on the LSDB and the bandwidth estimates, and EdgeFDL adds no accuracy there. Delay and jitter reach 100% with
  grouping, regression and voting once both interfaces of every link are probed. A device-down case is named in 60 of 60
  by every link-naming method (see below).
- **Where EdgeFDL loses.** The EMS rules are faster on the classes they cover: link down and route drift in 10 s against
  EdgeFDL's 30 s, bandwidth in 40 s against 60 s.
- **What the existing methods miss.** Ping paths alone carry no evidence of bandwidth degradation, and a route drift
  leaves the paths healthy, so both are at 0% without the EMS rules. The 15% loss faults on the access-ring links L8, L9
  and L10, each crossed by one monitored path, are not found by the best tuned settings (76.0–84.0% on loss), while
  EdgeFDL gets them under standard conditions. Per-target thresholds name no link and raise 4.86 alarms per fault on
  average, up to 36.

Correct link and type by fault type, %, standard conditions, 5 seeds (75 faults per type, 45 for route drift, 25 on the
backup link L3):

| Method | Link down | Delay | Loss | Jitter | Band- width | Route drift | L3 (all types) |
|---|---|---|---|---|---|---|---|
| Threshold, Zabbix ICMP defaults | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| Threshold, tuned | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| Alarm grouping | 100.0 | 100.0 | 76.0 | 100.0 | 0.0 | 0.0 | 80.0 |
| Boolean tomography (SCFS) | 100.0 | 94.7 | 38.7 | 94.7 | 0.0 | 0.0 | 64.0 |
| Greedy set cover (SCORE) | 100.0 | 94.7 | 38.7 | 94.7 | 0.0 | 0.0 | 64.0 |
| NetBouncer-style regression | 100.0 | 100.0 | 80.0 | 100.0 | 0.0 | 0.0 | 80.0 |
| 007-style voting | 100.0 | 100.0 | 84.0 | 100.0 | 0.0 | 0.0 | 80.0 |
| Alarm grouping + EMS rules | 100.0 | 100.0 | 76.0 | 100.0 | 100.0 | 100.0 | 100.0 |
| SCFS + EMS rules | 100.0 | 96.0 | 0.0 | 94.7 | 100.0 | 100.0 | 80.0 |
| SCORE + EMS rules | 100.0 | 96.0 | 0.0 | 94.7 | 100.0 | 100.0 | 80.0 |
| NetBouncer + EMS rules | 100.0 | 100.0 | 80.0 | 100.0 | 100.0 | 100.0 | 100.0 |
| 007 + EMS rules | 100.0 | 100.0 | 84.0 | 100.0 | 100.0 | 100.0 | 100.0 |
| 007-style voting, low-FA point | 100.0 | 100.0 | 20.0 | 100.0 | 0.0 | 0.0 | 60.0 |
| 007 + EMS rules, low-FA point | 100.0 | 100.0 | 20.0 | 100.0 | 100.0 | 100.0 | 80.0 |
| EdgeFDL | 100.0 | 100.0 | 100.0 | 100.0 | 100.0 | 100.0 | 100.0 |

Mean time to first alarm by fault type, s, standard conditions:

| Method | Link down | Delay | Loss | Jitter | Band- width | Route drift |
|---|---|---|---|---|---|---|
| Threshold, tuned | 88 | 88 | 101 | 88 | n/a | 89 |
| Alarm grouping | 119 | 118 | 133 | 118 | n/a | n/a |
| Boolean tomography (SCFS) | 60 | 59 | 91 | 64 | n/a | n/a |
| Greedy set cover (SCORE) | 60 | 59 | 91 | 64 | n/a | n/a |
| NetBouncer-style regression | 270 | 270 | 270 | 270 | n/a | n/a |
| 007-style voting | 90 | 90 | 92 | 90 | n/a | n/a |
| Alarm grouping + EMS rules | 10 | 118 | 133 | 120 | 40 | 10 |
| SCFS + EMS rules | 10 | 60 | n/a | 64 | 40 | 10 |
| SCORE + EMS rules | 10 | 60 | n/a | 64 | 40 | 10 |
| NetBouncer + EMS rules | 10 | 270 | 270 | 270 | 40 | 10 |
| 007 + EMS rules | 10 | 90 | 92 | 90 | 40 | 10 |
| 007-style voting, low-FA point | 90 | 90 | 121 | 90 | n/a | n/a |
| 007 + EMS rules, low-FA point | 10 | 90 | 121 | 90 | 40 | 10 |
| EdgeFDL | 30 | 33 | 40 | 34 | 60 | 30 |

### Results under harsh conditions

Link + type correct, % / false alarms per simulated day:

| Method | Standard | ICMP 5% new | ICMP 5% known | 15 s transients | Jitter 1 ms | Busy-hour drift |
|---|---|---|---|---|---|---|
| Threshold, Zabbix ICMP defaults | n/a / 0.0 | n/a / 0.0 | n/a / 0.2 | n/a / 765.4 | n/a / 0.0 | n/a / 0.0 |
| Threshold, tuned | n/a / 0.0 | n/a / 0.0 | n/a / 0.0 | n/a / 0.6 | n/a / 0.0 | n/a / 0.0 |
| Alarm grouping | 67.1 / 0.0 | 58.8 / 0.0 | 58.1 / 0.0 | 64.5 / 5.0 | 66.2 / 0.0 | 67.4 / 0.0 |
| Boolean tomography (SCFS) | 58.6 / 0.0 | 52.6 / 0.0 | 55.0 / 0.0 | 54.0 / 5.4 | 60.2 / 0.0 | 60.5 / 0.0 |
| Greedy set cover (SCORE) | 58.6 / 0.0 | 52.6 / 0.0 | 55.0 / 0.0 | 54.0 / 5.4 | 60.2 / 0.0 | 60.5 / 0.0 |
| NetBouncer-style regression | 67.9 / 0.0 | 68.3 / 0.0 | 69.0 / 0.0 | 64.8 / 5.6 | 68.1 / 0.0 | 68.1 / 0.0 |
| 007-style voting | 68.6 / 0.0 | 62.1 / 0.0 | 61.9 / 0.0 | 65.7 / 26.4 | 68.3 / 0.0 | 68.8 / 0.0 |
| Alarm grouping + EMS rules | 95.7 / 0.0 | 87.4 / 0.0 | 86.7 / 0.0 | 93.6 / 0.2 | 94.5 / 0.0 | 96.0 / 0.0 |
| SCFS + EMS rules | 80.5 / 0.0 | 81.0 / 0.0 | 80.7 / 0.0 | 76.9 / 0.2 | 80.7 / 0.0 | 81.7 / 0.0 |
| SCORE + EMS rules | 80.5 / 0.0 | 81.0 / 0.0 | 80.7 / 0.0 | 76.9 / 0.2 | 80.7 / 0.0 | 81.7 / 0.0 |
| NetBouncer + EMS rules | 96.4 / 0.0 | 96.7 / 0.0 | 97.1 / 0.0 | 94.0 / 0.0 | 96.7 / 0.0 | 96.7 / 0.0 |
| 007 + EMS rules | 97.1 / 0.0 | 91.2 / 0.0 | 90.5 / 0.0 | 95.0 / 21.6 | 96.9 / 0.0 | 97.4 / 0.0 |
| 007-style voting, low-FA point | 57.1 / 0.0 | 54.3 / 0.0 | 55.0 / 0.0 | 56.7 / 4.8 | 58.6 / 0.0 | 58.3 / 0.0 |
| 007 + EMS rules, low-FA point | 85.7 / 0.0 | 82.9 / 0.0 | 83.6 / 0.0 | 85.7 / 0.0 | 87.1 / 0.0 | 86.9 / 0.0 |
| EdgeFDL | 100.0 / 0.0 | 95.2 / 0.2 | 96.9 / 0.8 | 99.5 / 9.6 | 100.0 / 0.0 | 100.0 / 0.0 |

- **ICMP rate limiting is where EdgeFDL loses on accuracy:** 95.2% and 96.9% against 96.7% and 97.1% for regression + EMS
  rules, which takes about 151 s where EdgeFDL takes about 40 s. All 33 EdgeFDL errors in these two conditions are 15%
  loss faults: 24 are not alarmed (L8, L9, L10) and 9 are reported on a neighbouring link.
- **15 s transients are where EdgeFDL loses on false alarms:** 9.6 per day against 0.0 for regression + EMS rules, 0.2 for
  grouping + EMS rules and SCFS + EMS rules, and 0.6 for tuned thresholds, whose 1 to 5 minute persistence outlasts a
  15 s transient. EdgeFDL stays at 99.5% correct in this condition, against 94.0% and 93.6%, and is about 2 to 4 times
  faster. 007-style voting + EMS rules has 95.0% correct at 21.6 false alarms per day. At default parameters every
  baseline raises about 150 or more false alarms per day here.
- **Background jitter and busy-hour drift** change little for any method; EdgeFDL stays at 100.0% with no false alarm.
  All 2,520 faults over the six conditions: EdgeFDL 2,485 correct (98.6%), regression + EMS rules 2,426 (96.3%).

### Two simultaneous faults and device down

300 two-fault cases and 60 device-down cases, standard conditions. *First correct*: the first alarm names one of the
faults with its type. *Both*: both faults are named, with or without types, by any alarm during the fault.

| Method | Dual: first correct % | Dual: both links % | Dual: both + types % | Dual: alarms mean / max | Dual: wrong alarms | Device: named % | Device: alarms mean / max |
|---|---|---|---|---|---|---|---|
| Threshold, Zabbix ICMP defaults | n/a | n/a | n/a | 3.71 / 36 | 0.00 | n/a | 8.67 / 10 |
| Threshold, tuned | n/a | n/a | n/a | 7.72 / 37 | 0.00 | n/a | 11.50 / 19 |
| Alarm grouping | 73.3 | 12.3 | 12.3 | 0.97 / 2 | 0.09 | 100.0 | 1.67 / 2 |
| Boolean tomography (SCFS) | 80.7 | 26.0 | 22.7 | 1.45 / 7 | 0.16 | 100.0 | 1.18 / 3 |
| Greedy set cover (SCORE) | 80.7 | 26.0 | 22.7 | 1.45 / 7 | 0.16 | 100.0 | 1.22 / 3 |
| NetBouncer-style regression | 88.3 | 45.7 | 41.7 | 1.43 / 4 | 0.07 | 100.0 | 3.33 / 4 |
| 007-style voting | 68.7 | 6.0 | 6.0 | 1.03 / 3 | 0.26 | 100.0 | 2.33 / 3 |
| Alarm grouping + EMS rules | 94.0 | 61.7 | 61.7 | 1.66 / 3 | 0.09 | 100.0 | 1.67 / 2 |
| SCFS + EMS rules | 97.7 | 67.0 | 66.7 | 1.86 / 7 | 0.06 | 100.0 | 1.15 / 2 |
| SCORE + EMS rules | 97.7 | 67.0 | 66.7 | 1.86 / 7 | 0.06 | 100.0 | 1.17 / 2 |
| NetBouncer + EMS rules | 98.7 | 95.3 | 92.0 | 2.02 / 4 | 0.06 | 100.0 | 3.33 / 4 |
| 007 + EMS rules | 90.7 | 57.7 | 57.7 | 1.75 / 3 | 0.26 | 100.0 | 2.33 / 3 |
| 007-style voting, low-FA point | 65.7 | 6.3 | 5.0 | 0.96 / 4 | 0.20 | 100.0 | 2.33 / 3 |
| 007 + EMS rules, low-FA point | 90.7 | 53.7 | 52.3 | 1.66 / 4 | 0.20 | 100.0 | 2.33 / 3 |
| EdgeFDL | 100.0 | 97.3 | 97.3 | 1.97 / 2 | 0.00 | 100.0 | 1.03 / 2 |
| EdgeFDL without deep probes | 100.0 | 95.7 | 95.7 | 1.96 / 2 | 0.00 | n/a | n/a |

EdgeFDL reports both faults with their types in 97.3% of the two-fault cases with no wrong alarm; the next best is
regression + EMS rules at 92.0%, with 0.06 wrong alarms per case, and the others stay at 66.7% or below. EdgeFDL without
deep probes reaches 95.7%. Device down is a tie on naming the device, since every link-naming baseline has the device
rule; EdgeFDL raises 1.03 alarms per case, SCFS and SCORE + EMS rules 1.15 and 1.17, regression + EMS rules 3.33.

EdgeFDL used 1.05 deep probes per fault on the standard single-fault cases, 0.22 of them rechecks for clearing an
alarm.

### Consistency with the published self-test

In this harness EdgeFDL's first alarm has the right link and type in 84 of 84 scenarios on each of the 5 seeds, with a
mean time to alarm of 38.5 s, against 84 of 84 and 37.9 s in [`results/bench.json`](../results/bench.json) (seed 7). It is
the same engine, model and scenario list. The harness uses other seeds and keeps every fault for 6 minutes instead of
moving on after the first alarm and its clearing, so the times differ only through the timing of transients. The harsh
figures agree with [`results/robust.json`](../results/robust.json) within the spread between seeds: ICMP 5% new 95.2%
here and 96.4% there, ICMP 5% known 96.9% and 98.8%, 15 s transients 9.6 and 9 false alarms per day. A repeated run gave
identical EdgeFDL figures.

### What this comparison does not cover

- **The products.** These are re-implementations of methods on one kind of input, active probes. Zabbix, Prometheus,
  Alertmanager, Nightingale, LibreNMS and OpenNMS also receive SNMP traps, syslog and interface counters, so they learn
  of an interface going down from the device itself, often faster than any probe round. They offer inhibition rules,
  silences, routing, on-call integration, trigger dependencies, dashboards and long-term storage. ALEC's clustering
  engines work on any alarm source. None of that is measured here.
- **The settings the research methods were built for.** SCORE models shared-risk groups across IP and optical layers
  of a tier-1 backbone from many observations. NetBouncer probes thousands of paths of a Clos fabric from servers. 007
  relies on host agents seeing TCP retransmissions on flows that ECMP spreads over many links. A single vantage point on
  a 12-device network with 5 pings per target is not their setting: this page measures what their ideas do with the data
  an edge EMS has, not how good those systems are at home.
- **Physical networks.** All of this is simulation. EdgeFDL's agent was trained on samples from this simulator, and the
  baselines were tuned on streams from it. Training used randomized transients, ICMP rate limiting and background
  jitter; busy-hour drift was not part of training. No result here shows how any method behaves on a physical network.
- **Fault sizes.** Every scenario uses the fixed sizes of `bench.json` (delay +20 ms, loss 15%, jitter 15 ms, 20% of
  bandwidth left, the largest OSPF cost factor that changes a monitored path). Fault-size sweeps exist for EdgeFDL only
  ([results.md](results.md)).
- **Deep probes.** EdgeFDL may request a deep probe of one link; the baselines replay light measurements only. The row
  *EdgeFDL without deep probes* removes this difference for diagnosis on the standard single-fault and two-fault cases
  (its alarm-clearing rechecks may still probe, which cannot change whether or where an alarm is raised).
- **Coverage planning.** The NQA target on L3 comes from EdgeFDL's coverage planning, and every baseline gets it.
  Without that target no EMS probe crosses L3, and no path-based method here has evidence of delay, loss or jitter
  there. With it, L3 is at 100% for grouping, regression and voting with EMS rules.
- **Operator effort.** The tuned rows assume labelled fault streams from a simulator, the same kind of data EdgeFDL
  trains on. An operator without a simulator tunes by hand and lands elsewhere on the trade-off shown here.

### How each method was implemented

| Method | What the re-implementation does | Default parameters | Tuned grid |
|---|---|---|---|
| **Threshold** (Prometheus `for:`, Zabbix triggers) | One rule per target: RTT above baseline + margin, or lost pings, or RTT spread above margin, in k of the last n rounds. One alarm per target; it names no link, so link metrics are n/a | Zabbix "ICMP Ping" template [[Zabbix ICMP](#ref-zabbix-icmp)]: 5-min mean response above 150 ms, loss above 20% (5-min mean here, more sensitive than the template's 5-min minimum), 3 consecutive unreachable | 280 settings: margin, lost pings per round or windowed loss, k of n up to 24 of 24 |
| **Alarm grouping** (Alertmanager `group_by`, OpenNMS ALEC) | The same per-target alarms, clustered into one situation by time (`group_wait`, `group_interval`) and topology distance between the alarmed devices. One notification per situation; the root-cause candidate is the deepest link common to all alarmed paths | `group_wait` 30 s and `group_interval` 5 min (Alertmanager defaults [[Alertmanager](#ref-alertmanager)]), 2 hops, path state from EdgeFDL's own detector thresholds, 3 of 6 | 864 settings: path state, `group_wait`, hops |
| **SCFS** (boolean tomography, smallest consistent failure set) [[Duffield 2006](#ref-duffield)] | Links on good paths are exonerated; the smallest link set that explains all bad paths is reported, solved exactly. In the default, ties go to the link nearest the EMS, as on a tree | Path state from EdgeFDL's detector, 3 of 6 | 576 settings: path state, how strict "good" is, tie towards the near or the far link |
| **SCORE** (greedy set cover) [[Kompella 2005](#ref-kompella2005), [Kompella 2007](#ref-kompella)] | Risk groups are links. Among links whose hit ratio is above the error threshold, pick the one covering the most unexplained bad paths; repeat until all are explained. The hit ratio counts only paths whose state is clear | Error threshold 0.6 (the paper's simulations), path state from EdgeFDL's detector | 432 settings: path state, error threshold |
| **NetBouncer-style regression** [[Tan 2019](#ref-netbouncer)] | The paper's coordinate descent on Σ(yⱼ − Πxᵢ)² + λΣxᵢ(1 − xᵢ) on path success rates over a window. Added here so that the method can see delay at all: the same regularized fit on RTT excess, Σ(eⱼ − Σdᵢ)² + λΣdᵢ with dᵢ ≥ 0 | λ = 1, link success ≤ 0.99 (the 1% drop criterion), 5-min window (paper); 1 ms per crossing for the added delay fit | 144 settings: window up to 5 min, λ, loss and delay thresholds |
| **007-style voting** [[Arzani 2018](#ref-007)] | A target with a bad round is a bad flow and gives 1/h votes to each link on its path, tallied per epoch. Algorithm 1: take the most-voted link while it holds at least τ of all votes, then remove the flows it explains. With one vantage point upstream links tie with the faulty link, which ECMP spreading avoids in the original setting; ties go to the deepest link, the most favourable choice | 30 s epoch, τ = 1% (paper), any bad round votes | 432 settings: path state, epoch up to 2 min, bad rounds per epoch, τ |

### Reproduce

```bash
python -m edgefdl compare --model results/model.pt --out runs/compare --workers 24   # about 14 min with 24 workers
python tools/figures/compare.py --data runs/compare/compare.json --tables            # figure and the tables above
```

`--seeds`, `--rounds`, `--dual`, `--node-reps` and `--conditions` shorten a run. The per-case rows are written to
`runs/compare/rows.json`.

## Capability comparison

| | General monitoring and alerting<br>Zabbix · Prometheus + Alertmanager · Nightingale | Network management and alarm correlation<br>LibreNMS · OpenNMS ALEC | Active performance measurement<br>perfSONAR · SmokePing | **EdgeFDL** |
|---|---|---|---|---|
| Data source | Monitored items: host agents, exporters, SNMP, ICMP checks | Device events and counters: SNMP polling, traps, syslog | End-to-end measurements between measurement hosts, or from one host to targets | Active probes: pings and traceroutes from the single EMS; packet trains and on-demand deep probes between the two ends of a link; the read-only OSPF LSDB |
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
and trains a per-network model, so the model exists before the network has had its first fault. In release 0.1.2
the online service measures that equivalent network; connecting it to the physical network is on the
[roadmap](../ROADMAP.md).

### 4. Read-only, low overhead

EdgeFDL never writes device configuration. Its light measurements start at the EMS; only the packet-train bandwidth
estimate (3 links per round) and on-demand deep probes use a probe endpoint at a link end. On the demo topology the
measurement traffic is 0.222% of the narrowest 100 Mbps link even if all of it crossed that link, computation takes
0.0868% of one core, and one forward pass of the agent has a p99 of 0.575 ms, which fits a small EMS at the edge.

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

Example: send alarms to a syslog collector and a webhook receiver, and listen on the EMS's address in a management
network that a firewall restricts, so that a Prometheus server there can scrape `/metrics` (addresses are
documentation examples). The API has no authentication, so `--no-inject` disables the demonstration
fault-injection endpoints while the page is reachable by others:

```bash
python -m edgefdl serve --model results/model.pt --backend sim --host 192.0.2.30 --no-inject \
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
- <a id="ref-kompella2005"></a>R. R. Kompella, J. Yates, A. Greenberg, A. C. Snoeren. IP fault localization via risk
  modeling. *USENIX NSDI 2005*. <https://www.usenix.org/conference/nsdi-05/ip-fault-localization-risk-modeling>
- <a id="ref-netbouncer"></a>C. Tan, Z. Jin, C. Guo, et al. NetBouncer: active device and link failure localization in
  data center networks. *USENIX NSDI 2019*, 599–614. <https://www.usenix.org/conference/nsdi19/presentation/tan>
- <a id="ref-007"></a>B. Arzani, S. Ciraci, L. Chamon, et al. 007: democratically finding the cause of packet drops.
  *USENIX NSDI 2018*. <https://www.usenix.org/conference/nsdi18/presentation/arzani>
- <a id="ref-batfish"></a>A. Fogel, S. Fung, L. Pedrosa, et al. A general approach to network configuration analysis.
  *USENIX NSDI 2015*. <https://www.usenix.org/conference/nsdi15/technical-sessions/presentation/fogel>

Default parameters of the measured comparison:

- <a id="ref-zabbix-icmp"></a>Zabbix "ICMP Ping" template: `{$ICMP_RESPONSE_TIME_WARN}` 0.15 s, `{$ICMP_LOSS_WARN}` 20,
  unavailable on `max(/ICMP Ping/icmpping,#3)=0`. <https://www.zabbix.com/integrations/ping>
- <a id="ref-alertmanager"></a>Alertmanager configuration: `group_wait` 30 s, `group_interval` 5 min.
  <https://prometheus.io/docs/alerting/latest/configuration/>
