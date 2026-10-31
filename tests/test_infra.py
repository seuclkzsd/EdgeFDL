# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""基础设施单元测试：拓扑校验、虚拟网络 dry run、CSV 日志、样本生成与训练的可复现性、模型包加载、需求采集。"""
import contextlib
import copy
import csv
import io
import os
import shutil
import sys
import tempfile
import unittest

import numpy as np
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from edgefdl import topology  # noqa: E402
from edgefdl.vnet.sim import SimBackend  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
TOPO = os.path.join(ROOT, "configs", "demo_topology.yaml")
INTAKE = os.path.join(ROOT, "configs", "intake_demo")
MODEL = os.path.join(ROOT, "results", "model.pt")


def demo_dict():
    with open(TOPO, encoding="utf-8") as f:
        return yaml.safe_load(f)


def small(**node_kw):
    """ems - r1 - r2 - r3 的小拓扑（链路未填 id / 接口 / IP）。"""
    nodes = [dict(name="ems", role="ems")] + [dict(name=n, role="router", **node_kw.get(n, {}))
                                              for n in ("r1", "r2", "r3")]
    links = [dict(a="ems", b="r1"), dict(a="r1", b="r2"), dict(a="r2", b="r3")]
    return dict(name="t", vantage="ems", nodes=nodes, links=links)


class TopologyValidationTest(unittest.TestCase):
    def test_demo_and_intake_valid(self):
        t = topology.load(TOPO)
        self.assertEqual(len(t.nodes), 12)
        for n in t.nodes:                                                   # 每台设备接口名不重复
            ifs = [l.a_if if l.a == n else l.b_if for l in t.links if n in (l.a, l.b)]
            self.assertEqual(len(ifs), len(set(ifs)), n)
        from edgefdl.intake.form import build_topology
        ti = build_topology(os.path.join(INTAKE, "form.yaml"), configs=os.path.join(INTAKE, "configs"))
        self.assertEqual(len(ti.links), 4)
        self.assertIn("GigabitEthernet0/0/9", {l.a_if for l in ti.links})     # 厂商接口名保留
        topology.from_dict(t.to_dict())                                        # 完整规划可再次载入

    def test_unsafe_identifiers_rejected(self):
        bad_link = "L1\n!\nrouter bgp 65000\n neighbor 192.0.2.1 remote-as 65001\n!\ninterface eth1"
        cases = [(lambda d: d["links"][0].update(id=bad_link), "链路 id"),
                 (lambda d: d["nodes"][1].update(name="/tmp/pwned"), "设备名"),
                 (lambda d: d["nodes"][1].update(name="../../escape"), "设备名"),
                 (lambda d: d["nodes"][1].update(name="core\nhostname x"), "设备名"),
                 (lambda d: d["links"][0].update(a_if="eth1 x"), "接口名"),
                 (lambda d: d["links"][0].update(a_if="eth1\n!"), "接口名"),
                 (lambda d: d["links"][0].update(cost="10\nrouter bgp 1"), "OSPF 开销"),
                 (lambda d: d["links"][0].update(cost=0), "OSPF 开销"),
                 (lambda d: d["nodes"][1].update(loopback="10.255.0.9/32\n!"), "loopback"),
                 (lambda d: d["links"][0].update(bw="fast"), "带宽"),
                 (lambda d: d.update(vantage="nms"), "网管设备")]
        for mutate, what in cases:
            d = demo_dict()
            mutate(d)
            with self.assertRaisesRegex(ValueError, what):
                topology.from_dict(d)
        d = demo_dict()
        d["links"][0].update(bw="100", cost=20.0)                           # 数字字符串、整数值浮点可接受
        l = topology.from_dict(d).links[0]
        self.assertEqual((l.bw_mbps, l.cost), (100.0, 20))

    def test_duplicates_rejected(self):
        d = demo_dict()
        d["links"][1]["id"] = "L1"
        with self.assertRaisesRegex(ValueError, "链路 id 重复"):
            topology.from_dict(d)
        d = demo_dict()
        d["nodes"].append(dict(name="agg1", role="host"))
        with self.assertRaisesRegex(ValueError, "设备名重复"):
            topology.from_dict(d)
        d = small()
        d["links"][1].update(a_if="eth5")
        d["links"].append(dict(a="r1", b="r3", a_if="eth5"))
        with self.assertRaisesRegex(ValueError, "接口名重复"):
            topology.from_dict(d)
        d = small(r1=dict(loopback="10.255.0.7/32"), r2=dict(loopback="10.255.0.7/32"))
        with self.assertRaisesRegex(ValueError, "地址重复"):
            topology.from_dict(d)

    def test_autoplan_skips_explicit_values(self):
        t = topology.from_dict(small(r1=dict(loopback="10.255.0.2/32")))
        los = [t.nodes[n].loopback for n in ("r1", "r2", "r3")]
        self.assertEqual(len(set(los)), 3, los)                             # router-id 不重复
        d = small()
        d["links"][1].update(a_if="eth2")                                   # r1 上显式占用 eth2
        d["links"].append(dict(a="r1", b="r3"))
        t = topology.from_dict(d)
        r1 = [l.a_if if l.a == "r1" else l.b_if for l in t.links if "r1" in (l.a, l.b)]
        self.assertEqual(len(set(r1)), len(r1), r1)
        d = small()
        d["links"][2]["id"] = "L1"                                          # 自动 id 不与显式 id 冲突
        self.assertEqual(topology.from_dict(d).link_ids(), ["L2", "L3", "L1"])

    def test_frr_conf_guard(self):
        from edgefdl.vnet import frr
        t = topology.load(TOPO)
        t.links[0].id = "L1\nrouter bgp 1"                                  # 绕过 from_dict 直接改
        with self.assertRaises(ValueError):
            frr.router_conf(t, "core")

    def test_linux_ifnames_for_real_stack(self):
        from edgefdl.intake.form import build_topology
        from edgefdl.vnet.netns import NetnsBackend
        ti = build_topology(os.path.join(INTAKE, "form.yaml"), configs=os.path.join(INTAKE, "configs"))
        with self.assertRaisesRegex(ValueError, "Linux 接口名"):
            NetnsBackend(ti, dry_run=True)
        topology.check_linux_ifnames(topology.load(TOPO))

    def test_save_creates_directory(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a", "b", "topo.yaml")
            topology.load(TOPO).save(p)
            self.assertEqual(len(topology.load(p).links), 15)


class VnetDryRunTest(unittest.TestCase):
    def run_cli(self, *argv):
        from edgefdl import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(list(argv))
        return out.getvalue()

    def test_netns_dry_run_prints_commands(self):
        out = self.run_cli("vnet", "up", "--backend", "netns", "--dry-run", "--topo", TOPO)
        self.assertIn("dry run", out.splitlines()[0])
        self.assertIn("ip netns add efl-core", out)
        self.assertIn("vtysh -N efl-core -b", out)
        out = self.run_cli("vnet", "down", "--backend", "netns", "--dry-run", "--topo", TOPO)
        self.assertIn("ip netns del efl-core", out)

    def test_lxd_dry_run_prints_commands_and_cleans_up(self):
        tmp = tempfile.mkdtemp()
        old = tempfile.tempdir
        tempfile.tempdir = tmp
        try:
            out = self.run_cli("vnet", "up", "--backend", "lxd", "--dry-run", "--topo", TOPO)
            self.assertIn("lxc network create efl-L1", out)
            self.assertIn("lxc file push", out)
            self.assertEqual(os.listdir(tmp), [])                              # 暂存目录已删除
        finally:
            tempfile.tempdir = old
            shutil.rmtree(tmp)

    def test_lxd_stage_dir_and_names(self):
        from edgefdl.vnet.lxd import LxdBackend
        with tempfile.TemporaryDirectory() as d:
            b = LxdBackend(topology.load(TOPO), dry_run=True, stage_dir=d)
            b.up()
            self.assertTrue(os.path.isfile(os.path.join(d, "core", "frr.conf")))
            self.assertEqual(sorted(os.listdir(d)), sorted(b.routers()))
        # 绕过 from_dict 直接构造的拓扑：设备名不能成为暂存目录外的路径（实例名校验先拒绝）
        d = topology.load(TOPO).to_dict()
        ren = lambda x: "../../x" if x == "core" else x                      # noqa: E731
        nodes = {}
        for nd in d["nodes"]:
            nd["name"] = ren(nd["name"])
            nodes[nd["name"]] = topology.Node(**nd)
        links = [topology.Link(**dict(ld, a=ren(ld["a"]), b=ren(ld["b"]))) for ld in d["links"]]
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "实例名"):
                LxdBackend(topology.Topology("t", nodes, links, "ems"), dry_run=True, stage_dir=tmp)
            self.assertEqual(os.listdir(tmp), [])

    @unittest.skipIf(shutil.which("lxc"), "本机装有 lxc")
    def test_lxd_without_lxc(self):
        from edgefdl.vnet.lxd import LxdBackend
        with self.assertRaisesRegex(RuntimeError, "lxc"):
            LxdBackend(topology.load(TOPO), dry_run=False)
        self.assertTrue(LxdBackend(topology.load(TOPO)).ex.dry_run)


class CsvLogTest(unittest.TestCase):
    def test_formula_injection_neutralised(self):
        from edgefdl.measure.store import AlarmStore
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "alarm_log.csv")
            s = AlarmStore(p)
            s.write(dict(alarm_id="A1", event="raise", link_id="L4", site='=HYPERLINK("http://x/?"&A1,"o")',
                         endpoints="+cmd|' /C calc'!A0", repair="@SUM(1+1)", fault_type_cn="-2+3",
                         confidence=0.9, infer_ms=-1.5, round_id=12, model="-0.25"))
            s.close()
            with open(p, encoding="utf-8") as f:
                r = next(csv.DictReader(f))
        self.assertEqual(r["site"], '\'=HYPERLINK("http://x/?"&A1,"o")')
        self.assertTrue(r["endpoints"].startswith("'+") and r["repair"].startswith("'@"))
        self.assertEqual(r["fault_type_cn"], "'-2+3")
        self.assertEqual((r["confidence"], r["infer_ms"], r["round_id"], r["model"], r["link_id"]),
                         ("0.9", "-1.5", "12", "-0.25", "L4"))           # 数值与纯数字文本不变

    def test_rotation_and_retention(self):
        from edgefdl.measure.store import MEASURE_COLUMNS, _RollingCsv
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "m.csv")
            log = _RollingCsv(p, MEASURE_COLUMNS, max_bytes=2000, keep=3)
            for i in range(400):
                log.write([dict(round_id=i, kind="ping", target_id=f"t{i}", rtt_avg_ms=1.0)])
            log.close()
            arch = sorted(f for f in os.listdir(d) if f.startswith("m.csv."))
            self.assertEqual(len(arch), 3)                                    # 只保留最近 3 份归档
            self.assertTrue(all(f[len("m.csv."):].isdigit() for f in arch))
            ids = []
            for f in arch + ["m.csv"]:
                with open(os.path.join(d, f), encoding="utf-8") as fh:
                    rows = list(csv.DictReader(fh))
                self.assertTrue(rows and list(rows[0]) == [c for c, _ in MEASURE_COLUMNS])   # 每份都带表头
                ids += [int(r["round_id"]) for r in rows]
            self.assertEqual(ids, list(range(ids[0], 400)))                   # 保留的是最新、连续的记录
            self.assertGreater(ids[0], 0)
            self.assertLessEqual(os.path.getsize(os.path.join(d, arch[-1])), 2000 + 200)


class ReproducibilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from edgefdl.faults.generator import learn_baseline
        cls.t = topology.load(TOPO)
        b = SimBackend(cls.t, seed=3)
        b.up()
        cls.base = learn_baseline(b, rounds=12, deep_reps=1)

    def test_gen_same_dataset_for_any_worker_count(self):
        from edgefdl.faults.generator import generate_parallel
        with tempfile.TemporaryDirectory() as d:
            kw = dict(seed=5, chunk=25, rand=False)
            a = generate_parallel(self.t, self.base, 60, os.path.join(d, "a.npz"), workers=2, **kw)
            b = generate_parallel(self.t, self.base, 60, os.path.join(d, "b.npz"), workers=1, **kw)
            c = generate_parallel(self.t, self.base, 60, None, workers=1, raw_csv=os.path.join(d, "raw.csv"), **kw)
            for k in ("light", "glob", "deep", "y_type", "y_link", "y_value", "detected", "det_rounds"):
                self.assertTrue(np.array_equal(a[k], b[k]) and np.array_equal(a[k], c[k]), k)
            with open(os.path.join(d, "raw.csv"), encoding="utf-8") as f:
                sids = {r["sample_id"] for r in csv.DictReader(f)}
            self.assertEqual(len(sids), 60)

    def test_training_reproducible_with_seed(self):
        import torch
        from edgefdl.agent.ddqn import Agent, AgentConfig, Dataset
        rng = np.random.default_rng(0)
        N, L = 96, 4
        ds = Dataset(dict(light=rng.normal(size=(N, L, 5)).astype(np.float32),
                          glob=rng.normal(size=(N, 3)).astype(np.float32),
                          deep=rng.normal(size=(N, L, 2)).astype(np.float32),
                          y_type=rng.integers(0, 7, N), y_link=rng.integers(0, L, N),
                          y_value=np.zeros(N, np.float32), noisy=np.zeros(N, bool), detected=np.ones(N, bool),
                          det_rounds=np.zeros(N, int), link_ids=np.array([f"L{i}" for i in range(L)]),
                          drift_links=np.array([0, 1])))
        threads = torch.get_num_threads()
        torch.set_num_threads(1)
        try:
            def run(seed):
                cfg = AgentConfig(iters=40, batch=16, n_env=16, replay=4096, seed=seed, hidden=16)
                ag = Agent(5, 2, 3, L, [0, 1], cfg)
                w0 = copy.deepcopy(ag.net.state_dict())
                ag.train(ds, log_every=0)
                return w0, ag.net.state_dict()
            state = torch.random.get_rng_state()
            (i1, w1), (i2, w2), (i3, w3) = run(0), run(0), run(1)
            self.assertTrue(torch.equal(state, torch.random.get_rng_state()))      # 不扰动全局随机状态
        finally:
            torch.set_num_threads(threads)
        self.assertTrue(all(torch.equal(i1[k], i2[k]) and torch.equal(w1[k], w2[k]) for k in w1))
        self.assertFalse(all(torch.equal(i1[k], i3[k]) for k in i1))
        self.assertFalse(all(torch.equal(w1[k], w3[k]) for k in w1))


class BundleLoadTest(unittest.TestCase):
    @unittest.skipUnless(os.path.exists(MODEL), "无模型")
    def test_released_bundle_loads(self):
        from edgefdl.agent.ddqn import Agent
        ag = Agent.load(MODEL)
        self.assertIn("topology", ag.meta)

    def test_untrusted_objects_rejected(self):
        import torch
        from edgefdl.agent.ddqn import Agent
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "evil.pt")
            torch.save({"dims": [1, 1, 1, 1], "obj": Exploit()}, p)
            with self.assertRaisesRegex(ValueError, "拒绝加载"):
                Agent.load(p)
        self.assertFalse(Exploit.ran)


class Exploit:
    ran = False

    def __reduce__(self):
        return (_mark_ran, ())


def _mark_ran():
    Exploit.ran = True


class IntakeInfraTest(unittest.TestCase):
    def test_pdf_and_unreachable_llm_skipped(self):
        from edgefdl import cli
        from edgefdl.intake.extract import llm_extract
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(d, "configs")
            shutil.copytree(os.path.join(INTAKE, "configs"), cfg)
            for fn, data in (("drawing.pdf", b"%PDF-1.4\n"), ("topo.png", b"\x89PNG\r\n")):
                with open(os.path.join(cfg, fn), "wb") as f:
                    f.write(data)
            out = os.path.join(d, "new", "dir", "topo.yaml")
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                cli.main(["intake", "--form", os.path.join(INTAKE, "form.yaml"), "--configs", cfg,
                          "--llm", "http://127.0.0.1:9/v1", "-o", out])
            self.assertEqual(len(topology.load(out).links), 4)                 # 输出目录自动创建
            self.assertIn("drawing.pdf", err.getvalue())
            self.assertIn("pdftoppm", err.getvalue())
            self.assertIn("topo.png", err.getvalue())
            with self.assertRaisesRegex(ValueError, "PNG"):
                llm_extract(os.path.join(cfg, "drawing.pdf"), "http://127.0.0.1:9/v1")


if __name__ == "__main__":
    unittest.main()
