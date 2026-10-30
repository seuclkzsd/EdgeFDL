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
  serve   → 网管在线运行（测量 + 研判 + 告警 + Web 大屏）
pipeline 一键串起 baseline→gen→train→eval→bench。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict

from . import topology as topomod
from .measure.model import MeasurePlan

DEFAULT_TOPO = os.path.join(os.path.dirname(__file__), "..", "configs", "demo_topology.yaml")


def _plan(args) -> MeasurePlan:
    p = MeasurePlan()
    if getattr(args, "interval", None):
        p.interval_s = args.interval
    return p


def _backend(kind, topo, plan, args, auto_dry=False):
    """auto_dry：LXD 后端在本机没有 lxc 时自动改为 dry run（仅 vnet 子命令；serve/baseline 等须真实执行）。"""
    from .vnet.base import make_backend
    kw = {}
    if kind == "sim":
        kw = dict(seed=getattr(args, "seed", 0), noise_prob=getattr(args, "noise", 0.0) or 0.0)
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


def build_engine(model_path, backend_kind, log_dir, args, plan_override=None):
    from .agent.features import FeatureEncoder
    from .agent.infer import Policy
    from .core.alarms import make_sinks
    from .core.orchestrator import Engine
    try:                                    # 先校验告警通道，配置有误时在建虚拟网络之前退出
        sinks = make_sinks(json.loads(args.sinks) if getattr(args, "sinks", None) else [])
    except ValueError as e:                 # json.JSONDecodeError 也是 ValueError
        raise SystemExit(f"edgefdl: --sinks 配置有误：{e}")
    ag, topo, base, plan, meta = load_bundle(model_path)
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


def cmd_baseline(args):
    from .faults.generator import learn_baseline
    t = topomod.load(args.topo)
    b = _backend(args.backend, t, _plan(args), args)
    b.up()
    if hasattr(b, "wait_converged"):          # 真实协议栈：等 OSPF 收敛后再学习，避免把收敛前的不可达计入基线
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
    ek = json.loads(args.engine) if args.engine else {}
    if args.single_alarm:
        ek["multi"] = False
    run_robust(args.model, args.out, workers=args.workers, n=args.n, sweep_reps=args.sweep_reps,
               rounds=args.rounds, dual_n=args.dual, node_reps=args.node_reps, only=args.only, seed=args.seed,
               ek=ek or None, kinds=args.kinds.split(",") if args.kinds else None)


def cmd_serve(args):
    from .core.api import serve
    eng, topo, backend = build_engine(args.model, args.backend, args.log_dir, args,
                                      plan_override=lambda p: (setattr(p, "interval_s", args.interval) or p)
                                      if args.interval else p)
    if args.learn_rounds:
        from .faults.generator import learn_baseline
        if hasattr(backend, "wait_converged"):
            backend.wait_converged()
        print(f"上线基线学习：{args.learn_rounds} 轮（约 {args.learn_rounds * eng.plan.interval_s / 60:.0f} 分钟）…", flush=True)
        eng.set_baseline(learn_baseline(backend, rounds=args.learn_rounds))
    eng.start()
    httpd = serve(eng, topo, args.host, args.port, allow_inject=not args.no_inject)
    print(f"边缘网管已启动：http://{args.host}:{args.port}  后端={backend.name}  日志={args.log_dir}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        eng.stop()
        if backend.name == "sim":
            backend.down()


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
    p.add_argument("--backend", default="sim", choices=["sim", "netns", "lxd"])
    p.add_argument("--rounds", type=int, default=40)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("-o", "--out", required=True)
    p.set_defaults(fn=cmd_baseline)

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
    p.add_argument("--kinds", help="只跑指定在线项，逗号分隔：long,scen,dual,node,single,node_dual,node_back")
    p.set_defaults(fn=cmd_robust)

    p = sp.add_parser("serve", help="在线运行网管系统 + Web 大屏")
    p.add_argument("--model", required=True)
    p.add_argument("--backend", default="sim", choices=["sim", "netns", "lxd"])
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
    args.fn(args)


if __name__ == "__main__":
    main()
