# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""回放演示：录制帧的结构与故事内容、与大屏页面的约定、站点组装、Pages 工作流的固定版本。"""
import filecmp
import importlib.util
import json
import os
import re
import tempfile
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
DEMO = os.path.join(ROOT, "docs", "demo")
FRAMES = os.path.join(DEMO, "frames.json")
DASHBOARD = os.path.join(ROOT, "edgefdl", "core", "web", "index.html")
SCENES = ["normal", "degradation", "link_down", "route_drift", "two_faults", "device_down"]


def tool(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "tools", "demo", name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class FramesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(read(FRAMES))
        cls.rec = tool("record")
        cls.scenes = {s["id"]: s for s in cls.data["scenes"]}

    def unpack(self, v):
        return self.rec.unpack(v, self.data["pool"])

    def test_size_and_scenes(self):
        self.assertLess(os.path.getsize(FRAMES), 3 * 1000 * 1000)
        self.assertEqual([s["id"] for s in self.data["scenes"]], SCENES)
        self.assertEqual(self.data["meta"]["backend"], "sim")
        self.assertEqual(self.data["meta"]["engine_errors"], 0)

    def test_frames_are_consecutive_rounds(self):
        rounds = [self.unpack(f["status"])["round"] for f in self.data["frames"]]
        self.assertEqual(rounds, list(range(rounds[0], rounds[0] + len(rounds))))
        n = len(rounds)
        self.assertEqual(len(self.data["over_threshold"]), n)
        self.assertEqual(self.data["scenes"][0]["start"], 0)
        self.assertEqual(self.data["scenes"][-1]["end"], n)
        covered = sorted(i for c in self.data["captions"] for i in range(c["start"], c["end"]))
        self.assertEqual(covered, list(range(n)))                             # 字幕逐帧覆盖、互不重叠
        self.assertTrue(all(c["zh"] and c["en"] for c in self.data["captions"]))

    def test_story(self):
        s = self.scenes
        self.assertEqual(self.data["targets_total"], 36)
        d = s["degradation"]
        self.assertEqual(d["over_threshold_mode"], 16)                        # 16/36 个目标越过阈值
        self.assertEqual([(a["fault_type"], a["link_id"]) for a in d["alarms"]], [("delay", "L1")])    # 一条告警
        self.assertEqual([(a["fault_type"], a["link_id"]) for a in s["link_down"]["alarms"]], [("link_down", "L6")])
        self.assertGreater(s["link_down"]["routes_changed_before_repair"], 0)
        self.assertTrue(s["link_down"]["rehearsal"]["feasible"])
        self.assertEqual([(a["fault_type"], a["link_id"]) for a in s["route_drift"]["alarms"]], [("route_drift", "L4")])
        self.assertEqual({(a["fault_type"], a["link_id"]) for a in s["two_faults"]["alarms"]},
                         {("delay", "L5"), ("loss", "L7")})
        self.assertEqual([a["node"] for a in s["device_down"]["alarms"] if a["fault_type"] == "node_down"], ["acc3"])
        for sid in SCENES[1:]:
            self.assertTrue(s[sid]["expected_alarms_met"], sid)
            self.assertTrue(s[sid]["alarms_match_injection"], sid)
            self.assertIsNotNone(s[sid]["alarms_gone"], sid)                  # 修复后告警全部恢复

    def test_targets_over_threshold_are_those_crossing_l1(self):
        from edgefdl.topology import load
        t = load(os.path.join(ROOT, "configs", "demo_topology.yaml"))
        crossing = [x.id for x in t.targets() if x.kind != "nqa"
                    and "L1" in t.path_links(t.route_to_target(t.vantage, x)[0]) + t.path_links(t.route_to_target(t.vantage, x)[1])]
        self.assertEqual(sorted(crossing), self.scenes["degradation"]["over_threshold_targets"])

    def test_alarm_shown_while_fault_is_active(self):
        d = self.scenes["degradation"]
        st = self.unpack(self.data["frames"][d["first_alarm"]]["status"])
        self.assertEqual(st["active_alarm"]["link_id"], "L1")
        self.assertEqual(st["ground_truth"][0]["link"], "L1")
        self.assertGreaterEqual(self.data["over_threshold"][d["first_alarm"]], 16)  # 该轮的瞬时干扰可能再多一个

    def test_series_rebuilds(self):
        ser = self.data["series"]
        f = self.data["frames"][-1]
        out = self.rec.series_at(ser["points"], ser["links"], f["series_round"], "L1")
        self.assertTrue(out)
        for pts in out.values():
            self.assertLessEqual(len(pts), self.rec.SERIES_KEEP)
            self.assertEqual(pts[-1]["round"], f["series_round"])


class PackTest(unittest.TestCase):
    def test_round_trip(self):
        rec = tool("record")
        pool, index = [], {}
        a = dict(alarm="x" * 60, n=1.5, none=None, nested=[dict(k="y" * 60), dict(k="y" * 60)])
        p1, p2 = rec.pack(a, pool, index), rec.pack([a, a], pool, index)
        self.assertEqual(rec.unpack(p1, pool), a)
        self.assertEqual(rec.unpack(p2, pool), [a, a])
        self.assertLess(len(json.dumps(pool)), 2 * len(json.dumps(a)))


class DashboardContractTest(unittest.TestCase):
    """replay.js 依赖大屏页面的这些名字；改动大屏时这里先报错。"""

    def test_page_provides_what_the_replay_uses(self):
        self.assertTrue(os.path.isfile(DASHBOARD))
        html = read(DASHBOARD)
        self.assertIn("<script>", html)
        for name in ("function tick(", "function history(", "function series(", "function select(",
                     'id="inj-link"', 'id="truth"', 'id="svg"', 'class="node"', 'href="/api/export/'):
            self.assertIn(name, html.replace('{class:"node"}', 'class="node"'), name)

    def test_replay_answers_every_api_the_page_reads(self):
        js = read(os.path.join(DEMO, "replay.js"))
        used = set(re.findall(r'fetch\("(/api/[a-z]+)', read(DASHBOARD)))
        self.assertTrue(used)
        for path in used:
            self.assertIn(f'p.endsWith("{path}")', js)

    def test_site_uses_relative_paths_only(self):
        self.assertNotRegex(read(os.path.join(DEMO, "replay.js")), r'fetch\("/')
        self.assertNotRegex(read(os.path.join(DEMO, "index.html")), r'(src|href)="/')


class SiteTest(unittest.TestCase):
    def test_build(self):
        site = tool("build_site")
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "site")
            self.assertEqual(site.build(out), ["dashboard.html", "frames.json", "index.html", "replay.js"])
            self.assertTrue(filecmp.cmp(DASHBOARD, os.path.join(out, "dashboard.html"), shallow=False))

    def test_dashboard_not_duplicated_in_docs(self):
        self.assertFalse(os.path.exists(os.path.join(DEMO, "dashboard.html")))

    def test_workflow_pins_actions(self):
        wf = read(os.path.join(ROOT, ".github", "workflows", "pages.yml"))
        uses = re.findall(r"uses:\s*(\S+)(.*)", wf)
        self.assertTrue(uses)
        for action, comment in uses:
            self.assertRegex(action, r"@[0-9a-f]{40}$")
            self.assertRegex(comment, r"# v\d")
        self.assertIn("github.event.repository.private == false", wf)
        self.assertIn("refs/heads/main", wf)


if __name__ == "__main__":
    unittest.main()
