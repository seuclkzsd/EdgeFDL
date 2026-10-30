# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""虚拟网络后端接口。

三种实现共用这一接口，上层（样本生成、在线研判、前端）不感知差异：
  sim   —— 解析式仿真，毫秒级出样本，用于全量样本预训练与 CI；
  netns —— Linux 网络命名空间 + veth + tc + FRR，单机即可起真实协议栈；
  lxd   —— 现场部署形态：每台设备一个 LXD 容器（内跑 FRR），可桥接物理口与真实网络互通。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from ..faults.model import Fault
from ..measure.model import DeepProbe, MeasurePlan, RoundResult
from ..topology import Topology


class Backend:
    name = "base"

    def __init__(self, topo: Topology, plan: Optional[MeasurePlan] = None):
        self.topo = topo
        self.plan = plan or MeasurePlan()
        self.active: Dict[str, Fault] = {}      # link -> 当前注入的故障（单链路单故障）

    # 生命周期
    def up(self) -> None:
        raise NotImplementedError

    def down(self) -> None:
        raise NotImplementedError

    def status(self) -> dict:
        return {"backend": self.name, "topology": self.topo.name,
                "faults": [f.to_dict() for f in self.active.values()]}

    # 故障注入
    def inject(self, fault: Fault) -> None:
        if fault.link in self.active:
            self.clear(fault.link)
        self._apply(fault)
        self.active[fault.link] = fault

    def clear(self, link: Optional[str] = None) -> None:
        for lid in ([link] if link else list(self.active)):
            f = self.active.pop(lid, None)
            if f:
                self._revert(f)

    def _apply(self, fault: Fault) -> None:
        raise NotImplementedError

    def _revert(self, fault: Fault) -> None:
        raise NotImplementedError

    # 测量
    def measure_round(self, round_id: int) -> RoundResult:
        raise NotImplementedError

    def deep_probe(self, link: str) -> DeepProbe:
        raise NotImplementedError

    def faults(self) -> List[Fault]:
        return list(self.active.values())


def make_backend(kind: str, topo: Topology, plan: Optional[MeasurePlan] = None, **kw) -> Backend:
    if kind == "sim":
        from .sim import SimBackend
        return SimBackend(topo, plan, **kw)
    if kind == "netns":
        from .netns import NetnsBackend
        return NetnsBackend(topo, plan, **kw)
    if kind == "lxd":
        from .lxd import LxdBackend
        return LxdBackend(topo, plan, **kw)
    raise ValueError(f"未知后端 {kind}")
