# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""live 后端单元测试：用假执行器验证解析、只读约束、缺测处理、上线前检查和状态推断，不碰真实网络。"""
import contextlib
import io
import json
import math
import os
import shlex
import signal
import socket
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

import numpy

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from edgefdl import topology  # noqa: E402
from edgefdl.faults.model import Fault  # noqa: E402
from edgefdl.measure import probes  # noqa: E402
from edgefdl.measure.model import DeepProbe, MeasurePlan, PingStat, RoundResult  # noqa: E402
from edgefdl.vnet.base import make_backend  # noqa: E402
from edgefdl.vnet.live import (LiveBackend, LiveExec, ReadOnlyError, StateInference, bundle_plan,  # noqa: E402
                               format_preflight, limited, load_endpoints, preflight)

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
        if argv[0] == "sh":                                                   # 经端点前缀 sh -c：命令行是最后一个参数
            argv = shlex.split(argv[-1])
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
        null_metric = lsdb_json(topology.load(TOPO)).replace('"tos0Metric": 10', '"tos0Metric": null')
        self.assertIn("null", null_metric)
        for lsdb in ((1, "", "Permission denied"), (124, "", "超时"), (0, "not json", ""), (0, "{}", ""),
                     (0, '{"routerLinkStates": {"areas": {"0.0.0.0": []}}}', ""),
                     (0, '{"areas": {"0.0.0.0": {"routerLinkStates": [{"lsId": "10.255.0.1"}]}}}', ""),
                     (0, '{"routerLinkStates": {"areas": [1]}}', ""),            # 合法 JSON、结构不对：同样按缺测
                     (0, '{"routerLinkStates": {"areas": {"0.0.0.0": [1]}}}', ""),
                     (0, '{"routerLinkStates": {"areas": {"0.0.0.0": 1}}}', ""), (0, null_metric, "")):
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


class LivePreflightCommandTest(unittest.TestCase):
    def _run(self, ok):
        from edgefdl import cli
        args = cli.argparse.Namespace(model=None, topo=TOPO, count=1, json=None)
        with mock.patch.object(cli, "_backend"), mock.patch.object(cli, "_plan"), \
                mock.patch("edgefdl.vnet.live.preflight", return_value={"ok": ok}), \
                mock.patch("edgefdl.vnet.live.format_preflight", return_value=""), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                cli.cmd_preflight(args)
        return cm.exception.code

    def test_exit_code_follows_the_result(self):
        self.assertEqual(self._run(True), 0)
        self.assertEqual(self._run(False), 1)


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


class FailingPingExec(FakeExec):
    """ping 按 fail(argv) 返回失败：(返回码, 输出)，或抛出的异常。"""

    def __init__(self, vantage, endpoints=None, fail=lambda argv: None):
        super().__init__(vantage, endpoints)
        self.fail = fail

    def run(self, node, argv, **kw):
        self.calls.append(list(argv))
        r = self.fail(argv) if argv[0] == "ping" else None
        if isinstance(r, Exception):
            raise r
        return r if r is not None else self.host(self.wrap(node, argv), **kw)


def failing(fail, endpoints=None):
    t = topology.load(TOPO)
    ex = FailingPingExec(t.vantage, endpoints, fail)
    return LiveBackend(t, MeasurePlan(interval_s=0.001), ex=ex, trace_workers=2), ex


ALL_ENDS = {"core": "sh -c", "agg1": "sh -c", "acc1": "sh -c"}


class LiveFailedProbeTest(unittest.TestCase):
    """探测没跑成是缺测，不是"没有应答"：后者是链路中断的证据。"""

    def test_ping_outside_rc_0_1_is_missing(self):
        for r in ((2, "ping: cannot flood; minimal interval allowed for user is 200ms"), (124, ""), (127, ""), (255, ""),
                  (1, "ssh: something printed by the prefix"), FileNotFoundError("nosuch")):
            b, _ = failing(lambda argv: r)
            st = b._ping("ems", "10.0.0.1", "t", 5, 0.2)
            self.assertEqual((st.sent, st.recv), (0, 0), r)
            self.assertNotEqual(st.loss, st.loss)                             # 丢包率未知（NaN），不是 100%

    def test_ping_that_ran_and_got_no_answer_stays_no_answer(self):
        b, _ = failing(lambda argv: (1, PING_LOST.format(ip=argv[-1])))
        st = b._ping("ems", "10.0.0.1", "t", 5, 0.2)
        self.assertEqual((st.sent, st.recv), (5, 0))

    def test_state_inference_does_not_count_a_failed_ping_as_down(self):
        b, _ = failing(lambda argv: (255, ""))
        b.infer = StateInference(b.topo)
        for i in range(3):
            rr = b.measure_round(i)
        self.assertTrue(all(e.adj_up for e in rr.lsdb.values()))

    def test_deep_probe_stops_at_a_failed_ping_and_reports_it_as_not_run(self):
        b, ex = failing(lambda argv: (2, "cannot flood"), ALL_ENDS)
        with mock.patch.object(b, "_train_q", side_effect=AssertionError("不应再发包列")), \
                mock.patch.object(b, "_iperf", side_effect=AssertionError("不应再打流")):
            dp = b.deep_probe("L1")
        self.assertEqual((dp.sent, dp.recv, dp.probe_bytes), (0, 0, 0))

    def test_deep_probe_survives_a_missing_iperf3_on_the_ems(self):
        b, _ = make(endpoints=ALL_ENDS)                                       # FakeExec.popen 抛 OSError，同缺 iperf3
        dp = b.deep_probe("L15")
        self.assertEqual((dp.sent, dp.recv), (5, 5))
        self.assertNotEqual(dp.bw_mbps, dp.bw_mbps)                           # 带宽缺测，精测本身有效

    def test_preflight_separates_failed_ping_from_silence(self):
        b, _ = failing(lambda argv: (2, "ping: bad"))
        rep = preflight(b)
        self.assertEqual((rep["silent"], rep["answered"]), ([], 0))
        self.assertEqual(len(rep["failed"]), rep["total"])
        self.assertFalse(rep["ok"])
        self.assertIn("ping 没能运行", format_preflight(rep))

    def test_preflight_tests_the_short_interval_ping_of_deep_probes(self):
        b, _ = failing(lambda argv: (2, "cannot flood") if argv[argv.index("-i") + 1] == "0.01" else None, ALL_ENDS)
        with mock.patch.object(b, "_train_q", return_value=(100.0, 0, True)):
            rep = preflight(b)
        self.assertEqual(rep["silent"], [])
        self.assertFalse(rep["ok"])
        self.assertEqual(len([p for p in rep["problems"] if "0.01 s 间隔" in p]), len(rep["probe_links"]))

    def test_preflight_checks_the_tools_of_an_ems_on_a_probe_link(self):
        b, ex = make(endpoints=ALL_ENDS)
        with mock.patch.object(ex, "have", side_effect=lambda c: c not in ("iperf3", "python3")), \
                mock.patch.object(b, "_train_q", return_value=(100.0, 0, True)):
            rep = preflight(b)
        self.assertEqual(len([p for p in rep["problems"] if p.startswith("本机没有")]), 2)
        b, ex = make(endpoints={"core": "sh -c"})
        self.assertIn("L15", b._bw_cand())
        with mock.patch.object(b, "_train_q", return_value=(100.0, 0, True)), \
                mock.patch.object(ex, "host", side_effect=lambda argv, **kw: (127, "") if "iperf3" in " ".join(argv)
                                  else FakeExec.host(ex, argv, **kw)):
            rep = preflight(b)
        self.assertEqual(len([p for p in rep["problems"] if "iperf3 或 timeout" in p]), 2)   # 网管本机和 core

    def test_skip_silent_stops_when_nothing_answers(self):
        from edgefdl import cli
        b, _ = make(silent=[t.ip for t in topology.load(TOPO).targets()])
        args = cli.argparse.Namespace(skip_silent=True)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as cm:
            cli._live_ready(b, args)
        self.assertIn("没有任何地址应答", str(cm.exception.code))
        b, _ = make(silent=["10.255.0.4"])
        with contextlib.redirect_stdout(io.StringIO()):
            cli._live_ready(b, args)
        self.assertEqual(b.skipped, ["lo.acc1"])

    def test_skip_silent_does_not_hide_a_failed_ping(self):
        from edgefdl import cli
        b, _ = failing(lambda argv: (2, "ping: bad"))
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            cli._live_ready(b, cli.argparse.Namespace(skip_silent=True))
        self.assertEqual(b.skipped, [])


def stand_in(d, body):
    """PATH 最前面放一个假 iperf3，只发生在本测试进程里，不发包。"""
    p = os.path.join(d, "iperf3")
    with open(p, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\n" + body)
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
    return mock.patch.dict(os.environ, PATH=d + os.pathsep + os.environ["PATH"])


class LiveEndpointProcessTest(unittest.TestCase):
    """用真实子进程验证失败路径：设备上不留进程，一轮测量不被卡住。"""

    def backend(self, endpoints=None):
        return LiveBackend(topology.load(TOPO), MeasurePlan(interval_s=0.001), endpoints=endpoints)

    def test_iperf_server_has_its_own_time_limit_and_a_failed_client_leaves_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            pid = os.path.join(d, "pid")
            body = f'case "$*" in *-s*) echo $$ > {pid}; echo "Server listening on 5400"; exec sleep 30;; esac\nexit 3\n'
            with stand_in(d, body):
                b = self.backend()
                with mock.patch.object(b.ex, "popen", wraps=b.ex.popen) as po:
                    self.assertTrue(math.isnan(b._iperf("ems", "ems", "127.0.0.1", 5400, 10.0)[0]))
            argv = po.call_args[0][1]
            self.assertEqual(argv[0], "timeout")                              # 杀掉 ssh 不会结束对端进程，靠它自己的时限
            self.assertEqual(argv[1], "10")                                   # docs/deployment.md 写明的上限
            with open(pid, encoding="utf-8") as f:
                n = int(f.read())
            with self.assertRaises(ProcessLookupError):                       # 网管本机：SIGTERM 经 timeout 转发给 iperf3
                for _ in range(40):
                    os.kill(n, 0)
                    time.sleep(0.05)

    def test_client_waits_for_the_listening_line(self):
        with tempfile.TemporaryDirectory() as d:
            mark = os.path.join(d, "client-ran")
            with stand_in(d, f'case "$*" in *-s*) exec sleep 30;; esac\ntouch {mark}\n'):
                b = self.backend()
                b.READY_TIMEOUT_S = 0.2
                t0 = time.time()
                self.assertTrue(math.isnan(b._iperf("ems", "ems", "127.0.0.1", 5400, 10.0)[0]))
                self.assertLess(time.time() - t0, 2.0)
            self.assertFalse(os.path.exists(mark))

    def test_iperf_rate_follows_the_estimate_on_live_only(self):
        b, _ = make()
        l = b.topo.link("L1")
        self.assertEqual(b._iperf_want(l, 100.0), 100.0 * 0.3)                # 链路已劣化到 100 Mbit/s
        self.assertEqual(b._iperf_want(l, float("nan")), l.bw_mbps * 0.3)     # 没有可信估计
        self.assertEqual(b._iperf_want(l, 1e6), l.bw_mbps * 0.3)
        vn = make_backend("netns", b.topo, MeasurePlan(), dry_run=True)
        self.assertEqual(vn._iperf_want(l, 100.0), l.bw_mbps * 0.3)           # 虚拟后端不变，已发布结果不变

    def test_unreachable_endpoint_does_not_stall_a_round(self):
        b = self.backend({"agg1": "sh -c 'sleep 12' --", "acc1": "sh -c 'sleep 12' --"})
        b.READY_TIMEOUT_S = 0.2
        t0 = time.time()
        bw, nb, clean = b._train("L4", 16, 1400) + (None,)
        self.assertLess(time.time() - t0, 2.0)
        self.assertNotEqual(bw, bw)

    def test_lsdb_timeout_kills_the_whole_command(self):
        with tempfile.TemporaryDirectory() as d:
            pid = os.path.join(d, "pid")
            rc = LiveExec("ems").shell(f"echo $$ > {pid}; sleep 31; echo done", 0.3)
            self.assertEqual(rc[0], 124)
            with open(pid, encoding="utf-8") as f:
                n = int(f.read())
        with self.assertRaises(ProcessLookupError):                           # 被杀的子进程要等 init 回收，组才消失
            for _ in range(100):
                os.killpg(n, signal.SIG_DFL)
                time.sleep(0.05)

    def test_bad_prefixes(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "e.yaml")
            for bad in ("endpoints: {agg1: \"ssh 'x\"}", "endpoints: {agg1: \"''\"}"):
                with open(p, "w", encoding="utf-8") as f:
                    f.write(bad)
                with self.assertRaises(ValueError):
                    load_endpoints(p)
        b = self.backend({"agg1": "/nonexistent/program"})
        self.assertEqual(b._ping("agg1", "10.0.0.1", "t", 1, 0.2).sent, 0)    # 程序不存在：缺测，不是调用栈

    def test_only_timeout_around_iperf3_passes(self):
        ex = LiveExec("ems")
        self.assertEqual(ex.wrap("ems", limited(["iperf3", "-v"], 5)), ["timeout", "5", "iperf3", "-v"])
        for argv in (["timeout", "5", "sh", "-c", "reboot"], ["timeout", "-s9", "5", "iperf3"], ["timeout", "5"]):
            with self.assertRaises(ReadOnlyError):
                ex.wrap("ems", argv)

    def test_bundle_cannot_raise_the_probe_load(self):
        hostile = MeasurePlan(deep_iperf_s=3600.0, deep_iperf_rate_ratio=10.0, deep_ping_count=100000, ping_size=65000,
                              ping_count=1000, bw_links_per_round=100, interval_s=0.001)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            p = bundle_plan(hostile)
        self.assertEqual(p, MeasurePlan())
        self.assertIn("deep_iperf_s", err.getvalue())
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(bundle_plan(MeasurePlan()), MeasurePlan())       # 正常模型包没有提示
            p = bundle_plan(MeasurePlan(interval_s=10.0, window=8))
        self.assertEqual((p.interval_s, p.window), (10.0, 8))                 # 更慢的周期和研判窗口仍取模型包
        self.assertEqual(err.getvalue(), "")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(bundle_plan(MeasurePlan(interval_s=float("nan"), window=10 ** 9)), MeasurePlan())


class CliMistakeTest(unittest.TestCase):
    """常见错误给一行说明，不是调用栈；写不出结果在开始计算之前就知道。"""

    def fails(self, *argv):
        from edgefdl import cli
        with self.assertRaises(SystemExit) as cm:
            cli.main(list(argv))
        self.assertIsInstance(cm.exception.code, str)
        self.assertTrue(cm.exception.code.startswith("edgefdl: "), cm.exception.code)
        return cm.exception.code

    def test_missing_files_and_bad_port(self):
        self.assertIn("no-such-model.pt", self.fails("serve", "--model", "no-such-model.pt", "--port", "0"))
        self.assertIn("99999", self.fails("serve", "--model", "x", "--port", "99999"))
        self.assertIn("nosuch.yaml", self.fails("topo", "nosuch.yaml"))
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "bad.yaml")
            with open(p, "w", encoding="utf-8") as f:
                f.write("name: [\n")
            self.assertIn("bad.yaml", self.fails("topo", p))

    def test_netns_without_root_names_the_lab(self):
        from edgefdl.vnet.netns import NetnsBackend
        with mock.patch.object(NetnsBackend, "__init__", side_effect=RuntimeError("命令失败(1): ip netns add")), \
                mock.patch("os.geteuid", return_value=1000):
            self.assertIn("lab.sh", self.fails("vnet", "up"))

    def test_output_directories_exist_before_the_work_starts(self):
        with tempfile.TemporaryDirectory() as d:
            for i, argv in enumerate((("train", "--data", "nosuch.npz", "--baseline", "b.json", "--out"),
                                      ("eval", "--model", "nosuch.pt", "--data", "nosuch.npz", "--json"))):
                out = os.path.join(d, f"new{i}", "x")
                self.fails(*argv, out)
                self.assertTrue(os.path.isdir(os.path.dirname(out)))

    def test_busy_port_stops_before_anything_is_measured_or_written(self):
        with tempfile.TemporaryDirectory() as d, socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            logs = os.path.join(d, "logs")
            msg = self.fails("serve", "--model", MODEL, "--port", str(s.getsockname()[1]), "--log-dir", logs)
            self.assertIn("--port", msg)
            self.assertFalse(os.path.exists(logs))

    def test_names_are_validated_and_listed(self):
        from edgefdl import cli
        from edgefdl.compare import conditions
        from edgefdl.robust import online_matrix
        self.assertEqual({k for _, k in online_matrix(10, None)}, set(cli.ROBUST_KINDS))
        self.assertEqual([c["name"] for c in conditions(10)], list(cli.COMPARE_CONDITIONS))
        msg = self.fails("robust", "--model", "x", "--out", "y", "--kinds", "long,nonsense")
        self.assertIn("nonsense", msg)
        self.assertIn("node_back", msg)
        msg = self.fails("compare", "--model", "x", "--out", "y", "--conditions", "nonsense")
        self.assertIn("busy_hour_drift_1ms", msg)

    def test_compare_robust_names_missing_items(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import compare_robust
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "r.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(dict(config=dict(seed=1), online=dict(long={}, dual={}, node={})), f)
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(compare_robust.main(["--multi", p, "--single", p]), 2)
        self.assertIn("scen, single", err.getvalue())

    def test_route_drift_on_a_host_link_is_refused(self):
        from edgefdl.core.api import BadRequest, parse_fault
        t = topology.load(TOPO)
        for lid in ("L15", "L11"):
            with self.assertRaises(BadRequest) as cm:
                parse_fault(t, dict(type="route_drift", link=lid, value=3))
            self.assertEqual(cm.exception.code, 400)
            self.assertIn("路由器之间", str(cm.exception))
        self.assertEqual(parse_fault(t, dict(type="route_drift", link="L4", value=3)).link, "L4")


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

    def test_a_probe_that_did_not_run_is_masked_not_read_as_no_answer(self):
        from edgefdl.agent.ddqn import K, PROBE
        with tempfile.TemporaryDirectory() as d:
            eng, b = self.engine(d)
            for _ in range(6):
                eng.step()
            pol, calls = eng.policy, []
            pol.masked = []
            act = pol.enc.lidx["L4"] * K + PROBE

            def q(*a):                                                        # 先要精测 L4，其次判正常
                r = -numpy.ones(pol.L * K + 1, dtype=numpy.float32)
                r[pol.L * K], r[act] = 0.0, 1.0
                return r

            def probe(lid):
                calls.append(lid)
                return DeepProbe(lid, 0, 0, float("nan"), float("nan"), float("nan"))
            with mock.patch.object(pol, "_q", side_effect=q):
                res = pol.diagnose(list(eng.window), probe_fn=probe)
            eng.stop()
        self.assertEqual(calls, ["L4"])                                       # 只试一次
        self.assertEqual((res["type"], res["probes"]), ("normal", []))

    def test_recheck_keeps_the_alarm_when_the_probe_did_not_run(self):
        with tempfile.TemporaryDirectory() as d:
            eng, b = self.engine(d)
            a = dict(alarm_id="x", fault_type="bandwidth", link_id="L4")
            eng.alarms, eng._trk, eng.noprobe = [a], {"x": dict(probed=0)}, []
            with mock.patch.object(eng.backend, "deep_probe", create=True,
                                   return_value=DeepProbe("L4", 0, 0, float("nan"), float("nan"), float("nan"))), \
                    mock.patch.object(eng, "_clear", side_effect=AssertionError("不应恢复")):
                eng._recheck_probe("x", "L4")
            eng.alarms = []
            eng.stop()
        self.assertEqual(eng.extra_probes, 1)

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
