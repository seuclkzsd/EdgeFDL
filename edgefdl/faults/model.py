# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""故障模型：类型、取值范围（即"可判定口径"）与标签。

取值范围同时决定了故障样本生成的覆盖面和准确率指标的统计口径（见 docs/algorithms.md）。
"""
from __future__ import annotations

import random
from dataclasses import asdict, dataclass
from typing import List, Optional

FAULT_TYPES = ("link_down", "delay", "loss", "jitter", "bandwidth", "route_drift")

TYPE_LABEL = {
    "normal": "正常",
    "link_down": "链路中断",
    "delay": "时延劣化(RTT异常)",
    "loss": "丢包劣化",
    "jitter": "抖动劣化",
    "bandwidth": "带宽劣化",
    "route_drift": "路由漂移",
    "node_down": "设备宕机",          # 总控按链路中断 + 邻接状态聚合得出，不在智能体动作空间内
}

# 指标统计口径：故障分为三大类
CATEGORY = {
    "normal": "正常",
    "link_down": "链路中断",
    "delay": "性能劣化",
    "loss": "性能劣化",
    "jitter": "性能劣化",
    "bandwidth": "性能劣化",
    "route_drift": "路由漂移",
    "node_down": "链路中断",
}

# 各类故障的取值范围（单向施加在链路两端出口）
#   delay: 附加单向时延 ms；loss: 丢包率；jitter: 抖动 ms；
#   bandwidth: 剩余带宽占原带宽比例；route_drift: OSPF 开销倍率（<1 吸引流量，>1 排斥流量）
RANGES = {
    "delay": (5.0, 100.0),
    "loss": (0.05, 0.9),
    "jitter": (5.0, 50.0),
    "bandwidth": (0.05, 0.5),
}
DRIFT_FACTORS = (0.1, 0.3, 3.0, 10.0, 30.0)


@dataclass
class Fault:
    type: str
    link: str
    value: float = 1.0

    def __post_init__(self):
        if self.type not in FAULT_TYPES:
            raise ValueError(f"未知故障类型 {self.type}")

    @property
    def label(self) -> str:
        unit = {"delay": "ms", "loss": "", "jitter": "ms", "bandwidth": "", "route_drift": "x"}
        v = self.value
        if self.type == "loss":
            s = f"{v:.0%}"
        elif self.type == "bandwidth":
            s = f"剩余{v:.0%}"
        elif self.type == "link_down":
            s = ""
        else:
            s = f"{v:g}{unit[self.type]}"
        return f"{self.link} {TYPE_LABEL[self.type]} {s}".strip()

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Fault":
        return Fault(d["type"], d["link"], float(d.get("value", 1.0)))


def sample_value(ftype: str, rng: random.Random) -> float:
    if ftype == "link_down":
        return 1.0
    if ftype == "route_drift":
        return rng.choice(DRIFT_FACTORS)
    lo, hi = RANGES[ftype]
    if ftype in ("delay", "jitter", "loss"):   # 对数均匀，轻微故障占比更高
        import math
        return round(math.exp(rng.uniform(math.log(lo), math.log(hi))), 2)
    return round(rng.uniform(lo, hi), 3)


def enumerate_faults(link_ids: List[str], per_type: int = 3, rng: Optional[random.Random] = None,
                     types=FAULT_TYPES) -> List[Fault]:
    """全量故障空间：每条链路 × 每种类型 × 若干取值（含区间端点，保证边界样本被覆盖）。"""
    rng = rng or random.Random(0)
    out: List[Fault] = []
    for lid in link_ids:
        for t in types:
            if t == "link_down":
                out.append(Fault(t, lid))
            elif t == "route_drift":
                out.extend(Fault(t, lid, f) for f in DRIFT_FACTORS)
            else:
                lo, hi = RANGES[t]
                vals = [lo, hi] + [sample_value(t, rng) for _ in range(max(0, per_type - 2))]
                out.extend(Fault(t, lid, v) for v in vals)
    return out

