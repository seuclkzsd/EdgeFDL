# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""同台对比（edgefdl/compare.py）的测试：各定位算法的手算用例、旁路不改变测量流、回放不看未来、评分口径、
基线方法在同一条流上的基本能力与局限（python -m unittest discover -s tests）。"""
import argparse
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from edgefdl import compare as C  # noqa: E402
from edgefdl import topology  # noqa: E402
from edgefdl.agent.features import FeatureEncoder  # noqa: E402
from edgefdl.faults.generator import learn_baseline  # noqa: E402
from edgefdl.faults.model import Fault  # noqa: E402
from edgefdl.measure.model import MeasurePlan  # noqa: E402
from edgefdl.vnet.sim import SimBackend  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
TOPO = os.path.join(ROOT, "configs", "demo_topology.yaml")
MODEL = os.path.join(ROOT, "results", "model.pt")

# 两条路径共享上游链路 0：路径 0 = {0}，路径 1 = {0, 1}，路径 2 = {0, 2}
INC = np.array([[1, 0, 0], [1, 1, 0], [1, 0, 1]], dtype=bool)
DEPTH = np.array([0.0, 1.0, 1.0])


class Algorithms(unittest.TestCase):
    def test_scfs_good_path_exonerates(self):
        bad = np.array([False, True, False])
        good = np.array([True, False, True])
        self.assertEqual(C.scfs(INC, bad, good, DEPTH), [1])

    def test_scfs_tie_goes_near_or_deep(self):
        bad = np.array([False, True, False])
        none = np.zeros(3, bool)
        self.assertEqual(C.scfs(INC, bad, none, DEPTH), [0])
        self.assertEqual(C.scfs(INC, bad, none, DEPTH, tie="deep"), [1])

    def test_scfs_smallest_cover(self):
        bad = np.array([False, True, True])
        good = np.array([True, False, False])
        self.assertEqual(sorted(C.scfs(INC, bad, good, DEPTH)), [1, 2])

    def test_score_hit_ratio_filters(self):
        bad = np.array([False, True, True])
        good = np.array([True, False, False])
        self.assertEqual(sorted(C.score(INC, bad, good, 1.0, DEPTH)), [1, 2])      # 链路 0 命中率 2/3
        self.assertEqual(C.score(INC, bad, good, 0.6, DEPTH), [0])                 # 门限放宽后覆盖最大者胜出

    def test_vote_ties_go_deep_and_removes_explained_flows(self):
        bad = np.array([False, True, False])
        self.assertEqual(C.vote007(INC, bad, 0.01, DEPTH), [1])
        self.assertEqual(C.vote007(INC, np.zeros(3, bool), 0.01, DEPTH), [])

    def test_netbouncer_finds_the_lossy_link(self):
        A = INC.astype(float)
        y = np.array([1.0, 0.7, 1.0])
        x = C.nb_loss(A, y, 0.1)
        self.assertLess(x[1], 0.8)
        self.assertGreater(x[0], 0.97)
        self.assertGreater(x[2], 0.97)

    def test_netbouncer_gate_never_skips_a_link_the_solver_would_flag(self):
        # 只穿越一次的链路 y≈x，15% 丢包时 y=0.85 < 0.9，y≥0.81；λ 小时 y=0.91 还会被网格取整到 0.9
        n = 100
        recv = np.full((n, 1), 5, np.int8)
        recv[:9, 0] = 0
        ev = SimpleNamespace(routes=[np.ones((1, 1))], Inc=[np.ones((1, 1), bool)], rid=np.zeros(n, int), L=1,
                             sent=np.full((n, 1), 5, np.int8), recv=recv, e=np.full((n, 1), np.nan, np.float32))
        p = dict(n=n, step=n, lam=0.01, bad=0.9, d_ms=5.0)
        self.assertLessEqual(C.nb_loss(np.ones((1, 1)), np.array([0.91]), 0.01)[0], 0.9)
        self.assertEqual(C.infer_nb(ev, 0, n, p)[n - 1][0], [0])
        ev.recv = ev.sent
        self.assertEqual(C.infer_nb(ev, 0, n, p), {})

    def test_netbouncer_delay_channel(self):
        A = INC.astype(float)
        d = C.nb_delay(A, np.array([0.0, 20.0, 0.0]), 0.1)
        self.assertGreater(d[1], 19)
        self.assertLess(d[0], 1)


class Scoring(unittest.TestCase):
    CASE = dict(truth={("delay", "L5")}, links={"L5"})

    def test_first_alarm_decides(self):
        al = [(10, "link", "L4", "delay"), (11, "link", "L5", "delay")]
        r = C.score_case(al, 8, self.CASE, 72, 5.0)
        self.assertFalse(r["first_link"])
        self.assertTrue(r["all_link"])
        self.assertEqual(r["t"], 15.0)
        self.assertEqual(r["t_right"], 20.0)
        self.assertEqual(r["wrong"], 1)
        self.assertFalse(r["exact"])

    def test_target_alarms_never_name_a_link(self):
        al = [(10, "target", "L5.acc2", None), (10, "target", "h2", None)]
        r = C.score_case(al, 8, self.CASE, 72, 5.0)
        self.assertTrue(r["det"])
        self.assertFalse(r["first_link"])
        self.assertEqual(r["n"], 2)

    def test_pre_fault_alarm_is_not_a_detection(self):
        r = C.score_case([(5, "link", "L5", "delay")], 8, self.CASE, 72, 5.0)
        self.assertFalse(r["det"])
        self.assertEqual(r["pre"], 1)

    def test_node_truth(self):
        case = dict(truth={("node_down", "agg1")}, links={"L1", "L3", "L4", "L5"})
        r = C.score_case([(9, "node", "agg1", "node_down")], 8, case, 72, 5.0)
        self.assertTrue(r["first_joint"] and r["all_joint"] and r["exact"])
        r = C.score_case([(9, "link", "L1", "link_down")], 8, case, 72, 5.0)
        self.assertTrue(r["first_link"])
        self.assertFalse(r["all_joint"])


class Tuning(unittest.TestCase):
    ROWS = [dict(params=dict(a=1), f1=0.9, fa_per_day=20.0), dict(params=dict(a=2), f1=0.8, fa_per_day=0.5),
            dict(params=dict(a=3), f1=0.7, fa_per_day=0.0)]

    def test_low_fa_takes_best_f1_under_the_limit(self):
        self.assertEqual(C.pick_low_fa(self.ROWS)["params"], dict(a=2))

    def test_low_fa_falls_back_to_fewest_alarms(self):
        self.assertEqual(C.pick_low_fa(self.ROWS[:1] + [dict(params=dict(a=4), f1=0.1, fa_per_day=5.0)])["params"],
                         dict(a=4))

    def test_f1_counts(self):
        rows = [dict(m=1, hit=1, wrong=1, det=True), dict(m=2, hit=1, wrong=0, det=True), dict(m=1, hit=0, wrong=0, det=False)]
        self.assertEqual(C.f1_counts(rows, 4.0, True), (2.0, 5.0, 2.0))
        self.assertEqual(C.f1_counts(rows, 4.0, False), (2.0, 4.0, 1.0))

    def test_grid_edges(self):
        grid = [dict(a=1, b="x", c=7), dict(a=2, b="y", c=7), dict(a=3, b=4, c=7)]
        self.assertEqual(C.grid_edges(dict(a=1, b="x", c=7), grid), dict(a="min"))
        self.assertEqual(C.grid_edges(dict(a=3, b=4, c=7), grid), dict(a="max"))         # 只有一个数值取值的键不算边缘
        self.assertEqual(C.grid_edges(dict(a=2, b=4, c=7), grid), {})

    def test_007_grid_reaches_past_the_old_edge(self):
        eps = {p["epoch"] for p in C.grids()["vote007"]}
        self.assertEqual(eps, {3, 6, 12, 24, 36, 48})
        self.assertIn((36, 18), {(p["epoch"], p["m"]) for p in C.grids()["vote007"]})

    def test_lam_points_change_only_lambda(self):
        p = dict(n=60, step=30, lam=0.1, bad=0.9, d_ms=5.0)
        tr = [dict(params=dict(p, lam=x), f1=0.9) for x in (0.01, 0.1, 1.0)] + [dict(params=dict(p, lam=0.01, n=12), f1=0.9)]
        got = C.lam_points({"netbouncer": {"+ems": dict(params=p)}}, {"netbouncer+ems": tr})
        self.assertEqual(sorted(got), ["netbouncer+ems:lam0.01", "netbouncer+ems:lam1.0"])
        self.assertEqual(got["netbouncer+ems:lam0.01"]["params"], dict(p, lam=0.01))
        self.assertEqual(C.lam_points({}, {}), {})
        labels = [c[0] for c in C.configs({}, None, got)]
        self.assertIn("netbouncer+ems:lam0.01", labels)

    def test_window_tradeoff_keeps_the_best_of_each_window(self):
        tr = {"netbouncer+ems": [dict(params=dict(n=12, a=1), f1=0.7, fa_per_day=3.0), dict(params=dict(n=12, a=2), f1=0.8, fa_per_day=9.0),
                                 dict(params=dict(n=60, a=1), f1=0.9, fa_per_day=0.0)]}
        got = C.window_tradeoff(tr)["netbouncer+ems"]
        self.assertEqual((got["12"]["val_f1"], got["12"]["val_fa_per_day"]), (0.8, 9.0))
        self.assertEqual(got["60"]["val_f1"], 0.9)

    def test_paired_gap_counts_both_ways(self):
        def rows(m, ok):
            return [dict(method=m, suite="single", cond="c", seed=1, link=f"L{i}", type="loss", first_joint=x)
                    for i, x in enumerate(ok)]
        g = C.paired_gap(rows("a", [1, 1, 1, 0]) + rows("b", [1, 0, 0, 1]), "a", "b", n=200)
        self.assertEqual((g["only_a"], g["only_b"], g["gap_points"], g["faults"]), (2, 1, 25.0, 4))
        self.assertLessEqual(g["ci95"][0], 25.0)
        self.assertGreaterEqual(g["ci95"][1], 25.0)

    def test_overall_sums_every_condition(self):
        rows = [dict(method="m", suite="single", cond=c, seed=1, link="L1", type="loss", first_joint=j, t=t, t_right=t,
                     det=True, first_link=j, all_link=j, all_joint=j, exact=j, n=1, wrong=0, pre=0, value=0.15)
                for c, j, t in (("x", True, 10.0), ("y", False, 30.0))]
        res = [dict(cond=c, rows=[r for r in rows if r["cond"] == c], fa=dict(m=dict(false_alarms=f, rounds=17280)))
               for c, f in (("x", 2), ("y", 3))]
        o = C.summarize(res)["overall"]["m"]
        self.assertEqual((o["faults"], o["correct"], o["false_alarms"], o["simulated_days"]), (2, 1, 5, 2.0))
        self.assertEqual(o["time_to_alarm_mean_s"], 20.0)

    def test_every_config_is_labelled_once(self):
        best = {"scfs": {"": dict(params=C.DET), "+ems": dict(params=C.DET)}}
        low = {"scfs": dict(params=C.DET), "threshold": dict(params=C.DET)}
        labels = [c[0] for c in C.configs(best, low)]
        self.assertEqual(len(labels), len(set(labels)))
        self.assertIn("scfs:lowfa", labels)
        self.assertNotIn("threshold+ems:default", labels)


class Stream(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.topo = topology.load(TOPO)
        cls.plan = MeasurePlan()
        b = SimBackend(cls.topo, cls.plan, seed=3)
        b.up()
        cls.base = learn_baseline(b)

    def record(self, faults, seed=5, pre=6, after=20, noise=0.0):
        enc = FeatureEncoder(self.topo, self.base)
        rec = C.Recorder(enc)
        b = SimBackend(self.topo, self.plan, seed=seed, noise_prob=noise)
        b.up()
        for r in range(pre):
            rec.add(b.measure_round(r))
        for f in faults:
            b.inject(f)
        for r in range(pre, pre + after):
            rec.add(b.measure_round(r))
        return C.Evidence(enc, rec.arrays()), pre

    def test_tap_leaves_the_stream_unchanged(self):
        def run(tap):
            b = SimBackend(self.topo, self.plan, seed=9, noise_prob=0.15)
            b.up()
            if tap:
                C.Recorder(FeatureEncoder(self.topo, self.base)).tap(b)
            return [b.measure_round(r) for r in range(30)]
        for x, y in zip(run(False), run(True)):
            self.assertEqual({k: (v.recv, v.rtt_avg) for k, v in x.pings.items() if v.recv},
                             {k: (v.recv, v.rtt_avg) for k, v in y.pings.items() if v.recv})
            self.assertEqual(x.bw, y.bw)

    def test_replay_does_not_look_ahead(self):
        ev, pre = self.record([Fault("delay", "L5", 20.0)], noise=0.3, after=30)
        for m in ("grouped", "scfs", "score", "vote007", "netbouncer"):
            p = C.grids()[m][0] if m != "netbouncer" else dict(C.DEFAULTS[m], n=6, step=3)
            for v in ("", "+ems"):
                full = C.run_method(m, ev, 0, ev.R, p)[v]
                part = C.run_method(m, ev, 0, 20, p)[v]
                self.assertEqual([a for a in full if a[0] < 20], part, m)

    def test_delay_is_located_and_typed_by_tomography(self):
        ev, pre = self.record([Fault("delay", "L5", 20.0)])
        for m in ("scfs", "score", "grouped"):
            al = C.run_method(m, ev, 0, ev.R, C.DEFAULTS[m])[""]
            self.assertTrue(al, m)
            self.assertEqual(al[0][2:], ("L5", "delay"), m)

    def test_jitter_type_rule(self):
        ev, pre = self.record([Fault("jitter", "L5", 15.0)])
        al = C.run_method("grouped", ev, 0, ev.R, C.DEFAULTS["grouped"])[""]
        self.assertEqual(al[0][2:], ("L5", "jitter"))

    def test_bandwidth_needs_the_ems_rules(self):
        ev, pre = self.record([Fault("bandwidth", "L5", 0.2)], after=40)
        res = C.run_method("scfs", ev, 0, ev.R, C.DEFAULTS["scfs"])
        self.assertEqual(res[""], [])
        self.assertEqual({(a[2], a[3]) for a in res["+ems"]}, {("L5", "bandwidth")})

    def test_route_drift_needs_the_ems_rules(self):
        ev, pre = self.record([Fault("route_drift", "L2", 10.0)])
        res = C.run_method("scfs", ev, 0, ev.R, C.DEFAULTS["scfs"])
        self.assertFalse([a for a in res[""] if a[2] == "L2"])
        self.assertEqual(res["+ems"][0][2:], ("L2", "route_drift"))

    def test_netbouncer_finds_loss_on_a_once_crossed_link(self):
        ev, pre = self.record([Fault("loss", "L8", 0.15)], after=100)
        self.assertEqual(ev.enc.A0[:, ev.lids.index("L8")].sum(), 1)         # L8 只有一个目标穿越一次，y≈x
        p = dict(C.DEFAULTS["netbouncer"], lam=0.1, bad=0.9, d_ms=5.0, step=30)
        inf = C.infer_nb(ev, 0, ev.R, p)
        self.assertIn("L8", {ev.lids[i] for ls, _, _ in inf.values() for i in ls})
        self.assertEqual(C.run_method("netbouncer", ev, 0, ev.R, p)["+ems"][0][2:], ("L8", "loss"))

    def test_window_loss_separates_link_loss_from_background(self):
        ev, pre = self.record([Fault("loss", "L5", 0.15)], after=30)
        far = ev.enc.tidx["L5.acc2"]
        near = ev.enc.tidx["L5.agg1"]
        lw = ev.win_loss(0, ev.R, 12)
        self.assertGreater(lw[-1, far], 0.1)
        self.assertEqual(lw[-1, near], 0.0)
        p = dict(C.DET, lost=C.NO_ROUND_LOSS, lossw=0.1, k=6, n=12)
        al = C.run_method("scfs", ev, 0, ev.R, p)[""]
        self.assertIn(("L5", "loss"), [a[2:] for a in al])       # 窗口证据逐步累积，首条告警不一定已收敛到真值

    def test_link_down_is_not_a_bandwidth_event(self):
        ev, pre = self.record([Fault("link_down", "L5", 1.0)], after=30)
        self.assertFalse(ev.bw_low.any())
        self.assertTrue(ev.cp_down[-1, ev.lids.index("L5")])

    def test_device_rule(self):
        faults = [Fault("link_down", l) for l in ("L4", "L8", "L11")]         # acc1 的全部链路
        ev, pre = self.record(faults)
        self.assertEqual(C.node_dead(ev, pre + 1, 0, True), ["acc1"])
        self.assertEqual(C.node_dead(ev, pre + 1, 0), [])                     # 只看路径要连续 3 轮
        self.assertEqual(C.node_dead(ev, pre + 2, 0), ["acc1"])
        res = C.run_method("scfs", ev, 0, ev.R, C.DEFAULTS["scfs"])
        self.assertEqual(res["+ems"], [(pre + 1, "node", "acc1", "node_down")])

    def test_device_rule_needs_the_adjacency_with_lsdb(self):
        ev, pre = self.record([])
        ev.dead[:] = True
        ev.dead[:, [i for i, t in enumerate(ev.enc.targets) if t.node != "acc1"]] = False
        self.assertEqual(C.node_dead(ev, 10, 0), ["acc1"])                    # 突发丢包让地址全不通
        self.assertEqual(C.node_dead(ev, 10, 0, True), [])                    # 邻接还在，不是设备宕机

    def test_threshold_names_no_link(self):
        ev, pre = self.record([Fault("delay", "L1", 20.0)])
        al = C.run_method("threshold", ev, 0, ev.R, dict(rtt=5, lost="auto", k=3, n=3))[""]
        self.assertGreaterEqual(len(al), 10)
        self.assertEqual({a[1] for a in al}, {"target"})

    def test_zabbix_default_keeps_its_five_minute_window(self):
        ev, pre = self.record([Fault("delay", "L1", 100.0)], after=70)
        al = C.run_method("threshold", ev, 0, ev.R, C.DEFAULTS["threshold"])[""]
        self.assertTrue(al)
        self.assertGreater(min(a[0] for a in al) - pre, 30)       # RTT +200 ms 要等 5 分钟均值超过 150 ms

    def test_default_and_grid_cover_the_same_methods(self):
        self.assertEqual(set(C.DEFAULTS), set(C.grids()))
        self.assertEqual({c["name"] for c in C.conditions(100)} >= {"standard"}, True)


@unittest.skipUnless(os.path.isfile(MODEL), "需要 results/model.pt")
class Engine(unittest.TestCase):
    def test_engine_and_baselines_share_one_stream(self):
        from edgefdl.cli import load_bundle
        from edgefdl.robust import _engine, scenario_fault
        with tempfile.TemporaryDirectory() as d:
            _, topo, _, _, _ = load_bundle(MODEL)
            eng, topo = _engine(MODEL, C.C("standard"), 5, d)
            rec = C.Recorder(FeatureEncoder(eng.topo, eng.enc.base.__class__(**eng.enc.base.to_dict())))
            rec.tap(eng.backend)
            cs = dict(faults=[scenario_fault(topo, "L5", "delay")], truth={("delay", "L5")}, links={"L5"})
            segs = C.drive_engine(eng, rec, [cs], "single", 20, 4)
            eng.stop()
        self.assertEqual(rec.n, segs[0]["e"])
        self.assertEqual(segs[0]["e"] - segs[0]["inj"], 24)
        r = C.score_case(segs[0]["alarms"], segs[0]["inj"], cs, 20, 5.0)
        self.assertTrue(r["first_joint"] and r["exact"])


    def test_light_only_uses_no_deep_probe(self):
        from edgefdl.cli import load_bundle
        from edgefdl.robust import _engine, scenario_fault
        with tempfile.TemporaryDirectory() as d:
            _, topo, _, _, _ = load_bundle(MODEL)
            eng, topo = _engine(MODEL, C.C("standard"), 5, d)
            rec = C.Recorder(FeatureEncoder(eng.topo, eng.enc.base.__class__(**eng.enc.base.to_dict())))
            rec.tap(eng.backend)
            C.light_only(eng)
            cs = dict(faults=[scenario_fault(topo, "L5", "loss")], truth={("loss", "L5")}, links={"L5"})
            C.drive_engine(eng, rec, [cs], "single", 30, 4)
            eng.stop()
        self.assertEqual(eng.deep_probes, 0)
        self.assertEqual(eng.extra_probes, 0)

    def test_no_deep_control_runs_in_every_condition_with_its_false_alarm_run(self):
        real = C.single_cases
        c = C.conditions(100)[3]
        self.assertEqual(c["name"], "transient_15s")
        with tempfile.TemporaryDirectory() as d, mock.patch.object(C, "single_cases", lambda t: real(t)[:2]):
            r = C._test_job((MODEL, c, 5, [], 12, 10, 2, 2, 1, d))
        self.assertEqual({x["method"] for x in r["rows"]}, {"edgefdl", "edgefdl_no_deep"})
        self.assertEqual(r["fa"]["edgefdl_no_deep"]["rounds"], 12)
        self.assertFalse(any(x["probes"] for x in r["rows"] if x["method"] == "edgefdl_no_deep"))


class Published(unittest.TestCase):
    """发布的 results/compare.json 与本页对照行、网格口径一致。"""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "results", "compare.json"), encoding="utf-8") as f:
            cls.r = json.load(f)

    def test_no_deep_row_covers_all_conditions_and_false_alarm_runs(self):
        conds = {c["name"] for c in self.r["config"]["conditions"]}
        self.assertEqual(len(conds), 6)
        self.assertEqual(conds, {c for c, a in self.r["single"]["edgefdl_no_deep"].items() if a})
        self.assertEqual(conds, set(self.r["false_alarms"]["edgefdl_no_deep"]))
        self.assertEqual(self.r["edgefdl_deep_probes_per_fault"]["no_deep_row_probes"], 0)

    def test_grid_edges_are_recorded(self):
        for k, t in self.r["tuning"].items():
            self.assertIn("grid_edges", t, k)

    def test_window_tradeoff_is_published(self):
        self.assertEqual(set(self.r["window_tradeoff"]["netbouncer+ems"]), {"12", "24", "60"})
        self.assertEqual(set(self.r["window_tradeoff"]["vote007+ems"]), {"3", "6", "12", "24", "36", "48"})

    def test_lambda_points_were_run(self):
        self.assertTrue(self.r["sensitivity"])
        for k in self.r["sensitivity"]:
            self.assertEqual(self.r["single"][k]["standard"]["n"], 420)

    @unittest.skipUnless(importlib.util.find_spec("matplotlib"), "需要 matplotlib")
    def test_every_table_in_the_page_is_the_generated_one(self):
        spec = importlib.util.spec_from_file_location("compare_figure", os.path.join(ROOT, "tools", "figures", "compare.py"))
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        buf = io.StringIO()
        with redirect_stdout(buf):
            tool.tables(self.r)
        with open(os.path.join(ROOT, "docs", "comparison.md"), encoding="utf-8") as f:
            page = f.read()
        blocks = [b for b in buf.getvalue().split("\n\n") if b.strip()]
        self.assertGreaterEqual(len(blocks), 10)
        for b in blocks:
            self.assertIn(b.strip("\n"), page)


class Cli(unittest.TestCase):
    def test_compare_is_registered(self):
        import edgefdl.cli as cli
        self.assertTrue(callable(cli.cmd_compare))
        self.assertIsInstance(argparse.Namespace(), argparse.Namespace)


if __name__ == "__main__":
    unittest.main()
