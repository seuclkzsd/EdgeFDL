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
POST /api/inject             演示：注入故障 {type, link, value}（仅虚拟网络后端，live 后端一律拒绝）
POST /api/clear              演示：清除故障 {link?}

跨站防护（接口无认证，见 SECURITY.md）：
- POST 只接受 Content-Type: application/json（浏览器跨站发送此类请求须先预检，本服务不应答预检），
  带 Origin 头时须与请求的 Host 同源；
- 监听回环地址（默认 127.0.0.1）时，Host 头须为 localhost / 127.0.0.1 / ::1（端口不限，便于 SSH 隧道转发），
  防 DNS 重绑定；--host 显式指定非回环地址时不校验 Host（已决定对网络开放）。
JSON 响应不含 NaN/Infinity（未测得的值为 null）。
"""
from __future__ import annotations

import ipaddress
import json
import math
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional
from urllib.parse import parse_qs, urlparse, urlsplit

from ..faults.generator import drift_allowed
from ..faults.injector import drift_cost
from ..faults.model import FAULT_TYPES, TYPE_LABEL, Fault
from ..topology import Topology
from .alarms import dumps

WEB = os.path.join(os.path.dirname(__file__), "web")
LOCAL_NAMES = ("localhost", "127.0.0.1", "::1")
MAX_BODY = 64 * 1024
# /api/inject 的取值范围（value 的含义见 docs/api.md）；route_drift 另须使开销不超过 OSPF 上限 65535
VALUE_RANGE = {"delay": (0.0, 10000.0, "ms"), "jitter": (0.0, 10000.0, "ms"),
               "loss": (0.0, 1.0, "（丢包率）"), "bandwidth": (0.0, 1.0, "（剩余带宽比例）")}


class BadRequest(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


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


def _hostport(v: str, default_port: Optional[int] = None):
    """'host[:port]' → (小写主机名, 端口)；不合法抛 ValueError。"""
    u = urlsplit("//" + v)
    if not u.hostname:
        raise ValueError(v)
    return u.hostname.lower(), u.port or default_port


def _bad_const(c):
    raise BadRequest(400, f"请求体含 {c}，不是合法的 JSON 数值")


def parse_fault(topo: Topology, body) -> Fault:
    """校验 /api/inject 的请求体，返回故障；不合法抛 BadRequest(400)。"""
    if not isinstance(body, dict):
        raise BadRequest(400, "请求体须为 JSON 对象")
    for k in ("type", "link"):
        if k not in body:
            raise BadRequest(400, f"缺少字段 {k}")
    t, lid = body["type"], body["link"]
    if not isinstance(t, str) or t not in FAULT_TYPES:
        raise BadRequest(400, f"未知故障类型 {t}")
    if not isinstance(lid, str) or lid not in topo.link_ids():
        raise BadRequest(400, f"未知链路 {lid}")
    if t == "link_down":                          # 链路中断不使用 value
        return Fault(t, lid, 1.0)
    v = body.get("value", 1.0)
    try:
        x = None if isinstance(v, bool) or v is None else float(v)
    except (TypeError, ValueError):
        x = None
    if x is None:
        raise BadRequest(400, f"value 须为数值，收到 {v!r}")
    if not math.isfinite(x):
        raise BadRequest(400, f"value 须为有限数值，收到 {v!r}")
    if t == "route_drift":
        cost = topo.link(lid).cost
        if not drift_allowed(topo, lid):
            raise BadRequest(400, f"route_drift 只适用于路由器之间的链路，{lid} 的一端是主机或网管")
        if x <= 0 or drift_cost(topo.link(lid), x) > 65535:
            raise BadRequest(400, f"route_drift 的 value（开销倍率）须大于 0，且使 {lid} 的开销（基线 {cost:g}）"
                                  f"不超过 65535，即倍率不超过 {65535 // cost:g}，收到 {v!r}")
    else:
        lo, hi, unit = VALUE_RANGE[t]
        if not lo < x <= hi:
            raise BadRequest(400, f"{t} 的 value 须大于 {lo:g} 且不超过 {hi:g}{unit}，收到 {v!r}")
    return Fault(t, lid, x)


def make_handler(engine, topo: Topology, allow_inject: bool = True):
    pos = layout(topo)
    read_only = bool(engine.backend.read_only)
    topo_json = dict(topo.to_dict(), layout=pos, inject_enabled=allow_inject and not read_only,
                     fault_types=[dict(id=t, label=TYPE_LABEL[t]) for t in FAULT_TYPES])

    class H(BaseHTTPRequestHandler):
        server_version = "edgefdl"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
            if isinstance(body, (dict, list)):
                body = dumps(body).encode()
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

        def _check_host(self):
            """监听回环地址时只应答以本机名访问的请求（DNS 重绑定页面的 Host 是攻击者的域名）。"""
            bound = self.server.server_address[0]
            try:
                loopback = ipaddress.ip_address(bound).is_loopback
            except ValueError:
                loopback = bound == "localhost"
            h = self.headers.get("Host")
            if not loopback or h is None:
                return
            try:
                name = _hostport(h)[0]
            except ValueError:
                name = None
            if name not in LOCAL_NAMES + (bound,):
                raise BadRequest(403, f"拒绝 Host 为 {h!r} 的请求：服务监听回环地址，只接受经 localhost / 127.0.0.1 访问"
                                      "（防 DNS 重绑定；需从其他主机访问时用 --host 指定监听地址）")

        def _body(self) -> bytes:
            """先读完请求体（出错应答前也读完，避免连接被复位、客户端收不到应答）。"""
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise BadRequest(400, "Content-Length 不合法") from None
            if not 0 <= n <= MAX_BODY:
                self.close_connection = True
                raise BadRequest(413, f"请求体过大（上限 {MAX_BODY} 字节）")
            return self.rfile.read(n)

        def _check_post(self):
            """跨站请求防护：带 Origin 时须与 Host 同源，且只接受 JSON 请求体。"""
            o = self.headers.get("Origin")
            if o is not None:
                try:
                    u = urlsplit(o)
                    if u.scheme not in ("http", "https"):
                        raise ValueError(o)
                    dflt = 443 if u.scheme == "https" else 80
                    same = _hostport(u.netloc, dflt) == _hostport(self.headers.get("Host") or "", dflt)
                except ValueError:
                    same = False
                if not same:
                    raise BadRequest(403, f"拒绝跨站请求：Origin {o!r} 与本服务地址不一致")
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                raise BadRequest(415, "POST 请求须为 Content-Type: application/json")

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            try:
                self._check_host()
                if u.path in ("/", "/index.html"):
                    with open(os.path.join(WEB, "index.html"), "rb") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8")
                if u.path == "/api/topology":
                    return self._send(200, topo_json)
                if u.path == "/api/status":
                    return self._send(200, engine.snapshot())
                if u.path == "/api/alarms":
                    try:
                        n = int(q.get("n", ["50"])[0])
                    except ValueError:
                        n = -1
                    if n < 0:
                        raise BadRequest(400, "参数 n 须为非负整数")
                    return self._send(200, engine.alarm_history(n))
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
            except BadRequest as e:
                return self._send(e.code, {"error": str(e)})
            except Exception as e:                       # 接口异常不影响测量主循环
                return self._send(500, {"error": str(e)})

        def do_POST(self):
            u = urlparse(self.path)
            try:
                raw = self._body()
                self._check_host()
                if read_only:
                    raise BadRequest(403, "live 后端只读：不向真实网络注入或清除故障")
                if not allow_inject or engine.backend.name not in ("sim", "netns", "lxd"):
                    raise BadRequest(403, "当前后端不允许注入故障")
                self._check_post()
                try:
                    body = json.loads(raw or b"{}", parse_constant=_bad_const)
                except ValueError as e:                  # 含非 UTF-8 请求体
                    raise BadRequest(400, f"请求体不是合法的 JSON：{e}") from None
                if u.path == "/api/inject":
                    f = parse_fault(topo, body)
                    with engine.lock:
                        engine.backend.inject(f)
                    return self._send(200, {"ok": True, "fault": f.to_dict(), "label": f.label})
                if u.path == "/api/clear":
                    lid = body.get("link") if isinstance(body, dict) else None
                    if not isinstance(body, dict) or (lid not in (None, "") and lid not in topo.link_ids()):
                        raise BadRequest(400, "请求体须为 JSON 对象" if not isinstance(body, dict) else f"未知链路 {lid}")
                    with engine.lock:
                        engine.backend.clear(lid or None)
                    return self._send(200, {"ok": True})
                return self._send(404, {"error": "not found"})
            except BadRequest as e:
                return self._send(e.code, {"error": str(e)})
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


def bind(host: str, port: int) -> ThreadingHTTPServer:
    """先占住端口、暂不应答：端口不可用时在测量开始之前失败，不往运行中实例的日志里写东西。"""
    httpd = ThreadingHTTPServer((host, port), BaseHTTPRequestHandler)
    httpd.daemon_threads = True
    return httpd


def serve(engine, topo: Topology, host: str = "0.0.0.0", port: int = 8460, allow_inject: bool = True,
          httpd: Optional[ThreadingHTTPServer] = None) -> ThreadingHTTPServer:
    httpd = httpd or bind(host, port)
    httpd.RequestHandlerClass = make_handler(engine, topo, allow_inject)
    return httpd
