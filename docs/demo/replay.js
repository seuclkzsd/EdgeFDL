// SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
// SPDX-License-Identifier: Apache-2.0
// Static replay of the dashboard. The unchanged dashboard page runs in an iframe; its fetch() calls are answered from
// frames.json (recorded by tools/demo/record.py), so what it draws is what the running system returned round by round.
"use strict";
(async function () {
  const $ = s => document.querySelector(s);
  const frame = $("#dash");
  const SPEEDS = [[1, "实时 1×"], [5, "5×"], [10, "10×"], [20, "20×"]];
  const HOLD_END = 3;                     // 片尾停留的帧数，之后从头循环

  const data = await (await fetch("frames.json")).json();
  // 帧里重复的子对象共享在 pool 中，{"$": n} 指向 pool[n]（只引用序号更小的条目）
  const pool = [];
  const unpack = v => {
    if (Array.isArray(v)) return v.map(unpack);
    if (v && typeof v === "object") {
      if ("$" in v) return pool[v.$];
      return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, unpack(x)]));
    }
    return v;
  };
  data.pool.forEach(v => pool.push(unpack(v)));
  const frames = data.frames.map(f => ({status: unpack(f.status), alarms: unpack(f.alarms), seriesRound: f.series_round}));

  let cur = 0, playing = true, msPerFrame = 1000, timer = null, holding = 0;
  let dash = null;                        // 大屏页面初始化完成后才可调用其函数
  const sceneOf = i => data.scenes.find(s => i >= s.start && i < s.end) || data.scenes[data.scenes.length - 1];

  // ---------------------------------------------------------------- 大屏的接口应答
  function seriesAt(i, link) {
    const last = frames[i].seriesRound, out = {};
    for (const t of data.series.links[link] || [])
      out[t] = data.series.points[t].filter(p => p[0] <= last).slice(-120).map(p => ({round: p[0], rtt: p[1], loss: p[2]}));
    return out;
  }
  function answer(body, status) {
    return Promise.resolve(new Response(JSON.stringify(body), {status: status || 200, headers: {"Content-Type": "application/json"}}));
  }
  window.replayFetch = function (url, init) {
    const u = new URL(url, location.href), p = u.pathname;
    if (init && init.method === "POST") return answer({ok: false, error: "回放模式不能注入故障"}, 403);
    if (p.endsWith("/api/topology")) return answer(data.topology);
    if (p.endsWith("/api/status")) return answer(frames[cur].status);
    if (p.endsWith("/api/alarms")) return answer(frames[cur].alarms);
    if (p.endsWith("/api/series")) return answer(seriesAt(cur, u.searchParams.get("link")));
    return answer({error: "not found"}, 404);
  };

  // ---------------------------------------------------------------- 播放器
  function refresh() {                    // 换帧后立即重绘，不等大屏自己的轮询
    if (dash) { dash.tick(); dash.history(); dash.series(); }
  }
  function paintPlayer() {
    const st = frames[cur].status, c = data.captions.find(x => cur >= x.start && cur < x.end);
    $("#seek").value = cur;
    $("#pos").textContent = `第 ${st.round} 轮 · ${cur + 1}/${frames.length}`;
    $("#cap-zh").textContent = c ? c.zh : "";
    $("#cap-en").textContent = c ? c.en : "";
    const over = data.over_threshold[cur], n = st.active_alarms.length;
    $("#c-over b").textContent = `${over ?? "-"} / ${data.targets_total}`;
    $("#c-alarm b").textContent = n;
    $("#c-over").classList.toggle("hot", over > 0);
    $("#c-alarm").classList.toggle("bad", n > 0);
    const sc = sceneOf(cur);
    document.querySelectorAll("#scenes button").forEach(b => b.classList.toggle("on", b.dataset.scene === sc.id));
    if (dash) dash.document.querySelectorAll("button[data-scene]").forEach(b => b.classList.toggle("primary", b.dataset.scene === sc.id));
  }
  function goTo(i, focus) {
    const before = sceneOf(cur);
    cur = Math.max(0, Math.min(frames.length - 1, i));
    const sc = sceneOf(cur);
    paintPlayer();
    if (dash && (focus || sc !== before)) dash.select(sc.focus);   // 场景切换时显示其主链路的测量曲线
    refresh();
  }
  function schedule() {
    clearTimeout(timer);
    if (!playing) return;
    timer = setTimeout(() => {
      if (cur === frames.length - 1) {
        if (++holding > HOLD_END) { holding = 0; goTo(0); }
      } else goTo(cur + 1);
      schedule();
    }, msPerFrame);
  }
  function setPlaying(on) {
    playing = on;
    $("#play").textContent = on ? "暂停 Pause" : "播放 Play";
    schedule();
  }
  function pick(s) { holding = 0; goTo(s.start, true); schedule(); }

  function buildPlayer() {
    const m = data.meta, last = frames.length - 1;
    $("#rec").textContent = ` · ${m.recorded.slice(0, 10)} · EdgeFDL ${m.edgefdl} · ${m.model} · seed ${m.seed}`;
    $("#seek").max = last;
    $("#seek").addEventListener("input", e => { holding = 0; goTo(+e.target.value); schedule(); });
    $("#play").addEventListener("click", () => setPlaying(!playing));
    SPEEDS.forEach(([x, label]) => $("#speed").add(new Option(label, x, x === 5, x === 5)));
    const setSpeed = () => { msPerFrame = m.interval_s * 1000 / +$("#speed").value; schedule(); };
    $("#speed").addEventListener("change", setSpeed);
    setSpeed();
    data.scenes.forEach((s, n) => {
      const b = document.createElement("button");
      b.type = "button"; b.dataset.scene = s.id; b.textContent = `${n + 1}. ${s.zh}`; b.title = `${s.zh} · ${s.en}`;
      b.style.flexGrow = s.end - s.start;
      b.addEventListener("click", () => pick(s));
      $("#scenes").appendChild(b);
    });
    // 告警发出（红）与恢复（绿）在时间轴上的位置，取自录到的告警计数与活动告警
    frames.forEach((f, i) => {
      if (!i) return;
      const a = frames[i - 1].status, b = f.status;
      const gone = a.active_alarms.some(x => !b.active_alarms.some(y => y.alarm_id === x.alarm_id));
      for (const [cls, hit] of [["raise", b.alarms_total > a.alarms_total], ["clear", gone]]) {
        if (!hit) continue;
        const t = document.createElement("i");
        t.className = cls; t.style.left = `${100 * i / last}%`;
        t.title = `第 ${b.round} 轮`;
        $("#marks").appendChild(t);
      }
    });
    document.addEventListener("keydown", e => {
      if (e.target.tagName === "SELECT") return;
      if (e.key === " ") { e.preventDefault(); setPlaying(!playing); }
      if (e.key === "ArrowRight") { e.preventDefault(); goTo(cur + 1); schedule(); }
      if (e.key === "ArrowLeft") { e.preventDefault(); goTo(cur - 1); schedule(); }
    });
  }

  // ---------------------------------------------------------------- 大屏页面
  function adaptDashboard() {             // 故障注入面板换成场景选择；导出按钮依赖后端，回放中不提供
    const d = frame.contentDocument;
    const card = d.querySelector("#inj-link").closest(".card");
    card.querySelector("h2").textContent = "回放场景 Scenarios";
    card.querySelectorAll(".ctl").forEach(x => { x.style.display = "none"; });
    const box = d.createElement("div");
    box.className = "ctl";
    data.scenes.forEach((s, n) => {
      const b = d.createElement("button");
      b.type = "button"; b.dataset.scene = s.id; b.textContent = `${n + 1}. ${s.zh}`; b.title = s.en;
      b.addEventListener("click", () => pick(s));
      box.appendChild(b);
    });
    card.insertBefore(box, card.querySelector("#truth"));
    d.querySelectorAll('a[href^="/api/export/"]').forEach(a => { const c = a.closest(".ctl"); if (c) c.remove(); });
  }
  async function dashboardHtml() {        // 站点里是构建时复制的 dashboard.html；直接从源码树打开时取仓库内原文件
    for (const u of ["dashboard.html", "../../edgefdl/core/web/index.html"]) {
      const r = await fetch(u);
      if (r.ok) return r.text();
    }
    throw new Error("dashboard page not found");
  }

  const html = await dashboardHtml();
  if (!html.includes("<script>")) throw new Error("unexpected dashboard page");
  buildPlayer();
  paintPlayer();
  frame.addEventListener("load", async () => {
    // 大屏的 init() 取到拓扑、画完节点后才能调用 tick / select
    while (!frame.contentDocument.querySelector("#svg .node")) await new Promise(r => setTimeout(r, 30));
    adaptDashboard();
    dash = frame.contentWindow;
    paintPlayer();
    dash.select(sceneOf(cur).focus);
    refresh();
    schedule();
  }, {once: true});
  frame.srcdoc = html.replace("<script>", "<script>window.fetch = parent.replayFetch;<\/script>\n<script>");
  window.replay = {frames, data, goTo, pick, setPlaying, get cur() { return cur; }, get playing() { return playing; }};
})().catch(e => {
  document.querySelector("#cap-zh").textContent = "回放加载失败，请用静态文件服务器打开本页。";
  document.querySelector("#cap-en").textContent = `Replay failed to load; open this page from a static file server. (${e.message})`;
  console.error(e);
});
