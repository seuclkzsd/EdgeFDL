# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""多告警 vs 单告警（--single-alarm）同种子鲁棒性对比与通过判据。
用法: python scripts/compare_robust.py --multi a.json b.json ... --single a.json b.json ... [--json out.json]
各列表按种子一一对应。判据与结果见 docs/results.md；哪些改动必须运行本对比、输出须 ALL PASS，见 GOVERNANCE.md。精测与恢复时间两项按"不多于 / 不慢于"判定（pass_）；
另给字面双侧口径（|差| 在 10% / 5 s 以内）的结果 pass_literal，不计入 ALL PASS，单独汇总为 passed_all_literal。"""
import argparse
import json
import math
import sys


def load(ps):
    return [json.load(open(p, encoding="utf-8")) for p in ps]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--multi", nargs="+", required=True)
    ap.add_argument("--single", nargs="+", required=True)
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    M, S = load(a.multi), load(a.single)
    seeds = [m["config"]["seed"] for m in M]
    assert seeds == [s["config"]["seed"] for s in S], "种子不一致"
    rows, ok_all = [], True

    def crit(name, mv, sv, ok, note="", literal=None):
        nonlocal ok_all
        ok_all &= bool(ok)
        rows.append(dict(criterion=name, multi=mv, single=sv, pass_=bool(ok), note=note,
                         pass_literal=bool(ok) if literal is None else bool(literal)))

    on = lambda r, k: r["online"][k]
    # 双故障
    for key, thr in (("all_reported", 0.95), ("first_ok", 0.99), ("no_wrong_alarm", 0.98), ("partial_clear_ok", 0.95)):
        mv = [on(m, "dual")["双链路同时故障"]["summary"].get(key) for m in M]
        sv = [on(s, "dual")["双链路同时故障"]["summary"].get(key) for s in S]
        crit(f"dual.{key}>={thr}", mv, sv, all(v is not None and v >= thr for v in mv))
    # 设备宕机
    mv = [(on(m, "node")["设备整机宕机"]["summary"]["all_reported"], on(m, "node")["设备整机宕机"]["summary"]["no_wrong_alarm"]) for m in M]
    sv = [(on(s, "node")["设备整机宕机"]["summary"]["all_reported"], on(s, "node")["设备整机宕机"]["summary"]["no_wrong_alarm"]) for s in S]
    crit("node.all_reported==1 & no_wrong==1", mv, sv, all(x == (1.0, 1.0) for x in mv))
    # 单故障持续观察
    g = lambda r: (r["summary"]["mean_alarms_per_case"], r["summary"]["no_wrong_alarm"], r["summary"]["extra_alarm_rate"])
    mv = [g(on(m, "single")["单故障持续观察"]) for m in M]
    sv = [g(on(s, "single")["单故障持续观察"]) for s in S]
    crit("single_watch alarms/case==1 & no_wrong==1", mv, sv, all(x[0] == 1.0 and x[1] == 1.0 for x in mv))
    # 设备宕机 + 另一条链路故障（双故障同口径）；设备恢复而仍有链路中断时改报链路中断（不差于单告警，且不残留设备宕机）
    if all("node_dual" in m["online"] for m in M + S):
        for key, thr in (("all_reported", 0.95), ("no_wrong_alarm", 0.98), ("partial_clear_ok", 0.95)):
            mv = [on(m, "node_dual")["设备宕机+链路故障"]["summary"].get(key) for m in M]
            sv = [on(s, "node_dual")["设备宕机+链路故障"]["summary"].get(key) for s in S]
            crit(f"node_dual.{key}>={thr}", mv, sv, all(v is not None and v >= thr for v in mv))
    if all("node_back" in m["online"] for m in M + S):
        g = lambda r: (r["summary"]["downgrade_ok"], r["summary"]["stale_node"])
        mv = [g(on(m, "node_back")["设备恢复仍有链路中断"]) for m in M]
        sv = [g(on(s, "node_back")["设备恢复仍有链路中断"]) for s in S]
        crit("node_back downgrade_ok>=single & stale_node==0", mv, sv,
             sum(x[0] for x in mv) >= sum(x[0] for x in sv) and all(x[1] == 0 for x in mv))
    # 长跑：逐条件按种子求和；计数容差 2√(单告警计数, 至少 1)，暴露率容差 max(0.1 个百分点, 单告警的 25%)
    for name in on(M[0], "long"):
        fm = sum(on(m, "long")[name]["false_alarms"] for m in M)
        fs = sum(on(s, "long")[name]["false_alarms"] for s in S)
        em = round(sum(on(m, "long")[name]["false_alarm_exposure_pct"] for m in M) / len(M), 3)
        es = round(sum(on(s, "long")[name]["false_alarm_exposure_pct"] for s in S) / len(S), 3)
        ok = fm <= fs + 2 * math.sqrt(max(fs, 1)) and em <= es + max(0.1, 0.25 * es)
        crit(f"long[{name}] false_alarms(sum)/exposure%(mean)",
             [on(m, "long")[name]["false_alarms"] for m in M] + [f"exp {em}"],
             [on(s, "long")[name]["false_alarms"] for s in S] + [f"exp {es}"], ok)
    # 场景：逐条件错例数（按种子求和）多告警至多多 1 个
    for name in on(M[0], "scen"):
        wm = [len(on(m, "scen")[name]["wrong"]) for m in M]
        ws = [len(on(s, "scen")[name]["wrong"]) for s in S]
        crit(f"scen[{name}] wrong cases", wm, ws, sum(wm) <= sum(ws) + 1)
    # 精测：双故障期间每轮精测次数（注入后 40 轮）不超过单告警的 110%
    pr = lambda r: r["summary"]["probes_per_round"]
    mv = [pr(on(m, "dual")["双链路同时故障"]) for m in M]
    sv = [pr(on(s, "dual")["双链路同时故障"]) for s in S]
    crit("dual probes/round <= 1.1x single", mv, sv, sum(mv) <= 1.1 * sum(sv),
         note=f"ratio {sum(mv) / max(sum(sv), 1e-9):.2f}", literal=abs(sum(mv) - sum(sv)) <= 0.1 * sum(sv))
    # 恢复时间：逐条件平均恢复时间不慢于单告警 5 s，丢包不慢于 10 s
    for name in on(M[0], "scen"):
        tm = [on(m, "scen")[name]["mean_time_to_clear_s"] for m in M]
        ts = [on(s, "scen")[name]["mean_time_to_clear_s"] for s in S]
        lm = [on(m, "scen")[name]["time_to_clear_by_type"].get("loss") for m in M]
        ls = [on(s, "scen")[name]["time_to_clear_by_type"].get("loss") for s in S]
        avg = lambda v: sum(v) / len(v)
        crit(f"scen[{name}] time_to_clear mean / loss (s)", [round(avg(tm), 1), round(avg(lm), 1)],
             [round(avg(ts), 1), round(avg(ls), 1)], avg(tm) <= avg(ts) + 5 and avg(lm) <= avg(ls) + 10,
             note=f"diff {avg(tm) - avg(ts):+.1f} s", literal=abs(avg(tm) - avg(ts)) <= 5 and abs(avg(lm) - avg(ls)) <= 10)
    w = max(len(r["criterion"]) for r in rows)
    lit = all(r["pass_literal"] for r in rows)
    for r in rows:
        tag = "" if r["pass_literal"] == r["pass_"] else f"  [字面双侧口径 {'PASS' if r['pass_literal'] else 'FAIL'}]"
        print(f"{'PASS' if r['pass_'] else 'FAIL'}  {r['criterion']:<{w}}  multi={r['multi']}  single={r['single']}"
              f"{'  ' + r['note'] if r['note'] else ''}{tag}")
    print("ALL PASS" if ok_all else "NOT ALL PASS", "seeds", seeds,
          "| 字面双侧口径:", "ALL PASS" if lit else "NOT ALL PASS")
    if a.json:
        json.dump(dict(seeds=seeds, passed_all=ok_all, passed_all_literal=lit, rows=rows),
                  open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
