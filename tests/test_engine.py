# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""引擎与接口的健壮性测试：告警通道出错不影响告警、注入参数校验、跨站防护、JSON 不含 NaN、
并发告警下的处置预演、告警号不重复（python -m unittest discover -s tests）。"""
import argparse
import contextlib
import csv
import http.client
import io
import json
import os
import re
import sys
import tempfile
import threading
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from edgefdl import topology  # noqa: E402
from edgefdl.agent.ddqn import K, PROBE, Agent  # noqa: E402
from edgefdl.agent.features import LIGHT_FEATURES, FeatureEncoder  # noqa: E402
from edgefdl.agent.infer import Policy  # noqa: E402
from edgefdl.agent.knowledge import advise, advise_node, rehearse  # noqa: E402
from edgefdl.core import orchestrator  # noqa: E402
from edgefdl.core.alarms import dumps, json_safe, make_sinks  # noqa: E402
from edgefdl.core.api import serve  # noqa: E402
from edgefdl.faults.generator import learn_baseline  # noqa: E402
from edgefdl.faults.model import TYPE_LABEL, Fault  # noqa: E402
from edgefdl.measure.model import DeepProbe  # noqa: E402
from edgefdl.vnet.sim import SimBackend  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
TOPO = os.path.join(ROOT, "configs", "demo_topology.yaml")
MODEL = os.path.join(ROOT, "results", "model.pt")
NAN = float("nan")


def strict_json(b):
    """严格解析：NaN / Infinity 不是 JSON。"""
    def bad(c):
        raise ValueError(f"non-JSON constant {c}")
    return json.loads(b, parse_constant=bad)


def light_engine(d, sinks=None):
    """未训练的智能体 + 仿真后端：只测引擎的告警登记、外发与预演，不依赖模型。"""
    t = topology.load(TOPO)
    b = SimBackend(t, seed=2)
    b.up()
    enc = FeatureEncoder(t, learn_baseline(b, rounds=10, deep_reps=1))
    ag = Agent(len(LIGHT_FEATURES), 6, 8, len(t.links), drift_links=[0], device="cpu")
    return orchestrator.Engine(t, b, Policy(ag, enc), b.plan, d, sinks=sinks), t, b


def res(ftype, link, **kw):
    return dict(dict(type=ftype, type_cn=TYPE_LABEL[ftype], link=link, confidence=0.9, candidates=[], probes=[],
                     infer_ms=0.5, evidence=["测试"]), **kw)


def model_engine(d):
    from edgefdl.cli import build_engine
    ns = argparse.Namespace(seed=1, noise=0.0, engine="torch", sinks=None, confirm=2)
    return build_engine(MODEL, "sim", d, ns)


class Boom:
    def send(self, alarm):
        raise RuntimeError("receiver down")


class Rec:
    def __init__(self):
        self.got = []

    def send(self, alarm):
        self.got.append((alarm["event"], alarm["alarm_id"]))


def alarm_rows(d):
    with open(os.path.join(d, "alarm_log.csv"), encoding="utf-8") as f:
        return list(csv.DictReader(f))


class SinkTest(unittest.TestCase):
    def test_sink_failure_does_not_block_raise_or_clear(self):
        with tempfile.TemporaryDirectory() as d:
            rec = Rec()
            eng, _, _ = light_engine(d, sinks=[Boom(), rec])
            err = io.StringIO()
            with eng.lock, contextlib.redirect_stderr(err):
                eng._raise(res("delay", "L6"), {})
            self.assertEqual([a["link_id"] for a in eng.alarms], ["L6"])
            self.assertEqual((eng.alarm_seq, len(eng.history), eng.errors), (1, 1, 1))
            aid = eng.alarms[0]["alarm_id"]
            self.assertIn(f"告警 {aid} raise 写入/外发失败（Boom）", err.getvalue())
            with contextlib.redirect_stderr(err):
                eng._clear("测试恢复", alarm=eng.alarms[0])
            self.assertEqual(eng.alarms, [])
            self.assertEqual(eng.errors, 2)
            self.assertEqual(rec.got, [("raise", aid), ("clear", aid)])     # 其后的通道照常收到
            self.assertEqual([r["event"] for r in alarm_rows(d)], ["raise", "clear"])
            eng.stop()

    def test_make_sinks_validation(self):
        for cfg in ([{"type": "webhook", "url": "hooks.example.org/edgefdl"}], [{"type": "webhook"}],
                    [{"type": "webhook", "url": "ftp://h/x"}], [{"type": "snmp", "host": "10.0.0.5"}],
                    [{"type": "syslog"}], [{"type": "syslog", "host": "10.0.0.5", "port": 0}], ["syslog"]):
            with self.assertRaises(ValueError, msg=cfg):
                make_sinks(cfg)
        s = make_sinks([{"type": "webhook", "url": "http://127.0.0.1:9/hook"}, {"type": "syslog", "host": "127.0.0.1"}])
        self.assertEqual([type(x).__name__ for x in s], ["WebhookSink", "SyslogSink"])
        s += make_sinks({"type": "syslog", "host": "127.0.0.1", "port": "5514"})
        self.assertEqual(s[-1].addr, ("127.0.0.1", 5514))
        for x in s[1:]:
            x.sock.close()

    def test_serve_bad_sinks_exits_before_backend(self):
        from edgefdl import cli
        for bad in ('[{"type": "snmp"}]', "{bad"):
            with self.assertRaises(SystemExit) as cm:
                cli.main(["serve", "--model", "no-such-model.pt", "--sinks", bad])
            self.assertIn("--sinks 配置有误", str(cm.exception.code))

    def test_advice_failure_still_raises(self):
        with tempfile.TemporaryDirectory() as d:
            eng, _, _ = light_engine(d)
            orig = orchestrator.advise

            def boom(*a, **k):
                raise KeyError("probe")
            orchestrator.advise = boom
            try:
                with eng.lock, contextlib.redirect_stderr(io.StringIO()):
                    eng._raise(res("delay", "L6"), {})
            finally:
                orchestrator.advise = orig
            self.assertEqual((eng.alarm_seq, len(eng.history), len(eng.alarms), eng.errors), (1, 1, 1, 1))
            self.assertEqual(eng.alarms[0]["advice"]["steps"], [])
            eng.stop()

    def test_alarm_ids_do_not_collide(self):
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            e1, _, _ = light_engine(d1)
            e2, _, _ = light_engine(d2)
            for e in (e1, e2):
                self.assertRegex(e.id_prefix, r"^A\d{10}-[0-9a-f]{6}$")
            self.assertNotEqual(e1.id_prefix, e2.id_prefix)
            with e1.lock:
                e1._raise(res("delay", "L6"), {})
            self.assertTrue(re.fullmatch(r"A\d{10}-[0-9a-f]{6}-0001", e1.alarms[0]["alarm_id"]))
            e1.stop()
            e2.stop()

    @unittest.skipUnless(os.path.exists(MODEL), "无模型")
    def test_failing_sink_end_to_end(self):
        """发布模型 + 仿真：Webhook 等通道出错时链路中断告警照常发出一次、照常恢复（不会每轮重发）。"""
        with tempfile.TemporaryDirectory() as d:
            eng, _, b = model_engine(d)
            eng.sinks.append(Boom())
            for _ in range(6):
                eng.step()
            b.inject(Fault("link_down", "L6"))
            with contextlib.redirect_stderr(io.StringIO()):
                for _ in range(40):
                    eng.step()
            self.assertIn(("link_down", "L6"), {(a["fault_type"], a["link_id"]) for a in eng.alarms})
            self.assertEqual(eng.alarm_seq, len(eng.history))
            self.assertEqual(sum(r["event"] == "raise" for r in alarm_rows(d)), eng.alarm_seq)
            self.assertEqual(eng.errors, len(alarm_rows(d)))             # 每条告警记录外发失败一次，其余无异常
            b.clear()
            with contextlib.redirect_stderr(io.StringIO()):
                for _ in range(30):
                    eng.step()
            self.assertEqual(eng.alarms, [])
            eng.stop()


class JsonSafeTest(unittest.TestCase):
    def test_json_safe(self):
        x = {"a": NAN, "b": [float("inf"), np.float32(0.5), np.int64(3), np.bool_(True)], "c": np.array([1.0, NAN])}
        self.assertEqual(json_safe(x), {"a": None, "b": [None, 0.5, 3, True], "c": [1.0, None]})
        self.assertEqual(strict_json(dumps(x))["c"], [1.0, None])

    def _policy(self):
        t = topology.load(TOPO)
        b = SimBackend(t, seed=4)
        b.up()
        enc = FeatureEncoder(t, learn_baseline(b, rounds=10, deep_reps=1))
        ag = Agent(len(LIGHT_FEATURES), 6, 8, len(t.links), drift_links=[0], device="cpu")
        return Policy(ag, enc), [b.measure_round(i, transient=[]) for i in range(6)]

    def test_nan_deep_probe_in_evidence(self):
        """精测无应答（RTT、带宽为 NaN，真实协议栈的典型结果）：研判结果中为 None，精测特征为有限值。"""
        pol, win = self._policy()
        li = pol.enc.lidx["L6"]
        seen = []

        def fake_q(light, deep, probed, glob, step):
            seen.append(deep.copy())
            q = np.full(pol.L * K + 1, -1.0, dtype=np.float32)
            q[li * K + PROBE if step == 0 else li * K] = 1.0                # 先精测 L6，再判 L6 中断
            return q
        pol._q = fake_q
        r = pol.diagnose(win, probe_fn=lambda lid: DeepProbe(lid, 100, 0, NAN, NAN, NAN, 0))
        self.assertEqual((r["type"], r["link"]), ("link_down", "L6"))
        self.assertEqual(r["probes"], [dict(link="L6", loss=1.0, rtt=None, mdev=None, bw=None)])
        self.assertTrue(np.isfinite(seen[-1]).all())
        self.assertIn("精测：链路两端互 ping 不通", r["evidence"])
        strict_json(json.dumps(r, allow_nan=False))

    def test_nonfinite_q_gives_normal(self):
        pol, win = self._policy()
        pol._q = lambda *a: np.full(pol.L * K + 1, NAN, dtype=np.float32)
        r = pol.diagnose(win, probe_fn=lambda lid: self.fail("不应精测"))
        self.assertEqual((r["type"], r["confidence"]), ("normal", 0.0))
        strict_json(json.dumps(r, allow_nan=False))

        def partly(*a):                                                     # 非有限的动作视同屏蔽
            q = np.full(pol.L * K + 1, NAN, dtype=np.float32)
            q[pol.enc.lidx["L4"] * K + 1] = 0.6
            return q
        pol._q = partly
        r = pol.diagnose(win)
        self.assertEqual((r["type"], r["link"], r["confidence"]), ("delay", "L4", 0.8))


class _Server:
    def __init__(self, eng, topo, host):
        self.httpd = serve(eng, topo, host, 0)
        self.port = self.httpd.server_address[1]
        self.th = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.th.start()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def post(self, path, body, **h):
        return self.req("POST", path, body, dict({"Content-Type": "application/json"}, **h))

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.eng, cls.t, cls.b = light_engine(cls.tmp.name)
        cls.srv = _Server(cls.eng, cls.t, "127.0.0.1")

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()
        cls.eng.stop()
        cls.tmp.cleanup()

    def tearDown(self):
        self.b.clear()

    def test_inject_validation(self):
        bad = [('{"type":"delay","link":"L1","value":NaN}', "NaN"),
               ('{"type":"delay","link":"L1","value":Infinity}', "Infinity"),
               ('{"type":"delay","link":"L1","value":"nan"}', "有限数值"),
               ('{"type":"delay","link":"L1","value":-40}', "须大于 0"),
               ('{"type":"jitter","link":"L1","value":1e308}', "不超过 10000"),
               ('{"type":"loss","link":"L1","value":5}', "不超过 1"),
               ('{"type":"loss","link":"L1","value":-1}', "须大于 0"),
               ('{"type":"bandwidth","link":"L1","value":0}', "须大于 0"),
               ('{"type":"route_drift","link":"L1","value":-3}', "route_drift"),
               ('{"type":"route_drift","link":"L1","value":1e6}', "65535"),
               ('{"type":"delay","link":"L1","value":true}', "须为数值"),
               ('{"type":"delay","link":"L99","value":20}', "未知链路 L99"),
               ('{"type":"foo","link":"L1"}', "未知故障类型 foo"),
               ('{"link":"L1"}', "缺少字段 type"),
               ('[1, 2]', "JSON 对象"),
               ('not json', "不是合法的 JSON")]
        for body, msg in bad:
            code, data = self.srv.post("/api/inject", body)
            self.assertEqual(code, 400, body)
            self.assertIn(msg, strict_json(data)["error"], body)
        self.assertEqual(self.b.faults(), [])
        code, data = self.srv.post("/api/clear", {"link": "L99"})
        self.assertEqual((code, strict_json(data)["error"]), (400, "未知链路 L99"))
        code, data = self.srv.post("/api/inject", {"type": "delay", "link": "L1", "value": 20})
        self.assertEqual((code, strict_json(data)["label"]), (200, "L1 时延劣化(RTT异常) 20ms"))
        code, data = self.srv.post("/api/inject", {"type": "link_down", "link": "L6", "value": "ignored"})
        self.assertEqual((code, strict_json(data)["fault"]["value"]), (200, 1.0))
        self.assertEqual(self.srv.post("/api/clear", {"link": "L6"})[0], 200)
        self.assertEqual([f.link for f in self.b.faults()], ["L1"])
        self.assertEqual(self.srv.post("/api/clear", {})[0], 200)
        self.assertEqual(self.b.faults(), [])

    def test_csrf_and_rebinding_guard(self):
        p, f = self.srv.port, {"type": "link_down", "link": "L15"}
        s = self.srv
        self.assertEqual(s.req("POST", "/api/inject", json.dumps(f), {"Content-Type": "text/plain;charset=UTF-8"})[0], 415)
        self.assertEqual(s.req("POST", "/api/inject", json.dumps(f))[0], 415)                 # 无 Content-Type
        self.assertEqual(s.post("/api/inject", f, Origin="https://evil.example")[0], 403)
        self.assertEqual(s.post("/api/inject", f, Origin="null")[0], 403)
        self.assertEqual(s.post("/api/clear", {}, Origin=f"http://127.0.0.1:{p + 1}")[0], 403)
        # DNS 重绑定：页面与请求同源，但 Host 是攻击者的域名
        self.assertEqual(s.post("/api/inject", f, Host=f"rebind.evil.example:{p}",
                                Origin=f"http://rebind.evil.example:{p}")[0], 403)
        for path in ("/api/status", "/api/topology", "/api/export/alarm_log.csv", "/metrics", "/"):
            self.assertEqual(s.req("GET", path, headers={"Host": f"attacker.example:{p}"})[0], 403, path)
        self.assertEqual(self.b.faults(), [])
        # 本机访问照常：大屏的同源 JSON POST、经 SSH 隧道的 localhost 其他端口、Prometheus 抓取
        code, _ = s.post("/api/inject", f, Origin=f"http://127.0.0.1:{p}")
        self.assertEqual(code, 200)
        self.assertEqual(s.post("/api/clear", {}, **{"Content-Type": "application/json; charset=utf-8",
                                                     "Origin": f"http://127.0.0.1:{p}"})[0], 200)
        self.assertEqual(s.req("GET", "/api/status", headers={"Host": "localhost:9000"})[0], 200)
        self.assertEqual(s.req("GET", "/metrics")[0], 200)
        self.assertEqual(s.req("GET", "/")[0], 200)

    def test_non_loopback_bind(self):
        """--host 显式指定非回环地址：不校验 Host，跨站 POST 仍被拒绝。"""
        srv = _Server(self.eng, self.t, "0.0.0.0")
        try:
            p, f = srv.port, {"type": "delay", "link": "L1", "value": 20}
            self.assertEqual(srv.req("GET", "/api/status", headers={"Host": f"ems.example:{p}"})[0], 200)
            self.assertEqual(srv.post("/api/inject", f, Host=f"ems.example:{p}", Origin="https://evil.example")[0], 403)
            self.assertEqual(srv.post("/api/inject", f, Host=f"ems.example:{p}", Origin=f"http://ems.example:{p}")[0], 200)
        finally:
            srv.close()

    def test_responses_are_nan_free(self):
        e = self.eng
        a = dict(alarm_id="A0-nan", event="raise", fault_type="loss", fault_type_cn="丢包劣化", link_id="L1",
                 severity="major", first_round=0, confidence=NAN, ts=0.0, evidence=dict(probes=[dict(link="L1", rtt=NAN, bw=float("inf"))]))
        with e.lock:
            e.history.appendleft(a)
            e.alarms.append(a)
            e.link_health["L1"] = NAN
        try:
            st = strict_json(self.srv.req("GET", "/api/status")[1])
            self.assertIsNone(st["link_health"]["L1"])
            self.assertIsNone(st["active_alarms"][0]["evidence"]["probes"][0]["rtt"])
            al = strict_json(self.srv.req("GET", "/api/alarms?n=5")[1])
            self.assertEqual((al[0]["confidence"], al[0]["evidence"]["probes"][0]["bw"]), (None, None))
            self.assertEqual(self.srv.req("GET", "/api/alarms?n=x")[0], 400)
        finally:
            with e.lock:
                e.history.remove(a)
                e.alarms.remove(a)
                e.link_health["L1"] = 0.0


class RehearsalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t = topology.load(TOPO)

    def test_concurrent_down_link(self):
        """L2 已中断时，L1 劣化不能"绕行"到 L2；L2 中断的预演指出备份路径经过劣化中的 L1。"""
        a = advise(self.t, "delay", "L1")
        self.assertTrue(a["rehearsal"]["feasible"])
        self.assertTrue(any(s["step"].startswith("临时处置") for s in a["steps"]))
        a = advise(self.t, "delay", "L1", others=[dict(fault_type="link_down", link_id="L2")])
        self.assertFalse(a["rehearsal"]["feasible"])
        self.assertIn("L2 链路中断", a["rehearsal"]["detail"])
        self.assertFalse(any(s["step"].startswith("临时处置") for s in a["steps"]))
        r = rehearse(self.t, "link_down", "L2", others=[dict(fault_type="delay", link_id="L1")])
        self.assertTrue(r["feasible"])
        self.assertIn("备份路径经过同样告警中的 L1", r["detail"])
        r = rehearse(self.t, "link_down", "L6", others=[dict(fault_type="link_down", link_id=x) for x in ("L9", "L10")])
        self.assertFalse(r["feasible"])                          # L9、L10 已断：L6 再断则 acc3 与 h3 失联
        self.assertIn("L13.h3", r["detail"])
        self.assertFalse(advise_node(self.t, "agg1", others=[dict(fault_type="link_down", link_id="L2")])
                         ["rehearsal"]["feasible"])

    def test_host_link_and_nqa_only_link(self):
        r = rehearse(self.t, "link_down", "L11")
        self.assertFalse(r["feasible"])
        self.assertIn("L11.h1", r["detail"])
        r = rehearse(self.t, "link_down", "L3")
        self.assertTrue(r["feasible"])
        self.assertNotIn("nqa.L3", r["detail"])
        self.assertIn("agg1→core→agg2", r["detail"])

    def test_ring_link_backup_path(self):
        r = rehearse(self.t, "delay", "L9")
        self.assertTrue(r["feasible"])
        self.assertIn("acc3→agg2→acc4", r["detail"])
        r = rehearse(self.t, "loss", "L11")
        self.assertFalse(r["feasible"])
        self.assertIn("L11.h1", r["detail"])
        r = rehearse(self.t, "delay", "L9", others=[dict(fault_type="link_down", link_id="L7")])
        self.assertFalse(r["feasible"])                          # acc4 只剩 L9，两端之间无备份路径

    def test_route_drift_rehearsal_is_not_vacuous(self):
        r = rehearse(self.t, "route_drift", "L6", cur_cost=100)
        self.assertTrue(r["feasible"])
        self.assertRegex(r["detail"], r"^\d+ 个监测目标的路径随之回到基线，恢复后全部监测路径与基线一致$")
        self.assertFalse(rehearse(self.t, "route_drift", "L6", cur_cost=10)["feasible"])    # LSDB 已是基线
        self.assertFalse(rehearse(self.t, "route_drift", "L6")["feasible"])                # 当前开销未知
        r = rehearse(self.t, "route_drift", "L6", cur_cost=100, others=[dict(fault_type="link_down", link_id="L2")])
        self.assertTrue(r["feasible"])
        self.assertIn("因其他告警仍与基线不同", r["detail"])

    def test_engine_refreshes_advice_when_alarms_change(self):
        with tempfile.TemporaryDirectory() as d:
            eng, _, _ = light_engine(d)
            with eng.lock:
                eng._raise(res("link_down", "L2"), {})
                eng._raise(res("delay", "L1"), {})
            l2, l1 = eng.alarms
            self.assertFalse(l1["advice"]["rehearsal"]["feasible"])
            self.assertNotIn("临时处置", l1["repair"])
            self.assertIn("备份路径经过同样告警中的 L1", l2["advice"]["rehearsal"]["detail"])
            eng._clear("测试恢复", alarm=l2)
            self.assertTrue(l1["advice"]["rehearsal"]["feasible"])
            self.assertIn("临时处置", l1["repair"])
            self.assertEqual(eng.errors, 0)
            eng.stop()

    @unittest.skipUnless(os.path.exists(MODEL), "无模型")
    def test_concurrent_alarms_end_to_end(self):
        """发布模型 + 仿真：L2 中断告警后 L1 时延劣化，L1 告警的预演不推荐经 L2 绕行。"""
        with tempfile.TemporaryDirectory() as d:
            eng, _, b = model_engine(d)
            for _ in range(6):
                eng.step()
            b.inject(Fault("link_down", "L2"))
            for _ in range(30):
                eng.step()
            b.inject(Fault("delay", "L1", 20))
            for _ in range(60):
                eng.step()
            got = {(a["fault_type"], a["link_id"]): a for a in eng.alarms}
            self.assertIn(("link_down", "L2"), got)
            self.assertIn(("delay", "L1"), got)
            reh = got[("delay", "L1")]["advice"]["rehearsal"]
            self.assertFalse(reh["feasible"])
            self.assertIn("L2 链路中断", reh["detail"])
            self.assertIn("L1", got[("link_down", "L2")]["advice"]["rehearsal"]["detail"])
            self.assertEqual(eng.errors, 0)
            eng.stop()


if __name__ == "__main__":
    unittest.main()
