# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""LXD 后端：现场部署形态。

每台设备一个 LXD 系统容器（内跑 FRR + 测量工具），每条链路一个不带 IP 的 LXD bridge 网络，
设备接口按 Topology 命名并编址；链路时延/带宽/故障与 netns 后端完全相同（容器内 tc netem）。
可选把指定设备（通常是网管或接入路由器）以 macvlan / bridged 网卡桥接到宿主物理口，
使等价虚拟网络与物理网络互联互通。

命令执行与 netns 共用 KernelStackBackend，只是改为 `lxc exec <容器> -- ...`。
没有 LXD 的机器上默认 dry_run：只生成命令序列（审阅部署脚本、写进交付文档）。
离线环境：在联网机器上执行 image_commands() 制作节点镜像后 `lxc image export`，
到边缘网管服务器 `lxc image import` 即可，无需外网。
"""
from __future__ import annotations

import os
import shutil
import tempfile
from typing import Dict, List, Optional, Sequence

from ..measure.model import MeasurePlan
from ..topology import Topology
from . import frr
from .stack import KernelStackBackend, NodeExec, _vtysh_c

PREFIX = "efl-"


class LxdExec(NodeExec):
    def __init__(self, prefix: str = PREFIX, dry_run: bool = False):
        super().__init__(dry_run)
        self.prefix = prefix

    def ct(self, node: str) -> str:
        return self.prefix + node.replace("_", "-")

    def wrap(self, node: str, argv: Sequence[str]) -> List[str]:
        return ["lxc", "exec", self.ct(node), "--"] + list(argv)

    def vtysh_argv(self, node: str, lines: Sequence[str]) -> List[str]:
        return self.wrap(node, ["vtysh"] + _vtysh_c(lines))


class LxdBackend(KernelStackBackend):
    name = "lxd"

    def __init__(self, topo: Topology, plan: Optional[MeasurePlan] = None,
                 image: str = "edgefdl-node", dry_run: Optional[bool] = None, prefix: str = PREFIX,
                 stage_dir: Optional[str] = None, uplinks: Optional[Dict[str, dict]] = None,
                 pool: str = "default", use_frr: bool = True, **kw):
        """uplinks: {设备名: {"parent": "enp3s0", "nictype": "macvlan"|"bridged", "name": "up0",
                              "ip": "192.168.10.250/24", "routes": ["10.0.0.0/8 via 192.168.10.1"],
                              "ospf": None|"passive"|"active"}}"""
        super().__init__(topo, plan, use_frr=use_frr, **kw)
        if dry_run is None:
            dry_run = shutil.which("lxc") is None
        self.ex = LxdExec(prefix, dry_run)
        self.prefix, self.image, self.pool = prefix, image, pool
        self.profile = prefix + "node"
        self.stage_dir = stage_dir or tempfile.mkdtemp(prefix="edgefdl-lxd-")
        self.uplinks = uplinks or {}
        for l in topo.links:
            if len(self.net(l.id)) > 15:
                raise ValueError(f"LXD 网络名 {self.net(l.id)} 超过 15 字符，请缩短前缀或链路 id")

    def net(self, link_id: str) -> str:
        return self.prefix + link_id

    # ------------------------------------------------------------ 镜像
    def image_commands(self, base: str = "images:ubuntu/22.04") -> List[List[str]]:
        """制作节点镜像（联网执行一次）：FRR + 测量工具 + python3（包列探针）。"""
        b = self.prefix + "build"
        x = ["lxc", "exec", b, "--"]
        return [
            ["lxc", "launch", base, b],
            x + ["apt-get", "update"],
            x + ["apt-get", "install", "-y", "frr", "iperf3", "traceroute", "iproute2",
                 "iputils-ping", "python3"],
            x + ["sed", "-i", "s/^ospfd=no/ospfd=yes/", "/etc/frr/daemons"],
            x + ["systemctl", "enable", "frr"],
            ["lxc", "stop", b],
            ["lxc", "publish", b, "--alias", self.image],
            ["lxc", "delete", b],
        ]

    # ------------------------------------------------------------ 生命周期
    def up(self) -> None:
        ex, topo = self.ex, self.topo
        ex.host(["lxc", "profile", "create", self.profile])
        ex.host(["lxc", "profile", "device", "add", self.profile, "root", "disk", "path=/",
                 f"pool={self.pool}"])
        for l in topo.links:
            # 纯二层桥：不给宿主分配地址、不开 NAT/DHCP，两端容器接口即链路两端
            ex.host(["lxc", "network", "create", self.net(l.id), "--type=bridge",
                     "ipv4.address=none", "ipv6.address=none"], check=True)
        for n in topo.nodes:
            ex.host(["lxc", "init", self.image, ex.ct(n), "-p", self.profile], check=True)
        for l in topo.links:
            for n, ifn in ((l.a, l.a_if), (l.b, l.b_if)):
                ex.host(["lxc", "config", "device", "add", ex.ct(n), ifn, "nic",
                         f"network={self.net(l.id)}", f"name={ifn}"], check=True)
        for n, u in self.uplinks.items():
            ex.host(["lxc", "config", "device", "add", ex.ct(n), u.get("name", "up0"), "nic",
                     f"nictype={u.get('nictype', 'macvlan')}", f"parent={u['parent']}",
                     f"name={u.get('name', 'up0')}"], check=True)
        for n in topo.nodes:
            ex.host(["lxc", "start", ex.ct(n)], check=True)
        self._configure_l3()
        self._configure_uplinks()
        if self.use_frr:
            self._push_frr()
        else:
            self._static_program([])
        self._host_routes()

    def _configure_uplinks(self) -> None:
        for n, u in self.uplinks.items():
            ifn = u.get("name", "up0")
            if u.get("ip"):
                self.ex.run(n, ["ip", "addr", "replace", u["ip"], "dev", ifn], check=True)
            self.ex.run(n, ["ip", "link", "set", ifn, "up"], check=True)
            for r in u.get("routes", []):
                self.ex.run(n, ["ip", "route", "replace"] + r.split(), check=True)

    def _push_frr(self) -> None:
        ex = self.ex
        for n in self.routers():
            d = os.path.join(self.stage_dir, n)
            os.makedirs(d, exist_ok=True)
            conf = frr.router_conf(self.topo, n, *self.ospf_timers)
            for fn, text in (("frr.conf", conf), ("daemons", frr.DAEMONS), ("vtysh.conf", frr.VTYSH_CONF)):
                with open(os.path.join(d, fn), "w") as f:
                    f.write(text)
                ex.host(["lxc", "file", "push", os.path.join(d, fn), f"{ex.ct(n)}/etc/frr/{fn}"],
                        check=True)
            ex.run(n, ["systemctl", "restart", "frr"], check=True)
            u = self.uplinks.get(n)
            if u and u.get("ospf") in ("passive", "active"):
                lines = ["configure terminal", f"interface {u.get('name', 'up0')}", "ip ospf area 0"]
                if u["ospf"] == "passive":
                    lines.append("ip ospf passive")
                ex.vtysh(n, lines, check=True)

    def down(self) -> None:
        ex = self.ex
        for n in self.topo.nodes:
            ex.host(["lxc", "delete", "-f", ex.ct(n)])
        for l in self.topo.links:
            ex.host(["lxc", "network", "delete", self.net(l.id)])
        ex.host(["lxc", "profile", "delete", self.profile])
        self.active.clear()

    def commands(self) -> List[str]:
        return list(self.ex.log)


def main(argv: Optional[Sequence[str]] = None) -> None:
    """打印整套 LXD 部署命令：python3 -m edgefdl.vnet.lxd configs/demo_topology.yaml [--uplink ems=enp3s0]"""
    import argparse
    import shlex

    from ..faults.model import Fault
    from ..topology import load

    ap = argparse.ArgumentParser(description="生成等价虚拟网络的 LXD 部署命令（dry-run）")
    ap.add_argument("topology")
    ap.add_argument("--uplink", action="append", default=[], metavar="设备=物理口",
                    help="把设备桥接到宿主物理口（macvlan），可多次指定")
    ap.add_argument("--image", default="edgefdl-node")
    a = ap.parse_args(argv)
    topo = load(a.topology)
    ups = {}
    for s in a.uplink:
        n, parent = s.split("=", 1)
        ups[n] = {"parent": parent, "nictype": "macvlan", "name": "up0"}
    b = LxdBackend(topo, image=a.image, dry_run=True, uplinks=ups)
    q = lambda c: " ".join(shlex.quote(x) for x in c)  # noqa: E731
    print("# 1. 制作节点镜像（联网执行一次）")
    for c in b.image_commands():
        print(q(c))
    print("\n# 2. 构建等价虚拟网络")
    b.up()
    for c in b.commands():
        print(c)
    print(f"\n# FRR 配置已生成到 {b.stage_dir}")
    b.ex.log.clear()
    lid = topo.links[0].id
    print(f"\n# 3. 故障注入示例：{lid} 时延 +20ms 与恢复")
    b.inject(Fault("delay", lid, 20.0))
    b.clear(lid)
    for c in b.commands():
        print(c)
    b.ex.log.clear()
    print("\n# 4. 拆除")
    b.down()
    for c in b.commands():
        print(c)


if __name__ == "__main__":
    main()
