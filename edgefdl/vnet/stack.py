# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""真实内核协议栈后端的公共部分（netns 与 LXD 共用）。

两者的差别只在"命令发到哪台设备上执行"（NodeExec）：netns 用 `ip netns exec`，LXD 用
`lxc exec`。故障注入、路由（FRR / 静态回退）、周期测量、链路精测、LSDB 读取全部复用这里。
"""
from __future__ import annotations

import ipaddress
import json
import math
import random
import re
import shlex
import statistics
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Sequence, Tuple

from ..faults.injector import Cmd, apply_cmds, baseline_cmds, drift_cost, revert_cmds
from ..faults.model import Fault
from ..measure import probes
from ..measure.model import NAN, DeepProbe, LsdbEntry, MeasurePlan, PingStat, RoundResult
from ..topology import Link, Topology
from . import frr
from .base import Backend

# 只写按网络命名空间隔离的 sysctl；特权容器里写全局项会改到宿主机内核
ROUTER_SYSCTL = ["net.ipv4.ip_forward=1", "net.ipv4.conf.all.rp_filter=0",
                 "net.ipv4.conf.default.rp_filter=0", "net.ipv4.icmp_ratelimit=0"]
HOST_SYSCTL = ["net.ipv4.conf.all.rp_filter=0", "net.ipv4.icmp_ratelimit=0"]


class NodeExec:
    """按设备执行命令。dry_run 时只记录命令，不执行（用于审阅部署脚本、无 LXD 的机器）。"""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.log: List[str] = []

    def wrap(self, node: str, argv: Sequence[str]) -> List[str]:
        raise NotImplementedError

    def vtysh_argv(self, node: str, lines: Sequence[str]) -> List[str]:
        raise NotImplementedError

    def host(self, argv: Sequence[str], check: bool = False, timeout: Optional[float] = None,
             quiet: bool = False, input: Optional[str] = None) -> Tuple[int, str]:
        if not quiet or self.dry_run:
            self.log.append(" ".join(shlex.quote(a) for a in argv))
        if self.dry_run:
            return 0, ""
        try:
            p = subprocess.run(list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True, timeout=timeout, input=input)
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            return 124, out
        if check and p.returncode != 0:
            raise RuntimeError(f"命令失败({p.returncode}): {' '.join(argv)}\n{p.stderr.strip()}")
        return p.returncode, p.stdout

    def run(self, node: str, argv: Sequence[str], **kw) -> Tuple[int, str]:
        return self.host(self.wrap(node, argv), **kw)

    def popen(self, node: str, argv: Sequence[str]) -> subprocess.Popen:
        return subprocess.Popen(self.wrap(node, argv), stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, universal_newlines=True)

    def vtysh(self, node: str, lines: Sequence[str], **kw) -> Tuple[int, str]:
        return self.host(self.vtysh_argv(node, lines), **kw)


def _vtysh_c(lines: Sequence[str]) -> List[str]:
    out: List[str] = []
    for l in lines:
        out += ["-c", l]
    return out


def parse_lsdb(js: dict, topo: Topology, last_cost: Optional[Dict[str, float]] = None
               ) -> Dict[str, LsdbEntry]:
    """FRR `show ip ospf database router json` → 每条链路的邻接状态与开销。

    路由器—路由器链路：两端 router-LSA 都出现指向对端的 point-to-point 链接才算邻接正常；
    路由器—主机链路：接口所在网段以 Stub Network 出现在路由器 LSA 中即视为接口正常。"""
    rid2node = {n.loopback.split("/")[0]: n.name for n in topo.nodes.values() if n.loopback}
    p2p: Dict[Tuple[str, str], float] = {}
    stub: Dict[Tuple[str, str], float] = {}
    areas = ((js.get("routerLinkStates") or {}).get("areas") or {})
    for lsas in areas.values():
        for lsa in lsas:
            node = rid2node.get(lsa.get("advertisingRouter", ""))
            if node is None:
                continue
            for lk in (lsa.get("routerLinks") or {}).values():
                t = lk.get("linkType", "")
                m = float(lk.get("tos0Metric", 0))
                if t.startswith("another Router"):
                    p2p[(node, lk.get("routerInterfaceAddress", ""))] = m
                elif t.startswith("Stub"):
                    stub[(node, lk.get("networkAddress", ""))] = m
    out: Dict[str, LsdbEntry] = {}
    last_cost = last_cost if last_cost is not None else {}
    for l in topo.links:
        ta, tb = topo.nodes[l.a].transit, topo.nodes[l.b].transit
        if not (ta or tb):
            continue
        net = str(ipaddress.ip_interface(l.a_ip).network.network_address)
        if ta and tb:
            ka, kb = (l.a, l.ip_of(l.a)), (l.b, l.ip_of(l.b))
            up = ka in p2p and kb in p2p
            cost = p2p.get(ka, p2p.get(kb, stub.get((l.a, net), stub.get((l.b, net)))))
        else:
            r = l.a if ta else l.b
            up = (r, net) in stub
            cost = stub.get((r, net))
        if cost is None:
            cost = last_cost.get(l.id, float(l.cost))
        last_cost[l.id] = cost
        out[l.id] = LsdbEntry(adj_up=up, cost=float(cost))
    return out


class KernelStackBackend(Backend):
    name = "kernel"
    TRAIN_PORT = 5300
    IPERF_PORT = 5400

    def __init__(self, topo: Topology, plan: Optional[MeasurePlan] = None,
                 use_frr: bool = True, workers: int = 48, trace_workers: int = 8,
                 ospf_timers: Tuple[int, int] = (1, 4)):
        """ospf_timers=(hello, dead) 秒。默认 1/4 求快速收敛；现网多为 10/40。两者 dead/hello 比相同，
        但时间尺度压缩 10 倍后，丢包引起的邻接抖动按墙钟计也会频繁 10 倍（见 docs 的实测表）。"""
        super().__init__(topo, plan)
        self.ospf_timers = ospf_timers
        self.ex: NodeExec = NodeExec()
        self.use_frr = use_frr
        self.workers = workers
        self.trace_workers = trace_workers      # 并发 traceroute 受 ICMP 差错全局限速影响，需错峰
        self._owner = topo.ip_owner()
        self._targets = topo.targets()
        self._bw_cursor = 0
        self._lsdb_cost: Dict[str, float] = {}
        self._link_idx = {l.id: i for i, l in enumerate(topo.links)}
        self.deep_extra: Dict[str, dict] = {}   # 精测附加信息（iperf3 吞吐、各组包列估计）
        gw = frr.gateway_of(topo, topo.vantage)
        self.lsdb_node = gw[2] if gw else next(n for n in topo.nodes if topo.nodes[n].transit)

    # ------------------------------------------------------------ 工具
    def routers(self) -> List[str]:
        return [n for n, nd in self.topo.nodes.items() if nd.transit]

    def ends(self) -> List[str]:
        return [n for n, nd in self.topo.nodes.items() if not nd.transit]

    def _run_cmds(self, cmds: Sequence[Cmd]) -> None:
        for c in cmds:
            if c.kind == "vtysh":
                if self.use_frr:
                    self.ex.vtysh(c.node, c.argv, check=True)
            else:
                self.ex.run(c.node, c.argv, check=True)

    @staticmethod
    def _ifname(l: Link, node: str) -> str:
        return l.a_if if node == l.a else l.b_if

    # ------------------------------------------------------------ 地址 / 队列 / 路由
    def _configure_l3(self) -> None:
        for n, nd in self.topo.nodes.items():
            self.ex.run(n, ["ip", "link", "set", "lo", "up"], check=True)
            self.ex.run(n, ["sysctl", "-qw"] + (ROUTER_SYSCTL if nd.transit else HOST_SYSCTL), check=True)
            if nd.loopback:
                self.ex.run(n, ["ip", "addr", "replace", nd.loopback, "dev", "lo"], check=True)
        for l in self.topo.links:
            for n, ifn, ip in ((l.a, l.a_if, l.a_ip), (l.b, l.b_if, l.b_ip)):
                self.ex.run(n, ["ip", "addr", "replace", ip, "dev", ifn], check=True)
                self.ex.run(n, ["ip", "link", "set", ifn, "up"], check=True)
        self._run_cmds(baseline_cmds(self.topo))

    def _host_routes(self) -> None:
        for n in self.ends():
            gw = frr.gateway_of(self.topo, n)
            if gw:
                ifn, gw_ip, _ = gw
                # 接口 down 时内核会清掉经它的路由，恢复后需补回；失败（接口仍 down）忽略
                self.ex.run(n, ["ip", "route", "replace", "default", "via", gw_ip, "dev", ifn])

    def _static_program(self, faults: Sequence[Fault]) -> None:
        """无 FRR 时的回退：控制器按 SPF 计算并下发静态路由，链路中断/开销变化时整表重算，
        以此模拟动态路由的重收敛。"""
        topo = self.topo
        down = {f.link for f in faults if f.type == "link_down"}
        cost = {f.link: float(drift_cost(topo.link(f.link), f.value))
                for f in faults if f.type == "route_drift"}
        for u in self.routers():
            dist, prev = topo.spf(u, down, cost)
            nh: Dict[str, str] = {}
            for l in topo.links:
                if l.id in down or u in (l.a, l.b):
                    continue
                best = None
                for e in (l.a, l.b):
                    if e in dist and topo.nodes[e].transit:
                        c = dist[e] + cost.get(l.id, l.cost)
                        if best is None or (c, e) < best:
                            best = (c, e)
                if best is not None:
                    nh[str(ipaddress.ip_interface(l.a_ip).network)] = topo._walk(prev, u, best[1])[1]
            for v in self.routers():
                if v != u and v in dist and topo.nodes[v].loopback:
                    nh[topo.nodes[v].loopback] = topo._walk(prev, u, v)[1]
            self.ex.run(u, ["ip", "route", "flush", "proto", "static"])
            for prefix, h in nh.items():
                l = topo.link_between(u, h)
                self.ex.run(u, ["ip", "route", "replace", prefix, "via", l.ip_of(h),
                                "dev", self._ifname(l, u), "proto", "static"], check=True)

    # ------------------------------------------------------------ 故障注入
    def _apply(self, fault: Fault) -> None:
        after = [f for f in self.active.values() if f.link != fault.link] + [fault]
        if fault.type == "route_drift" and not self.use_frr:
            self._static_program(after)
            return
        self._run_cmds(apply_cmds(self.topo, fault))
        if fault.type == "link_down" and not self.use_frr:
            self._static_program(after)

    def _revert(self, fault: Fault) -> None:
        if not (fault.type == "route_drift" and not self.use_frr):
            self._run_cmds(revert_cmds(self.topo, fault))
        if fault.type == "link_down":
            self._host_routes()
        if not self.use_frr and fault.type in ("link_down", "route_drift"):
            self._static_program(list(self.active.values()))

    # ------------------------------------------------------------ 测量
    def _ping(self, node: str, ip: str, tid: str, count: int, interval: float) -> PingStat:
        cmd = probes.ping_cmd(ip, count, interval, self.plan.ping_size)
        _, out = self.ex.run(node, cmd, quiet=True, timeout=count * interval + 4)
        return probes.parse_ping(out, tid, count)

    def _trace(self, ip: str, retries: int = 1) -> Tuple[Tuple[str, ...], int]:
        """逐跳路径；有丢跳或未到达时错峰重测，仍不完整则返回 ()（路径未知）。返回 (路径, 探测包数)。"""
        sent = 0
        for k in range(retries + 1):
            if k:
                time.sleep(0.05 + 0.1 * random.random())
            _, out = self.ex.run(self.topo.vantage, probes.traceroute_cmd(ip), quiet=True, timeout=8)
            hops = probes.parse_traceroute(out)
            sent += len(hops)
            path = probes.hops_to_path(hops, self._owner, ip)
            if path:
                return path, sent
        return (), sent

    def _train(self, link_id: str, n: int, size: int, port_off: int = 0,
               clip: float = 1.05) -> Tuple[float, int]:
        """包列法轻量带宽估计，返回 (Mbps, 注入字节)；到达间隔被抖动打乱时估计不可信，返回 nan。"""
        bw, nb, clean = self._train_q(link_id, n, size, port_off, clip)
        return (bw if clean else NAN), nb

    def _train_q(self, link_id: str, n: int, size: int, port_off: int = 0,
                 clip: float = 1.05) -> Tuple[float, int, bool]:
        """a 端背靠背发 n 个包，b 端按到达离散度估瓶颈速率，返回 (Mbps, 注入字节, 是否可信)。

        列长按链路速率自适应，保证整列在瓶颈上至少占 0.5ms（千兆口约 44 包），否则内核定时器
        批量出队的误差会主导结果；估计值不超过标称速率×clip（物理上不可能更快，与仿真一致）。"""
        l = self.topo.link(link_id)
        n = max(n, int(math.ceil(0.0005 * l.bw_mbps * 1e6 / ((size + 42) * 8))) + 3)
        src, dst = (l.a, l.b) if self.topo.nodes[l.a].agent else (l.b, l.a)
        port = self.TRAIN_PORT + self._link_idx[link_id] * 4 + port_off
        if self.ex.dry_run:
            return NAN, 0, False
        rx = self.ex.popen(dst, probes.train_recv_cmd(port, n, 2.0))
        try:
            rx.stdout.readline()                    # 等接收端 bind 完成
            self.ex.run(src, probes.train_send_cmd(l.ip_of(dst), port, n, size), quiet=True, timeout=5)
            out, _ = rx.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            rx.kill()
            out = ""
        bw, clean = probes.parse_train(out)
        return (min(bw, l.bw_mbps * clip) if bw == bw else bw), n * (size + 42), clean

    def _read_lsdb(self) -> Dict[str, LsdbEntry]:
        if not self.use_frr:
            # 静态路由模式没有控制面，用当前注入状态近似（仅供开发调试）
            out = {}
            for l in self.topo.links:
                f = self.active.get(l.id)
                up = not (f and f.type == "link_down")
                c = drift_cost(l, f.value) if f and f.type == "route_drift" else l.cost
                out[l.id] = LsdbEntry(adj_up=up, cost=float(c))
            return out
        rc, out = self.ex.vtysh(self.lsdb_node, ["show ip ospf database router json"],
                                quiet=True, timeout=5)
        if rc != 0 or not out.strip():
            return {}
        try:
            js = json.loads(out)
        except ValueError:
            return {}
        return parse_lsdb(js, self.topo, self._lsdb_cost)

    def _bw_links(self) -> List[str]:
        k = self.plan.bw_links_per_round
        cand = [l.id for l in self.topo.links
                if self.topo.nodes[l.a].agent or self.topo.nodes[l.b].agent]
        if not cand or k <= 0:
            return []
        out = [cand[(self._bw_cursor + i) % len(cand)] for i in range(min(k, len(cand)))]
        self._bw_cursor = (self._bw_cursor + k) % len(cand)
        return out

    def measure_round(self, round_id: int) -> RoundResult:
        p, v = self.plan, self.topo.vantage
        res = RoundResult(ts=time.time(), round_id=round_id, vantage=v)
        if self.ex.dry_run:
            return res
        do_trace = p.traceroute_every > 0 and round_id % p.traceroute_every == 0
        with ThreadPoolExecutor(self.workers) as pool, ThreadPoolExecutor(self.trace_workers) as tpool:
            # nqa 类目标（盲区链路）由链路端点设备直接 ping 对端，对应真实网络的 NQA / IP SLA，
            # 只看连通/时延/丢包，不做逐跳路径测量
            pf = {t.id: pool.submit(self._ping, getattr(t, "src", None) or v, t.ip, t.id,
                                    p.ping_count, p.ping_interval_s)
                  for t in self._targets}
            tf = {t.id: tpool.submit(self._trace, t.ip) for t in self._targets
                  if getattr(t, "src", None) is None} if do_trace else {}
            bf = {lid: pool.submit(self._train, lid, p.bw_train_pkts, p.bw_pkt_size)
                  for lid in self._bw_links()}
            lf = pool.submit(self._read_lsdb)
            nbytes = 0
            for tid, f in pf.items():
                st = f.result()
                nbytes += st.sent * (p.ping_size + 28 + 14) * 2
                if tid in tf:
                    st.path, nh = tf[tid].result()
                    nbytes += nh * 1 * (60 + 14) * 2
                res.pings[tid] = st
            for lid, f in bf.items():
                bw, nb = f.result()
                nbytes += nb
                if bw == bw:            # 链路不通时估计失败，不写入，避免把 nan 传给特征层
                    res.bw[lid] = bw
            res.lsdb = lf.result()
        res.probe_bytes = int(nbytes)
        return res

    def deep_probe(self, link: str) -> DeepProbe:
        """链路精测：一端对另一端高频 ping；3 组 64 包包列测瓶颈容量（bw_mbps）；
        再以 UDP 按链路带宽 deep_iperf_rate_ratio 限速打流 deep_iperf_s 秒，测可用吞吐。

        bw_mbps 取包列容量而不取 iperf3 吞吐：吞吐会随丢包/抖动下降，若混用会把丢包、抖动
        误判成带宽劣化；iperf3 结果放在 self.deep_extra 供告警详情展示，仅在包列失败时兜底。"""
        p, l = self.plan, self.topo.link(link)
        src, dst = (l.a, l.b) if self.topo.nodes[l.a].agent else (l.b, l.a)
        dst_ip = l.ip_of(dst)
        st = self._ping(src, dst_ip, link, p.deep_ping_count, p.deep_ping_interval_s)
        nbytes = st.sent * (p.ping_size + 42) * 2
        caps = []
        for k in range(3):
            bw, nb, clean = self._train_q(link, 64, p.bw_pkt_size, port_off=1 + k, clip=1.0)
            nbytes += nb
            if bw == bw and clean:
                caps.append(bw)
        bw = statistics.median(caps) if caps else NAN
        thr = NAN
        if not self.ex.dry_run and st.recv > 0 and p.deep_iperf_s > 0:
            port = self.IPERF_PORT + self._link_idx[link]
            srv = self.ex.popen(dst, probes.iperf3_server_cmd(port))
            time.sleep(0.2)
            want = l.bw_mbps * p.deep_iperf_rate_ratio
            _, out = self.ex.run(src, probes.iperf3_client_cmd(dst_ip, p.deep_iperf_s, want, port,
                                                              udp=True),
                                 quiet=True, timeout=p.deep_iperf_s + 6)
            try:
                srv.wait(timeout=2)
            except subprocess.TimeoutExpired:
                srv.kill()
            thr, tx = probes.parse_iperf3(out)
            nbytes += tx
            if thr == thr and thr >= 0.85 * want and (bw != bw or bw < 0.85 * thr):
                # 吞吐已跑满请求速率，容量不可能低于实测吞吐：包列全部被抖动打乱，或抖动远大于单包服务时间、
                # 到达间隔整体拉长而各窗口仍一致（被误判为可信）时，包列估计都不可用，容量按标称计
                bw, caps = l.bw_mbps, []
            elif bw != bw and thr == thr:
                bw = thr                          # 跑不满请求速率 → 瓶颈已低于请求速率，实测吞吐即容量
        self.deep_extra[link] = {"iperf_mbps": thr, "train_mbps": caps,
                                 "bw_source": "train" if caps else ("iperf3" if bw == bw else "none")}
        return DeepProbe(link=link, sent=st.sent, recv=st.recv, rtt_avg=st.rtt_avg,
                         rtt_mdev=st.rtt_mdev, bw_mbps=bw, probe_bytes=int(nbytes))

    # ------------------------------------------------------------ 收敛等待
    def reachable_all(self, timeout_s: float = 1.0, skip: Sequence[str] = ()) -> Tuple[int, int]:
        ts = [t for t in self._targets if t.id not in skip]
        with ThreadPoolExecutor(self.workers) as pool:
            fs = [pool.submit(self._ping, getattr(t, "src", None) or self.topo.vantage, t.ip, t.id, 1, 0.2)
                  for t in ts]
            ok = sum(1 for f in fs if f.result().recv > 0)
        return ok, len(ts)

    def wait_stable(self, timeout_s: float = 60.0, expect_down: Sequence[str] = (),
                    expect_cost: Optional[Dict[str, float]] = None, settle_s: float = 0.5) -> float:
        """等网络进入稳态，返回耗时；超时抛错。稳态 = 控制面 + 数据面同时满足：
          · LSDB 中每条路由器互联链路的邻接状态符合预期（expect_down 内的为断，其余必须双向 Full——
            router-LSA 只在邻居 Full 后才含该点到点链接），expect_cost 内的链路开销已生效；
          · 网管到全部监测目标可达（expect_down 链路上的接口地址除外）；
          · 再留 settle_s 给 SPF 与转发表下发。
        故障注入前、清除后都应调用，避免上一步引起的邻接重建拖进下一步。"""
        t0 = time.time()
        down, cost = set(expect_down), dict(expect_cost or {})
        skip = [t.id for t in self._targets if t.link in down]
        why = ""
        while True:
            ok = True
            if self.use_frr:
                ls = self._read_lsdb()
                for l in self.topo.links:
                    if not (self.topo.nodes[l.a].transit and self.topo.nodes[l.b].transit):
                        continue
                    e = ls.get(l.id)
                    if e is None or e.adj_up == (l.id in down):
                        ok, why = False, f"{l.id} 邻接{'未断' if l.id in down else '未 Full'}"
                        break
                    if l.id in cost and e.cost != cost[l.id]:
                        ok, why = False, f"{l.id} 开销 {e.cost:g}≠{cost[l.id]:g}"
                        break
            if ok:
                n_ok, n = self.reachable_all(skip=skip)
                ok, why = n_ok == n, f"{n_ok}/{n} 目标可达"
            if ok:
                time.sleep(settle_s)
                return time.time() - t0
            if time.time() - t0 > timeout_s:
                raise TimeoutError(f"等待稳态超时（{timeout_s:g}s）：{why}")
            time.sleep(0.3)

    def fib_path(self, dst_ip: str, src: Optional[str] = None, max_hops: int = 16) -> Tuple[str, ...]:
        """按各设备转发表逐跳查 `ip route get` 还原路径（语义同 traceroute：不含源、末项为目标属主）。

        只读控制面，不发探测包，丢包/抖动下也稳定，用于断言"路径是否改变"和样本真值标注。
        不可达或无法解析时返回 ()。等价多路径时取内核对本机发起流量的选路结果。"""
        node, path = src or self.topo.vantage, []
        for _ in range(max_hops):
            rc, out = self.ex.run(node, ["ip", "-o", "route", "get", dst_ip], quiet=True, timeout=3)
            if rc != 0 or not out.strip():
                return ()
            if out.startswith("local "):
                return tuple(path)
            m = re.search(r"\svia\s+(\S+)", out)
            nh = m.group(1) if m else dst_ip           # 无 via 即直连：下一跳就是目标本身
            nxt = self._owner.get(nh)
            if nxt is None:
                return ()
            path.append(nxt)
            if nh == dst_ip:
                return tuple(path)
            node = nxt
        return ()

    def adj_uptime(self, link_id: str) -> Optional[float]:
        """链路两端 OSPF 邻居的 Full 持续时间（秒，取两端较小值）；未 Full 或非 FRR 模式返回 None。
        与"注入以来经过的时间"比较，即可判定这一步里邻接是否发生过抖动。"""
        l = self.topo.link(link_id)
        if not (self.use_frr and self.topo.nodes[l.a].transit and self.topo.nodes[l.b].transit):
            return None
        ups = []
        for n, peer, ifn in ((l.a, l.b, l.a_if), (l.b, l.a, l.b_if)):
            rc, out = self.ex.vtysh(n, ["show ip ospf neighbor json"], quiet=True, timeout=3)
            rid = frr.router_id(self.topo, peer)
            try:
                nbrs = json.loads(out).get("neighbors", {}).get(rid, [])
            except ValueError:
                return None
            up = [x.get("upTimeInMsec", 0) / 1000.0 for x in nbrs
                  if (x.get("nbrState") or x.get("state") or "").startswith("Full")
                  and str(x.get("ifaceName", ifn)).split(":")[0] == ifn]
            if not up:
                return None
            ups.append(min(up))
        return min(ups)

    def wait_converged(self, timeout_s: float = 60.0) -> float:
        """等到网管能 ping 通全部监测目标（OSPF 收敛），返回耗时；超时抛错。"""
        t0 = time.time()
        while True:
            ok, n = self.reachable_all()
            if ok == n:
                return time.time() - t0
            if time.time() - t0 > timeout_s:
                raise TimeoutError(f"收敛超时：{ok}/{n} 目标可达")
            time.sleep(0.5)

    def status(self) -> dict:
        d = super().status()
        d.update({"routing": "frr-ospf" if self.use_frr else "static", "dry_run": self.ex.dry_run})
        return d
