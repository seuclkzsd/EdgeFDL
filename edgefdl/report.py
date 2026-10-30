# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""评估结果 → 与设计指标逐条对照的 Markdown。"""
from __future__ import annotations

from typing import Dict

from .faults.model import TYPE_LABEL

TARGETS = {"链路中断": 0.98, "性能劣化": 0.95, "路由漂移": 0.95}


def _p(x):
    return "-" if x is None else f"{x * 100:.2f}%"


def eval_markdown(m: Dict) -> str:
    lines = ["| 指标类别 | 样本数 | 类别识别准确率 | 故障类型准确率 | 定位准确率 | 类型+定位 | 设计指标 | 结论 |",
             "|---|---|---|---|---|---|---|---|"]
    for c, v in m["per_category"].items():
        req = TARGETS.get(c)
        ok = "" if req is None else ("达标" if v["category_acc"] >= req else "未达标")
        lines.append(f"| {c} | {v['n']} | {_p(v['category_acc'])} | {_p(v['type_acc'])} | {_p(v.get('loc_acc'))} | "
                     f"{_p(v.get('joint_acc'))} | {'≥' + format(req, '.0%') if req else '-'} | {ok} |")
    lines += ["", "| 故障类型 | 样本数 | 判决器触发率 | 类型准确率 | 定位准确率 | 平均精测次数 | 平均发现时延 s |",
              "|---|---|---|---|---|---|---|"]
    for t, v in m["per_type"].items():
        lines.append(f"| {TYPE_LABEL.get(t, t)} | {v['n']} | {_p(v.get('detect_rate'))} | {_p(v['type_acc'])} | "
                     f"{_p(v.get('loc_acc'))} | {v['avg_probes']:.2f} | "
                     f"{'-' if v.get('detect_delay_s') is None else format(v['detect_delay_s'], '.1f')} |")
    o = m["overall"]
    lines += ["", f"总体：类型准确率 {_p(o['type_acc'])}，类型+定位 {_p(o['joint_acc'])}，漏报率 {_p(o['miss_rate'])}，"
                  f"正常样本误报率 {_p(o['false_alarm_rate'])}（其中含瞬时噪声样本 {_p(o['false_alarm_rate_noisy'])}），"
                  f"判决器误触发率 {_p(o['false_trigger_rate'])}，平均精测 {o['avg_probes']:.2f} 次/样本。"]
    return "\n".join(lines)
