# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""HTTP 接口与可视化前端（仅标准库，便于在麒麟等离线环境部署）。

GET  /                       运维大屏
GET  /api/topology           拓扑 + 布局
GET  /api/status             运行快照（告警、链路健康、推理时延、带宽/资源占用）
GET  /api/alarms             告警历史
GET  /api/series?link=L1     链路相关目标的 RTT/丢包序列
GET  /api/export/<name>.csv  下载测量日志 / 告警日志
GET  /metrics                Prometheus 指标（与其他网管/监控系统集成）
POST /api/inject             演示：注入故障 {type, link, value}（仅虚拟网络后端）
POST /api/clear              演示：清除故障 {link?}
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict
from urllib.parse import parse_qs, urlparse

from ..faults.model import FAULT_TYPES, TYPE_LABEL, Fault
from ..topology import Topology

WEB = os.path.join(os.path.dirname(__file__), "web")


def layout(topo: Topology) -> Dict[str, list]:
    """按到网管的跳数分层布局，层内按名称排序。"""
    level = {topo.vantage: 0}
    frontier = [topo.vantage]
    while frontier:
        nxt = []
        for u in frontier:
            for v, _ in topo._adj[u]:
                if v not in level:
                    level[v] = level[u] + 1
                    nxt.append(v)
        frontier = nxt
    rows: Dict[int, list] = {}
    for n in topo.nodes:
        rows.setdefault(level.get(n, max(level.values()) + 1), []).append(n)
    pos = {}
    H = max(rows) or 1
    for lv, ns in rows.items():
        ns.sort()
        for i, n in enumerate(ns):
            pos[n] = [round((i + 1) / (len(ns) + 1), 3), round(0.08 + 0.84 * lv / H, 3)]
    return pos


def make_handler(engine, topo: Topology, allow_inject: bool = True):
    pos = layout(topo)
    topo_json = dict(topo.to_dict(), layout=pos,
                     fault_types=[dict(id=t, label=TYPE_LABEL[t]) for t in FAULT_TYPES])

    class H(BaseHTTPRequestHandler):
        server_version = "edgefdl"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False, default=str).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            try:
                if u.path in ("/", "/index.html"):
                    with open(os.path.join(WEB, "index.html"), "rb") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8")
                if u.path == "/api/topology":
                    return self._send(200, topo_json)
                if u.path == "/api/status":
                    return self._send(200, engine.snapshot())
                if u.path == "/api/alarms":
                    return self._send(200, engine.alarm_history(int(q.get("n", ["50"])[0])))
                if u.path == "/api/series":
                    return self._send(200, engine.target_series(q.get("link", [""])[0]))
                if u.path.startswith("/api/export/"):
                    name = os.path.basename(u.path)
                    if name not in ("measure_log.csv", "alarm_log.csv"):
                        return self._send(404, {"error": "unknown"})
                    with open(os.path.join(engine.log_dir, name), "rb") as f:
                        return self._send(200, f.read(), "text/csv; charset=utf-8",
                                          {"Content-Disposition": f"attachment; filename={name}"})
                if u.path == "/metrics":
                    return self._send(200, prometheus(engine), "text/plain; version=0.0.4")
                return self._send(404, {"error": "not found"})
            except Exception as e:                       # 接口异常不影响测量主循环
                return self._send(500, {"error": str(e)})

        def do_POST(self):
            u = urlparse(self.path)
            n = int(self.headers.get("Content-Length", 0) or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
                if not allow_inject or engine.backend.name not in ("sim", "netns", "lxd"):
                    return self._send(403, {"error": "当前后端不允许注入故障"})
                if u.path == "/api/inject":
                    f = Fault(body["type"], body["link"], float(body.get("value", 1.0)))
                    topo.link(f.link)
                    with engine.lock:
                        engine.backend.inject(f)
                    return self._send(200, {"ok": True, "fault": f.to_dict(), "label": f.label})
                if u.path == "/api/clear":
                    with engine.lock:
                        engine.backend.clear(body.get("link") or None)
                    return self._send(200, {"ok": True})
                return self._send(404, {"error": "not found"})
            except Exception as e:
                return self._send(400, {"error": str(e)})

    return H


def prometheus(engine) -> str:
    s = engine.snapshot()
    lines = [
        "# HELP edgefdl_link_health 链路健康度 0 正常 1 异常",
        "# TYPE edgefdl_link_health gauge",
    ]
    for lid, h in s["link_health"].items():
        lines.append(f'edgefdl_link_health{{link="{lid}"}} {h}')
    a = s["active_alarm"]
    lines += ["# TYPE edgefdl_alarm_active gauge",
              f'edgefdl_alarm_active{{type="{a["fault_type"] if a else ""}",link="{a["link_id"] if a else ""}"}} {1 if a else 0}',
              "# TYPE edgefdl_alarms_active gauge", f"edgefdl_alarms_active {len(s.get('active_alarms', []))}",
              "# TYPE edgefdl_alarms_total counter", f"edgefdl_alarms_total {s['alarms_total']}",
              "# TYPE edgefdl_filtered_total counter", f"edgefdl_filtered_total {s['suppressed']}",
              "# TYPE edgefdl_round counter", f"edgefdl_round {s['round']}"]
    if s.get("active_alarms"):                        # 多故障：每条活动告警一条
        lines.append("# TYPE edgefdl_alarm gauge")
        for x in s["active_alarms"]:
            lines.append(f'edgefdl_alarm{{id="{x["alarm_id"]}",type="{x["fault_type"]}",link="{x["link_id"]}",'
                         f'severity="{x["severity"]}"}} 1')
    if s["infer"]:
        lines += ["# TYPE edgefdl_infer_ms gauge", f"edgefdl_infer_ms{{q=\"p99\"}} {s['infer']['p99_ms']}"]
    if s["overhead"]:
        lines += ["# TYPE edgefdl_probe_bw_pct gauge", f"edgefdl_probe_bw_pct {s['overhead']['pct_of_min_link']}"]
    return "\n".join(lines) + "\n"


def serve(engine, topo: Topology, host: str = "0.0.0.0", port: int = 8460, allow_inject: bool = True):
    httpd = ThreadingHTTPServer((host, port), make_handler(engine, topo, allow_inject))
    httpd.daemon_threads = True
    return httpd
