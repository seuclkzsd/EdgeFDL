# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""处置建议知识库 + 等价网络预演。

智能体输出 (故障类型, 链路) 后，这里给出：
  1. 分厂商的排查/修复命令（华为 VRP、华三 Comware、Linux/FRR）；
  2. 在等价拓扑上"预演"临时处置动作（如调高开销绕行、恢复开销）的效果，只推荐预演可行的动作。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from ..faults.model import TYPE_LABEL
from ..topology import Topology

CMDS = {
    "huawei": dict(
        if_status="display interface {ifn}", if_err="display interface {ifn} | include CRC|error",
        optic="display transceiver interface {ifn} verbose", peer="display ospf peer brief",
        route="display ip routing-table {ip}", qos="display qos queue statistics interface {ifn}",
        car="display qos car statistics interface {ifn}", nego="display interface {ifn} | include Speed|Duplex",
        changes="display configuration commit changes", device="display device", log="display logbuffer",
        set_cost="system-view ; interface {ifn} ; ospf cost {cost}", up="system-view ; interface {ifn} ; undo shutdown"),
    "h3c": dict(
        if_status="display interface {ifn}", if_err="display interface {ifn} | include CRC|error",
        optic="display transceiver interface {ifn}", peer="display ospf peer",
        route="display ip routing-table {ip}", qos="display qos queue-statistics interface {ifn} outbound",
        car="display qos car interface {ifn}", nego="display interface {ifn} | include Speed|Duplex",
        changes="display archive configuration", device="display device", log="display logbuffer",
        set_cost="system-view ; interface {ifn} ; ospf cost {cost}", up="system-view ; interface {ifn} ; undo shutdown"),
    "generic": dict(
        if_status="ip -s link show {ifn}", if_err="ip -s -s link show {ifn}", optic="ethtool -m {ifn}",
        peer="vtysh -c 'show ip ospf neighbor'", route="ip route get {ip}", qos="tc -s qdisc show dev {ifn}",
        car="tc -s qdisc show dev {ifn}", nego="ethtool {ifn}", changes="vtysh -c 'show running-config'",
        device="uptime", log="journalctl -k -n 100",
        set_cost="vtysh -c 'conf t' -c 'interface {ifn}' -c 'ip ospf cost {cost}'", up="ip link set {ifn} up"),
}


def _cmd(topo: Topology, node: str, key: str, **kw) -> str:
    v = topo.nodes[node].vendor
    table = CMDS.get(v, CMDS["generic"])
    return f"[{node}] " + table[key].format(**kw)


def _ends(topo: Topology, lid: str):
    l = topo.link(lid)
    return [(l.a, l.a_if, l.a_ip.split("/")[0]), (l.b, l.b_if, l.b_ip.split("/")[0])]


def rehearse(topo: Topology, ftype: str, lid: str, cur_cost: Optional[float] = None) -> Dict:
    """在等价拓扑上预演处置动作，返回可行性与影响面。"""
    targets = topo.targets()
    v = topo.vantage
    base = {t.id: topo.route_to_target(v, t)[0] for t in targets}
    l = topo.link(lid)
    if ftype == "link_down":
        after = {t.id: topo.route_to_target(v, t, down={lid})[0] for t in targets}
        lost = [tid for tid, p in after.items() if not p and not tid.startswith(lid + ".")]
        moved = [tid for tid in after if after[tid] and after[tid] != base[tid]]
        return dict(action="依赖 OSPF 自动切换至备份路径",
                    feasible=not lost,
                    detail=(f"{len(moved)} 个监测目标已切换到备份路径" +
                            (f"；无冗余、仍中断的目标: {', '.join(lost)}" if lost else "；业务无单点中断")))
    if ftype == "route_drift":
        restored = {t.id: topo.route_to_target(v, t)[0] for t in targets}
        same = all(restored[k] == base[k] for k in base)
        return dict(action=f"将 {lid} 两端 OSPF 开销恢复为基线值 {l.cost}"
                           + (f"（当前 {cur_cost:g}）" if cur_cost else ""),
                    feasible=same, detail="恢复后全部监测路径与基线一致" if same else "恢复后仍有路径与基线不一致，需人工核查")
    # 性能劣化：预演"调高开销引导流量绕行"
    cost = {lid: l.cost * 10}
    after = {t.id: topo.route_to_target(v, t, cost=cost)[0] for t in targets}
    still = [tid for tid, p in after.items() if p and lid in topo.path_links(p) and not tid.startswith(lid + ".")]
    moved = [tid for tid in after if base[tid] and after[tid] != base[tid]]
    feasible = bool(moved) and not still
    return dict(action=f"临时将 {lid} 两端 OSPF 开销 {l.cost}→{l.cost * 10}，引导业务绕行劣化链路",
                feasible=feasible,
                detail=(f"{len(moved)} 个目标改走备份路径" +
                        (f"；{len(still)} 个目标无备份路径仍经过该链路" if still else "")) if moved
                else "该链路无备份路径，绕行不可行，须现场处理")


def advise_node(topo: Topology, node: str) -> Dict:
    """设备宕机：现场核查设备本身，并在等价拓扑上预演其全部链路中断后的业务影响。"""
    links = [l for l in topo.links if node in (l.a, l.b)]
    peers = [(l.other(node), l.b_if if l.a == node else l.a_if) for l in links]
    steps = [dict(step=f"确认设备 {node} 供电、主控板与风扇状态，经 Console 登录查看是否重启或死机",
                  cmds=[_cmd(topo, node, "device"), _cmd(topo, node, "log")]),
             dict(step="在相邻设备上核对对端端口状态与 OSPF 邻居",
                  cmds=[_cmd(topo, p, "if_status", ifn=ifn) for p, ifn in peers if topo.nodes[p].transit]
                  + [_cmd(topo, p, "peer") for p, _ in peers[:1] if topo.nodes[p].transit]),
             dict(step="设备恢复后确认全部邻接重建、路径回到基线",
                  cmds=[_cmd(topo, p, "peer") for p, _ in peers if topo.nodes[p].transit][:2])]
    v = topo.vantage
    down = {l.id for l in links}
    # 故障设备自身、其链路上对端接口的地址（随链路撤销）、由它发起的 NQA 探测，不计入业务影响
    targets = [t for t in topo.targets()
               if t.node != node and t.src != node and not (t.kind == "if" and t.link in down)]
    base = {t.id: topo.route_to_target(v, t)[0] for t in targets}
    after = {t.id: topo.route_to_target(v, t, down=down)[0] for t in targets}
    lost = [tid for tid, p in after.items() if not p]
    moved = [tid for tid in after if after[tid] and after[tid] != base[tid]]
    reh = dict(action="依赖 OSPF 自动绕开故障设备", feasible=not lost,
               detail=f"{len(moved)} 个监测目标已切换到备份路径" +
                      (f"；仍中断的目标: {', '.join(lost)}" if lost else "；其余业务无单点中断"))
    return dict(summary=f"设备 {node} 宕机（{'、'.join(l.id for l in links)} 全部中断）", type_cn="设备宕机",
                steps=steps, rehearsal=reh)


def advise(topo: Topology, ftype: str, lid: Optional[str], cur_cost: Optional[float] = None) -> Dict:
    if ftype == "normal" or not lid:
        return dict(summary="未发现需处置的故障", steps=[], rehearsal=None)
    (na, ia, ipa), (nb, ib, ipb) = _ends(topo, lid)
    l = topo.link(lid)
    steps: List[Dict] = []
    if ftype == "link_down":
        summary = f"{lid}（{na}:{ia} ↔ {nb}:{ib}）链路中断"
        steps.append(dict(step="查看两端端口物理/协议状态，确认是否 down 或被误 shutdown",
                          cmds=[_cmd(topo, na, "if_status", ifn=ia), _cmd(topo, nb, "if_status", ifn=ib)]))
        steps.append(dict(step="检查光模块收发光功率 / 网线，必要时更换跳线或光模块",
                          cmds=[_cmd(topo, na, "optic", ifn=ia), _cmd(topo, nb, "optic", ifn=ib)]))
        steps.append(dict(step="如为误关闭，恢复端口", cmds=[_cmd(topo, na, "up", ifn=ia), _cmd(topo, nb, "up", ifn=ib)]))
        steps.append(dict(step="确认 OSPF 邻接恢复", cmds=[_cmd(topo, na, "peer")]))
    elif ftype == "route_drift":
        summary = f"{lid} 路由开销异常导致路径漂移（基线 cost={l.cost}）"
        steps.append(dict(step="核对两端接口 OSPF 开销与近期配置变更",
                          cmds=[_cmd(topo, na, "changes"), _cmd(topo, nb, "changes")]))
        steps.append(dict(step=f"恢复开销为基线值 {l.cost}",
                          cmds=[_cmd(topo, na, "set_cost", ifn=ia, cost=l.cost),
                                _cmd(topo, nb, "set_cost", ifn=ib, cost=l.cost)]))
        steps.append(dict(step="确认路由收敛、路径回到基线", cmds=[_cmd(topo, na, "route", ip=ipb)]))
    else:
        what = {"delay": "时延升高", "loss": "丢包", "jitter": "抖动增大", "bandwidth": "可用带宽下降"}[ftype]
        summary = f"{lid}（{na}:{ia} ↔ {nb}:{ib}）{what}"
        if ftype in ("loss", "jitter"):
            steps.append(dict(step="检查端口 CRC/错包计数与光功率，排查线路/光模块劣化",
                              cmds=[_cmd(topo, na, "if_err", ifn=ia), _cmd(topo, nb, "if_err", ifn=ib),
                                    _cmd(topo, na, "optic", ifn=ia)]))
        if ftype in ("delay", "jitter", "loss"):
            steps.append(dict(step="检查出方向队列是否拥塞、是否有突发流量",
                              cmds=[_cmd(topo, na, "qos", ifn=ia), _cmd(topo, nb, "qos", ifn=ib)]))
        if ftype == "bandwidth":
            steps.append(dict(step="检查端口协商速率（如千兆降为百兆）与限速策略",
                              cmds=[_cmd(topo, na, "nego", ifn=ia), _cmd(topo, na, "car", ifn=ia),
                                    _cmd(topo, nb, "nego", ifn=ib)]))
        if ftype == "delay":
            steps.append(dict(step="若链路经运营商/微波传输，联系线路方核查传输时延",
                              cmds=[_cmd(topo, na, "if_status", ifn=ia)]))
    reh = rehearse(topo, ftype, lid, cur_cost)
    if reh["feasible"] and ftype in ("delay", "loss", "jitter", "bandwidth"):
        steps.append(dict(step="临时处置（已在等价网络预演通过）：" + reh["action"],
                          cmds=[_cmd(topo, na, "set_cost", ifn=ia, cost=l.cost * 10),
                                _cmd(topo, nb, "set_cost", ifn=ib, cost=l.cost * 10)]))
    return dict(summary=summary, type_cn=TYPE_LABEL[ftype], steps=steps, rehearsal=reh)
