# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""处置建议知识库 + 等价网络预演。

智能体输出 (故障类型, 链路) 后，这里给出：
  1. 分厂商的排查/修复命令（华为 VRP、华三 Comware、Linux/FRR）；
  2. 在等价拓扑上"预演"临时处置动作（如调高开销绕行、恢复开销）的效果，只推荐预演可行的动作。
预演在当前网络状态上进行：其余活动告警（others）中断的链路按中断计、漂移的开销按当前值计，
绕行经过其余告警中劣化链路的处置不推荐。
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from ..faults.model import TYPE_LABEL
from ..topology import Topology

PERF = ("delay", "loss", "jitter", "bandwidth")

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


def _context(others: Optional[Iterable[Dict]]):
    """其余活动告警 → 预演所用的当前网络状态：(中断的链路, 漂移后的开销, 劣化中的链路→类型, 说明后缀)。
    others 的每项含 fault_type、link_id，设备宕机另含 node、links，路由漂移可带 cost（LSDB 当前开销）。"""
    down, cost, bad, notes = set(), {}, {}, []
    for a in others or ():
        ft, links = a["fault_type"], a.get("links") or [a["link_id"]]
        if ft == "node_down":
            down.update(links)
            notes.append(f"设备 {a.get('node', '')} 宕机")
            continue
        notes.append(f"{links[0]} {TYPE_LABEL.get(ft, ft)}")
        if ft == "link_down":
            down.add(links[0])
        elif ft == "route_drift" and a.get("cost"):
            cost[links[0]] = float(a["cost"])
        elif ft in PERF:
            bad[links[0]] = TYPE_LABEL[ft]
    return down, cost, bad, ("（已计入其他活动告警：" + "、".join(notes) + "）") if notes else ""


def _routes(topo: Topology, targets, down, cost, cache) -> Dict[str, Optional[tuple]]:
    """各目标的 (正向节点路径, 往返经过的链路集合, 正向经过的链路集合)，不可达为 None。"""
    out = {}
    for t in targets:
        fwd, rev = topo.route_to_target(topo.vantage, t, frozenset(down), cost or None, cache)
        f = topo.path_links(fwd)
        out[t.id] = (tuple(fwd), frozenset(f + topo.path_links(rev)), frozenset(f)) if fwd else None
    return out


def _ends_path(topo: Topology, l, down, cost) -> Optional[List[str]]:
    """链路两端设备之间（a→b）的当前最短路径；一端为主机/网管或不可达时为 None。"""
    if not (topo.nodes[l.a].transit and topo.nodes[l.b].transit):
        return None
    dist, prev = topo.spf(l.a, frozenset(down), cost or None)
    return topo._walk(prev, l.a, l.b) if l.b in dist else None


def _names(ids: List[str], k: int = 8) -> str:
    return ", ".join(ids[:k]) + (f" 等 {len(ids)} 个" if len(ids) > k else "")


def _via(bad: Dict[str, str], links) -> str:
    v = sorted(set(links) & set(bad))
    return "、".join(f"{x}（{bad[x]}）" for x in v)


def rehearse(topo: Topology, ftype: str, lid: str, cur_cost: Optional[float] = None,
             others: Optional[Iterable[Dict]] = None) -> Dict:
    """在等价拓扑上预演处置动作，返回可行性与影响面。cur_cost：该链路 LSDB 中的当前开销（路由漂移用）；
    others：其余活动告警（见 _context）。链路自身的接口地址与 NQA 探测随链路中断/绕行而变，不计入影响；
    主机目标计入（主机单归属于接入链路时即业务中断）。"""
    down, cost, bad, note = _context(others)
    targets = topo.targets()
    own = {t.id for t in targets if t.link == lid and t.kind in ("if", "nqa")}
    cache: dict = {}
    cur = _routes(topo, targets, down, cost, cache)
    l = topo.link(lid)
    if ftype == "link_down":
        after = _routes(topo, targets, down | {lid}, cost, cache)
        lost = [i for i in cur if i not in own and cur[i] and not after[i]]
        moved = [i for i in cur if i not in own and cur[i] and after[i] and after[i][0] != cur[i][0]]
        parts = [f"{len(moved)} 个监测目标已切换到备份路径"] if moved else []
        via = _via(bad, {x for i in moved for x in after[i][1]})
        if via:
            parts.append(f"备份路径经过同样告警中的 {via}")
        if not moved and not lost:
            ends = _ends_path(topo, l, down | {lid}, cost)
            parts.append("没有监测路径经过该链路（其自身接口地址与 NQA 探测除外）"
                         + (f"，两端设备经 {'→'.join(ends)} 互通" if ends else ""))
        parts.append(f"无冗余、仍中断的目标: {_names(lost)}" if lost else "业务无单点中断")
        return dict(action="依赖 OSPF 自动切换至备份路径", feasible=not lost, detail="；".join(parts) + note)
    if ftype == "route_drift":
        action = f"将 {lid} 两端 OSPF 开销恢复为基线值 {l.cost}" + (f"（当前 {cur_cost:g}）" if cur_cost else "")
        if not cur_cost:
            return dict(action=action, feasible=False, detail="未取得该链路的当前开销（LSDB），无法预演，需人工核查" + note)
        if abs(cur_cost - l.cost) < 1e-6:
            return dict(action=action, feasible=False,
                        detail=f"LSDB 中该链路开销已是基线值 {l.cost}，路径变化另有原因，需人工核查" + note)
        # 当前状态（漂移后的开销 + 其余告警）与恢复后对比：恢复须让漂移改道的路径回去，且不新增中断
        drifted = _routes(topo, targets, down, dict(cost, **{lid: cur_cost}), cache)
        back = [i for i in cur if cur[i] != drifted[i]]
        lost = [i for i in cur if drifted[i] and not cur[i]]
        base = _routes(topo, targets, set(), {}, cache) if (down or cost) else cur
        differ = [i for i in cur if cur[i] != base[i]]
        detail = ((f"{len(back)} 个监测目标的路径随之回到基线，" if back else f"当前开销 {cur_cost:g} 下监测路径未改变，") +
                  (f"另有 {len(differ)} 个目标的路径因其他告警仍与基线不同" if differ else "恢复后全部监测路径与基线一致"))
        if lost:
            detail += f"；恢复后不可达: {_names(lost)}"
        return dict(action=action, feasible=not lost, detail=detail + note)
    # 性能劣化：预演"调高开销引导流量绕行"
    raised = dict(cost, **{lid: l.cost * 10})
    after = _routes(topo, targets, down, raised, cache)
    cross = [i for i in cur if i not in own and cur[i] and lid in cur[i][1]]       # 往返任一方向经过该链路
    still = [i for i in cross if after[i] and lid in after[i][1]]
    moved = [i for i in cross if after[i] and lid not in after[i][1]]
    action = f"临时将 {lid} 两端 OSPF 开销 {l.cost}→{l.cost * 10}，引导业务绕行劣化链路"
    if cross:
        via = _via(bad, {x for i in moved for x in after[i][1]})
        n = sum(lid in cur[i][2] for i in moved) or len(moved)     # 改道目标按正向路径计数
        if not moved:
            detail = f"经过该链路的监测目标均无备份路径（{_names(still)}），绕行不可行，须现场处理"
        else:
            detail = f"{n} 个目标改走备份路径" + (f"；{len(still)} 个目标无备份路径仍经过该链路" if still else "")
            if via:
                detail += f"；备份路径经过同样告警中的 {via}，绕行不可行"
        return dict(action=action, feasible=bool(moved) and not still and not via, detail=detail + note)
    # 没有监测路径经过该链路（环网链路、只有 NQA 探测的链路）：按两端设备之间的流量预演
    ends = _ends_path(topo, l, down, raised)
    if ends is None or lid in topo.path_links(ends):
        alt = _ends_path(topo, l, down | {lid}, cost)
        detail = ("该链路两端之间无备份路径，绕行不可行，须现场处理" if alt is None else
                  f"备份路径 {'→'.join(alt)} 的开销高于调高后的开销，两端设备间流量仍经过该链路，绕行不可行")
        return dict(action=action, feasible=False, detail="没有监测路径经过该链路；" + detail + note)
    via = _via(bad, topo.path_links(ends))
    return dict(action=action, feasible=not via,
                detail=f"没有监测路径经过该链路；两端设备间流量改走 {'→'.join(ends)}"
                       + (f"，经过同样告警中的 {via}，绕行不可行" if via else "") + note)


def advise_node(topo: Topology, node: str, others: Optional[Iterable[Dict]] = None) -> Dict:
    """设备宕机：现场核查设备本身，并在等价拓扑上预演其全部链路中断后的业务影响（计入其余活动告警）。"""
    links = [l for l in topo.links if node in (l.a, l.b)]
    peers = [(l.other(node), l.b_if if l.a == node else l.a_if) for l in links]
    steps = [dict(step=f"确认设备 {node} 供电、主控板与风扇状态，经 Console 登录查看是否重启或死机",
                  cmds=[_cmd(topo, node, "device"), _cmd(topo, node, "log")]),
             dict(step="在相邻设备上核对对端端口状态与 OSPF 邻居",
                  cmds=[_cmd(topo, p, "if_status", ifn=ifn) for p, ifn in peers if topo.nodes[p].transit]
                  + [_cmd(topo, p, "peer") for p, _ in peers[:1] if topo.nodes[p].transit]),
             dict(step="设备恢复后确认全部邻接重建、路径回到基线",
                  cmds=[_cmd(topo, p, "peer") for p, _ in peers if topo.nodes[p].transit][:2])]
    down = {l.id for l in links}
    odown, cost, bad, note = _context(others)
    # 故障设备自身、其链路上对端接口的地址（随链路撤销）、由它发起的 NQA 探测，不计入业务影响
    targets = [t for t in topo.targets()
               if t.node != node and t.src != node and not (t.kind == "if" and t.link in down)]
    cache: dict = {}
    base = _routes(topo, targets, odown, cost, cache)
    after = _routes(topo, targets, odown | down, cost, cache)
    lost = [i for i in base if base[i] and not after[i]]
    moved = [i for i in base if base[i] and after[i] and after[i][0] != base[i][0]]
    via = _via(bad, {x for i in moved for x in after[i][1]})
    reh = dict(action="依赖 OSPF 自动绕开故障设备", feasible=not lost,
               detail=f"{len(moved)} 个监测目标已切换到备份路径" +
                      (f"；备份路径经过同样告警中的 {via}" if via else "") +
                      (f"；仍中断的目标: {_names(lost)}" if lost else "；其余业务无单点中断") + note)
    return dict(summary=f"设备 {node} 宕机（{'、'.join(l.id for l in links)} 全部中断）", type_cn="设备宕机",
                steps=steps, rehearsal=reh)


def advise(topo: Topology, ftype: str, lid: Optional[str], cur_cost: Optional[float] = None,
           others: Optional[Iterable[Dict]] = None) -> Dict:
    """链路级告警的处置建议；预演计入其余活动告警 others（见 rehearse）。"""
    if ftype == "normal" or not lid:
        return dict(summary="未发现需处置的故障", steps=[], rehearsal=None)
    if ftype not in PERF + ("link_down", "route_drift"):
        raise ValueError(f"没有处置建议模板的故障类型 {ftype}")
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
    reh = rehearse(topo, ftype, lid, cur_cost, others)
    if reh["feasible"] and ftype in PERF:
        steps.append(dict(step="临时处置（已在等价网络预演通过）：" + reh["action"],
                          cmds=[_cmd(topo, na, "set_cost", ifn=ia, cost=l.cost * 10),
                                _cmd(topo, nb, "set_cost", ifn=ib, cost=l.cost * 10)]))
    return dict(summary=summary, type_cn=TYPE_LABEL[ftype], steps=steps, rehearsal=reh)
