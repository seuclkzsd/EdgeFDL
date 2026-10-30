# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""测量数据结构与测量计划。仿真后端与真实后端（netns/LXD/物理网）产出完全相同的结构，
保证"虚拟网络里训练的模型"可以直接用于"物理网络上的在线推理"。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

NAN = float("nan")


@dataclass
class PingStat:
    target: str
    sent: int
    recv: int
    rtt_min: float = NAN
    rtt_avg: float = NAN
    rtt_max: float = NAN
    rtt_mdev: float = NAN
    path: Tuple[str, ...] = ()       # traceroute 逐跳设备名（不含源），未测为空

    @property
    def loss(self) -> float:
        return 1.0 - self.recv / self.sent if self.sent else NAN

    @property
    def reachable(self) -> bool:
        return self.recv > 0


@dataclass
class LsdbEntry:
    """控制面视图（OSPF LSDB 被动监听）：邻接状态与接口开销。"""
    adj_up: bool
    cost: float


@dataclass
class RoundResult:
    ts: float
    round_id: int
    vantage: str
    pings: Dict[str, PingStat] = field(default_factory=dict)
    bw: Dict[str, float] = field(default_factory=dict)          # 轻量带宽估计 Mbps（本轮轮询到的链路）
    lsdb: Dict[str, LsdbEntry] = field(default_factory=dict)
    probe_bytes: int = 0                                        # 本轮测量注入的字节数（带宽占用核算）


@dataclass
class DeepProbe:
    """针对单条链路的精测：两端之间高频 ping + 限时 iperf3。"""
    link: str
    sent: int
    recv: int
    rtt_avg: float
    rtt_mdev: float
    bw_mbps: float
    probe_bytes: int = 0

    @property
    def loss(self) -> float:
        return 1.0 - self.recv / self.sent if self.sent else NAN


@dataclass
class MeasurePlan:
    """周期测量计划（默认值即低开销配置，见 docs/algorithms.md）。"""
    interval_s: float = 5.0          # 轮询周期
    ping_count: int = 5              # 每目标每轮 ping 个数
    ping_interval_s: float = 0.2
    ping_size: int = 56
    traceroute_every: int = 1        # 每几轮做一次逐跳路径测量
    bw_links_per_round: int = 3      # 轻量带宽估计：每轮轮询几条链路（包列法）
    bw_train_pkts: int = 16
    bw_pkt_size: int = 1400
    window: int = 6                  # 研判滑动窗口（轮）
    deep_ping_count: int = 100
    deep_ping_interval_s: float = 0.01
    deep_iperf_s: float = 1.0
    deep_iperf_rate_ratio: float = 0.3   # 精测打流不超过链路带宽 30%，且只持续 1s

    def light_bytes_per_round(self, n_targets: int, avg_hops: float = 4.0) -> int:
        ping = n_targets * self.ping_count * (self.ping_size + 28 + 14) * 2
        trace = (n_targets * avg_hops * 3 * 60 * 2) / max(1, self.traceroute_every)
        bw = self.bw_links_per_round * self.bw_train_pkts * self.bw_pkt_size
        return int(ping + trace + bw)


def finite(x: Optional[float], default: float = 0.0) -> float:
    return default if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)
