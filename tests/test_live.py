# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""live 后端单元测试：用假执行器验证解析、只读约束、缺测处理、上线前检查和状态推断，不碰真实网络。"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from edgefdl import topology  # noqa: E402
from edgefdl.faults.model import Fault  # noqa: E402
from edgefdl.measure import probes  # noqa: E402
from edgefdl.measure.model import MeasurePlan, PingStat, RoundResult  # noqa: E402
from edgefdl.vnet.base import make_backend  # noqa: E402
from edgefdl.vnet.live import (LiveBackend, LiveExec, ReadOnlyError, StateInference, format_preflight,  # noqa: E402
                               load_endpoints, preflight)

ROOT = os.path.join(os.path.dirname(__file__), "..")
TOPO = os.path.join(ROOT, "configs", "demo_topology.yaml")
INTAKE = os.path.join(ROOT, "configs", "intake_demo")
MODEL = os.path.join(ROOT, "results", "model.pt")
PING = ("PING {ip} ({ip}) 56(84) bytes of data.\n64 bytes from {ip}: icmp_seq=1 ttl=62 time=1.20 ms\n\n"
        "--- {ip} ping statistics ---\n5 packets transmitted, 5 received, 0% packet loss, time 804ms\n"
        "rtt min/avg/max/mdev = 1.100/1.250/1.400/0.100 ms\n")
PING_LOST = "PING {ip} ({ip}) 56(84) bytes of data.\n\n--- {ip} ping statistics ---\n" \
            "5 packets transmitted, 0 received, 100% packet loss, time 830ms\n"
TRACE = "traceroute to {ip} ({ip}), 12 hops max, 60 byte packets\n 1  {ip}  0.5 ms\n"


class FakeExec(LiveExec):
    """记录全部命令；ping / traceroute 给固定输出，silent 中的地址不应答。"""

    def __init__(self, vantage, endpoints=None, lsdb=(0, "{}", ""), silent=()):
        super().__init__(vantage, endpoints)
        self.calls, self.shells, self.lsdb, self.silent = [], [], lsdb, set(silent)

    def host(self, argv, check=False, timeout=None, quiet=False, input=None):
        self.calls.append(list(argv))
        ip = argv[-1]
        if argv[0] == "ping":
            return 0, (PING_LOST if ip in self.silent else PING).format(ip=ip)
        if argv[0] == "traceroute":
            return 0, TRACE.format(ip=ip)
        return 0, ""

    def popen(self, node, argv):
        self.wrap(node, argv)
        raise OSError("fake")

    def have(self, cmd):
        return True

    def shell(self, cmd, timeout):
        self.shells.append(cmd)
        return self.lsdb


def lsa(rid, links):
    return dict(advertisingRouter=rid, routerLinks={f"link{i}": x for i, x in enumerate(links)})


def lsdb_json(topo, kind="another Router (point-to-point)", drop=()):
    """demo 拓扑的 router-LSA：每条路由器互联链路两端各一条，drop 中的链路不出现。"""
    areas = {}
    for n, nd in topo.nodes.items():
        if not nd.transit:
            continue
        links = []
        for l in topo.links:
            if n in (l.a, l.b) and l.id not in drop:
                import ipaddress
                if topo.nodes[l.other(n)].transit:
                    links.append(dict(linkType=kind, routerInterfaceAddress=l.ip_of(n), tos0Metric=l.cost))
                else:
                    links.append(dict(linkType="Stub Network", tos0Metric=l.cost,
                                      networkAddress=str(ipaddress.ip_interface(l.a_ip).network.network_address)))
        areas.setdefault("0.0.0.0", []).append(lsa(nd.loopback.split("/")[0], links))
    return json.dumps(dict(routerLinkStates=dict(areas=areas)))


def make(lsdb_cmd=None, endpoints=None, silent=(), lsdb=(0, "{}", ""), **kw):
    t = topology.load(TOPO)
    ex = FakeExec(t.vantage, endpoints, lsdb=lsdb, silent=silent)
    return LiveBackend(t, MeasurePlan(interval_s=0.001), lsdb_cmd=lsdb_cmd, ex=ex, trace_workers=2, **kw), ex


class LiveMeasureTest(unittest.TestCase):
    def test_round_parses_ping_and_traceroute_and_leaves_the_rest_missing(self):
        b, ex = make(trace_per_round=0)
        rr = b.measure_round(0)
        self.assertEqual(len(rr.pings), len(b.topo.targets()) - 1)            # 盲区 NQA 目标不测
        self.assertNotIn("nqa.L3", rr.pings)
        p = rr.pings["L4.acc1"]
        self.assertEqual((p.sent, p.recv, p.rtt_avg, p.rtt_mdev), (5, 5, 1.25, 0.1))
        self.assertEqual(p.path, ("acc1",))
        self.assertEqual((rr.lsdb, rr.bw), ({}, {}))                          # 缺测不写入，不是"断开"或 0
        self.assertEqual({c[0] for c in ex.calls}, {"ping", "traceroute"})
        self.assertGreater(rr.probe_bytes, 0)

    def test_traceroute_budget_rotates_over_all_targets(self):
        b, ex = make(trace_per_round=5)
        seen = set()
        n = len(b.topo.targets()) - 1
        for i in range((n + 4) // 5):
            ex.calls.clear()
            rr = b.measure_round(i)
            traced = [c[-1] for c in ex.calls if c[0] == "traceroute"]
            self.assertEqual(len(traced), 5)                                  # 不重测，不超预算
            self.assertEqual(sum(1 for p in rr.pings.values() if p.path), 5)
            seen |= set(traced)
        self.assertEqual(len(seen), len({t.ip for t in b.topo.targets() if t.src is None}))

    def test_default_budget_is_one_traceroute_per_second(self):
        t = topology.load(TOPO)
        self.assertEqual(LiveBackend(t, MeasurePlan(interval_s=5.0)).trace_per_round, 5)
        self.assertEqual(LiveBackend(t, MeasurePlan(interval_s=2.0)).trace_per_round, 2)
        self.assertEqual(LiveBackend(t, MeasurePlan(), trace_per_round=0).trace_per_round, 0)

    def test_make_backend_and_vendor_interface_names(self):
        from edgefdl.intake.form import build_topology
        t = build_topology(os.path.join(INTAKE, "form.yaml"), configs=os.path.join(INTAKE, "configs"))
        self.assertTrue(any("/" in (l.a_if + l.b_if) for l in t.links))       # 设备配置里的接口名
        b = make_backend("live", t, MeasurePlan())
        self.assertEqual((b.name, b.read_only), ("live", True))
        with self.assertRaises(ValueError):                                   # netns 仍要求 Linux 接口名
            make_backend("netns", t, MeasurePlan(), dry_run=True)

    def test_capabilities_follow_what_the_operator_gave(self):
        b, _ = make()
        self.assertFalse(b.can_lsdb() or b.can_probe("L4") or b.can_bw("L4"))
        ep = {"agg1": "ssh a", "acc1": "ssh b", "core": "ssh c"}
        b, ex = make(lsdb_cmd="x", endpoints=ep)
        self.assertTrue(b.can_lsdb() and b.can_probe("L4") and b.can_bw("L1"))
        self.assertTrue(b.can_probe("L15"))                                   # 网管本机算一端
        self.assertFalse(b.can_probe("L5"))                                   # acc2 未列入
        self.assertEqual(sorted(b._bw_cand()), ["L1", "L15", "L4"])

    def test_learn_baseline_has_no_clear_and_no_deep_probe_without_endpoints(self):
        from edgefdl.faults.generator import learn_baseline
        b, _ = make()
        with mock.patch.object(LiveBackend, "deep_probe", side_effect=AssertionError("deep")):
            base = learn_baseline(b, rounds=2)
        self.assertEqual(float(base.rtt[0]), 1.25)
        self.assertNotEqual(float(base.rtt[-1]), float(base.rtt[-1]))         # 盲区 NQA 没测到：NaN
        self.assertEqual(list(base.deep_bw), [l.bw_mbps for l in b.topo.links])

    def test_skipped_targets_are_missing_not_unreachable(self):
        b, ex = make()
        b.skip_targets(["lo.acc1"])
        rr = b.measure_round(0)
        self.assertNotIn("lo.acc1", rr.pings)
        self.assertEqual(b.status()["skipped"], ["lo.acc1"])


class LiveReadOnlyTest(unittest.TestCase):
    def test_inject_and_clear_are_refused(self):
        b, ex = make()
        f = Fault("delay", "L1", 20.0)
        for fn in (lambda: b.inject(f), b.clear, lambda: b.clear("L1"), lambda: b._apply(f), lambda: b._revert(f),
                   lambda: b._run_cmds([]), b._configure_l3, b._host_routes):
            with self.assertRaises(ReadOnlyError):
                fn()
        self.assertEqual(b.faults(), [])
        self.assertEqual(ex.calls, [])                                        # 一条命令也没发出去

    def test_up_and_down_do_nothing(self):
        b, ex = make()
        b.up()
        b.down()
        self.assertEqual((ex.calls, ex.shells), ([], []))

    def test_only_probe_commands_pass(self):
        ex = LiveExec("ems", {"agg1": "ssh -o BatchMode=yes probe@10.0.0.1"})
        for argv in (["tc", "qdisc", "show"], ["ip", "link", "set", "eth1", "down"], ["sh", "-c", "reboot"],
                     ["python3", "-c", "print(1)"], ["vtysh", "-c", "conf t"], []):
            with self.assertRaises(ReadOnlyError):
                ex.wrap("ems", argv)
        with self.assertRaises(ReadOnlyError):
            ex.vtysh_argv("agg1", ["configure terminal"])
        with self.assertRaises(ReadOnlyError):
            ex.wrap("acc1", probes.ping_cmd("10.0.0.1"))                      # 未列入端点
        self.assertEqual(ex.wrap("ems", ["ping", "x"]), ["ping", "x"])
        w = ex.wrap("agg1", probes.train_send_cmd("10.0.0.2", 5300, 16, 1400))
        self.assertEqual(w[:3], ["ssh", "-o", "BatchMode=yes"])
        self.assertEqual(len(w), 5)                                           # 命令行作为一个参数交给前缀
        self.assertIn("python3 -c", w[-1])

    def test_http_refuses_injection_and_hides_the_panel(self):
        from edgefdl.core.api import make_handler
        eng = mock.Mock(backend=mock.Mock(read_only=True))
        h = make_handler(eng, topology.load(TOPO), allow_inject=True)
        self.assertIsNotNone(h)
        import http.client
        import threading
        from http.server import ThreadingHTTPServer
        srv = ThreadingHTTPServer(("127.0.0.1", 0), h)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            for path in ("/api/inject", "/api/clear"):
                c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
                c.request("POST", path, body=b'{"type":"delay","link":"L1","value":1}',
                          headers={"Content-Type": "application/json"})
                r = c.getresponse()
                self.assertEqual(r.status, 403)
                self.assertIn("只读", json.loads(r.read())["error"])
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            c.request("GET", "/api/topology")
            self.assertIs(json.loads(c.getresponse().read())["inject_enabled"], False)
        finally:
            srv.shutdown()
            srv.server_close()
        eng.backend.inject.assert_not_called()


class LiveLsdbTest(unittest.TestCase):
    def test_lsdb_command_is_parsed(self):
        t = topology.load(TOPO)
        b, ex = make(lsdb_cmd="show", lsdb=(0, lsdb_json(t, drop=("L4",)), ""))
        rr = b.measure_round(0)
        self.assertEqual(ex.shells, ["show"])
        self.assertTrue(rr.lsdb["L1"].adj_up)
        self.assertEqual(rr.lsdb["L1"].cost, 10.0)
        self.assertFalse(rr.lsdb["L4"].adj_up)

    def test_broadcast_network_links_count_as_adjacencies(self):
        t = topology.load(TOPO)
        b, _ = make(lsdb_cmd="show", lsdb=(0, lsdb_json(t, kind="a Transit Network"), ""))
        ls = b._read_lsdb()
        self.assertTrue(ls and all(e.adj_up for e in ls.values()))

    def test_failed_reads_are_missing_not_all_links_down(self):
        for lsdb in ((1, "", "Permission denied"), (124, "", "超时"), (0, "not json", ""), (0, "{}", ""),
                     (0, '{"routerLinkStates": {"areas": {"0.0.0.0": []}}}', ""),
                     (0, '{"areas": {"0.0.0.0": {"routerLinkStates": [{"lsId": "10.255.0.1"}]}}}', "")):
            b, _ = make(lsdb_cmd="show", lsdb=lsdb)
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(b._read_lsdb(), {}, lsdb)
                self.assertEqual(b._read_lsdb(), {}, lsdb)
            self.assertEqual(err.getvalue().count("--lsdb-cmd"), 1)           # 连续失败只报一次
            self.assertEqual(b.lsdb_fails, 2)
            self.assertTrue(b.lsdb_error)
            self.assertEqual(b.health()["lsdb_fails"], 2)


class LivePreflightTest(unittest.TestCase):
    def test_lists_addresses_that_do_not_answer(self):
        b, _ = make(silent=["10.255.0.4"])
        rep = preflight(b)
        self.assertFalse(rep["ok"])
        self.assertEqual([s["id"] for s in rep["silent"]], ["lo.acc1"])
        self.assertEqual(rep["answered"] + 1, rep["total"])
        txt = format_preflight(rep)
        self.assertIn("不应答：10.255.0.4", txt)
        self.assertIn("检查未通过", txt)
        self.assertEqual(rep["not_measured"], ["nqa.L3"])

    def test_passes_when_everything_answers_and_reports_a_bad_lsdb_command(self):
        t = topology.load(TOPO)
        b, _ = make(lsdb_cmd="show", lsdb=(0, lsdb_json(t), ""))
        rep = preflight(b)
        self.assertTrue(rep["ok"], rep["problems"])
        self.assertEqual(rep["lsdb"]["links"], 15)
        b, _ = make(lsdb_cmd="show", lsdb=(0, "{}", ""))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(preflight(b)["ok"])

    def test_lldp_link_addresses_are_planned_so_preflight_lists_them(self):
        from edgefdl.intake.form import build_topology
        with tempfile.TemporaryDirectory() as d:
            lldp = os.path.join(d, "lldp.csv")
            with open(lldp, "w", encoding="utf-8") as f:
                f.write("local_device,local_if,remote_device,remote_if\nagg1,GigabitEthernet9/0/1,core,GigabitEthernet9/0/2\n")
            with open(os.path.join(INTAKE, "form.yaml"), encoding="utf-8") as f:
                form = f.read()
            p = os.path.join(d, "form.yaml")
            with open(p, "w", encoding="utf-8") as f:
                f.write(form)
            t = build_topology(p, lldp=lldp)                                  # 无设备配置：地址全靠规划
        l = t.link_between("agg1", "core")
        ex = FakeExec(t.vantage, silent=[l.ip_of("agg1"), l.ip_of("core")])
        rep = preflight(LiveBackend(t, MeasurePlan(interval_s=0.001), ex=ex))
        self.assertFalse(rep["ok"])
        self.assertEqual({s["ip"] for s in rep["silent"]}, {l.ip_of("agg1"), l.ip_of("core")})

    def test_cli_needs_a_site_baseline(self):
        from edgefdl import cli
        with self.assertRaises(SystemExit) as cm:
            cli.main(["serve", "--model", "no-such-model.pt", "--backend", "live"])
        self.assertIn("--learn-rounds", str(cm.exception.code))

    def test_endpoints_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "e.yaml")
            with open(p, "w", encoding="utf-8") as f:
                f.write("endpoints:\n  agg1: ssh probe@10.255.0.2\n")
            self.assertEqual(load_endpoints(p), {"agg1": "ssh probe@10.255.0.2"})
            self.assertEqual(load_endpoints(None), {})
            for bad in ("agg1: ssh", "endpoints: [a]", "endpoints: {agg1: ''}", "endpoints: {a: [}"):
                with open(p, "w", encoding="utf-8") as f:
                    f.write(bad)
                with self.assertRaises(ValueError):
                    load_endpoints(p)
        t = topology.load(TOPO)
        with self.assertRaises(ValueError):
            LiveBackend(t, endpoints={"nosuch": "ssh x"})


def rr_of(topo, dead=(), paths=None):
    rr = RoundResult(ts=0.0, round_id=0, vantage=topo.vantage)
    for t in topo.targets():
        if t.kind != "nqa":
            k = 0 if t.id in dead else 5
            rr.pings[t.id] = PingStat(t.id, 5, k, 1.0, 1.0, 1.0, 0.1, (paths or {}).get(t.id, ()) if k else ())
    return rr


class StateInferenceTest(unittest.TestCase):
    def setUp(self):
        self.t = topology.load(TOPO)
        self.inf = StateInference(self.t)

    def test_link_down_needs_two_rounds_and_a_live_end(self):
        dead = ("L4.agg1", "L4.acc1")
        self.assertTrue(self.inf.update(rr_of(self.t, dead))["L4"].adj_up)
        self.assertFalse(self.inf.update(rr_of(self.t, dead))["L4"].adj_up)
        self.assertTrue(self.inf.update(rr_of(self.t))["L4"].adj_up)

    def test_one_end_answering_is_not_a_link_down(self):
        rr = rr_of(self.t, ("L4.acc1",))
        self.inf.update(rr)
        self.assertTrue(self.inf.update(rr)["L4"].adj_up)

    def test_region_cut_off_blames_the_edge_of_the_region_only(self):
        # 网管上联断：全部目标不应答，只有与网管相连的链路算中断
        dead = [t.id for t in self.t.targets()]
        self.inf.update(rr_of(self.t, dead))
        ls = self.inf.update(rr_of(self.t, dead))
        self.assertEqual([l for l, e in ls.items() if not e.adj_up], ["L15"])

    def test_device_down_marks_its_links_for_device_aggregation(self):
        dead = [t.id for t in self.t.targets() if t.node == "acc1" or t.link in ("L4", "L8", "L11")]
        self.inf.update(rr_of(self.t, dead))
        ls = self.inf.update(rr_of(self.t, dead))
        # 下挂主机的 L11 两端都不应答、也没有存活的一端：不下结论；设备宕机判据只看相邻路由设备的邻接
        self.assertEqual(sorted(l for l, e in ls.items() if not e.adj_up), ["L4", "L8"])

    def test_cost_change_is_explained_by_the_link_the_paths_left(self):
        ts = {t.id: t for t in self.t.targets() if t.kind != "nqa"}
        base = {i: tuple(self.t.route_to_target(self.t.vantage, t)[0][1:]) for i, t in ts.items()}
        self.inf.set_reference(base)
        cost = {"L4": 100}
        now = {i: tuple(self.t.route_to_target(self.t.vantage, t, cost=cost)[0][1:]) for i, t in ts.items()}
        self.assertNotEqual(base, now)
        self.assertEqual(self.inf.update(rr_of(self.t, paths=base))["L4"].cost, 10.0)
        self.assertEqual(self.inf.update(rr_of(self.t, paths=now))["L4"].cost, 10.0)     # 第一次出现，待确认
        ls = self.inf.update(rr_of(self.t, paths=now))
        self.assertGreater(ls["L4"].cost, 10.0)
        self.assertTrue(ls["L4"].adj_up)
        self.assertEqual(sorted(l for l, e in ls.items() if e.cost != self.t.link(l).cost), ["L4"])

    def test_equal_cost_flip_of_a_subnet_is_not_a_cost_change(self):
        # L3 网段从 core 经 agg1、agg2 等价：其两端地址的路径可以在两边之间换，这不是开销变化
        ts = {t.id: t for t in self.t.targets() if t.kind != "nqa"}
        base = {i: tuple(self.t.route_to_target(self.t.vantage, t)[0][1:]) for i, t in ts.items()}
        self.inf.set_reference(dict(base, **{"L3.agg1": ("core", "agg1"), "L3.agg2": ("core", "agg1", "agg2")}))
        flip = dict(base, **{"L3.agg1": ("core", "agg2", "agg1"), "L3.agg2": ("core", "agg2")})
        for _ in range(3):
            ls = self.inf.update(rr_of(self.t, paths=flip))
        self.assertEqual([l for l, e in ls.items() if e.cost != self.t.link(l).cost], [])

    def test_paths_from_an_outage_are_dropped_when_the_link_returns(self):
        ts = {t.id: t for t in self.t.targets() if t.kind != "nqa"}
        base = {i: tuple(self.t.route_to_target(self.t.vantage, t)[0][1:]) for i, t in ts.items()}
        self.inf.set_reference(base)
        down = {i: tuple(self.t.route_to_target(self.t.vantage, t, {"L1"})[0][1:]) for i, t in ts.items()}
        dead = ("L1.core", "L1.agg1")
        for _ in range(3):
            ls = self.inf.update(rr_of(self.t, dead, paths=down))
        self.assertFalse(ls["L1"].adj_up)
        ls = self.inf.update(rr_of(self.t))                                   # 恢复，还没重新测到路径
        self.assertEqual(self.inf.cur, {})
        ls = self.inf.update(rr_of(self.t))
        self.assertTrue(all(e.adj_up and e.cost == self.t.link(l).cost for l, e in ls.items()))

    def test_no_reference_no_cost_inference(self):
        ls = self.inf.update(rr_of(self.t, paths={"L4.acc1": ("core", "agg2", "acc1")}))
        self.assertEqual(ls["L4"].cost, 10.0)


@unittest.skipUnless(os.path.exists(MODEL), "无模型")
class LiveEngineTest(unittest.TestCase):
    """引擎按能力工作：不能精测就不调用精测，没有带宽估计就不下带宽结论。"""

    def engine(self, d, infer=False):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        from live_missing import MaskedSim

        from edgefdl.agent.features import FeatureEncoder
        from edgefdl.agent.infer import Policy
        from edgefdl.cli import load_bundle
        from edgefdl.core.orchestrator import Engine
        ag, topo, base, plan, _ = load_bundle(MODEL)
        b = MaskedSim(topo, plan, True, True, True, seed=3, noise_prob=0.0)
        b.up()
        if infer:
            b.infer = StateInference(topo)
            b.infer.set_reference({t.id: p for t, p in zip(topo.targets(), base.paths)})
        eng = Engine(topo, b, Policy(ag, FeatureEncoder(topo, base)), plan, d)
        return eng, b

    def run_fault(self, eng, b, fault, rounds=30):
        with mock.patch.object(type(b), "deep_probe", side_effect=AssertionError("不应精测")):
            for _ in range(8):
                eng.step()
            b.inject(fault)
            for _ in range(rounds):
                eng.step()
                if eng.alarm_seq:
                    break
        return eng.active

    def test_masked_actions(self):
        from edgefdl.agent.ddqn import K
        with tempfile.TemporaryDirectory() as d:
            eng, b = self.engine(d)
            self.assertEqual(len(eng.noprobe), 15)
            self.assertEqual(len(eng.policy.masked), 30)
            self.assertTrue(all(m < 15 * K for m in eng.policy.masked))
            eng.stop()

    def test_delay_without_lsdb_or_endpoints(self):
        with tempfile.TemporaryDirectory() as d:
            eng, b = self.engine(d)
            a = self.run_fault(eng, b, Fault("delay", "L4", 20.0))
            eng.stop()
        self.assertEqual((a["fault_type"], a["link_id"]), ("delay", "L4"))

    def test_device_down_is_one_alarm_with_state_inference(self):
        with tempfile.TemporaryDirectory() as d:
            eng, b = self.engine(d, infer=True)
            with mock.patch.object(type(b), "deep_probe", side_effect=AssertionError("不应精测")):
                for _ in range(8):
                    eng.step()
                for l in eng.topo.links:
                    if "acc3" in (l.a, l.b):
                        b.inject(Fault("link_down", l.id))
                for _ in range(30):
                    eng.step()
            al = eng.active_list()
            eng.stop()
        self.assertEqual([(a["fault_type"], a.get("node")) for a in al], [("node_down", "acc3")])

    def test_link_down_with_state_inference(self):
        with tempfile.TemporaryDirectory() as d:
            eng, b = self.engine(d, infer=True)
            a = self.run_fault(eng, b, Fault("link_down", "L6"))
            eng.stop()
        self.assertEqual((a["fault_type"], a["link_id"]), ("link_down", "L6"))


if __name__ == "__main__":
    unittest.main()
