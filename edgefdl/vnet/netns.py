# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""Linux 网络命名空间后端：单机用真实内核协议栈复现整张边缘网络。

每台设备一个 netns（名字加 efl- 前缀），每条链路一对 veth，链路时延/带宽由两端出口 netem 复现；
路由器跑 FRR（zebra+ospfd，用 -N 路径空间隔离各实例的配置、pid 与 vty 套接字），
未安装 FRR 时退化为控制器下发静态路由。需要 root（或特权容器，见 scripts/lab.sh）。
"""
from __future__ import annotations

import glob
import os
import shutil
import signal
import time
from typing import List, Optional, Sequence

from ..measure.model import MeasurePlan
from ..topology import Topology
from . import frr
from .stack import KernelStackBackend, NodeExec, _vtysh_c

FRR_BIN = "/usr/lib/frr"
PREFIX = "efl-"


class NetnsExec(NodeExec):
    def __init__(self, prefix: str = PREFIX, dry_run: bool = False):
        super().__init__(dry_run)
        self.prefix = prefix

    def ns(self, node: str) -> str:
        return self.prefix + node

    def wrap(self, node: str, argv: Sequence[str]) -> List[str]:
        return ["ip", "netns", "exec", self.ns(node)] + list(argv)

    def vtysh_argv(self, node: str, lines: Sequence[str]) -> List[str]:
        # vtysh 通过 /var/run/frr/<路径空间>/ 下的套接字连到该实例，无需进入 netns
        return ["vtysh", "-N", self.ns(node)] + _vtysh_c(lines)


def frr_available() -> bool:
    return os.path.exists(f"{FRR_BIN}/zebra") and os.path.exists(f"{FRR_BIN}/ospfd") \
        and shutil.which("vtysh") is not None


class NetnsBackend(KernelStackBackend):
    name = "netns"

    def __init__(self, topo: Topology, plan: Optional[MeasurePlan] = None,
                 use_frr: Optional[bool] = None, prefix: str = PREFIX, dry_run: bool = False, **kw):
        """dry_run：只记录命令序列（self.ex.log），不执行、不写文件、不动已有命名空间与 FRR 进程；
        此时 use_frr 缺省按装有 FRR 处理（打印的是部署目标上的命令）。"""
        if use_frr is None:
            use_frr = True if dry_run else frr_available()
        super().__init__(topo, plan, use_frr=use_frr, **kw)
        self.ex = NetnsExec(prefix, dry_run)
        self.prefix = prefix

    # ------------------------------------------------------------ 生命周期
    def up(self) -> None:
        self.down()
        ex, topo = self.ex, self.topo
        for n in topo.nodes:
            ex.host(["ip", "netns", "add", ex.ns(n)], check=True)
        for l in topo.links:
            # 两端直接在各自命名空间里创建并命名，避免宿主侧临时名冲突
            ex.host(["ip", "link", "add", l.a_if, "netns", ex.ns(l.a), "type", "veth",
                     "peer", "name", l.b_if, "netns", ex.ns(l.b)], check=True)
        self._configure_l3()
        if self.use_frr:
            self._start_frr()
        else:
            self._static_program([])
        self._host_routes()

    def down(self) -> None:
        ex = self.ex
        if ex.dry_run:
            ex.log.append(f"# 结束 FRR 守护进程：/var/run/frr/{self.prefix}*/*.pid 中的进程（SIGTERM）")
        for pidf in ([] if ex.dry_run else glob.glob(f"/var/run/frr/{self.prefix}*/*.pid")):
            try:
                with open(pidf) as f:
                    os.kill(int(f.read().strip()), signal.SIGTERM)
            except (OSError, ValueError):
                pass
        if not ex.dry_run:
            _, out = ex.host(["ip", "netns", "list"], quiet=True)
            names = [ln.split()[0] for ln in out.splitlines() if ln.startswith(self.prefix)]
        else:
            names = [ex.ns(n) for n in self.topo.nodes]
        for name in names:
            ex.host(["ip", "netns", "del", name])        # 删除命名空间时其中的 veth 一并销毁
        if not ex.dry_run:
            for d in glob.glob(f"/etc/frr/{self.prefix}*") + glob.glob(f"/var/run/frr/{self.prefix}*"):
                shutil.rmtree(d, ignore_errors=True)
        else:
            ex.log.append(f"# 删除 /etc/frr/{self.prefix}* 与 /var/run/frr/{self.prefix}*")
        self.active.clear()

    def _start_frr(self) -> None:
        ex = self.ex
        try:
            import pwd
            pwd.getpwnam("frr")
            user = "frr"
        except (ImportError, KeyError):
            user = "root"
        routers = self.routers()
        for n in routers:
            ps = ex.ns(n)
            etc, run = f"/etc/frr/{ps}", f"/var/run/frr/{ps}"
            conf = frr.router_conf(self.topo, n, *self.ospf_timers)
            if ex.dry_run:
                ex.log.append(f"# 写入 {etc}/frr.conf（{len(conf.splitlines())} 行，frr.router_conf 生成）"
                              f"与 {etc}/vtysh.conf，属主 {user}")
            else:
                os.makedirs(etc, exist_ok=True)
                os.makedirs(run, exist_ok=True)
                with open(f"{etc}/frr.conf", "w") as f:
                    f.write(conf)
                with open(f"{etc}/vtysh.conf", "w") as f:
                    f.write(frr.VTYSH_CONF)
                if user == "frr":
                    shutil.chown(etc, "frr", "frr")
                    shutil.chown(run, "frr", "frr")
                    for p in glob.glob(f"{etc}/*"):
                        shutil.chown(p, "frr", "frr")
            for d in ("zebra", "ospfd"):
                ex.run(n, [f"{FRR_BIN}/{d}", "-d", "-N", ps, "-u", user, "-g", user,
                           "-i", f"{run}/{d}.pid"], check=True)
        # 守护进程起来后统一下发集成配置；vty 套接字可能稍晚就绪，重试几次
        for n in routers:
            for i in range(20):
                rc, _ = ex.host(["vtysh", "-N", ex.ns(n), "-b"], quiet=i > 0)
                if rc == 0:
                    break
                time.sleep(0.2)
            else:
                raise RuntimeError(f"{n}: vtysh -b 下发配置失败")

    def status(self) -> dict:
        d = super().status()
        if not self.ex.dry_run:
            _, out = self.ex.host(["ip", "netns", "list"], quiet=True)
            d["namespaces"] = sorted(ln.split()[0] for ln in out.splitlines()
                                     if ln.startswith(self.prefix))
        return d
