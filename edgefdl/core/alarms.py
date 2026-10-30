# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""告警输出通道：与其他网管系统集成（Webhook / Syslog），CSV 落盘见 measure/store。

Prometheus 指标由 core/api 的 /metrics 暴露；SNMP Trap 需要 pysnmp，按需在部署期接入。
"""
from __future__ import annotations

import json
import socket
import threading
import urllib.request
from datetime import datetime
from typing import Dict, List

SEVERITY = {"node_down": "critical", "link_down": "critical", "route_drift": "major", "loss": "major", "bandwidth": "major",
            "delay": "minor", "jitter": "minor"}


class WebhookSink:
    def __init__(self, url: str, timeout: float = 3.0):
        self.url, self.timeout = url, timeout

    def send(self, alarm: Dict):
        body = json.dumps(alarm, ensure_ascii=False).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
        threading.Thread(target=self._post, args=(req,), daemon=True).start()   # 不阻塞测量循环

    def _post(self, req):
        try:
            urllib.request.urlopen(req, timeout=self.timeout).read()
        except Exception:
            pass


class SyslogSink:
    """RFC 5424 over UDP，主流网管 / SIEM 均可直接接收。"""
    PRI = {"critical": 2, "major": 3, "minor": 4, "clear": 6}

    def __init__(self, host: str, port: int = 514, app: str = "edgefdl"):
        self.addr = (host, port)
        self.app = app
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.hostname = socket.gethostname()

    def send(self, alarm: Dict):
        sev = "clear" if alarm.get("event") == "clear" else alarm.get("severity", "minor")
        pri = 16 * 8 + self.PRI.get(sev, 5)                  # facility local0
        msg = (f"<{pri}>1 {datetime.now().astimezone().isoformat()} {self.hostname} {self.app} - "
               f"{alarm.get('event', 'raise')} - {alarm.get('fault_type_cn', '')} {alarm.get('link_id', '')} "
               f"{alarm.get('endpoints', '')} conf={alarm.get('confidence', '')}")
        try:
            self.sock.sendto(msg.encode(), self.addr)
        except OSError:
            pass


def make_sinks(cfg: List[Dict]):
    out = []
    for c in cfg or []:
        if c.get("type") == "webhook":
            out.append(WebhookSink(c["url"]))
        elif c.get("type") == "syslog":
            out.append(SyslogSink(c["host"], int(c.get("port", 514))))
    return out
