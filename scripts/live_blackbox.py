#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""live 后端黑盒验证：被测进程只看得到网络本身，看不到注入了什么。

三个彼此独立的部分（都在实验容器里，EDGEFDL_LAB_IMAGE 须带 torch）：
  1. `edgefdl vnet up --backend netns` 一次性进程：建好真实协议栈（命名空间 + FRR OSPF + tc）后退出；
  2. `ip netns exec efl-ems python3 -m edgefdl serve --backend live`：在网管命名空间里常驻，只发 ping / traceroute，
     可选读 LSDB、用探测端点；它的进程内没有任何故障对象；
  3. 本脚本：直接对设备命名空间执行 tc / ip / vtysh 注入和恢复，只读告警日志 alarm_log.csv 记录结果。

用法（宿主机）：
  EDGEFDL_LAB_IMAGE=edgefdl-lab-torch:latest scripts/lab.sh run python3 scripts/live_blackbox.py \\
      --mode lsdb --out runs/lab/live_lsdb.json
模式：ping（只有 ping + traceroute）、infer（再加 --infer-state）、lsdb（再加 --lsdb-cmd）、
      ends（--lsdb-cmd + --endpoints）。多份结果用 --merge 合并成 results/live_blackbox.json。
路由器按 Linux 缺省限速 ICMP 差错（--icmp-ratelimit 1000），更接近真实设备，而不是 vnet up 关掉限速后的状态。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import subprocess
import sys
import time
from typing import Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from edgefdl.bench import SCENARIO_VALUE  # noqa: E402
from edgefdl.faults.generator import drift_allowed, drift_effective  # noqa: E402
from edgefdl.faults.injector import Cmd, apply_cmds, revert_cmds  # noqa: E402
from edgefdl.faults.model import DRIFT_FACTORS, Fault  # noqa: E402
from edgefdl.topology import load  # noqa: E402

PREFIX = "efl-"
LSDB_CMD = "vtysh -N efl-core -c 'show ip ospf database router json'"
ORDER = ("link_down", "delay", "loss", "jitter", "bandwidth", "route_drift")


def sh(argv: List[str], check: bool = True, timeout: Optional[float] = 30) -> str:
    p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                       timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"命令失败({p.returncode}): {' '.join(argv)}\n{p.stdout}")
    return p.stdout


def do_cmds(cmds: List[Cmd]):
    """直接在设备上执行，不经过任何 EdgeFDL 后端对象。"""
    for c in cmds:
        if c.kind == "vtysh":
            argv = ["vtysh", "-N", PREFIX + c.node]
            for line in c.argv:
                argv += ["-c", line]
            sh(argv)
        else:
            sh(["ip", "netns", "exec", PREFIX + c.node] + c.argv)


def in_ems(code: str, timeout: float = 15) -> str:
    return sh(["ip", "netns", "exec", PREFIX + "ems", "python3", "-c", code], check=False, timeout=timeout)


def api(path: str, port: int, body: Optional[dict] = None) -> str:
    """在网管命名空间里访问被测服务（它只监听该命名空间的回环地址）。"""
    code = ("import json,sys,urllib.request as u,urllib.error as e\n"
            f"r=u.Request('http://127.0.0.1:{port}{path}',"
            f"data={json.dumps(body).encode() if body is not None else None!r},"
            "headers={'Content-Type':'application/json'})\n"
            "try:\n  print(u.urlopen(r,timeout=10).read().decode())\n"
            "except e.HTTPError as x:\n  print('HTTP',x.code,x.read().decode())\n")
    return in_ems(code)


def read_alarms(path: str) -> List[Dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def stamp(r: Dict) -> float:
    return float(r.get("epoch") or time.mktime(time.strptime(r["ts"].split(".")[0], "%Y-%m-%dT%H:%M:%S")))


def scenarios(topo, links: List[str], types: List[str]) -> List[Fault]:
    out = []
    for lid in links:
        for t in types:
            if t == "route_drift":
                if not drift_allowed(topo, lid):
                    continue
                c = [Fault(t, lid, f) for f in DRIFT_FACTORS[::-1] if drift_effective(topo, Fault(t, lid, f))]
                if c:
                    out.append(c[0])
            else:
                out.append(Fault(t, lid, SCENARIO_VALUE[t]))
    return out


def endpoints_file(topo, path: str):
    """lab 里探测端点的"进入方式"是 ip netns exec；现网换成 ssh。"""
    import yaml
    ep = {n: f"ip netns exec {PREFIX}{n} sh -c" for n in topo.nodes if n != topo.vantage}
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump({"endpoints": ep}, f, allow_unicode=True)


def wait_for(cond, timeout: float, step: float = 1.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = cond()
        if v:
            return v
        time.sleep(step)
    return None


def merge(paths: List[str], out: str):
    """合并各模式、各分片的原始结果；汇总只做计数。"""
    docs = [json.load(open(p, encoding="utf-8")) for p in paths]
    modes: Dict[str, dict] = {}
    for d in docs:
        m = modes.setdefault(d["mode"], dict(serve_argv=d["serve_argv"], runs=[], rows=[]))
        m["rows"] += d["rows"]
        m["runs"].append({k: d[k] for k in ("links", "preflight_seconds", "false_alarms_idle", "serve_status",
                                            "inject_refused", "wall_s", "probe_kbps", "idle_s", "idle_alarms")
                          if k in d})
    for m in modes.values():
        rows = m["rows"]
        by: Dict[str, dict] = {}
        for t in ORDER:
            rs = [r for r in rows if r["type"] == t]
            if rs:
                ta = [r["time_to_alarm_s"] for r in rs if r["time_to_alarm_s"] is not None]
                by[t] = dict(n=len(rs), alarmed=sum(r["alarmed"] for r in rs),
                             correct=sum(r["type_ok"] and r["link_ok"] for r in rs),
                             mean_time_to_alarm_s=round(sum(ta) / len(ta), 1) if ta else None,
                             max_time_to_alarm_s=max(ta) if ta else None)
        m["summary"] = dict(scenarios=len(rows), correct=sum(r["type_ok"] and r["link_ok"] for r in rows),
                            by_type=by, false_alarms_idle=sum(r.get("false_alarms_idle", 0) for r in m["runs"]))
    doc = dict(conditions=docs[0]["conditions"], modes=modes)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print("写出", out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="ping", choices=["ping", "infer", "lsdb", "ends"])
    ap.add_argument("--model", default="results/model.pt")
    ap.add_argument("--topo", default="configs/demo_topology.yaml")
    ap.add_argument("--links", default="L1,L6,L12")
    ap.add_argument("--types", default=",".join(ORDER))
    ap.add_argument("--learn-rounds", type=int, default=40)
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--alarm-timeout", type=float, default=120.0, help="等待告警的上限（s）")
    ap.add_argument("--clear-timeout", type=float, default=90.0, help="恢复后等待告警清除的上限（s）")
    ap.add_argument("--settle", type=float, default=20.0, help="两个场景之间的静置时间（s）")
    ap.add_argument("--idle", type=float, default=0.0, help="开始注入前先空跑多久（s），此间的告警都是误报")
    ap.add_argument("--icmp-ratelimit", type=int, default=1000,
                    help="路由器 net.ipv4.icmp_ratelimit（ms）；Linux 缺省 1000，真实设备同样限速 TTL 超时报文")
    ap.add_argument("--port", type=int, default=8460)
    ap.add_argument("--work", default="runs/lab/live", help="日志目录（仓库内）")
    ap.add_argument("--out", default="runs/lab/live_blackbox.json")
    ap.add_argument("--merge", nargs="*", help="只合并这些结果文件到 --out，不跑实验")
    a = ap.parse_args()
    if a.merge is not None:
        return merge(a.merge, a.out)

    t00 = time.time()
    topo = load(a.topo)
    faults = scenarios(topo, a.links.split(","), a.types.split(","))
    work = os.path.join(a.work, a.mode + "-" + a.links.replace(",", "_"))
    logs = os.path.join(work, "logs")
    os.makedirs(work, exist_ok=True)
    print(f"== 建网（独立进程）；{len(faults)} 个场景，模式 {a.mode}", flush=True)
    sh([sys.executable, "-m", "edgefdl", "vnet", "up", "--backend", "netns", "--topo", a.topo], timeout=120)
    for n, nd in topo.nodes.items():                 # vnet up 为测量方便关掉了限速，这里恢复成设备的常态
        if nd.transit:
            sh(["ip", "netns", "exec", PREFIX + n, "sysctl", "-qw", f"net.ipv4.icmp_ratelimit={a.icmp_ratelimit}"])

    live = []
    if a.mode == "infer":
        live += ["--infer-state"]
    if a.mode in ("lsdb", "ends"):
        live += ["--lsdb-cmd", LSDB_CMD]
    if a.mode == "ends":
        ep = os.path.join(work, "endpoints.yaml")
        endpoints_file(topo, ep)
        live += ["--endpoints", ep]
    ems = ["ip", "netns", "exec", PREFIX + "ems", sys.executable, "-m", "edgefdl"]

    def ready():
        p = subprocess.run(ems + ["preflight", "--model", a.model, "--json", os.path.join(work, "preflight.json")]
                           + live, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        return p.stdout if p.returncode == 0 else None

    t0 = time.time()
    pre = wait_for(ready, 120, 2)
    if pre is None:
        raise SystemExit(f"preflight 未通过，见 {work}/preflight.json")
    pre_s = round(time.time() - t0, 1)
    print(pre, f"preflight 用时 {pre_s} s（含等待 OSPF 收敛）", flush=True)

    serve = ems + ["serve", "--model", a.model, "--backend", "live", "--learn-rounds", str(a.learn_rounds),
                   "--interval", f"{a.interval:g}", "--port", str(a.port), "--log-dir", logs] + live
    log = open(os.path.join(work, "serve.log"), "w")
    proc = subprocess.Popen(serve, stdout=log, stderr=subprocess.STDOUT)
    rows: List[Dict] = []
    alarm_log = os.path.join(logs, "alarm_log.csv")
    res: Dict = {}
    idle_false = 0

    def alive():
        if proc.poll() is not None:
            raise SystemExit(f"serve 退出，返回码 {proc.returncode}，见 {work}/serve.log")

    try:
        def up():
            alive()
            return "边缘网管已启动" in open(os.path.join(work, "serve.log"), encoding="utf-8").read()

        if not wait_for(up, 1800, 2):
            raise SystemExit("serve 未就绪")
        print(f"== serve 已启动（{time.time() - t00:.0f} s）", flush=True)
        time.sleep(a.interval * 8)                   # 滑动窗口填满
        res["inject_refused"] = api("/api/inject", a.port, dict(type="delay", link="L1", value=20.0))[:200]
        if a.idle:
            print(f"== 空跑 {a.idle:g} s", flush=True)
            n0 = len(read_alarms(alarm_log))
            for _ in range(int(a.idle / 5)):
                time.sleep(5)
                alive()
            res["idle_s"], res["idle_alarms"] = a.idle, [
                f"{r['fault_type']} {r['link_id']}" for r in read_alarms(alarm_log)[n0:] if r["event"] == "raise"]
            idle_false += len(res["idle_alarms"])
        for i, f in enumerate(faults, 1):
            # 注入前的静置期里出现的告警都是误报
            seen = len(read_alarms(alarm_log))
            time.sleep(a.settle)
            alive()
            al = read_alarms(alarm_log)
            extra = [r for r in al[seen:] if r["event"] == "raise"]
            idle_false += len(extra)
            seen = len(al)
            print(f"== [{i}/{len(faults)}] 注入 {f.label}", flush=True)
            t_inj = time.time()
            do_cmds(apply_cmds(topo, f))

            def raised():
                alive()
                new = [r for r in read_alarms(alarm_log)[seen:] if r["event"] == "raise"]
                return new or None

            new = wait_for(raised, a.alarm_timeout) or []
            first = new[0] if new else None
            do_cmds(revert_cmds(topo, f))
            t_rev = time.time()
            if f.type == "link_down":                # 接口 down 时内核清掉了经它的默认路由
                for n in (topo.link(f.link).a, topo.link(f.link).b):
                    if not topo.nodes[n].transit:
                        from edgefdl.vnet import frr
                        gw = frr.gateway_of(topo, n)
                        if gw:
                            sh(["ip", "netns", "exec", PREFIX + n, "ip", "route", "replace", "default",
                                "via", gw[1], "dev", gw[0]], check=False)

            def cleared():
                alive()
                rs = read_alarms(alarm_log)[seen:]
                ids = {r["alarm_id"] for r in rs if r["event"] == "raise"}
                done = {r["alarm_id"] for r in rs if r["event"] == "clear"}
                return ids and ids <= done

            ok_clear = bool(new) and bool(wait_for(cleared, a.clear_timeout))
            rs = read_alarms(alarm_log)[seen:]
            raises = [r for r in rs if r["event"] == "raise"]
            clears = [r for r in rs if r["event"] == "clear"]
            row = dict(link=f.link, type=f.type, value=f.value, alarmed=first is not None,
                       alarm_type=first["fault_type"] if first else None,
                       alarm_link=first["link_id"] if first else None,
                       type_ok=bool(first and first["fault_type"] == f.type),
                       link_ok=bool(first and first["link_id"] == f.link),
                       time_to_alarm_s=round(stamp(first) - t_inj, 1) if first else None,
                       time_to_clear_s=round(stamp(clears[-1]) - t_rev, 1) if ok_clear and clears else None,
                       cleared=ok_clear, n_alarms=len(raises),
                       all_alarms=[f"{r['fault_type']} {r['link_id']}" for r in raises],
                       probes_used=int(first.get("probes_used") or 0) if first else 0)
            rows.append(row)
            print("  ", row, flush=True)
        st = api("/api/status", a.port)
        try:
            s = json.loads(st)
            res["serve_status"] = {k: s.get(k) for k in ("round", "backend", "read_only", "errors", "suppressed",
                                                         "alarms_total", "ground_truth", "overhead", "round_ms")}
            res["probe_kbps"] = (s.get("overhead") or {}).get("kbps")
        except ValueError:
            res["serve_status"] = st[:300]
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(20)
            except subprocess.TimeoutExpired:
                proc.kill()
        log.close()
        sh([sys.executable, "-m", "edgefdl", "vnet", "down", "--backend", "netns", "--topo", a.topo], check=False)

    doc = dict(mode=a.mode, links=a.links, serve_argv=serve, rows=rows, false_alarms_idle=idle_false,
               preflight_seconds=pre_s, wall_s=round(time.time() - t00, 1), **res,
               conditions=dict(
                   network="demo topology on network namespaces, FRR OSPF (hello 1 s, dead 4 s), tc netem + tbf, "
                           f"in a privileged container; net.ipv4.icmp_ratelimit={a.icmp_ratelimit} on the routers",
                   model=a.model, interval_s=a.interval, learn_rounds=a.learn_rounds,
                   alarm_timeout_s=a.alarm_timeout, clear_timeout_s=a.clear_timeout, settle_s=a.settle,
                   fault_values=dict(SCENARIO_VALUE, route_drift="largest factor that changes a monitored path"),
                   isolation="serve ran in the efl-ems namespace as its own process; faults were applied by this "
                             "script with tc, ip and vtysh directly on the device namespaces; results were read "
                             "from alarm_log.csv only"))
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    n = sum(r["type_ok"] and r["link_ok"] for r in rows)
    print(f"== {a.mode}: {n}/{len(rows)} 类型与链路都对；静置期误报 {idle_false}；写出 {a.out}", flush=True)


if __name__ == "__main__":
    main()
