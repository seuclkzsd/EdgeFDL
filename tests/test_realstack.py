# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""真实协议栈准确率测试脚本的快速单元测试：场景枚举、分片、汇总口径（不起容器、不用 netns）。"""
import importlib.util
import json
import os
import random
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from edgefdl import topology  # noqa: E402
from edgefdl.measure import probes  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
spec = importlib.util.spec_from_file_location("realstack_eval", os.path.join(ROOT, "scripts", "realstack_eval.py"))
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)


def row(link, ftype, alarm=None, t=30.0, cleared=True, wrong=0):
    """单场景记录；alarm=(类型, 链路)，缺省为告警完全正确，False 为未告警。"""
    a = (ftype, link) if alarm is None else alarm
    hit = bool(a)
    return dict(link=link, type=ftype, value=1.0, alarmed=hit, alarm_type=a[0] if hit else None,
                alarm_link=a[1] if hit else None, type_ok=hit and a[0] == ftype, loc_ok=hit and a[1] == link,
                time_to_alarm_s=t if hit else None, cleared=hit and cleared, time_to_clear_s=15.0 if hit and cleared else None,
                wrong_alarms=wrong, alarms=[], pre_alarms=0)


class TestScenarios(unittest.TestCase):
    def setUp(self):
        self.topo = topology.load(os.path.join(ROOT, "configs", "demo_topology.yaml"))

    def test_same_scenarios_as_bench(self):
        """84 个单故障，顺序与取值和已发布的 results/bench.json 逐条相同。"""
        with open(os.path.join(ROOT, "results", "bench.json"), encoding="utf-8") as f:
            rows = json.load(f)["scenarios"]["rows"]
        faults = R.single_faults(self.topo)
        self.assertEqual(len(faults), 84)
        self.assertEqual([(f.link, f.type, f.value) for f in faults], [(r["link"], r["type"], r["value"]) for r in rows])

    def test_shards_partition_scenarios(self):
        faults = R.single_faults(self.topo)
        parts = [R.shard_of(faults, i, 6) for i in range(6)]
        self.assertEqual([len(p) for p in parts], [14] * 6)
        self.assertEqual(sorted(id(f) for p in parts for f in p), sorted(id(f) for f in faults))
        for p in parts:                   # 每片都含多种链路与类型，单个分片的异常不会集中在一类
            self.assertGreaterEqual(len({f.type for f in p}), 5)
            self.assertGreaterEqual(len({f.link for f in p}), 12)

    def test_parse_only(self):
        self.assertEqual(R.parse_only("L4:delay,L9:loss"), [("L4", "delay"), ("L9", "loss")])


class TestSummary(unittest.TestCase):
    def test_counts_and_targets(self):
        rows = [row("L1", "link_down"), row("L2", "link_down", alarm=("link_down", "L1")),
                row("L1", "delay", t=40.0), row("L2", "delay", alarm=False),
                row("L1", "route_drift", cleared=False)]
        s = R.summarize(rows)
        self.assertEqual(s["overall"]["n"], 5)
        self.assertEqual(s["overall"]["alarmed"], 4)
        self.assertEqual(s["overall"]["joint_ok"], 3)
        self.assertEqual(s["overall"]["cleared"], 3)
        self.assertEqual(s["by_type"]["link_down"]["type_ok"], 2)
        self.assertEqual(s["by_type"]["link_down"]["joint_ok"], 1)
        self.assertEqual(s["by_type"]["delay"]["mean_time_to_alarm_s"], 40.0)
        self.assertFalse(s["by_category"]["Link down"]["meets_target"])
        self.assertTrue(s["by_category"]["Route drift"]["meets_target"])
        self.assertEqual({(m["link"], m["type"]) for m in s["misses"]}, {("L2", "link_down"), ("L2", "delay")})

    def test_normal_rates(self):
        runs = [dict(noise_prob=0.0, rounds=720, false_alarms=1, detector_triggers=2, filtered_by_agent=1,
                     false_alarm_exposure_rounds=5, alarms=[dict(type="loss", link="L1")]),
                dict(noise_prob=0.15, rounds=720, false_alarms=0, detector_triggers=3, filtered_by_agent=3,
                     false_alarm_exposure_rounds=0, alarms=[])]
        s = R.summarize_normal(runs, 5.0)
        self.assertEqual(s["quiet"]["hours"], 1.0)
        self.assertEqual(s["quiet"]["false_alarms_per_day"], 24.0)
        self.assertEqual(s["transients"]["false_alarms"], 0)

    def test_markdown_table_uses_the_json(self):
        rows = [row("L1", "link_down"), row("L1", "delay", alarm=False)]
        t = R.markdown_table(dict(summary=dict(single=R.summarize(rows))))
        self.assertIn("| Link down | 1 | 1 | 1 | 1 (100.0%) | 30.0 s |", t)
        self.assertIn("| **All** | 2 | 1 | 1 | 1 (50.0%) |", t)


class TestTrainOrder(unittest.TestCase):
    """包列带宽估计：到达明显乱序（逐包抖动打散包列）时估计不可信。"""

    @staticmethod
    def text(order, gap=1.2e-5, size=1400):
        ts = " ".join(f"{i * gap:.9f}" for i in range(len(order)))
        return f"ready\nseq {' '.join(map(str, order))}\n{len(order)} {size} {ts}\n"

    def test_in_order_train_is_clean(self):
        bw, clean = probes.parse_train(self.text(list(range(47))))
        self.assertTrue(clean)
        self.assertAlmostEqual(bw, (1400 + 42) * 8 / 1.2e-5 / 1e6, delta=1)

    def test_shuffled_train_is_not_clean(self):
        order = list(range(47))
        random.Random(1).shuffle(order)
        bw, clean = probes.parse_train(self.text(order))    # 到达间隔本身很均匀：只有乱序能发现
        self.assertFalse(clean)
        self.assertEqual(bw, bw)                            # 估计值照常给出，由调用方丢弃

    def test_single_swap_is_tolerated(self):
        order = list(range(47))
        order[10], order[11] = order[11], order[10]
        self.assertFalse(probes.train_reordered(order))
        self.assertTrue(probes.parse_train(self.text(order))[1])

    def test_lost_packets_are_not_reordering(self):
        self.assertFalse(probes.train_reordered([0, 1, 3, 4, 7, 8, 9, 12]))

    def test_output_without_seq_line_still_parses(self):
        ts = " ".join(f"{i * 1e-4:.9f}" for i in range(8))
        self.assertTrue(probes.parse_train(f"ready\n8 1400 {ts}\n")[1])

    def test_receiver_script_reports_order(self):
        """接收端脚本先输出到达序号，parse_train 依赖这一行。"""
        self.assertIn("print('seq'", probes.TRAIN_RECV)


class TestLabImage(unittest.TestCase):
    def test_default_image_has_no_torch(self):
        """PyTorch 是可选构建参数：默认镜像（CI 用）不变，只有 TORCH=1 才装。"""
        with open(os.path.join(ROOT, "scripts", "lab", "Dockerfile"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("ARG TORCH=0", text)
        self.assertTrue(re.search(r'if \[ "\$TORCH" = 1 \]; then', text))


if __name__ == "__main__":
    unittest.main()
