#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""录制在线演示：启动真实的 edgefdl serve（仿真后端 + results/model.pt），经 REST 接口按脚本注入故障，
逐轮记录大屏轮询的全部接口应答（/api/status、/api/alarms?n=12、/api/series?link=<每条链路>），写入
docs/demo/frames.json。帧内容全部来自运行中的系统，没有手写数据；字幕里的数字由录制结果计算，
告警未按预期出现时如实写进 results/demo_replay.json 并以非零状态退出。

  python tools/demo/record.py                  # 默认 5 s 一轮（与 serve 默认一致），约 14 分钟
  python tools/demo/record.py --interval 0.25 --out /tmp/f.json --summary /tmp/s.json    # 试跑
  python tools/demo/record.py --work DIR --rebuild      # 不重新运行，由 DIR/raw.json 重新生成帧文件与字幕

"越过阈值的目标数"：每轮测量记录取自 /api/export/measure_log.csv，判据是判决器的逐目标判据
（FeatureEncoder.anomaly_matrix：RTT、抖动、丢包、不可达），基线取模型包自带基线，只计网管发起的 36 个目标。
"""
import argparse
import csv
import hashlib
import io
import json
import os
import platform
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections import Counter

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # 本机服务，不走代理
FORMAT = 1
POOL_MIN = 48                       # 序列化后不短于此长度的对象 / 数组进入共享池
SERIES_KEEP = 120                   # Engine.series 每个目标保留的轮数
LIMIT = 40                          # 注入后、修复后最多等待的轮数（与 robust.multi_fault 的 after 一致）
HOLD_ALARM, HOLD_CLEAR, LEAD_IN = 6, 4, 3

TYPE_ZH = {"delay": "时延劣化", "loss": "丢包劣化", "link_down": "链路中断", "route_drift": "路由漂移",
           "node_down": "设备宕机", "jitter": "抖动劣化", "bandwidth": "带宽劣化"}
TYPE_EN = {"delay": "delay degradation", "loss": "loss degradation", "link_down": "link down",
           "route_drift": "route drift", "node_down": "device down", "jitter": "jitter degradation",
           "bandwidth": "bandwidth degradation"}

# 故事脚本。取值与自测场景一致（bench.SCENARIO_VALUE：时延 20 ms、丢包 15%；路由漂移 10 倍）。
SCENES = (
    dict(id="normal", zh="网络正常", en="Normal network", focus="L1", hold=12),
    dict(id="degradation", zh="L1 性能劣化", en="Degradation of L1", focus="L1",
         faults=[dict(type="delay", link="L1", value=20.0)], expect=[("delay", "L1")]),
    dict(id="link_down", zh="链路中断与备份路径接管", en="Link down, backup path takes over", focus="L6",
         faults=[dict(type="link_down", link="L6")], expect=[("link_down", "L6")]),
    dict(id="route_drift", zh="路由漂移", en="Route drift", focus="L4",
         faults=[dict(type="route_drift", link="L4", value=10.0)], expect=[("route_drift", "L4")]),
    dict(id="two_faults", zh="两处故障同时发生", en="Two simultaneous faults", focus="L7",
         faults=[dict(type="delay", link="L5", value=20.0), dict(type="loss", link="L7", value=0.15)],
         expect=[("delay", "L5"), ("loss", "L7")], repair_first="L7"),
    dict(id="device_down", zh="设备宕机", en="A device goes down", focus="L6", node="acc3",
         faults=[dict(type="link_down", link=l) for l in ("L6", "L9", "L10", "L13")], expect=[("node_down", "acc3")]),
)


# ---------------------------------------------------------------------- 帧的无损压缩
def pack(v, pool, index):
    """自底向上把重复出现的子对象放进共享池，原位留 {"$": 序号}；unpack 还原后与原对象完全相等。"""
    if isinstance(v, dict):
        if "$" in v:
            raise ValueError("接口应答含保留键 $")
        v = {k: pack(x, pool, index) for k, x in v.items()}
    elif isinstance(v, list):
        v = [pack(x, pool, index) for x in v]
    else:
        return v
    s = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    if len(s) < POOL_MIN:
        return v
    i = index.get(s)
    if i is None:
        i = index[s] = len(pool)
        pool.append(v)
    return {"$": i}


def unpack(v, pool):
    """pack 的逆过程；池内条目只引用序号更小的条目。docs/demo/replay.js 里有同样的实现。"""
    if isinstance(v, list):
        return [unpack(x, pool) for x in v]
    if isinstance(v, dict):
        return unpack(pool[v["$"]], pool) if "$" in v else {k: unpack(x, pool) for k, x in v.items()}
    return v


def series_at(points, links, last_round, link):
    """由各目标的逐轮测量点还原某一轮时 GET /api/series?link= 的应答。"""
    return {t: [dict(round=p[0], rtt=p[1], loss=p[2]) for p in points[t] if p[0] <= last_round][-SERIES_KEEP:]
            for t in links[link]}


def compress(raw, topology):
    """raw: 逐帧 {status, alarms, series}。返回 frames.json 中的 pool / frames / series，并校验可无损还原。"""
    pool, index = [], {}
    links = {l["id"]: sorted({t for r in raw for t in r["series"][l["id"]]}) for l in topology["links"]}
    points = {}
    for r in raw:
        for resp in r["series"].values():
            for t, pts in resp.items():
                d = points.setdefault(t, {})
                for p in pts:
                    d[p["round"]] = [p["round"], p["rtt"], p["loss"]]
    points = {t: [d[k] for k in sorted(d)] for t, d in points.items()}
    frames = []
    for r in raw:
        last = max([p["round"] for resp in r["series"].values() for pts in resp.values() for p in pts], default=-1)
        frames.append(dict(status=pack(r["status"], pool, index), alarms=pack(r["alarms"], pool, index),
                           series_round=last))
    # 校验：还原结果与录到的接口应答逐帧完全相等
    for r, f in zip(raw, frames):
        assert unpack(f["status"], pool) == r["status"], "status 还原不一致"
        assert unpack(f["alarms"], pool) == r["alarms"], "alarms 还原不一致"
        for lid, resp in r["series"].items():
            assert series_at(points, links, f["series_round"], lid) == resp, f"series {lid} 还原不一致"
    return pool, frames, dict(links=links, points=points)


# ---------------------------------------------------------------------- 与运行中的系统对话
class Server:
    def __init__(self, args, log_dir):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        self.cmd = [sys.executable, "-m", "edgefdl", "serve", "--model", args.model, "--backend", "sim",
                    "--host", "127.0.0.1", "--port", str(port), "--log-dir", log_dir,
                    "--seed", str(args.seed), "--noise", str(args.noise)]
        if args.interval:
            self.cmd += ["--interval", str(args.interval)]
        self.log = open(os.path.join(log_dir, "serve.log"), "w")
        self.proc = subprocess.Popen(self.cmd, cwd=ROOT, stdout=self.log, stderr=subprocess.STDOUT)

    def raw(self, path):
        with OPENER.open(self.base + path, timeout=10) as r:
            return r.read()

    def get(self, path):
        return json.loads(self.raw(path))

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with OPENER.open(req, timeout=10) as r:
            out = json.loads(r.read())
        if not out.get("ok"):
            raise RuntimeError(f"{path} {body}: {out}")
        return out

    def wait_up(self, timeout=180):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.proc.poll() is not None:
                raise SystemExit(f"serve 已退出（返回码 {self.proc.returncode}），日志见 {self.log.name}")
            try:
                s = self.get("/api/status")
                if s["round"] >= 1:
                    return s
            except (OSError, ValueError):
                pass
            time.sleep(0.1)
        raise SystemExit("等待 serve 启动超时")

    def stop(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.log.close()


def alarm_keys(status):
    return {("node_down", a["node"]) if a["fault_type"] == "node_down" else (a["fault_type"], a["link_id"])
            for a in status["active_alarms"]}


class Recorder:
    def __init__(self, srv, interval, link_ids):
        self.srv, self.link_ids = srv, link_ids
        self.poll = min(0.1, interval / 20)
        self.settle = min(0.5, interval / 4)      # 等这一轮的研判做完（轮次号在测量后、研判前就已增加）
        self.raw = []
        self.round = srv.get("/api/status")["round"]
        self.skipped = 0

    def frame(self):
        """等下一轮结束，抓取大屏轮询的全部接口应答，存为一帧。"""
        while self.srv.get("/api/status")["round"] <= self.round:
            if self.srv.proc.poll() is not None:
                raise SystemExit("serve 意外退出")
            time.sleep(self.poll)
        time.sleep(self.settle)
        s = self.srv.get("/api/status")
        if self.raw and s["round"] != self.round + 1:
            self.skipped += s["round"] - self.round - 1
        self.round = s["round"]
        self.raw.append(dict(status=s, alarms=self.srv.get("/api/alarms?n=12"),
                             series={l: self.srv.get(f"/api/series?link={l}") for l in self.link_ids}))
        return s

    def rounds(self, n):
        for _ in range(n):
            self.frame()

    def until(self, cond, limit):
        for _ in range(limit):
            if cond(self.frame()):
                return True
        return False

    @property
    def i(self):
        return len(self.raw)                      # 下一帧的序号


def play(rec, srv, sc, log):
    """按脚本演一个场景，返回各阶段的帧序号（半开区间边界）。"""
    m = dict(id=sc["id"], zh=sc["zh"], en=sc["en"], focus=sc.get("focus"), start=rec.i)
    if not sc.get("faults"):
        rec.rounds(sc["hold"])
        m["end"] = rec.i
        return m
    rec.rounds(LEAD_IN)
    want = {tuple(k) for k in sc["expect"]}
    t0 = rec.raw[-1]["status"]["alarms_total"]
    for f in sc["faults"]:
        srv.post("/api/inject", f)
    m["inject"] = rec.i
    log(f"场景 {sc['id']}：已注入 {sc['faults']}（第 {rec.round} 轮）")
    rec.until(lambda s: s["alarms_total"] > t0, LIMIT)
    fa = next((i for i in range(m["inject"], rec.i) if rec.raw[i]["status"]["alarms_total"] > t0), None)
    m["first_alarm"] = fa
    ok = want <= alarm_keys(rec.raw[-1]["status"])
    if fa is not None and not ok:
        ok = rec.until(lambda s: want <= alarm_keys(s), LIMIT)
    m["expected_alarms_met"] = ok
    m["all_alarms"] = rec.i - 1 if ok else None
    rec.rounds(HOLD_ALARM)
    if sc.get("repair_first"):
        first = sc["repair_first"]
        srv.post("/api/clear", dict(link=first))
        m["repair_first"] = rec.i
        kx = next(k for k in want if k[1] == first)
        rec.until(lambda s: kx not in alarm_keys(s), LIMIT)
        m["first_cleared"] = rec.i - 1
        rec.rounds(HOLD_ALARM)
    srv.post("/api/clear", {})
    m["repair"] = rec.i
    log(f"场景 {sc['id']}：已修复（第 {rec.round} 轮）")
    gone = rec.until(lambda s: not s["active_alarms"], LIMIT)
    m["alarms_gone"] = rec.i - 1 if gone else None
    rec.until(lambda s: not s["detector"]["active"], LIMIT)
    rec.rounds(HOLD_CLEAR)
    m["end"] = rec.i
    return m


# ---------------------------------------------------------------------- 由测量日志算"越过阈值的目标数"
def threshold_view(csv_text, model, raw):
    """逐帧：越过逐目标阈值的目标数（网管发起的目标）、路径偏离基线的目标数、改道目标的路径、越过阈值的目标号。"""
    import numpy as np

    from edgefdl.agent.features import FeatureEncoder
    from edgefdl.cli import load_bundle
    _, topo, base, _, _ = load_bundle(model)
    enc = FeatureEncoder(topo, base)
    ids = [t.id for t in enc.targets]
    ems = [i for i, t in enumerate(enc.targets) if t.kind != "nqa"]
    rows = [r for r in csv.DictReader(io.StringIO(csv_text)) if r["kind"] == "ping"]
    R = max(int(r["round_id"]) for r in rows) + 1
    T = len(ids)
    sent, recv = np.zeros((R, T)), np.zeros((R, T))
    rtt, mdev = np.full((R, T), np.nan), np.full((R, T), np.nan)
    paths = {}
    for r in rows:
        k, ti = int(r["round_id"]), enc.tidx[r["target_id"]]
        sent[k, ti], recv[k, ti] = float(r["sent"]), float(r["recv"])
        if r["rtt_avg_ms"]:
            rtt[k, ti], mdev[k, ti] = float(r["rtt_avg_ms"]), float(r["rtt_mdev_ms"] or 0.0)
        if r["path"]:
            paths.setdefault(k, {})[ti] = tuple(r["path"].split(">"))
    an = enc.anomaly_matrix(sent, recv, rtt, mdev)[:, ems]
    over, moved, last, route, names = [], [], {}, {}, []
    for fr in raw:
        k = fr["status"]["round"] - 1             # 快照的 round 已加 1，测量记录的 round_id 从 0 起
        over.append(int(an[k].sum()) if k < R else None)
        names.append(sorted(ids[ems[j]] for j in np.nonzero(an[k])[0]) if k < R else [])
        for j in sorted(x for x in paths if x <= k):
            last.update(paths.pop(j))
        diff = {ti: p for ti, p in last.items() if base.paths[ti] and tuple(base.paths[ti]) != p}
        moved.append(len(diff))
        route[k] = {ids[ti]: (">".join([topo.vantage] + list(base.paths[ti])), ">".join([topo.vantage] + list(p)))
                    for ti, p in diff.items()}
    return over, moved, [route[fr["status"]["round"] - 1] for fr in raw], names, len(ems)


# ---------------------------------------------------------------------- 字幕与摘要
def mode(xs):
    xs = [x for x in xs if x is not None]
    return Counter(xs).most_common(1)[0][0] if xs else None


def fault_text(f):
    v, l = f.get("value"), f["link"]
    if f["type"] == "delay":
        return f"{l} 单向时延增加 {v:g} ms", f"{v:g} ms of extra one-way delay on {l}"
    if f["type"] == "loss":
        return f"{l} 丢包 {v:.0%}", f"{v:.0%} loss on {l}"
    if f["type"] == "route_drift":
        return f"{l} 的 OSPF 开销变为 {v:g} 倍", f"the OSPF cost of {l} multiplied by {v:g}"
    return f"{l} 中断", f"{l} down"


def faults_text(sc):
    if sc.get("node"):
        ls = [f["link"] for f in sc["faults"]]
        return f"设备 {sc['node']} 宕机（{'、'.join(ls)} 全部中断）", f"device {sc['node']} down ({', '.join(ls)} all down)"
    tx = [fault_text(f) for f in sc["faults"]]
    return "；".join(t[0] for t in tx), "; ".join(t[1] for t in tx)


def alarm_text(a):
    if a["fault_type"] == "node_down":
        return f"设备 {a['node']} 宕机", f"device {a['node']} down"
    return f"{a['link_id']} {TYPE_ZH[a['fault_type']]}", f"{a['link_id']} {TYPE_EN[a['fault_type']]}"


def summarize(m, sc, raw, over, moved, routes, names, total, interval):
    """由录到的帧计算场景摘要（写入 frames.json 与 results/demo_replay.json）。"""
    st = lambda i: raw[i]["status"]
    s = dict(m, focus=sc.get("focus"))
    if "inject" not in m:
        s.update(alarms_raised=st(m["end"] - 1)["alarms_total"] - st(m["start"])["alarms_total"],
                 over_threshold_max=max(x or 0 for x in over[m["start"]:m["end"]]))
        return s
    s["injected"] = sc["faults"]
    r0 = st(m["inject"] - 1)["round"]             # 注入在这一帧之后、下一轮测量之前
    fix = m.get("repair_first", m["repair"])      # 第一次修复：此前全部故障都在
    tgt = [over[i] for i in range(m["inject"], fix)]
    s.update(inject_round=r0, fault_rounds=len(tgt), over_threshold_mode=mode(tgt), over_threshold_max=max(tgt),
             over_threshold_mode_rounds=tgt.count(mode(tgt)),
             over_threshold_targets=next(names[i] for i in range(m["inject"], fix) if over[i] == mode(tgt)),
             unreachable_max=max(len(st(i)["unreachable"]) for i in range(m["inject"], fix)))
    seen = {}
    for i in range(m["inject"], m["end"]):
        for a in raw[i]["alarms"]:
            if a["first_round"] > r0:
                seen[a["alarm_id"]] = a
    fixed = {sc.get("repair_first"): st(fix - 1)["round"]}
    s["alarms"] = [dict(alarm_id=a["alarm_id"], fault_type=a["fault_type"], link_id=a["link_id"], node=a.get("node"),
                        first_round=a["first_round"], rounds_after_injection=a["first_round"] - r0,
                        seconds_after_injection=round((a["first_round"] - r0) * interval, 1),
                        confidence=a["confidence"], cleared_why=a.get("cleared_why"),
                        rounds_after_repair=(a["cleared_round"] - fixed.get(a["link_id"], st(m["repair"] - 1)["round"]))
                        if "cleared_round" in a else None)
                   for a in sorted(seen.values(), key=lambda a: (a["first_round"], a["alarm_id"]))]
    s["alarms_raised"] = len(s["alarms"])
    if m["first_alarm"] is not None:
        s["rounds_to_first_alarm"] = st(m["first_alarm"])["round"] - r0
        s["seconds_to_first_alarm"] = round(s["rounds_to_first_alarm"] * interval, 1)
    if m["alarms_gone"] is not None:
        s["rounds_to_clear"] = st(m["alarms_gone"])["round"] - st(m["repair"] - 1)["round"]
        s["seconds_to_clear"] = round(s["rounds_to_clear"] * interval, 1)
    i = fix - 1
    active = sorted(st(i)["active_alarms"], key=lambda a: (a["first_round"], a["alarm_id"]))
    s["alarms_before_repair"] = [alarm_text(a)[1] for a in active]
    s["alarms_match_injection"] = sorted(alarm_keys(st(i))) == sorted(tuple(k) for k in sc["expect"])
    s["routes_changed_before_repair"] = moved[i]
    s["route_example"] = None
    if routes[i]:                                 # 优先举业务主机的例子（目标号形如 L13.h3）
        t = sorted(routes[i], key=lambda x: (".h" not in x, x))[0]
        s["route_example"] = dict(target=t, baseline=routes[i][t][0], current=routes[i][t][1])
    s["rehearsal"] = next((a["advice"].get("rehearsal") for a in active if a.get("advice")), None)
    s["ospf_cost"] = {f["link"]: st(i)["link_state"][f["link"]]["cost"] for f in sc["faults"]
                      if f["type"] == "route_drift"}
    return s


def route_text(ex):
    if not ex:
        return "", ""
    return f"（{ex['target']}：{ex['baseline']} → {ex['current']}）", f" ({ex['target']}: {ex['baseline']} to {ex['current']})"


def alarm_caption(s, sc, topo, total):
    """告警发出后到第一次修复前的字幕 [(起始帧, 结束帧, 中文, 英文)]。"""
    live = [a for a in s["alarms"] if a["cleared_why"] != "升级为设备宕机告警"]
    zh = "、".join(alarm_text(a)[0] for a in live)
    en = "; ".join(alarm_text(a)[1] for a in live)
    sec, over = s["seconds_to_first_alarm"], s["over_threshold_mode"]
    n = len(s["alarms"])
    first, fix = s["first_alarm"], s.get("repair_first", s["repair"])
    moved, rt = s["routes_changed_before_repair"], route_text(s["route_example"])
    reach = ("全部目标仍可达", "All targets stay reachable") if not s["unreachable_max"] else \
        (f"{s['unreachable_max']} 个目标不可达", f"{s['unreachable_max']} targets unreachable")
    if sc["id"] == "degradation":
        return [(first, fix, f"{over} / {total} 个目标越过阈值，却都说不出故障在哪；EdgeFDL 只发 {n} 条告警：{zh}（注入后 {sec:g} 秒）。",
                 f"{over} of {total} targets cross their thresholds and none says where the fault is; "
                 f"EdgeFDL raises {n} alarm: {en}, {sec:g} s after the injection.")]
    if sc["id"] == "link_down":
        ok = (s["rehearsal"] or {}).get("feasible")
        return [(first, fix, f"{zh}（注入后 {sec:g} 秒）。{moved} 个目标已改走备份路径{rt[0]}；"
                             f"等价网络预演：备份接管{'可行' if ok else '不可行'}。",
                 f"{en}, {sec:g} s after the injection. {moved} targets moved to the backup path{rt[1]}; "
                 f"rehearsal on the equivalent network: takeover {'feasible' if ok else 'not feasible'}.")]
    if sc["id"] == "route_drift":
        lid, cost = next(iter(s["ospf_cost"].items()))
        c0 = next(l["cost"] for l in topo["links"] if l["id"] == lid)
        return [(first, fix, f"{zh}（注入后 {sec:g} 秒）：{lid} 的 OSPF 开销 {c0:g} → {cost:g}。{reach[0]}，{moved} 个目标换了路径{rt[0]}。",
                 f"{en}, {sec:g} s after the injection: the OSPF cost of {lid} went from {c0:g} to {cost:g}. "
                 f"{reach[1]}; {moved} take a new path{rt[1]}.")]
    if sc["id"] == "two_faults":
        both = s["all_alarms"] if s["all_alarms"] is not None else fix
        out = [(first, both, f"第一条告警：{alarm_text(live[0])[0]}（注入后 {sec:g} 秒）。系统扣除它的效应，继续研判残差。",
                f"First alarm: {alarm_text(live[0])[1]}, {sec:g} s after the injection. The system subtracts its effect "
                "and keeps diagnosing the residual.")]
        if s["all_alarms"] is not None:
            t2 = live[-1]["seconds_after_injection"]
            out.append((both, fix, f"第二条告警：{alarm_text(live[-1])[0]}（注入后 {t2:g} 秒）。两处故障，两条告警，各有证据和处置建议。",
                        f"Second alarm: {alarm_text(live[-1])[1]}, {t2:g} s after the injection. Two faults, two alarms, "
                        "each with its own evidence and advice."))
        return out
    k = len(sc["faults"])
    return [(first, fix, f"{sc['node']} 的 {k} 条链路全部中断，告警只有 {len(live)} 条：{zh}（注入后 {sec:g} 秒），覆盖这 {k} 条链路。",
             f"All {k} links of {sc['node']} are down and there is {len(live)} alarm: {en}, {sec:g} s after the injection, "
             f"covering the {k} links.")]


def captions(s, sc, n, N, topo, total):
    """一个场景的分段字幕 [(起始帧, 结束帧, 中文, 英文)]，数字都来自摘要 s。"""
    head = f"场景 {n}/{N} · {s['zh']}：", f"Scene {n}/{N} · {s['en']}: "
    if "inject" not in s:
        return [(s["start"], s["end"], head[0] + f"网管每轮探测全部 {total} 个目标，没有告警。",
                 head[1] + f"the EMS probes all {total} targets every round; no alarm.")]
    zh, en = faults_text(sc)
    over = s["over_threshold_mode"]
    first = s["first_alarm"]
    fix = s.get("repair_first", s["repair"])
    out = [(s["start"], s["inject"], head[0] + "网络正常，下一轮注入故障。", head[1] + "healthy; a fault is injected next."),
           (s["inject"], first if first is not None else fix,
            f"已注入：{zh}。{over} / {total} 个目标越过阈值；判决器持续观察，智能体研判并复核。",
            f"Injected: {en}. {over} of {total} targets cross their thresholds; the detector keeps watching, "
            "the agent diagnoses and re-checks.")]
    if first is None:
        out.append((fix, s["end"], "故障已修复。这次注入在等待时间内没有产生告警。",
                    "Fault repaired. This injection raised no alarm within the waiting time."))
        return out
    out += alarm_caption(s, sc, topo, total)
    if "repair_first" in s:
        lid = sc["repair_first"]
        fx = next(a for a in s["alarms"] if a["link_id"] == lid)
        keep = "、".join(alarm_text(a)[0] for a in s["alarms"] if a["link_id"] != lid)
        keep_en = "; ".join(alarm_text(a)[1] for a in s["alarms"] if a["link_id"] != lid)
        out.append((s["repair_first"], s["first_cleared"], f"只修复 {lid}，等它的告警按自身证据恢复。",
                    f"Only {lid} is repaired; its alarm has to clear on its own evidence."))
        out.append((s["first_cleared"], s["repair"], f"{alarm_text(fx)[0]} 的告警已恢复，{keep} 的告警继续保持。",
                    f"The alarm {alarm_text(fx)[1]} has cleared; {keep_en} stays active."))
    gone = s["alarms_gone"] if s["alarms_gone"] is not None else s["end"]
    out.append((s["repair"], gone, "故障已修复，系统按告警自身的证据确认恢复。",
                "Fault repaired; the system confirms recovery on the alarm's own evidence."))
    if s["alarms_gone"] is not None:
        out.append((gone, s["end"], f"告警已恢复：修复后 {s['seconds_to_clear']:g} 秒。",
                    f"Alarm cleared {s['seconds_to_clear']:g} s after the repair."))
    return [c for c in out if c[0] < c[1]]


def transient_captions(caps, raw, over, total):
    """网络无故障时只持续一轮的瞬时干扰（≥3 个目标越过阈值、前后两轮都没有）单独加一条字幕：判决器未触发、未发告警
    都由录到的帧判定，否则不加。"""
    calm = lambda i: not raw[i]["status"]["ground_truth"] and not raw[i]["status"]["active_alarms"]
    for i in range(1, len(raw) - 7):
        if not (calm(i - 1) and calm(i) and calm(i + 1) and over[i] >= 3 and over[i - 1] < 3 and over[i + 1] < 3):
            continue
        st = [raw[k]["status"] for k in range(i, i + 7)]
        if any(x["detector"]["active"] for x in st[:2]) or st[-1]["alarms_total"] != st[0]["alarms_total"]:
            continue
        c = next(x for x in caps if x["start"] <= i < x["end"])
        j = min(i + 2, c["end"])
        new = [dict(c, end=i), dict(start=i, end=j, zh=f"只持续一轮的瞬时干扰：{over[i]} / {total} 个目标越过阈值，判决器不触发，没有告警。",
                                    en=f"A one-round transient: {over[i]} of {total} targets cross their thresholds; "
                                       "the detector does not trigger and no alarm is raised."), dict(c, start=j)]
        k = caps.index(c)
        caps[k:k + 1] = [x for x in new if x["start"] < x["end"]]
    return caps


def git_rev():
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def build(args, raw, marks, topology, csv_text, interval, skipped):
    """由录到的原始应答生成 frames.json 与摘要。"""
    over, moved, routes, names, total = threshold_view(csv_text, args.model, raw)
    scenes = [summarize(m, sc, raw, over, moved, routes, names, total, interval) for m, sc in zip(marks, SCENES)]
    caps = []
    for n, (s, sc) in enumerate(zip(scenes, SCENES), 1):
        caps += [dict(start=a, end=b, zh=z, en=e) for a, b, z, e in captions(s, sc, n, len(SCENES), topology, total)]
    caps = transient_captions(caps, raw, over, total)
    pool, frames, series = compress(raw, topology)
    import numpy
    import torch

    import edgefdl
    with open(args.model, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    last = raw[-1]["status"]
    meta = dict(
        note="Recorded REST API responses of a real `edgefdl serve` run on the simulation backend; every frame comes "
             "from the running system. tools/demo/record.py regenerates this file.",
        recorded=args.recorded, edgefdl=edgefdl.__version__, commit=args.commit,
        model=last["model"], model_file="results/model.pt", model_sha256=sha, backend=last["backend"],
        topology=topology["name"], command="python -m edgefdl serve --model results/model.pt --backend sim "
                                           f"--seed {args.seed} --noise {args.noise}"
                                           + (f" --interval {args.interval}" if args.interval else ""),
        interval_s=interval, seed=args.seed, noise=args.noise, rounds=len(raw), skipped_rounds=skipped,
        python=platform.python_version(), torch=torch.__version__, numpy=numpy.__version__,
        engine_errors=last["errors"], alarms_total=last["alarms_total"], filtered_total=last["suppressed"],
        time_rule="seconds after an injection or repair = rounds x interval_s, as in the self-test",
        over_threshold_rule="per-target test of the detector (FeatureEncoder.anomaly_matrix: RTT, jitter, loss, "
                            "unreachable) against the baseline in the model bundle, for the EMS-side targets")
    out = dict(format=FORMAT, meta=meta, topology=topology, targets_total=total, scenes=scenes, captions=caps,
               over_threshold=over, routes_changed=moved, series=series, pool=pool, frames=frames)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    with open(args.summary, "w", encoding="utf-8") as f:
        json.dump(dict(meta=meta, targets_total=total, scenes=scenes), f, ensure_ascii=False, indent=1)
        f.write("\n")
    return scenes


def main():
    ap = argparse.ArgumentParser(description="录制在线演示帧（真实 serve + 仿真后端）")
    ap.add_argument("--model", default=os.path.join(ROOT, "results", "model.pt"))
    ap.add_argument("--interval", type=float, default=0.0, help="测量周期 s（默认不传，取模型内计划 5 s）")
    ap.add_argument("--seed", type=int, default=0, help="仿真种子（默认同 serve）")
    ap.add_argument("--noise", type=float, default=0.1, help="每轮瞬时噪声概率（默认同 serve）")
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "demo", "frames.json"))
    ap.add_argument("--summary", default=os.path.join(ROOT, "results", "demo_replay.json"))
    ap.add_argument("--work", help="工作目录：serve 日志与原始录制 raw.json（默认临时目录）")
    ap.add_argument("--rebuild", action="store_true", help="不重新运行：由 --work 里的 raw.json 重新生成帧文件与字幕")
    args = ap.parse_args()
    sys.path.insert(0, ROOT)
    log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)
    work = args.work or tempfile.mkdtemp(prefix="edgefdl-demo-")
    os.makedirs(work, exist_ok=True)
    raw_path = os.path.join(work, "raw.json")
    if args.rebuild:
        with open(raw_path, encoding="utf-8") as f:
            d = json.load(f)
        raw, marks, topology, csv_text, interval, skipped = (d[k] for k in ("raw", "marks", "topology", "csv", "interval", "skipped"))
        args.recorded, args.interval, args.seed, args.noise = d["recorded"], d["arg_interval"], d["seed"], d["noise"]
        args.commit = d.get("commit") or git_rev()
    else:
        srv = Server(args, work)
        try:
            s0 = srv.wait_up()
            log(f"serve 已就绪：{' '.join(srv.cmd)}")
            interval = s0["interval_s"]
            topology = srv.get("/api/topology")
            rec = Recorder(srv, interval, [l["id"] for l in topology["links"]])
            marks = []
            for sc in SCENES:
                marks.append(play(rec, srv, sc, log))
                log(f"场景 {sc['id']} 结束，累计 {rec.i} 帧")
            csv_text = srv.raw("/api/export/measure_log.csv").decode("utf-8")
        finally:
            srv.stop()
        raw, skipped = rec.raw, rec.skipped
        args.recorded, args.commit = time.strftime("%Y-%m-%dT%H:%M:%S%z"), git_rev()
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(dict(raw=raw, marks=marks, topology=topology, csv=csv_text, interval=interval, skipped=skipped,
                           recorded=args.recorded, commit=args.commit, arg_interval=args.interval, seed=args.seed,
                           noise=args.noise), f,
                      ensure_ascii=False)
    scenes = build(args, raw, marks, topology, csv_text, interval, skipped)
    log(f"{args.out}：{len(raw)} 帧，{os.path.getsize(args.out) / 2 ** 20:.2f} MiB；摘要 {args.summary}；原始录制 {raw_path}")
    miss = [s["id"] for s in scenes if s.get("expected_alarms_met") is False]
    if skipped:
        log(f"注意：跳过 {skipped} 轮")
    if miss:
        log(f"未按预期告警的场景：{miss}")
    return 1 if miss else 0


if __name__ == "__main__":
    sys.exit(main())
