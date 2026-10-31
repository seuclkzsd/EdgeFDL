# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""命令行入口：edgefdl <子命令>。

典型流程（个性化模型）：
  intake  → 由需求采集表/设备配置生成拓扑
  baseline→ 在等价虚拟网络学习正常基线
  gen     → 全量故障样本生成
  train   → DDQN 训练
  eval    → 分类别指标评估
  bench   → 指标自测（推理时延、带宽/资源占用、抗干扰、场景测试）
  robust  → 鲁棒性测试（强干扰、测量缺陷、故障幅度、背景波动、多故障）
  compare → 同台对比：同一测量流上的阈值告警、告警分组、布尔层析、NetBouncer 式回归、007 式投票
  serve   → 网管在线运行（测量 + 研判 + 告警 + Web 大屏）
  preflight → 只读接入真实网络前的检查（--backend live 的前置步骤）
pipeline 一键串起 baseline→gen→train→eval→bench。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict

import yaml

from . import topology as topomod
from .measure.model import MeasurePlan

DEFAULT_TOPO = os.path.join(os.path.dirname(__file__), "..", "configs", "demo_topology.yaml")
ROBUST_KINDS = ("long", "scen", "dual", "node", "single", "node_dual", "node_back")     # robust.online_matrix 的在线项
COMPARE_CONDITIONS = ("standard", "icmp_limit_5pct_new", "icmp_limit_5pct_known", "transient_15s",
                      "background_jitter_1ms", "busy_hour_drift_1ms")                   # compare.conditions 的条件名


def _plan(args) -> MeasurePlan:
    p = MeasurePlan()
    if getattr(args, "interval", None):
        p.interval_s = args.interval
    return p


def _mkparent(path):
    """输出文件的目录提前建好：不能等长时间的计算跑完才发现写不了。"""
    if path:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)


def _names(text, valid, opt):
    """逗号分隔的名称表，不认识的名称报错并列出可选值。"""
    xs = [x.strip() for x in text.split(",") if x.strip()]
    bad = [x for x in xs if x not in valid]
    if bad or not xs:
        raise SystemExit(f"edgefdl: {opt} 取值有误：{', '.join(bad) or repr(text)}；可选：{', '.join(valid)}")
    return xs


def _backend(kind, topo, plan, args, auto_dry=False):
    """auto_dry：LXD 后端在本机没有 lxc 时自动改为 dry run（仅 vnet 子命令；serve/baseline 等须真实执行）。"""
    from .vnet.base import make_backend
    kw = {}
    if kind == "sim":
        kw = dict(seed=getattr(args, "seed", 0), noise_prob=getattr(args, "noise", 0.0) or 0.0)
    elif kind == "live":
        from .vnet.live import load_endpoints
        try:
            kw = dict(lsdb_cmd=getattr(args, "lsdb_cmd", None),
                      endpoints=load_endpoints(getattr(args, "endpoints", None)),
                      infer_state=getattr(args, "infer_state", False),
                      trace_per_round=getattr(args, "trace_per_round", None))
        except (OSError, ValueError) as e:
            raise SystemExit(f"edgefdl: --endpoints 有误：{e}")
    elif getattr(args, "dry_run", False):
        kw = dict(dry_run=True)
    elif kind == "lxd":
        kw = dict(dry_run=None if auto_dry else False)
    return make_backend(kind, topo, plan, **kw)


def load_bundle(model_path: str):
    """模型文件自带拓扑、基线与测量计划，部署时只需一个文件。"""
    from .agent.ddqn import Agent
    from .agent.features import Baseline
    ag = Agent.load(model_path)
    meta = ag.meta
    topo = topomod.from_dict(meta["topology"])
    base = Baseline(**meta["baseline"])
    plan = MeasurePlan(**meta.get("plan", {}))
    return ag, topo, base, plan, meta


def _live_ready(b, args):
    """live 后端开始测量前：有地址不应答时停下并列出；--skip-silent 时这些地址按缺测处理。"""
    from .vnet.live import format_preflight, preflight
    rep = preflight(b)
    print(format_preflight(rep), flush=True)
    silent = [s["id"] for s in rep["silent"]]
    if silent and getattr(args, "skip_silent", False):
        if not rep["answered"]:             # 全部跳过后什么也测不到，却不会有任何告警
            raise SystemExit("edgefdl: 没有任何地址应答，--skip-silent 不适用：检查网管的路由、地址和 ICMP 策略")
        b.skip_targets(silent)
        print(f"--skip-silent：{len(silent)} 个不应答的地址不再探测，按缺测处理", flush=True)
        rep["problems"] = [p for p in rep["problems"] if "不应答" not in p]
    if rep["problems"]:
        raise SystemExit("edgefdl: 接入检查未通过；地址确实不回 ICMP 时可加 --skip-silent")


def _sinks(args):
    from .core.alarms import make_sinks
    try:                                    # 先校验告警通道，配置有误时在建虚拟网络之前退出
        return make_sinks(json.loads(args.sinks) if getattr(args, "sinks", None) else [])
    except ValueError as e:                 # json.JSONDecodeError 也是 ValueError
        raise SystemExit(f"edgefdl: --sinks 配置有误：{e}")


def build_engine(model_path, backend_kind, log_dir, args, plan_override=None, sinks=None):
    from .agent.features import FeatureEncoder
    from .agent.infer import Policy
    from .core.orchestrator import Engine
    sinks = _sinks(args) if sinks is None else sinks
    ag, topo, base, plan, meta = load_bundle(model_path)
    if backend_kind == "live":              # 模型包不决定现网上的探测量
        from .vnet.live import bundle_plan
        plan = bundle_plan(plan)
    if plan_override:
        plan = plan_override(plan)
    backend = _backend(backend_kind, topo, plan, args)
    backend.up()
    policy = Policy(ag, FeatureEncoder(topo, base), engine=getattr(args, "engine", "torch"))
    kw = {}
    if getattr(args, "single_alarm", False):
        kw["multi"] = False
    eng = Engine(topo, backend, policy, plan, log_dir, sinks=sinks, model_version=meta.get("version", ""),
                 confirm_n=getattr(args, "confirm", 2), **kw)
    return eng, topo, backend


# ---------------------------------------------------------------------- 子命令
def cmd_topo(args):
    t = topomod.load(args.topo)
    print(topomod.describe(t))
    unc = t.uncovered_links(t._base_targets())
    print(f"监测目标 {len(t.targets())} 个；盲区链路（已规划 NQA 探测目标）: {', '.join(unc) or '无'}")
    if args.out:
        t.save(args.out)
        print(f"已写出完整规划（含自动分配的接口/IP）: {args.out}")


def cmd_intake(args):
    from .intake.form import build_topology
    t = build_topology(args.form, configs=args.configs, lldp=args.lldp, llm=args.llm)
    t.save(args.out)
    print(topomod.describe(t))
    print(f"已生成拓扑: {args.out}")


def cmd_preflight(args):
    from .vnet.live import format_preflight, preflight
    if args.model:
        t = load_bundle(args.model)[1]
    else:
        t = topomod.load(args.topo)
    _mkparent(args.json)
    rep = preflight(_backend("live", t, _plan(args), args), count=args.count)
    print(format_preflight(rep))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=1)
    sys.exit(0 if rep["ok"] else 1)


def cmd_baseline(args):
    from .faults.generator import learn_baseline
    t = topomod.load(args.topo)
    b = _backend(args.backend, t, _plan(args), args)
    b.up()
    if b.read_only:
        _live_ready(b, args)
    elif hasattr(b, "wait_converged"):        # 真实协议栈：等 OSPF 收敛后再学习，避免把收敛前的不可达计入基线
        print(f"OSPF 收敛用时 {b.wait_converged():.1f} s", flush=True)
    base = learn_baseline(b, rounds=args.rounds)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    base.save(args.out)
    print(f"基线已保存: {args.out}")


def cmd_gen(args):
    """同一 --seed 得到同一数据集：仿真后端无论 --workers 几个、是否 --raw-csv，都按同样的分块与种子生成。"""
    from .agent.features import Baseline
    from .faults.generator import generate_on, generate_parallel
    t = topomod.load(args.topo)
    base = Baseline.load(args.baseline)
    t0 = time.time()
    rand = not getattr(args, "plain", False)
    if args.backend == "sim":
        d = generate_parallel(t, base, args.n, args.out, seed=args.seed, workers=args.workers, rand=rand,
                              raw_csv=args.raw_csv)
    else:
        b = _backend(args.backend, t, _plan(args), args)
        b.up()
        if hasattr(b, "wait_converged"):
            print(f"OSPF 收敛用时 {b.wait_converged():.1f} s", flush=True)
        d = generate_on(b, t, base, args.n, args.out, seed=args.seed, rand=rand, raw_csv=args.raw_csv)
    print(f"生成 {len(d['y_type'])} 个样本 -> {args.out}  ({time.time() - t0:.1f}s)，"
          f"触发率 {d['detected'].mean():.3f}")


def cmd_train(args):
    import torch
    from .agent.ddqn import Agent, AgentConfig, Dataset
    from .agent.features import Baseline
    _mkparent(args.out)
    torch.set_num_threads(args.threads)
    tr = Dataset(args.data)
    va = Dataset(args.val) if args.val else None
    cfg = AgentConfig(iters=args.iters, seed=args.seed)
    ag = Agent(tr.F1, tr.F2, tr.G, tr.L, tr.drift_links, cfg)
    print(f"训练集 {tr.N}（可训练 {tr.trainable().N}），链路 {tr.L}，动作数 {tr.L * 7 + 1}，"
          f"参数量 {sum(p.numel() for p in ag.net.parameters())}")
    hist = ag.train(tr.trainable(), log_every=args.log_every, val=va)
    t = topomod.load(args.topo)
    base = Baseline.load(args.baseline)
    meta = dict(version=args.version or time.strftime("ddqn-%Y%m%d-%H%M"), topology=t.to_dict(),
                baseline=base.to_dict(), plan=asdict(MeasurePlan()), train=hist,
                created=time.strftime("%Y-%m-%d %H:%M:%S"))
    if va is not None:
        meta["val"] = ag.evaluate(va)
    ag.save(args.out, meta)
    print(f"模型已保存: {args.out}")


def cmd_eval(args):
    from .agent.ddqn import Agent, Dataset
    from .report import eval_markdown
    _mkparent(args.json)
    ag = Agent.load(args.model)
    ds = Dataset(args.data)
    m = ag.evaluate(ds)
    print(eval_markdown(m))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False, indent=1)


def cmd_bench(args):
    from .bench import run_bench
    r = run_bench(args.model, args.out, rounds=args.rounds, scenarios=not args.no_scenarios, seed=args.seed)
    print(json.dumps(r["summary"], ensure_ascii=False, indent=1))


def cmd_robust(args):
    from .robust import run_robust
    kinds = _names(args.kinds, ROBUST_KINDS, "--kinds") if args.kinds else None
    ek = json.loads(args.engine) if args.engine else {}
    if args.single_alarm:
        ek["multi"] = False
    run_robust(args.model, args.out, workers=args.workers, n=args.n, sweep_reps=args.sweep_reps,
               rounds=args.rounds, dual_n=args.dual, node_reps=args.node_reps, only=args.only, seed=args.seed,
               ek=ek or None, kinds=kinds)


def cmd_compare(args):
    from .compare import TEST_SEEDS, run_compare
    conds = _names(args.conditions, COMPARE_CONDITIONS, "--conditions") if args.conditions else None
    seeds = [int(x) for x in args.seeds.split(",")] if args.seeds else TEST_SEEDS
    run_compare(args.model, args.out, workers=args.workers, seeds=seeds, rounds=args.rounds, val_rounds=args.val_rounds,
                dual_n=args.dual, node_reps=args.node_reps, conds=conds)


def _site_baseline(site, bundle):
    """现场基线里缺测的目标（不回 ICMP、盲区 NQA）取模型包基线：只用于区分链路远近端，这些目标仍按缺测处理。"""
    import numpy as np
    miss = np.isnan(site.rtt)
    site.rtt[miss], site.mdev[miss] = bundle.rtt[miss], bundle.mdev[miss]
    site.paths = [p if p else q for p, q in zip(site.paths, bundle.paths)]
    return site


def _bind(args):
    from .core.api import bind
    try:
        return bind(args.host, args.port)
    except (OSError, OverflowError) as e:
        raise SystemExit(f"edgefdl: 无法监听 {args.host}:{args.port}：{e}（端口被占用时用 --port 换一个）")


def cmd_serve(args):
    from .core.api import serve
    if args.backend == "live" and not (args.learn_rounds or args.baseline):
        raise SystemExit("edgefdl: live 后端须用现场基线：加 --learn-rounds（建议 ≥120）或 --baseline <文件>；"
                         "模型包自带的基线来自等价虚拟网络，与现场时延不同")
    sinks = _sinks(args)
    httpd = _bind(args)                     # 端口不可用时在建网络、写日志、开始测量之前退出；学习基线期间端口已占住、暂不应答
    try:
        eng, topo, backend = build_engine(args.model, args.backend, args.log_dir, args, sinks=sinks,
                                          plan_override=lambda p: (setattr(p, "interval_s", args.interval) or p)
                                          if args.interval else p)
        if backend.read_only:
            _live_ready(backend, args)
        if args.baseline:
            from .agent.features import Baseline
            eng.set_baseline(_site_baseline(Baseline.load(args.baseline), eng.enc.base))
        elif args.learn_rounds:
            from .faults.generator import learn_baseline
            if hasattr(backend, "wait_converged"):
                backend.wait_converged()
            print(f"上线基线学习：{args.learn_rounds} 轮（约 {args.learn_rounds * eng.plan.interval_s / 60:.0f} 分钟）…", flush=True)
            learned = learn_baseline(backend, rounds=args.learn_rounds)
            eng.set_baseline(_site_baseline(learned, eng.enc.base) if backend.read_only else learned)
        if backend.read_only:
            backend.set_reference(eng.enc.base.paths)
        serve(eng, topo, args.host, args.port, allow_inject=not args.no_inject and not backend.read_only, httpd=httpd)
        eng.start()
        print(f"边缘网管已启动：http://{args.host}:{args.port}  后端={backend.name}  日志={args.log_dir}", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            eng.stop()
            if backend.name == "sim":
                backend.down()
    finally:
        httpd.server_close()


def cmd_vnet(args):
    t = topomod.load(args.topo)
    b = _backend(args.backend, t, _plan(args), args, auto_dry=True)
    if args.action == "up":
        b.up()
    elif args.action == "down":
        b.down()
    if b.ex.dry_run and args.action != "status":
        why = "--dry-run" if args.dry_run else "本机没有 lxc 命令"
        print(f"# {b.name} {args.action}（dry run，{why}）：以下命令只打印、未执行", flush=True)
        for c in b.ex.log:
            print(c)
    else:
        print(json.dumps(b.status(), ensure_ascii=False, indent=1, default=str))


def cmd_pipeline(args):
    """一键：基线 → 训练集/测试集 → 训练 → 评估 → 指标自测。
    训练集、验证集做域随机化；独立测试集按标准条件生成（test），另附一份域随机化测试集（test_rand）。"""
    os.makedirs(args.out, exist_ok=True)
    o = lambda n: os.path.join(args.out, n)
    ns = argparse.Namespace
    cmd_baseline(ns(topo=args.topo, backend="sim", out=o("baseline.json"), rounds=40, seed=1, noise=0.0, interval=None))
    for name, n, seed, plain in (("train.npz", args.n, 1, False), ("val.npz", max(2000, args.n // 10), 2, False),
                                 ("test.npz", max(4000, args.n // 6), 3, True),
                                 ("test_rand.npz", max(4000, args.n // 6), 4, False)):
        cmd_gen(ns(topo=args.topo, baseline=o("baseline.json"), n=n, out=o(name), seed=seed, workers=args.workers,
                   backend="sim", raw_csv=None, noise=0.0, interval=None, plain=plain))
    cmd_train(ns(data=o("train.npz"), val=o("val.npz"), out=o("model.pt"), iters=args.iters, seed=0,
                 threads=args.threads, log_every=1000, topo=args.topo, baseline=o("baseline.json"), version=None))
    cmd_eval(ns(model=o("model.pt"), data=o("test.npz"), json=o("eval_test.json")))
    cmd_eval(ns(model=o("model.pt"), data=o("test_rand.npz"), json=o("eval_test_rand.json")))
    if not args.no_bench:
        cmd_bench(ns(model=o("model.pt"), out=o("bench"), rounds=args.bench_rounds, no_scenarios=False, seed=7))


def _live_args(p, skip: bool = True):
    p.add_argument("--lsdb-cmd", help="live 后端：输出 FRR show ip ospf database router json 的 shell 命令；缺省时 LSDB 特征按缺测处理")
    p.add_argument("--endpoints", help="live 后端：探测端点 YAML；缺省时带宽估计与精测按缺测处理")
    p.add_argument("--infer-state", action="store_true",
                   help="live 后端：没有 --lsdb-cmd 时由 ping 与 traceroute 推断链路中断和开销变化，填入 LSDB 特征")
    p.add_argument("--trace-per-round", type=int,
                   help="live 后端：每轮 traceroute 几个目标（轮转，默认每秒 1 个，适应设备的 ICMP 限速；0 表示全部）")
    if skip:
        p.add_argument("--skip-silent", action="store_true", help="live 后端：不应答的地址不再探测，按缺测处理（默认停止并列出）")


def _hint(args):
    if getattr(args, "backend", "") == "netns" and os.geteuid() != 0:
        return "\n（netns 后端需要 root；没有 root 时用 scripts/lab.sh run <命令>，见 docs/quickstart.md 第 6 节）"
    return ""


def main(argv=None):
    from . import __version__
    ap = argparse.ArgumentParser(prog="edgefdl", description="边缘网络故障自主定位系统")
    ap.add_argument("-V", "--version", action="version", version=f"edgefdl {__version__}")
    sp = ap.add_subparsers(dest="cmd", required=True)

    p = sp.add_parser("topo", help="查看拓扑 / 输出自动规划结果")
    p.add_argument("topo", nargs="?", default=DEFAULT_TOPO)
    p.add_argument("-o", "--out")
    p.set_defaults(fn=cmd_topo)

    p = sp.add_parser("intake", help="需求采集：采集表 + 设备配置/LLDP → 拓扑")
    p.add_argument("--form", required=True)
    p.add_argument("--configs", help="设备配置/命令输出目录（display/show 文本）")
    p.add_argument("--lldp", help="LLDP 邻居表 CSV")
    p.add_argument("--llm", help="OpenAI 兼容接口地址，用于拓扑图片/非结构化文档提取（可选）")
    p.add_argument("-o", "--out", required=True)
    p.set_defaults(fn=cmd_intake)

    p = sp.add_parser("baseline", help="在虚拟网络学习正常基线")
    p.add_argument("--topo", default=DEFAULT_TOPO)
    p.add_argument("--backend", default="sim", choices=["sim", "netns", "lxd", "live"])
    p.add_argument("--rounds", type=int, default=40)
    p.add_argument("--seed", type=int, default=1)
    _live_args(p)
    p.add_argument("--interval", type=float, help="测量周期 s（live 后端基线学习按此定速，默认 5s）")
    p.add_argument("-o", "--out", required=True)
    p.set_defaults(fn=cmd_baseline)

    p = sp.add_parser("preflight", help="只读检查接入真实网络的条件：地址应答、traceroute、LSDB 命令、探测端点")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--model", help="模型包（取其中的拓扑）")
    g.add_argument("--topo", help="拓扑文件")
    p.add_argument("--count", type=int, default=3, help="每个地址 ping 几个包")
    p.add_argument("--json", help="同时写出 JSON 报告")
    _live_args(p, skip=False)
    p.set_defaults(fn=cmd_preflight)

    p = sp.add_parser("gen", help="全量故障样本生成")
    p.add_argument("--topo", default=DEFAULT_TOPO)
    p.add_argument("--baseline", required=True)
    p.add_argument("--n", type=int, default=60000)
    p.add_argument("--out", required=True)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--backend", default="sim", choices=["sim", "netns", "lxd"])
    p.add_argument("--raw-csv", help="同时输出逐轮原始测量 CSV")
    p.add_argument("--plain", action="store_true", help="按标准条件生成（不做域随机化），用于独立测试集")
    p.set_defaults(fn=cmd_gen)

    p = sp.add_parser("train", help="训练 DDQN 智能体")
    p.add_argument("--data", required=True)
    p.add_argument("--val")
    p.add_argument("--topo", default=DEFAULT_TOPO)
    p.add_argument("--baseline", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--iters", type=int, default=15000)
    p.add_argument("--threads", type=int, default=8)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--log-every", type=int, default=1000)
    p.add_argument("--version")
    p.set_defaults(fn=cmd_train)

    p = sp.add_parser("eval", help="分类别评估")
    p.add_argument("--model", required=True)
    p.add_argument("--data", required=True)
    p.add_argument("--json")
    p.set_defaults(fn=cmd_eval)

    p = sp.add_parser("bench", help="指标自测：推理时延/带宽/资源/抗干扰/场景测试")
    p.add_argument("--model", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--rounds", type=int, default=17280, help="抗干扰长跑轮数（默认 = 24h @5s）")
    p.add_argument("--no-scenarios", action="store_true")
    p.add_argument("--seed", type=int, default=7)
    p.set_defaults(fn=cmd_bench)

    p = sp.add_parser("robust", help="鲁棒性测试：强干扰/测量缺陷/故障幅度/背景波动/多故障")
    p.add_argument("--model", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--workers", type=int, default=32)
    p.add_argument("--n", type=int, default=4000, help="离线每种条件的样本数")
    p.add_argument("--sweep-reps", type=int, default=20, help="幅度扫描每条链路每个取值的样本数")
    p.add_argument("--rounds", type=int, default=17280, help="在线长跑轮数（默认 = 24h @5s）")
    p.add_argument("--dual", type=int, default=300, help="双链路故障试验次数")
    p.add_argument("--node-reps", type=int, default=10, help="每台设备宕机试验次数")
    p.add_argument("--only", choices=["online", "offline"])
    p.add_argument("--engine", help='总控参数 JSON，如 {"confirm_gap": 2, "track": false}')
    p.add_argument("--seed", type=int, help="随机种子（默认取环境变量 EDGEFDL_ROBUST_SEED，再缺省 21）")
    p.add_argument("--single-alarm", action="store_true", help="总控退化为单告警（同一时刻至多一条活动告警），用于对照/回退")
    p.add_argument("--kinds", help="只跑指定在线项，逗号分隔：" + ",".join(ROBUST_KINDS))
    p.set_defaults(fn=cmd_robust)

    p = sp.add_parser("compare", help="同台对比：同一测量流上回放现有方法并与在线引擎比较")
    p.add_argument("--model", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--workers", type=int, default=24)
    p.add_argument("--seeds", help="测试种子，逗号分隔（默认 1001~1005）")
    p.add_argument("--rounds", type=int, default=17280, help="正常运行轮数（默认 = 24h @5s）")
    p.add_argument("--val-rounds", type=int, default=4320, help="验证集每个条件的正常运行轮数")
    p.add_argument("--dual", type=int, default=60, help="每个种子的双故障试验次数")
    p.add_argument("--node-reps", type=int, default=2, help="每个种子每台设备的宕机试验次数")
    p.add_argument("--conditions", help="只跑指定条件，逗号分隔：" + ",".join(COMPARE_CONDITIONS))
    p.set_defaults(fn=cmd_compare)

    p = sp.add_parser("serve", help="在线运行网管系统 + Web 大屏")
    p.add_argument("--model", required=True)
    p.add_argument("--backend", default="sim", choices=["sim", "netns", "lxd", "live"])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8460)
    p.add_argument("--interval", type=float, help="测量周期 s（默认取模型内计划，5s）")
    p.add_argument("--log-dir", default="runs/live")
    p.add_argument("--noise", type=float, default=0.1, help="仿真后端每轮瞬时噪声概率")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--engine", default="torch", choices=["torch", "numpy"])
    p.add_argument("--confirm", type=int, default=2, help="连续几次一致研判才发告警")
    p.add_argument("--sinks", help='告警外发 JSON，如 [{"type":"syslog","host":"10.0.0.5"}]')
    p.add_argument("--no-inject", action="store_true", help="禁用演示用故障注入接口")
    p.add_argument("--single-alarm", action="store_true", help="单告警模式：同一时刻至多一条活动告警（回退开关）")
    p.add_argument("--learn-rounds", type=int, default=0,
                   help="上线前在现场网络学习基线的轮数（接入物理网络时建议 ≥120，即 10 分钟；0 表示沿用模型包基线）")
    p.add_argument("--baseline", help="用已保存的现场基线（edgefdl baseline --backend live），不重新学习")
    _live_args(p)
    p.set_defaults(fn=cmd_serve)

    p = sp.add_parser("vnet", help="等价虚拟网络 up/down/status")
    p.add_argument("action", choices=["up", "down", "status"])
    p.add_argument("--topo", default=DEFAULT_TOPO)
    p.add_argument("--backend", default="netns", choices=["netns", "lxd"])
    p.add_argument("--dry-run", action="store_true",
                   help="只打印 up/down 的命令序列，不执行、不改动本机（netns 与 lxd 均可）")
    p.set_defaults(fn=cmd_vnet)

    p = sp.add_parser("pipeline", help="一键 基线→样本→训练→评估→自测")
    p.add_argument("--topo", default=DEFAULT_TOPO)
    p.add_argument("--out", default="runs/demo")
    p.add_argument("--n", type=int, default=60000)
    p.add_argument("--iters", type=int, default=15000)
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--threads", type=int, default=8)
    p.add_argument("--bench-rounds", type=int, default=17280)
    p.add_argument("--no-bench", action="store_true")
    p.set_defaults(fn=cmd_pipeline)

    args = ap.parse_args(argv)
    if getattr(args, "topo", None) == DEFAULT_TOPO and not os.path.isfile(DEFAULT_TOPO):
        raise SystemExit("edgefdl: 找不到默认拓扑 configs/demo_topology.yaml（它在源码仓库中，不随 Python 包安装）。"
                         "请在命令行指定拓扑文件，或在仓库目录下用 python -m edgefdl 运行")
    try:
        args.fn(args)
    except (OSError, RuntimeError, ValueError, OverflowError, yaml.YAMLError) as e:     # 文件缺失、权限等常见错误不打印调用栈
        raise SystemExit(f"edgefdl: {e}{_hint(args)}") from None


if __name__ == "__main__":
    main()
