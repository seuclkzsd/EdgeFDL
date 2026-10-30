# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""网络测量工具的命令构造与输出解析：ping / traceroute / iperf3 / 包列带宽估计。

只依赖标准库。命令在哪台设备上执行由调用方（vnet.stack.NodeExec）决定，这里只管
"发什么命令、怎么读结果"，所以同一套解析既用于虚拟网络，也用于物理网络上的网管服务器。
"""
from __future__ import annotations

import json
import math
import re
from typing import Dict, List, Optional, Sequence, Tuple

from .model import NAN, DeepProbe, PingStat

# ---------------------------------------------------------------------- ping

_RE_TX = re.compile(r"(\d+)\s+packets?\s+transmitted,\s*(\d+)\s+(?:packets?\s+)?received")
# iputils: rtt min/avg/max/mdev = a/b/c/d ms ；BSD/macOS: round-trip min/avg/max/stddev = ... ；
# busybox: round-trip min/avg/max = a/b/c ms（无 mdev）
_RE_RTT = re.compile(r"(?:rtt|round-trip)\s+min/avg/max(?:/(?:mdev|stddev))?\s*=\s*"
                     r"([\d.]+)/([\d.]+)/([\d.]+)(?:/([\d.]+))?\s*ms")
_RE_TIME = re.compile(r"time[=<]([\d.]+)\s*ms")


def ping_cmd(ip: str, count: int = 5, interval: float = 0.2, size: int = 56,
             timeout_s: float = 1.0) -> List[str]:
    # -n 不做反解；-W 无应答时的等待上限。不加 -w：iputils 在 -c 与 -w 同时给出时会一直补发
    # 直到收满 count 个应答，高时延下"发送数"多于 count，凭空算出丢包。挂死由调用方超时兜底。
    return ["ping", "-n", "-c", str(count), "-i", f"{interval:g}", "-s", str(size),
            "-W", f"{max(1, int(math.ceil(timeout_s)))}", ip]


def parse_ping(text: str, target: str = "", sent_hint: int = 0) -> PingStat:
    """解析 ping 输出。统计行缺失（进程被杀、busybox 版本差异）时退回逐包 time= 行。"""
    m = _RE_TX.search(text)
    times = [float(x) for x in _RE_TIME.findall(text)]
    if m:
        sent, recv = int(m.group(1)), int(m.group(2))
    else:
        sent, recv = max(sent_hint, len(times)), len(times)
    st = PingStat(target=target, sent=sent, recv=recv)
    r = _RE_RTT.search(text)
    if r and recv > 0:
        st.rtt_min, st.rtt_avg, st.rtt_max = float(r.group(1)), float(r.group(2)), float(r.group(3))
        if r.group(4) is not None:
            st.rtt_mdev = float(r.group(4))
        elif times:
            st.rtt_mdev = _mdev(times)
    elif times:
        st.rtt_min, st.rtt_max = min(times), max(times)
        st.rtt_avg = sum(times) / len(times)
        st.rtt_mdev = _mdev(times)
    return st


def _mdev(xs: Sequence[float]) -> float:
    # 与 iputils 一致：sqrt(E[x^2] - E[x]^2)
    n = len(xs)
    if n == 0:
        return NAN
    mu = sum(xs) / n
    return math.sqrt(max(0.0, sum(x * x for x in xs) / n - mu * mu))


# ---------------------------------------------------------------- traceroute

_RE_HOP = re.compile(r"^\s*(\d+)\s+(.*)$")
_RE_IP = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


def traceroute_cmd(ip: str, max_hops: int = 12, wait_s: float = 1.0, queries: int = 1) -> List[str]:
    # 每跳 1 个探测包、ICMP 方式（-I），开销约为默认 3 包 UDP 的 1/3，且不依赖目标开放 UDP 端口
    return ["traceroute", "-n", "-I", "-q", str(queries), "-w", f"{wait_s:g}",
            "-m", str(max_hops), "-N", "16", ip]


def parse_traceroute(text: str) -> List[Optional[str]]:
    """返回逐跳 IP（* 为 None）。首行 'traceroute to ...' 跳过。"""
    hops: List[Optional[str]] = []
    for line in text.splitlines():
        m = _RE_HOP.match(line)
        if not m:
            continue
        ips = _RE_IP.findall(m.group(2))
        hops.append(ips[0] if ips else None)
    return hops


def hops_to_path(hops: Sequence[Optional[str]], ip_owner: Dict[str, str],
                 target_ip: str = "", strict: bool = True) -> Tuple[str, ...]:
    """逐跳 IP → 设备名序列（与仿真的 path 语义一致：不含源，末项为目标属主）。

    未到达目标（最后一跳不是目标）时返回空元组，由上层当作"路径未知"而非"路径改变"。
    strict 时中间有未应答跳（*）也返回空：路由器 ICMP 差错报文受内核全局限速，偶发丢跳，
    若直接略去会得到一条"更短的路径"，被误判为路由漂移。"""
    if strict and any(ip is None for ip in hops):
        return ()
    path: List[str] = []
    for ip in hops:
        if ip is None or ip not in ip_owner:
            continue
        n = ip_owner[ip]
        if not path or path[-1] != n:
            path.append(n)
    if target_ip and (not hops or hops[-1] != target_ip):
        return ()
    return tuple(path)


# -------------------------------------------------------------------- iperf3

def iperf3_server_cmd(port: int = 5201, one_off: bool = True) -> List[str]:
    return ["iperf3", "-s", "-p", str(port)] + (["-1"] if one_off else [])


def iperf3_client_cmd(ip: str, seconds: float = 1.0, rate_mbps: float = 0.0, port: int = 5201,
                      udp: bool = False) -> List[str]:
    # rate_mbps>0 时限速（TCP 也支持 -b 节流），保证精测不把业务链路打满
    cmd = ["iperf3", "-c", ip, "-p", str(port), "-t", f"{seconds:g}", "-J", "-O", "0"]
    if udp:
        cmd.append("-u")
    if rate_mbps > 0:
        cmd += ["-b", f"{rate_mbps:g}M"]
    return cmd


def parse_iperf3(text: str) -> Tuple[float, int]:
    """返回 (接收端吞吐 Mbps, 发送字节数)。解析失败返回 (nan, 0)。"""
    try:
        d = json.loads(text)
    except ValueError:
        return NAN, 0
    end = d.get("end") or {}
    rx = end.get("sum_received") or end.get("sum") or {}
    tx = end.get("sum_sent") or end.get("sum") or {}
    bps = rx.get("bits_per_second")
    return (bps / 1e6 if bps else NAN), int(tx.get("bytes") or 0)


# ---------------------------------------------------------- 包列带宽估计（轻量）

# 包列法（packet train）：发送端背靠背发 N 个等长 UDP 包，瓶颈链路把它们按线速"摊开"，
# 接收端由到达间隔反推瓶颈带宽。每次只注入 N×size 字节（默认 16×1400≈22KB），远低于 iperf3 打流。
# 到达时间取内核时间戳（SO_TIMESTAMPNS），避免用户态唤醒延迟把间隔压扁导致高估。
TRAIN_RECV = r"""
import socket,sys,time,struct
TS=getattr(socket,'SO_TIMESTAMPNS',35)
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.bind(('0.0.0.0',int(sys.argv[1])))
try: s.setsockopt(socket.SOL_SOCKET,TS,1)
except OSError: pass
s.settimeout(float(sys.argv[3]));n=int(sys.argv[2]);ts=[];sz=0
print('ready',flush=True)
try:
  while len(ts)<n:
    b,anc,_,_=s.recvmsg(65535,64);t=None
    for lv,ty,d in anc:
      if lv==socket.SOL_SOCKET and ty==TS: sec,ns=struct.unpack('qq',d[:16]);t=sec+ns*1e-9
    ts.append(t if t is not None else time.time());sz=len(b)
    s.settimeout(0.5)
except socket.timeout: pass
print(len(ts),sz,' '.join('%.9f'%t for t in ts),flush=True)
"""

TRAIN_SEND = r"""
import socket,sys,struct
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);n=int(sys.argv[3]);pad=b'x'*(int(sys.argv[4])-4)
for i in range(n): s.sendto(struct.pack('!I',i)+pad,(sys.argv[1],int(sys.argv[2])))
"""


def train_recv_cmd(port: int, n: int, timeout_s: float = 2.0) -> List[str]:
    return ["python3", "-c", TRAIN_RECV, str(port), str(n), f"{timeout_s:g}"]


def train_send_cmd(ip: str, port: int, n: int, size: int) -> List[str]:
    return ["python3", "-c", TRAIN_SEND, ip, str(port), str(n), str(size)]


def train_estimate(ts: Sequence[float], size: int) -> Tuple[float, bool]:
    """由到达时间序列估计瓶颈带宽，返回 (Mbps, 是否可信)。

    按到达顺序取跨度 s=⌊m/4⌋ 的滑动窗口，单包服务时间 = 窗口时长/s：
      · 正常时各窗口一致（中位数 ≤ 1.3×最短），取中位数，抗内核批量出队造成的个别窗口偏短；
      · 窗口长短悬殊说明到达间隔已被抖动打乱（逐包独立抖动 ≫ 单包服务时间时，离散度不再反映
        瓶颈速率），此时仍给出最短窗口的估计，但标记为不可信，由调用方决定丢弃或改用吞吐测试；
      · 丢包的包不占用瓶颈服务时间，按"收到的第几个包"计数即可，不需要按序号补洞；
      · 令牌桶整形（tbf / 设备整形器）的存量令牌会让前几个包线速通过，丢弃前 2 个包。"""
    ts = sorted(ts)
    if len(ts) >= 8:
        ts = ts[2:]
    m = len(ts)
    if m < 4:
        return NAN, False
    s = max(2, m // 4)
    w = sorted((ts[i + s] - ts[i]) / s for i in range(m - s))
    lo, med = w[0], w[len(w) // 2]
    clean = med <= 1.3 * lo
    step = med if clean else lo
    if step <= 0:
        return NAN, False
    return (size + 42) * 8 / step / 1e6, clean       # 以太网/IP/UDP 头 42B 也占线速


def train_capacity(ts: Sequence[float], size: int) -> float:
    return train_estimate(ts, size)[0]


def parse_train(text: str) -> Tuple[float, bool]:
    """接收端输出 'got size t1 t2 ...'，返回 (估计带宽 Mbps, 是否可信)；样本不足返回 (nan, False)。"""
    lines = [l for l in text.splitlines() if l and not l.startswith("ready")]
    if not lines:
        return NAN, False
    parts = lines[-1].split()
    try:
        got, size = int(parts[0]), int(parts[1])
        ts = [float(x) for x in parts[2:]]
    except (ValueError, IndexError):
        return NAN, False
    if got < 4 or len(ts) < 4:
        return NAN, False
    return train_estimate(ts, size)


def deep_probe_from(link: str, ping_text: str, iperf_text: str, ping_sent: int,
                    ping_size: int = 56) -> DeepProbe:
    st = parse_ping(ping_text, link, ping_sent)
    bw, tx = parse_iperf3(iperf_text)
    return DeepProbe(link=link, sent=st.sent, recv=st.recv, rtt_avg=st.rtt_avg,
                     rtt_mdev=st.rtt_mdev, bw_mbps=bw,
                     probe_bytes=st.sent * (ping_size + 42) * 2 + tx)
