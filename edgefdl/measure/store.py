# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""测量日志 / 告警日志的 CSV 标准化存储。

- 固定列、带 schema_version，每行自包含（时间戳、轮次、源、目标 IP/设备/链路），可追溯、可二次解析；
- 逐行 flush、定期 fsync，按大小滚动归档并限制保留份数，7×24 运行时不丢日志、不撑满磁盘；
- 列定义同时输出到同目录的 *.schema.json，供其他网管系统对接；
- 防公式注入（OWASP CSV Injection）：以 = + - @ 制表符、回车开头的文本单元格前加单引号，
  电子表格软件打开导出文件时按文本显示、不当公式执行；数值列与形如 -1.5 的纯数字文本不受影响。
"""
from __future__ import annotations

import csv
import json
import os
import re
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional

from .model import DeepProbe, RoundResult

SCHEMA_VERSION = "1.0"

MEASURE_COLUMNS = [
    ("schema_version", "日志格式版本"),
    ("ts", "ISO8601 时间（本地时区）"),
    ("epoch", "Unix 时间戳 s"),
    ("round_id", "测量轮次号"),
    ("sample_id", "样本号（仅样本生成时填写）"),
    ("vantage", "测量源"),
    ("kind", "记录类型 ping|bw|lsdb|deep"),
    ("target_id", "目标编号"),
    ("target_ip", "目标 IP"),
    ("target_node", "目标设备"),
    ("link_id", "关联链路"),
    ("sent", "发包数"),
    ("recv", "收包数"),
    ("loss", "丢包率 0~1"),
    ("rtt_min_ms", "最小 RTT ms"),
    ("rtt_avg_ms", "平均 RTT ms"),
    ("rtt_max_ms", "最大 RTT ms"),
    ("rtt_mdev_ms", "RTT 标准差（抖动）ms"),
    ("bw_mbps", "带宽 Mbps"),
    ("path", "逐跳路径（设备名，> 分隔）"),
    ("adj_up", "OSPF 邻接是否正常"),
    ("cost", "OSPF 接口开销"),
    ("fault_label", "注入故障（仅样本生成时填写）"),
    ("probe_bytes", "本条测量注入字节数"),
]

ALARM_COLUMNS = [
    ("schema_version", "日志格式版本"),
    ("alarm_id", "告警编号"),
    ("ts", "ISO8601 时间"),
    ("epoch", "Unix 时间戳 s"),
    ("event", "raise 产生 | update 更新 | clear 恢复"),
    ("severity", "critical|major|minor"),
    ("category", "链路中断|性能劣化|路由漂移"),
    ("fault_type", "故障类型代码"),
    ("fault_type_cn", "故障类型"),
    ("link_id", "故障链路（设备宕机时为该设备全部链路，以 / 分隔）"),
    ("endpoints", "链路两端 设备:接口（设备宕机时为设备名与型号）"),
    ("site", "物理位置"),
    ("confidence", "置信度 0~1"),
    ("evidence", "研判依据（JSON）"),
    ("repair", "处置建议"),
    ("probes_used", "研判期间追加精测次数"),
    ("infer_ms", "推理耗时 ms"),
    ("round_id", "触发轮次"),
    ("model", "模型版本"),
]


_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_PLAIN_NUMBER = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")


def csv_safe(v):
    """文本单元格若会被电子表格当公式解析，前加单引号；数字与纯数字文本原样返回。"""
    if isinstance(v, str) and v.startswith(_FORMULA_START) and not _PLAIN_NUMBER.fullmatch(v):
        return "'" + v
    return v


def _now():
    t = time.time()
    return t, datetime.fromtimestamp(t).isoformat(timespec="milliseconds")


class _RollingCsv:
    def __init__(self, path: str, columns, max_bytes: int = 200 * 1024 * 1024, fsync_every: float = 5.0,
                 keep: int = 20):
        self.path = path
        self.keep = keep                       # 最多保留的归档份数（默认约 4GB），超出删除最旧的
        self.cols = [c for c, _ in columns]
        self.max_bytes = max_bytes
        self.fsync_every = fsync_every
        self._last_sync = time.time()
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(os.path.splitext(path)[0] + ".schema.json", "w", encoding="utf-8") as f:
            json.dump({"schema_version": SCHEMA_VERSION,
                       "columns": [{"name": c, "desc": d} for c, d in columns]}, f, ensure_ascii=False, indent=1)
        self._open()

    def _open(self):
        new = not os.path.exists(self.path) or os.path.getsize(self.path) == 0
        self.f = open(self.path, "a", newline="", encoding="utf-8")
        self.w = csv.DictWriter(self.f, fieldnames=self.cols, extrasaction="ignore")
        if new:
            self.w.writeheader()

    def write(self, rows: List[dict]):
        with self._lock:
            for r in rows:
                r.setdefault("schema_version", SCHEMA_VERSION)
                self.w.writerow({k: csv_safe(v) for k, v in r.items()})
            self.f.flush()
            if time.time() - self._last_sync > self.fsync_every:
                os.fsync(self.f.fileno())
                self._last_sync = time.time()
            if self.f.tell() > self.max_bytes:
                self.f.close()
                ts = int(f"{datetime.now():%Y%m%d%H%M%S%f}")
                while os.path.exists(f"{self.path}.{ts}"):     # 同一微秒内再次滚动：不覆盖已有归档
                    ts += 1
                os.rename(self.path, f"{self.path}.{ts}")
                self._prune()
                self._open()

    def _prune(self):
        d, base = os.path.split(os.path.abspath(self.path))
        olds = sorted(f for f in os.listdir(d) if f.startswith(base + ".") and f[len(base) + 1:].isdigit())
        for f in olds[:max(0, len(olds) - self.keep)]:
            os.remove(os.path.join(d, f))

    def close(self):
        with self._lock:
            self.f.flush()
            os.fsync(self.f.fileno())
            self.f.close()


def _fmt(x, nd=3):
    if x is None or (isinstance(x, float) and x != x):
        return ""
    return round(x, nd) if isinstance(x, float) else x


class CsvStore:
    """测量日志。topo 可选：给定时补全目标 IP / 设备 / 链路字段。"""

    def __init__(self, path: str, topo=None):
        self.log = _RollingCsv(path, MEASURE_COLUMNS)
        self.tmap: Dict[str, object] = {t.id: t for t in topo.targets()} if topo else {}

    def write_round(self, rr: RoundResult, sample_id: Optional[int] = None, fault=None):
        ts = datetime.fromtimestamp(rr.ts).isoformat(timespec="milliseconds")
        base = dict(ts=ts, epoch=round(rr.ts, 3), round_id=rr.round_id, vantage=rr.vantage,
                    sample_id="" if sample_id is None else sample_id,
                    fault_label=fault.label if fault else ("normal" if sample_id is not None else ""))
        rows = []
        n = max(1, len(rr.pings))
        for tid, p in rr.pings.items():
            t = self.tmap.get(tid)
            rows.append(dict(base, kind="ping", target_id=tid,
                             target_ip=getattr(t, "ip", ""), target_node=getattr(t, "node", ""),
                             link_id=getattr(t, "link", "") or "", sent=p.sent, recv=p.recv,
                             loss=_fmt(p.loss), rtt_min_ms=_fmt(p.rtt_min), rtt_avg_ms=_fmt(p.rtt_avg),
                             rtt_max_ms=_fmt(p.rtt_max), rtt_mdev_ms=_fmt(p.rtt_mdev),
                             path=">".join(p.path), probe_bytes=rr.probe_bytes // n))
        for lid, bw in rr.bw.items():
            rows.append(dict(base, kind="bw", link_id=lid, bw_mbps=_fmt(bw, 1)))
        for lid, e in rr.lsdb.items():
            rows.append(dict(base, kind="lsdb", link_id=lid, adj_up=int(e.adj_up), cost=_fmt(e.cost, 0)))
        self.log.write(rows)

    def write_deep(self, dp: DeepProbe, round_id: int, vantage: str):
        t, ts = _now()
        self.log.write([dict(ts=ts, epoch=round(t, 3), round_id=round_id, vantage=vantage, kind="deep",
                             link_id=dp.link, sent=dp.sent, recv=dp.recv, loss=_fmt(dp.loss),
                             rtt_avg_ms=_fmt(dp.rtt_avg), rtt_mdev_ms=_fmt(dp.rtt_mdev),
                             bw_mbps=_fmt(dp.bw_mbps, 1), probe_bytes=dp.probe_bytes)])

    def close(self):
        self.log.close()


class AlarmStore:
    def __init__(self, path: str):
        self.log = _RollingCsv(path, ALARM_COLUMNS, fsync_every=0.0)   # 告警逐条落盘

    def write(self, alarm: dict):
        t, ts = _now()
        row = dict(alarm)
        row.setdefault("ts", ts)
        row.setdefault("epoch", round(t, 3))
        if isinstance(row.get("evidence"), (dict, list)):
            row["evidence"] = json.dumps(row["evidence"], ensure_ascii=False)
        self.log.write([row])

    def close(self):
        self.log.close()
