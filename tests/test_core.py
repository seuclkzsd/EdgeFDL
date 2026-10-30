# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""核心模块单元测试（python -m unittest discover tests）。"""
import csv
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from edgefdl import topology  # noqa: E402
from edgefdl.agent.features import FeatureEncoder, LIGHT_FEATURES, min_lost  # noqa: E402
from edgefdl.core.denoise import Detector  # noqa: E402
from edgefdl.faults.generator import fault_space, learn_baseline  # noqa: E402
from edgefdl.faults.model import Fault  # noqa: E402
from edgefdl.vnet.sim import SimBackend, Transient  # noqa: E402

TOPO = os.path.join(os.path.dirname(__file__), "..", "configs", "demo_topology.yaml")


class TopologyTest(unittest.TestCase):
    def setUp(self):
        self.t = topology.load(TOPO)

    def test_autoplan(self):
        self.assertEqual(len(self.t.links), 15)
        ips = [l.a_ip for l in self.t.links] + [l.b_ip for l in self.t.links]
        self.assertEqual(len(ips), len(set(ips)))
        self.assertTrue(all(n.loopback for n in self.t.nodes.values() if n.transit))

    def test_link_down_withdraws_subnet(self):
        tg = next(x for x in self.t.targets() if x.id == "L6.acc3")
        self.assertTrue(self.t.route_to_target("ems", tg)[0])
        self.assertEqual(self.t.route_to_target("ems", tg, down={"L6"}), ([], []))

    def test_reroute_on_failure(self):
        tg = next(x for x in self.t.targets() if x.id == "lo.acc3")
        p0 = self.t.route_to_target("ems", tg)[0]
        p1 = self.t.route_to_target("ems", tg, down={"L6"})[0]
        self.assertIn("agg2", p0)
        self.assertNotEqual(p0, p1)
        self.assertEqual(p1[-1], "acc3")

    def test_coverage_nqa(self):
        self.assertEqual(self.t.uncovered_links(self.t._base_targets()), ["L3"])
        nqa = [x for x in self.t.targets() if x.kind == "nqa"]
        self.assertEqual([(x.link, x.src) for x in nqa], [("L3", "agg1")])


class SimTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t = topology.load(TOPO)
        cls.b = SimBackend(cls.t, seed=3)
        cls.b.up()
        cls.base = learn_baseline(cls.b, rounds=20, deep_reps=2)
        cls.enc = FeatureEncoder(cls.t, cls.base)

    def tearDown(self):
        self.b.clear()

    def test_delay_only_affects_crossing_targets(self):
        self.b.inject(Fault("delay", "L8", 20))
        rr = self.b.measure_round(0, transient=[])
        self.assertGreater(rr.pings["L8.acc2"].rtt_avg - self.base.rtt[self.enc.tidx["L8.acc2"]], 15)
        self.assertLess(abs(rr.pings["L6.acc3"].rtt_avg - self.base.rtt[self.enc.tidx["L6.acc3"]]), 2)

    def test_link_down_features(self):
        self.b.inject(Fault("link_down", "L6"))
        win = [self.b.measure_round(i, transient=[]) for i in range(6)]
        F, G = self.enc.light(win)
        li = self.enc.lidx["L6"]
        self.assertEqual(F[li, LIGHT_FEATURES.index("end_unreach_both")], 1.0)
        self.assertEqual(F[li, LIGHT_FEATURES.index("adj_down")], 1.0)
        self.assertEqual(F.shape, (15, len(LIGHT_FEATURES)))

    def test_deep_probe(self):
        self.b.inject(Fault("bandwidth", "L5", 0.1))
        dp = self.b.deep_probe("L5")
        self.assertLess(dp.bw_mbps, 15)
        self.assertEqual(self.b.deep_probe("L4").recv, self.b.plan.deep_ping_count)

    def test_detector_ignores_single_transient(self):
        det = Detector(self.enc)
        for i in range(6):
            tr = [Transient("L1", "spike", 100)] if i == 3 else []
            st = det.update(self.b.measure_round(i, transient=tr))
            self.assertFalse(st["trigger"])

    def test_detector_triggers_on_persistent_fault(self):
        det = Detector(self.enc)
        for i in range(6):
            det.update(self.b.measure_round(i, transient=[]))
        self.b.inject(Fault("loss", "L4", 0.4))
        fired = any(det.update(self.b.measure_round(10 + i, transient=[]))["trigger"] for i in range(10))
        self.assertTrue(fired)

    def test_loss_threshold_vs_baseline(self):
        # 5 个探测：基线无丢包时丢 1 个即异常；基线 2% 须丢 2 个，5% 须丢 3 个
        self.assertEqual(min_lost(5, np.array([0.0, 0.02, 0.05])).tolist(), [1, 2, 3])

    def test_icmp_ratelimit_learned_in_baseline(self):
        b = SimBackend(self.t, seed=5, icmp_drop=0.05)
        b.up()
        base = learn_baseline(b, rounds=60, deep_reps=1)
        router = b.router_tgt
        self.assertTrue(0.03 < base.loss[router].mean() <= 0.05)
        # 基线已含限速丢包时，单轮丢 1 个应答不再算异常；基线干净时则算
        ti = int(np.where(router)[0][0])
        sent = np.full((1, len(router)), 5.0)
        recv = sent.copy()
        recv[0, ti] = 4
        rtt = np.tile(base.rtt, (1, 1))
        mdev = np.tile(base.mdev, (1, 1))
        self.assertFalse(FeatureEncoder(self.t, base).anomaly_matrix(sent, recv, rtt, mdev)[0, ti])
        self.assertTrue(self.enc.anomaly_matrix(sent, recv, np.tile(self.base.rtt, (1, 1)),
                                                np.tile(self.base.mdev, (1, 1)))[0, ti])

    def test_baseline_ignores_bursts(self):
        rr = [self.b.measure_round(i, transient=[Transient("L4", "burst", 0.9)] if i == 3 else [])
              for i in range(20)]
        from edgefdl.agent.features import Baseline
        deeps = [{l: self.b.deep_probe(l) for l in self.t.link_ids()}]
        self.assertEqual(Baseline.fit(self.t, rr, deeps).loss.max(), 0.0)

    def test_missing_measurement_is_not_unreachable(self):
        li = self.enc.lidx["L6"]
        far = self.enc.targets[self.enc.far[li]].id
        for drop in (range(5, 6), range(6)):                     # 末轮缺测；整窗缺测（采集超时）
            win = [self.b.measure_round(i, transient=[]) for i in range(6)]
            for i in drop:
                del win[i].pings[far]
            F, _ = self.enc.light(win)
            self.assertEqual(F[li, LIGHT_FEATURES.index("end_unreach_mean")], 0.0)
            self.assertEqual(F[li, LIGHT_FEATURES.index("end_unreach_both")], 0.0)
            self.assertAlmostEqual(float(F[li, LIGHT_FEATURES.index("diff_loss")]), 0.0, places=6)
            self.assertEqual(F[li, LIGHT_FEATURES.index("paths_lost")], 0.0)

    def test_multi_round_transient(self):
        b = SimBackend(self.t, seed=9, hold_p=(0.0, 0.0, 1.0))
        first = []
        while not first:
            first = b.random_transient(0.5)
        self.assertEqual(b.random_transient(0.0), first)        # 同一干扰持续 3 轮
        self.assertEqual(b.random_transient(0.0), first)
        self.assertEqual(b.random_transient(0.0), [])

    def test_fault_space(self):
        fs = fault_space(self.t)
        drift_links = {f.link for f in fs if f.type == "route_drift"}
        self.assertNotIn("L11", drift_links)          # 主机链路不做开销漂移
        self.assertEqual(sum(f.type == "link_down" for f in fs), 15)


class StoreTest(unittest.TestCase):
    def test_measure_csv_schema(self):
        from edgefdl.measure.store import CsvStore, MEASURE_COLUMNS
        t = topology.load(TOPO)
        b = SimBackend(t, seed=1)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "m.csv")
            s = CsvStore(p, t)
            s.write_round(b.measure_round(0))
            s.close()
            with open(p, encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(list(rows[0].keys()), [c for c, _ in MEASURE_COLUMNS])
            ping = [r for r in rows if r["kind"] == "ping"]
            self.assertEqual(len(ping), len(t.targets()))
            self.assertTrue(all(r["target_ip"] for r in ping))
            self.assertTrue(os.path.exists(os.path.join(d, "m.schema.json")))


class AgentTest(unittest.TestCase):
    def test_numpy_matches_torch(self):
        import torch
        from edgefdl.agent.ddqn import Agent, action_mask
        from edgefdl.agent.infer import NumpyQNet
        ag = Agent(19, 6, 8, 15, drift_links=[0, 1, 2], device="cpu")
        B, L = 3, 15
        x = [torch.randn(B, L, 19), torch.randn(B, L, 6), (torch.rand(B, L) > 0.5).float(), torch.randn(B, 8),
             torch.rand(B)]
        mask = action_mask(ag.base_mask, x[2].bool(), torch.zeros(B, dtype=torch.long), 4)
        with torch.no_grad():
            qt = ag.net(*x, mask).numpy()
        qn = NumpyQNet.from_torch(ag.net)(*[v.numpy() for v in x], mask.numpy())
        self.assertTrue(np.allclose(qt, qn, atol=1e-4))

    def test_advice(self):
        from edgefdl.agent.knowledge import advise
        t = topology.load(TOPO)
        a = advise(t, "route_drift", "L6", cur_cost=100)
        self.assertTrue(a["rehearsal"]["feasible"])
        self.assertTrue(any("ospf cost 10" in c for s in a["steps"] for c in s["cmds"]))
        a = advise(t, "loss", "L11")                      # 主机接入链路无备份路径
        self.assertFalse(a["rehearsal"]["feasible"])


class EngineTest(unittest.TestCase):
    def test_node_down_aggregation(self):
        from edgefdl.agent.ddqn import Agent
        from edgefdl.agent.infer import Policy
        from edgefdl.core.orchestrator import Engine
        t = topology.load(TOPO)
        b = SimBackend(t, seed=2)
        b.up()
        enc = FeatureEncoder(t, learn_baseline(b, rounds=10, deep_reps=1))
        ag = Agent(len(LIGHT_FEATURES), 6, 8, len(t.links), drift_links=[0], device="cpu")
        with tempfile.TemporaryDirectory() as d:
            eng = Engine(t, b, Policy(ag, enc), b.plan, d)
            for l in ("L4", "L8", "L11"):                      # acc1 的全部链路
                b.inject(Fault("link_down", l))
            eng.window.append(b.measure_round(0, transient=[]))
            r = eng._node_down(dict(type="link_down", link="L8", evidence=[]))
            self.assertEqual((r["type"], r["node"], sorted(r["links"])), ("node_down", "acc1", ["L11", "L4", "L8"]))
            b.clear("L11")
            b.clear("L4")
            eng.window.append(b.measure_round(1, transient=[]))
            self.assertEqual(eng._node_down(dict(type="link_down", link="L8", evidence=[]))["type"], "link_down")
            eng.stop()

    def test_node_advice(self):
        from edgefdl.agent.knowledge import advise_node
        a = advise_node(topology.load(TOPO), "acc1")
        self.assertFalse(a["rehearsal"]["feasible"])               # h1 单归属于 acc1
        self.assertTrue(a["rehearsal"]["detail"].endswith("仍中断的目标: L11.h1"))
        self.assertTrue(advise_node(topology.load(TOPO), "agg1")["rehearsal"]["feasible"])
        self.assertTrue(a["steps"][0]["cmds"])


class IntakeTest(unittest.TestCase):
    def test_parse_configs(self):
        from edgefdl.intake.form import build_topology
        root = os.path.join(os.path.dirname(__file__), "..", "configs", "intake_demo")
        t = build_topology(os.path.join(root, "form.yaml"), configs=os.path.join(root, "configs"))
        pairs = {frozenset((l.a, l.b)) for l in t.links}
        self.assertIn(frozenset(("agg1", "agg2")), pairs)
        l = t.link_between("agg1", "agg2")
        self.assertEqual(l.cost, 15)
        self.assertEqual(t.link_between("core", "ems").a_ip, "10.20.9.1/30")



class ResidualTest(unittest.TestCase):
    """多故障残差基线：扣除已确认告警的效应后，同一路径上的另一处故障仍可见。"""

    @classmethod
    def setUpClass(cls):
        cls.t = topology.load(TOPO)
        cls.b = SimBackend(cls.t, seed=5)
        cls.b.up()
        cls.base = learn_baseline(cls.b, rounds=20, deep_reps=2)
        cls.enc = FeatureEncoder(cls.t, cls.base)
        cls.d = LIGHT_FEATURES.index("diff_drtt")

    def tearDown(self):
        self.b.clear()

    def _win(self, faults, n=6):
        self.b.clear()
        for f in faults:
            self.b.inject(f)
        return [self.b.measure_round(i, transient=[]) for i in range(n)]

    def test_empty_ref_matches_plain(self):
        w = self._win([Fault("delay", "L7", 20)])
        ref = self.enc.residual_ref(w, [])
        self.assertTrue(np.allclose(self.enc.light(w)[0], self.enc.light(w, ref)[0], atol=1e-6))

    def test_downstream_fault_survives_rebase(self):
        w = self._win([Fault("delay", "L7", 20), Fault("delay", "L14", 20)])
        F = self.enc.light(w, self.enc.residual_ref(w, [("delay", "L7")]))[0]
        li = self.enc.lidx
        self.assertLess(abs(F[li["L7"], self.d]), 0.5)
        self.assertGreater(F[li["L14"], self.d], 2.0)

    def test_return_path_subtracted(self):
        # L10.acc2 正向经 acc3、回程经 L5：只按 traceroute 路径扣除会在 L10 上留下半个 L5 时延
        w = self._win([Fault("delay", "L5", 20)])
        F = self.enc.light(w, self.enc.residual_ref(w, [("delay", "L5")]))[0]
        self.assertLess(np.abs(F[:, self.d]).max(), 0.7)

    def test_reroute_explained(self):
        from edgefdl.agent.features import link_evidence
        w = self._win([Fault("link_down", "L4"), Fault("delay", "L12", 20)])
        ref = self.enc.residual_ref(w, [("link_down", "L4")])
        F = self.enc.light(w, ref)[0]
        li = self.enc.lidx
        self.assertTrue(link_evidence(F[li["L12"]], "delay"))
        self.assertFalse(link_evidence(F[li["L4"]], "link_down"))
        self.assertEqual(self.enc.residual_activity(self._win([Fault("link_down", "L4")]), ref), [])

    def test_unreachable_attributed_only_to_alarmed_cut(self):
        # L2、L6 同时中断而只告警 L2：L6 两端的不可达不能算作 L2 的已解释效应
        w = self._win([Fault("link_down", "L2"), Fault("link_down", "L6")])
        ref = self.enc.residual_ref(w, [("link_down", "L2")])
        self.assertFalse(ref.unreach[self.enc.tidx["L6.acc3"]] and ref.unreach[self.enc.tidx["L13.h3"]])
        w = self._win([Fault("link_down", "L6")])
        ref = self.enc.residual_ref(w, [("link_down", "L6")])
        self.assertTrue(ref.unreach[self.enc.tidx["L6.acc3"]])

    def test_round_evidence(self):
        A = self.enc.traversal({})
        rr = self._win([Fault("delay", "L5", 20)], n=1)[0]
        self.assertTrue(self.enc.round_evidence(rr, "delay", "L5", A))
        self.assertFalse(self.enc.round_evidence(rr, "delay", "L7", A))
        self.assertIsNone(self.enc.round_evidence(rr, "bandwidth", "L5", A) if "L5" not in rr.bw else None)
        lost, lam, llr = self.enc.round_evidence(self._win([], n=1)[0], "loss", "L5", A)
        self.assertEqual((lost, lam), (0, 0.0))
        self.assertLess(llr, 0)                                  # 全收：倾向"已恢复"
        # 网管上联中断时全网不可达，但 L4 的 LSDB 邻接正常：不算 L4 中断的证据
        w = self._win([Fault("link_down", "L15")], n=1)
        A15 = self.enc.traversal(w[0].lsdb)
        self.assertFalse(self.enc.round_evidence(w[0], "link_down", "L4", A15))
        self.assertTrue(self.enc.round_evidence(self._win([Fault("link_down", "L4")], n=1)[0], "link_down", "L4", A))


MODEL = next((p for p in (os.path.join(os.path.dirname(__file__), "..", *r, "model.pt")
                          for r in (("results",), ("runs", "v6")))  # 仓库发布的模型 / 本地训练目录
              if os.path.exists(p)), "")


@unittest.skipUnless(os.path.exists(MODEL), "无模型")
class MultiAlarmEngineTest(unittest.TestCase):
    def _engine(self, d, **kw):
        import argparse
        from edgefdl.cli import build_engine
        ns = argparse.Namespace(seed=1, noise=0.0, engine="torch", sinks=None, confirm=2, **kw)
        return build_engine(MODEL, "sim", d, ns)

    def test_two_faults_two_alarms_partial_clear(self):
        with tempfile.TemporaryDirectory() as d:
            eng, _, b = self._engine(d)
            for _ in range(6):
                eng.step()
            b.inject(Fault("bandwidth", "L7", 0.2))
            b.inject(Fault("jitter", "L5", 15))
            for _ in range(40):
                eng.step()
            snap = eng.snapshot()
            got = {(a["fault_type"], a["link_id"]) for a in snap["active_alarms"]}
            self.assertEqual(got, {("bandwidth", "L7"), ("jitter", "L5")})
            self.assertEqual(snap["active_alarm"]["alarm_id"], snap["active_alarms"][0]["alarm_id"])
            self.assertEqual(snap["active_alarm"]["fault_type"], "bandwidth")      # 最严重的在前
            snap["active_alarms"][0]["confidence"] = -1                            # 快照是副本
            self.assertNotEqual(eng.active["confidence"], -1)
            keep = [a["alarm_id"] for a in eng.alarms if a["link_id"] == "L5"]
            b.clear("L7")
            for _ in range(60):
                eng.step()
            self.assertEqual([a["alarm_id"] for a in eng.alarms], keep)
            from edgefdl.core.api import prometheus
            m = prometheus(eng)
            self.assertIn("edgefdl_alarms_active 1", m)
            self.assertIn('link="L5"', m)
            b.clear()
            for _ in range(30):
                eng.step()
            self.assertEqual(eng.alarms, [])
            eng.stop()

    def test_single_alarm_switch(self):
        with tempfile.TemporaryDirectory() as d:
            eng, _, b = self._engine(d, single_alarm=True)
            self.assertFalse(eng.multi)
            for _ in range(6):
                eng.step()
            b.inject(Fault("bandwidth", "L7", 0.2))
            b.inject(Fault("jitter", "L5", 15))
            for _ in range(40):
                eng.step()
                self.assertLessEqual(len(eng.alarms), 1)
            self.assertEqual(len(eng.snapshot()["active_alarms"]), 1)
            eng.stop()

    def test_snapshot_under_concurrency(self):
        import threading
        with tempfile.TemporaryDirectory() as d:
            eng, _, b = self._engine(d)
            b.inject(Fault("link_down", "L6"))
            b.inject(Fault("delay", "L4", 20))
            errs, stop = [], threading.Event()

            def reader():
                import json
                while not stop.is_set():
                    try:
                        json.dumps(eng.snapshot(), default=str)
                        json.dumps(eng.alarm_history(), default=str)
                    except Exception as e:                 # noqa: BLE001
                        errs.append(e)
            th = threading.Thread(target=reader)
            th.start()
            for _ in range(40):
                eng.step()
            stop.set()
            th.join()
            self.assertEqual(errs, [])
            self.assertEqual(eng.errors, 0)
            eng.stop()

    @staticmethod
    def _keys(eng):
        return {("node_down", a["node"]) if a["fault_type"] == "node_down" else (a["fault_type"], a["link_id"])
                for a in eng.alarms}

    def test_node_down_second_fault_and_downgrade(self):
        """设备宕机期间另一处故障照常追加告警；设备恢复运行而仍有链路中断时降为该链路中断告警，另一条保持。"""
        with tempfile.TemporaryDirectory() as d:
            eng, _, b = self._engine(d)
            for _ in range(6):
                eng.step()
            for l in ("L7", "L9", "L14"):
                b.inject(Fault("link_down", l))
            b.inject(Fault("delay", "L1", 20))
            for _ in range(60):
                eng.step()
            self.assertEqual(self._keys(eng), {("node_down", "acc4"), ("delay", "L1")})
            keep = [a["alarm_id"] for a in eng.alarms if a["link_id"] == "L1"]
            b.clear("L7")
            b.clear("L14")                                  # acc4 恢复运行，只剩 L9 中断
            for _ in range(40):
                eng.step()
            self.assertEqual(self._keys(eng), {("link_down", "L9"), ("delay", "L1")})
            self.assertEqual([a["alarm_id"] for a in eng.alarms if a["link_id"] == "L1"], keep)
            b.clear()
            for _ in range(30):
                eng.step()
            self.assertEqual(eng.alarms, [])
            eng.stop()

    def _flap_run(self, d, carry):
        eng, _, b = self._engine(d)
        if not carry:                                       # 旧行为：新基线冻结前不扣除其余告警效应
            eng._trk_ref = lambda: eng.ref
        for _ in range(6):
            eng.step()
        b.inject(Fault("delay", "L6", 20))
        b.inject(Fault("delay", "L10", 20))
        seq = eng.alarm_seq
        for _ in range(60):
            eng.step()
        keys = sorted(self._keys(eng))
        n = eng.alarm_seq - seq
        eng.stop()
        return keys, n

    def test_secondary_kept_until_rebase(self):
        """L10 远端经过 L6 一次、近端往返两次：追加的 L10 时延告警在新残差基线冻结前不被误判为证据消失。"""
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self._flap_run(d, True), ([("delay", "L10"), ("delay", "L6")], 2))
        with tempfile.TemporaryDirectory() as d:
            self.assertGreater(self._flap_run(d, False)[1], 2)

    def _churn_run(self, d, gate):
        """L1 弱丢包（5%）告警后，不精测的刷新研判轮流指向 L5/L12 丢包（模拟偏差）：返回是否重学了丢包基线。"""
        eng, _, b = self._engine(d)
        eng.rot_gate = gate
        for _ in range(6):
            eng.step()
        b.inject(Fault("loss", "L1", 0.05))
        orig, n = eng.policy.diagnose, [0]

        def fake(window, probe_fn=None, ref=None, gate=False, exclude=()):
            r = orig(window, probe_fn=probe_fn, ref=ref, gate=gate, exclude=exclude)
            if eng.alarms and ref is None and probe_fn is None:
                n[0] += 1
                r = dict(r, type="loss", type_cn="丢包", link=("L5", "L12")[(n[0] // 2) % 2], evident=True)
            return r
        eng.policy.diagnose = fake
        for _ in range(150):
            eng.step()
        relearned = bool(eng.enc.base.loss.any())
        alarmed = ("loss", "L1") in self._keys(eng)
        eng.stop()
        return relearned, alarmed, n[0]

    def test_weak_loss_not_rotated_into_baseline(self):
        with tempfile.TemporaryDirectory() as d:
            relearned, alarmed, n = self._churn_run(d, True)
            self.assertGreater(n, 4)
            self.assertFalse(relearned)
            self.assertTrue(alarmed)
        with tempfile.TemporaryDirectory() as d:             # 不校验告警链路自身证据时会误判为背景丢包变化
            self.assertTrue(self._churn_run(d, False)[0])

    def test_relearn_skips_alarm_affected_targets(self):
        """多告警重学背景丢包：告警期间受其影响的目标不计入，样本不足时取同类目标的中位。"""
        with tempfile.TemporaryDirectory() as d:
            eng, _, b = self._engine(d)
            T = eng.enc.T
            router = np.array([eng.topo.nodes[t.node].transit for t in eng.enc.targets])
            aff = eng.enc.U[eng.enc.lidx["L1"]].astype(bool)
            for r in range(300):
                S = np.full(T, 5)
                R = S.copy()
                R[router & ~aff & (r % 5 == 0)] -= 1          # 设备目标背景 4%
                R[aff] -= 1                                   # 告警链路下游目标的故障丢包 20%
                eng.loss_hist.append((S, R))
                eng.loss_aff.append(aff)
            eng._relearn_loss()
            np.testing.assert_allclose(eng.enc.base.loss[router & ~aff], 0.04)
            np.testing.assert_allclose(eng.enc.base.loss[router & aff], 0.04)
            np.testing.assert_allclose(eng.enc.base.loss[~router & ~aff], 0.0)
            eng.stop()


if __name__ == "__main__":
    unittest.main()
