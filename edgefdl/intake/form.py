# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""用户需求采集：采集表（YAML）+ 设备配置 + LLDP + 图片 → 个性化边缘网络拓扑。

采集表给出设备清单、角色、机房位置、网管接入点和链路性能（时延/带宽实测值）；
未填的接口、IP、开销、链路从配置文本与 LLDP 中提取，仍缺失的由系统按地址池自动规划。
"""
from __future__ import annotations

import glob
import os
from typing import Dict, List, Optional

import yaml

from ..topology import Topology, from_dict
from .extract import links_from_subnets, llm_extract, parse_config, read_lldp

FORM_TEMPLATE = """# 边缘网络需求采集表（请按实际填写，未知项可留空）
name: 某单位边缘网络
vantage: ems                 # 网管服务器（EdgeMS）设备名
nodes:                       # 设备清单
  - {name: core, role: router, vendor: huawei, model: AR6280, site: 中心机房}
  - {name: ems,  role: ems, site: 中心机房}
links:                       # 已知链路（可留空，由配置/LLDP 自动识别）
  - {a: core, b: ems, bw: 1000, delay: 0.1}
link_perf:                   # 可选：链路实测性能，用于虚拟网络等价校准
  # core-agg1: {delay: 1.0, bw: 1000}
"""


def build_topology(form_path: str, configs: Optional[str] = None, lldp: Optional[str] = None,
                   llm: Optional[str] = None) -> Topology:
    with open(form_path, encoding="utf-8") as f:
        form = yaml.safe_load(f) or {}
    nodes: Dict[str, Dict] = {n["name"]: dict(n) for n in form.get("nodes", [])}
    links: List[Dict] = [dict(l) for l in form.get("links", []) or []]
    devices = []
    if configs:
        for p in sorted(glob.glob(os.path.join(configs, "*"))):
            ext = os.path.splitext(p)[1].lower()
            if ext in (".png", ".jpg", ".jpeg", ".pdf"):
                if llm:
                    got = llm_extract(p, llm)
                    for n in got.get("nodes", []):
                        nodes.setdefault(n["name"], n)
                    links.extend(got.get("links", []))
                continue
            with open(p, encoding="utf-8", errors="ignore") as f:
                d = parse_config(f.read())
            d["name"] = d["name"] or os.path.splitext(os.path.basename(p))[0]
            devices.append(d)
            nd = nodes.setdefault(d["name"], {"name": d["name"], "role": "router"})
            nd.setdefault("vendor", d["vendor"])
            if d["loopback"]:
                nd.setdefault("loopback", d["loopback"])
    known = {frozenset((l["a"], l["b"])) for l in links}
    for l in links_from_subnets(devices, known_nodes=set(nodes)):
        k = frozenset((l["a"], l["b"]))
        if k not in known:
            links.append(l)
            known.add(k)
        else:                                # 采集表已列出该链路：用配置补全接口/IP/开销
            for x in links:
                if frozenset((x["a"], x["b"])) == k:
                    same = x["a"] == l["a"]
                    swap = {"a_if": "b_if", "b_if": "a_if", "a_ip": "b_ip", "b_ip": "a_ip"}
                    for key in ("a_if", "b_if", "a_ip", "b_ip"):
                        src = key if same else swap[key]
                        if l.get(src):
                            x.setdefault(key, l[src])
                    x.setdefault("cost", l["cost"])
    if lldp:
        for l in read_lldp(lldp):
            k = frozenset((l["a"], l["b"]))
            if k not in known and l["a"] in nodes and l["b"] in nodes:
                links.append(l)
                known.add(k)
    perf = form.get("link_perf") or {}
    for l in links:
        p = perf.get(f"{l['a']}-{l['b']}") or perf.get(f"{l['b']}-{l['a']}")
        if p:
            l.update({k: v for k, v in p.items() if k in ("delay", "bw", "jitter", "loss")})
    d = dict(name=form.get("name", "edge"), description=form.get("description", ""),
             vantage=form.get("vantage", "ems"), ip_plan=form.get("ip_plan", {}),
             nodes=list(nodes.values()), links=links)
    return from_dict(d)
