# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""告警输出通道：与其他网管系统集成（Webhook / Syslog），CSV 落盘见 measure/store。

Prometheus 指标由 core/api 的 /metrics 暴露；SNMP Trap 需要 pysnmp，按需在部署期接入。
通道配置在启动时校验（make_sinks），配置有误即报错退出，而不是运行中每条告警都发送失败。
"""
from __future__ import annotations

import json
import math
import socket
import sys
import threading
import urllib.request
from datetime import datetime
from typing import Dict, List
from urllib.parse import urlsplit

import numpy as np

SEVERITY = {"node_down": "critical", "link_down": "critical", "route_drift": "major", "loss": "major", "bandwidth": "major",
            "delay": "minor", "jitter": "minor"}


def json_safe(x):
    """对外 JSON（REST、Webhook、CSV 证据列）：NaN/±Inf 换成 None（JSON 没有这些值），numpy 标量/数组换成
    Python 原生类型。"""
    if isinstance(x, dict):
        return {k: json_safe(v) for k, v in x.items()}
    if isinstance(x, (list, tuple, set, frozenset, np.ndarray)):
        return [json_safe(v) for v in (x.tolist() if isinstance(x, np.ndarray) else x)]
    if isinstance(x, np.generic):
        x = x.item()
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return x


def dumps(x) -> str:
    return json.dumps(json_safe(x), ensure_ascii=False, allow_nan=False, default=str)


class WebhookSink:
    def __init__(self, url: str, timeout: float = 3.0):
        u = urlsplit(url or "")
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError(f"Webhook 地址须为以 http:// 或 https:// 开头的完整 URL，收到 {url!r}")
        self.url, self.timeout = url, timeout
        self.failed = 0                       # 后台投递失败次数（接收方不可达、超时、非 2xx）

    def send(self, alarm: Dict):
        body = dumps(alarm).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
        threading.Thread(target=self._post, args=(req,), daemon=True).start()   # 不阻塞测量循环

    def _post(self, req):
        try:
            urllib.request.urlopen(req, timeout=self.timeout).read()
        except Exception as e:                 # noqa: BLE001  投递失败只记录，不影响告警
            self.failed += 1
            print(f"Webhook 投递失败（{self.url}）：{e!r}", file=sys.stderr, flush=True)


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
    """按配置（serve --sinks 的 JSON：对象或对象数组）建立告警通道；配置有误抛 ValueError。"""
    if isinstance(cfg, dict):
        cfg = [cfg]
    out = []
    for c in cfg or []:
        if not isinstance(c, dict):
            raise ValueError(f"告警通道配置须为 JSON 对象，收到 {c!r}")
        t = c.get("type")
        if t == "webhook":
            out.append(WebhookSink(c.get("url", "")))
        elif t == "syslog":
            if not c.get("host"):
                raise ValueError("syslog 告警通道缺少 host")
            try:
                port = int(c.get("port", 514))
            except (TypeError, ValueError):
                port = 0
            if not 0 < port < 65536:
                raise ValueError(f"syslog 端口须为 1~65535，收到 {c.get('port')!r}")
            out.append(SyslogSink(c["host"], port))
        else:
            raise ValueError(f"未知告警通道类型 {t!r}（支持 webhook、syslog）")
    return out
