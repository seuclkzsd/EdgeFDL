#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""在无头浏览器里检查回放页面（需要 playwright，不是 edgefdl 的依赖）：把站点放在静态文件服务器的 /EdgeFDL/ 子路径下
打开，检查没有控制台错误和失败的请求、自动播放、场景选择；逐场景把大屏上显示的数字与 frames.json 逐项比对。

  python tools/demo/check_page.py --chrome /path/to/chrome --shots DIR
"""
import argparse
import functools
import http.server
import json
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_site import build  # noqa: E402
from record import unpack  # noqa: E402


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


READ = """() => {
  const d = document.querySelector('#dash').contentDocument;
  const t = s => { const e = d.querySelector(s); return e ? e.textContent : null; };
  return {round: t('#k-round b'), kpi_alarms: t('#k-al b'), inf: t('#k-inf b'), bw: t('#k-bw b'), up: t('#k-up b'), cpu: t('#k-cpu b'),
    title: t('.atitle'), truth: t('#truth'), alarmbox: t('#alarm'), sub: t('#sub'),
    alarm_links: [...d.querySelectorAll('.link.alarm')].map(e => e.dataset.id),
    dashed: [...d.querySelectorAll('.link')].filter(e => e.getAttribute('stroke-dasharray')).map(e => e.dataset.id),
    red: [...d.querySelectorAll('.link')].filter(e => e.getAttribute('stroke') === 'var(--bad)').map(e => e.dataset.id),
    nodes: d.querySelectorAll('#svg .node').length, links: d.querySelectorAll('#svg line.link').length,
    events: [...d.querySelectorAll('#events > div')].map(e => e.textContent).slice(0, 3),
    hist: [...d.querySelectorAll('#hist tr')].map(r => [...r.children].map(c => c.textContent)),
    sel_title: t('#sel-title'), sel_meta: t('#sel-meta'),
    over: document.querySelector('#c-over b').textContent, alarms: document.querySelector('#c-alarm b').textContent,
    cap: document.querySelector('#cap-zh').textContent, cap_en: document.querySelector('#cap-en').textContent};
}"""


def expected(st, alarms, data, i):
    """大屏（edgefdl/core/web/index.html 的 tick / renderAlarm / history）对这一帧应显示的内容。"""
    a = st["active_alarm"]
    e = dict(round=str(st["round"]), kpi_alarms=f"{st['alarms_total']} / {st['suppressed']}",
             inf=f"{st['infer']['p99_ms']:.2f} ms" if st["infer"] else "-",
             bw=f"{st['overhead']['pct_of_min_link']:.3f} %",
             cpu=f"{st['resource']['cpu_pct']:.1f}% / {round(st['resource']['rss_mb'])}M",
             alarm_links=sorted({x for c in st["active_alarms"] for x in (c.get("links") or [c["link_id"]])}),
             dashed=sorted(k for k, v in st["link_state"].items() if not v["adj_up"]),
             over=f"{data['over_threshold'][i]} / {data['targets_total']}", alarms=str(len(st["active_alarms"])))
    if a:
        name = "设备 " + a["node"] if a.get("node") else a["link_id"]
        e["title"] = f"{name} {a['fault_type_cn']}"
    e["hist_ids"] = [x["alarm_id"] for x in alarms]
    return e


def compare(got, exp):
    bad = []
    for k, v in exp.items():
        g = got.get(k)
        if k == "hist_ids":
            g = [r[0] for r in got["hist"]]
        if k in ("alarm_links", "dashed"):
            g = sorted(g)
        if g != v:
            bad.append((k, g, v))
    return bad


def main():
    ap = argparse.ArgumentParser(description="无头浏览器检查回放页面")
    ap.add_argument("--chrome", help="Chromium 可执行文件（默认用 playwright 自带的）")
    ap.add_argument("--shots", help="截图目录")
    ap.add_argument("--image", help="把 L1 告警时刻的截图另存到此路径（docs/images/demo.png）")
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=1000)
    args = ap.parse_args()
    from playwright.sync_api import sync_playwright

    tmp = tempfile.mkdtemp(prefix="edgefdl-site-")
    build(os.path.join(tmp, "EdgeFDL"))
    with open(os.path.join(tmp, "EdgeFDL", "frames.json"), encoding="utf-8") as f:
        data = json.load(f)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=tmp))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/EdgeFDL/"
    problems, console, failed = [], [], []
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=args.chrome, args=["--force-color-profile=srgb", "--no-proxy-server"])
        pg = b.new_page(viewport=dict(width=args.width, height=args.height))
        pg.on("console", lambda m: console.append(m.text) if m.type in ("error", "warning") else None)
        pg.on("pageerror", lambda e: console.append(str(e)))
        pg.on("response", lambda r: failed.append((r.status, r.url)) if r.status >= 400 else None)
        pg.on("requestfailed", lambda r: failed.append((0, r.url)))
        pg.goto(url)
        pg.wait_for_function("window.replay && document.querySelector('#dash').contentDocument.querySelector('button[data-scene]')")
        c0 = pg.evaluate("replay.cur")
        pg.wait_for_function(f"replay.cur >= {c0 + 2}", timeout=15000)      # 自动播放
        pg.click("#play")
        c1 = pg.evaluate("replay.cur")
        pg.wait_for_timeout(2500)
        if pg.evaluate("replay.cur") != c1:
            problems.append("暂停后帧仍在前进")
        report = []
        checks = []
        for s in data["scenes"]:
            mids = [s["start"]]
            if s.get("first_alarm") is not None:
                mids += [s["inject"], s["first_alarm"], s["repair"] - 1, s["end"] - 1]
                if s.get("first_cleared"):
                    mids.insert(-1, s["first_cleared"] + 1)
            checks += [(s["id"], i) for i in mids]
        for sid, i in checks:
            pg.evaluate(f"replay.goTo({i}, true)")
            pg.wait_for_timeout(120)
            got = pg.evaluate(READ)
            st = unpack(data["frames"][i]["status"], data["pool"])
            al = unpack(data["frames"][i]["alarms"], data["pool"])
            bad = compare(got, expected(st, al, data, i))
            if got["nodes"] != 12 or got["links"] != 15:
                bad.append(("topology", (got["nodes"], got["links"]), (12, 15)))
            cap = next((c for c in data["captions"] if c["start"] <= i < c["end"]), None)
            if cap and (got["cap"] != cap["zh"] or got["cap_en"] != cap["en"]):
                bad.append(("caption", got["cap"], cap["zh"]))
            report.append(dict(scene=sid, frame=i, round=got["round"], over=got["over"], alarms=got["alarms"],
                               title=got["title"], ok=not bad))
            if bad:
                problems.append((sid, i, bad))
        # 场景选择：顶部时间轴与大屏面板各点一次
        for s in data["scenes"]:
            pg.click(f"#scenes button[data-scene={s['id']}]")
            pg.wait_for_timeout(100)
            if pg.evaluate("replay.cur") != s["start"]:
                problems.append(("顶部场景按钮", s["id"]))
        fr = pg.frame_locator("#dash")
        for s in data["scenes"]:
            fr.locator(f"button[data-scene={s['id']}]").click()
            pg.wait_for_timeout(100)
            if pg.evaluate("replay.cur") != s["start"]:
                problems.append(("面板场景按钮", s["id"]))
            if fr.locator("button[data-scene].primary").get_attribute("data-scene") != s["id"]:
                problems.append(("面板未高亮当前场景", s["id"]))
        if fr.locator("#btn-inj").is_visible():
            problems.append("故障注入面板仍可见")
        if fr.locator('a[href^="/api/export/"]').count():
            problems.append("导出按钮仍在")
        shots = {}
        for s in data["scenes"]:
            if s.get("first_alarm") is not None:
                shots[s["id"]] = s["first_alarm"] + 2
        shots["normal"] = data["scenes"][0]["end"] - 1
        if args.shots:
            os.makedirs(args.shots, exist_ok=True)
            for sid, i in shots.items():
                pg.evaluate(f"replay.goTo({i}, true)")
                pg.wait_for_timeout(400)
                pg.screenshot(path=os.path.join(args.shots, f"{sid}.png"), animations="disabled")
        if args.image:
            pg.evaluate(f"replay.goTo({shots['degradation']}, true)")
            pg.wait_for_timeout(400)
            pg.screenshot(path=args.image, animations="disabled")
        b.close()
    httpd.shutdown()
    print(json.dumps(report, ensure_ascii=False, indent=0))
    print(f"控制台错误 {len(console)} 条，失败请求 {len(failed)} 个，不一致 {len(problems)} 处")
    for x in console[:10] + failed[:10] + problems[:20]:
        print(" ", x)
    return 1 if console or failed or problems else 0


if __name__ == "__main__":
    sys.exit(main())
