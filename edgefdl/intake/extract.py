# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""多模态信息提取：从网络运维方提供的各类材料中抽取设备、接口、IP、链路与开销。

支持：
  - 设备配置 / 命令回显文本：华为 VRP、华三 Comware（display current-configuration），
    思科 IOS（show running-config）；自动识别 sysname/hostname；
  - LLDP 邻居表 CSV（local_device,local_if,remote_device,remote_if）；
  - 拓扑图片 / 扫描件 / PDF：可选调用 OpenAI 兼容的多模态大模型（可本地部署），输出结构化 JSON。
"""
from __future__ import annotations

import base64
import csv
import ipaddress
import json
import os
import re
import urllib.request
from typing import Dict, List, Optional

IF_SPEED = [  # 接口名前缀 → 带宽 Mbps
    (r"^(100GE|HundredGig)", 100000), (r"^(40GE|FortyGig)", 40000), (r"^(25GE|TwentyFiveGig)", 25000),
    (r"^(XGigabitEthernet|Ten-GigabitEthernet|TenGigabitEthernet|XGE|Te)", 10000),
    (r"^(GigabitEthernet|GE|Gi)", 1000), (r"^(FastEthernet|Ethernet|Fa|Eth)", 100),
]


def if_speed(name: str) -> Optional[float]:
    for pat, sp in IF_SPEED:
        if re.match(pat, name, re.I):
            return float(sp)
    return None


def detect_vendor(text: str) -> str:
    if re.search(r"^\s*sysname\s", text, re.M):
        return "h3c" if re.search(r"Comware|H3C", text) else "huawei"
    if re.search(r"^\s*hostname\s", text, re.M):
        return "cisco"
    return "generic"


def parse_config(text: str) -> Dict:
    """返回 {name, vendor, interfaces:[{name, ip, prefix, cost, bw, shutdown, desc}], loopback}"""
    vendor = detect_vendor(text)
    m = re.search(r"^\s*(?:sysname|hostname)\s+(\S+)", text, re.M)
    name = m.group(1) if m else None
    ifs: List[Dict] = []
    cur: Optional[Dict] = None
    for line in text.splitlines():
        mi = re.match(r"^interface\s+(\S+)", line.strip()) if not line.startswith((" ", "\t")) else None
        if mi:
            cur = dict(name=mi.group(1), ip=None, prefix=None, cost=None, bw=if_speed(mi.group(1)),
                       shutdown=False, desc="")
            ifs.append(cur)
            continue
        if cur is None:
            continue
        s = line.strip()
        if s in ("#", "!", "quit", "exit") or (line and not line.startswith((" ", "\t")) and s):
            cur = None
            continue
        ma = re.match(r"ip address\s+(\d+\.\d+\.\d+\.\d+)\s+(\d+\.\d+\.\d+\.\d+|\d+)", s)
        if ma and cur["ip"] is None:
            mask = ma.group(2)
            prefix = int(mask) if mask.isdigit() else ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
            cur["ip"], cur["prefix"] = ma.group(1), prefix
        mc = re.match(r"(?:ospf cost|ip ospf cost)\s+(\d+)", s)
        if mc:
            cur["cost"] = int(mc.group(1))
        mb = re.match(r"bandwidth\s+(\d+)", s)
        if mb:                                   # 思科 bandwidth 单位 kbps
            cur["bw"] = int(mb.group(1)) / 1000.0
        if s in ("shutdown",):
            cur["shutdown"] = True
        md = re.match(r"description\s+(.*)", s)
        if md:
            cur["desc"] = md.group(1)
    lo = next((i for i in ifs if re.match(r"^(LoopBack|Loopback)", i["name"]) and i["ip"]), None)
    return dict(name=name, vendor=vendor, interfaces=[i for i in ifs if i["ip"] and i is not lo],
                loopback=f"{lo['ip']}/32" if lo else "")


def links_from_subnets(devices: List[Dict], known_nodes=()) -> List[Dict]:
    """互联地址同网段（/30、/31 或两台设备独占的网段）的接口对即为一条链路；
    只有一端有配置的 /30（如对端是网管服务器、主机）按接口描述 "to-<设备名>" 补全另一端。"""
    by_net: Dict[str, List] = {}
    for d in devices:
        for i in d["interfaces"]:
            if i["shutdown"]:
                continue
            net = str(ipaddress.ip_interface(f"{i['ip']}/{i['prefix']}").network)
            by_net.setdefault(net, []).append((d["name"], i))
    links = []
    for net, ends in sorted(by_net.items()):
        if len(ends) == 1 and ipaddress.ip_network(net).prefixlen >= 30:
            da, ia = ends[0]
            m = re.search(r"to[-_ ]?([\w.-]+)", ia["desc"] or "", re.I)
            if m and m.group(1) in known_nodes:
                peer = next(h for h in ipaddress.ip_network(net).hosts() if str(h) != ia["ip"])
                links.append(dict(a=da, b=m.group(1), a_if=ia["name"], a_ip=f"{ia['ip']}/{ia['prefix']}",
                                  b_ip=f"{peer}/{ia['prefix']}", bw=ia["bw"] or 1000, cost=ia["cost"] or 10))
            continue
        if len(ends) != 2:
            continue
        (da, ia), (db, ib) = ends
        links.append(dict(a=da, b=db, a_if=ia["name"], b_if=ib["name"],
                          a_ip=f"{ia['ip']}/{ia['prefix']}", b_ip=f"{ib['ip']}/{ib['prefix']}",
                          bw=min(x for x in (ia["bw"], ib["bw"], 1e9) if x) if (ia["bw"] or ib["bw"]) else 1000,
                          cost=ia["cost"] or ib["cost"] or 10))
    return links


def read_lldp(path: str) -> List[Dict]:
    with open(path, encoding="utf-8") as f:
        return [dict(a=r["local_device"], a_if=r["local_if"], b=r["remote_device"], b_if=r["remote_if"])
                for r in csv.DictReader(f)]


LLM_PROMPT = (
    "你是网络工程师。请从这张网络拓扑图/文档中提取设备与链路，只输出 JSON："
    '{"nodes":[{"name":"","role":"router|switch|host|ems","vendor":"","model":"","site":""}],'
    '"links":[{"a":"","b":"","a_if":"","b_if":"","a_ip":"","b_ip":"","bw":1000}]}。'
    "看不清的字段留空，不要编造。")


def llm_extract(path: str, endpoint: str, model: Optional[str] = None, timeout: float = 120) -> Dict:
    """调用 OpenAI 兼容多模态接口（如本地 vLLM 部署的 Qwen2.5-VL）提取拓扑。"""
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    ext = os.path.splitext(path)[1].lower().lstrip(".") or "png"
    mime = "application/pdf" if ext == "pdf" else f"image/{'jpeg' if ext == 'jpg' else ext}"
    body = dict(model=model or os.environ.get("EDGEFDL_LLM_MODEL", "qwen2.5-vl"), temperature=0,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": LLM_PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]}])
    req = urllib.request.Request(endpoint.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + os.environ.get("EDGEFDL_LLM_KEY", "none")})
    out = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    text = out["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0)) if m else {"nodes": [], "links": []}
