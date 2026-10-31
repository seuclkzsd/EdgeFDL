# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""在线轻量化推理：一次研判 = 1~4 次前向（每次 <1ms），按需调用精测。

时延统计（latency_stats，即 /api/status 的 infer 与 /metrics 的 edgefdl_infer_ms）按单次前向计，一次研判贡献
1~4 个样本；研判结果中 infer_ms 是其中最慢的一次前向，infer_ms_total 是各次之和（不含特征计算 feature_ms）。

另提供与 QNet 等价的纯 numpy 前向（--engine numpy）：只替换前向计算，模型包仍由 PyTorch 读取（torch.load），
运行环境仍需安装 PyTorch。

非有限值：精测未测得的量（无应答时的 RTT、iperf3 失败时的带宽，记为 NaN）在研判结果的 probes 中为 None；
带宽 NaN 在编码精测特征前按"未测得"处理（无应答记 0，与仿真后端一致；有应答则视同基线，不作带宽证据）；
个别前向输出非有限 Q 值时，这些动作视同屏蔽，全部无效则研判为正常（置信度 0），不据此告警。
"""
from __future__ import annotations

import dataclasses
import math
import time
from typing import Callable, Dict, List, Optional, Sequence

import numpy as np
import torch

from ..faults.model import FAULT_TYPES, TYPE_LABEL
from ..measure.model import DeepProbe, RoundResult
from .ddqn import K, PROBE, Agent, action_mask
from .features import LIGHT_FEATURES, FeatureEncoder, ResidualRef, link_evidence


def unlog(x: float) -> float:
    return math.copysign(math.expm1(abs(x)), x)


def _num(x):
    """对外记录的数值：NaN/±Inf（未测得）记为 None。"""
    x = float(x)
    return x if math.isfinite(x) else None


Q_MASKED = -1e4                                 # 屏蔽动作的 Q 值（与 NumpyQNet / action_mask 一致）


class NumpyQNet:
    """与 QNet 等价的 numpy 前向（ReLU MLP）：权重取自已加载的 QNet，前向计算不经 PyTorch。"""

    def __init__(self, weights: Dict[str, np.ndarray]):
        self.w = weights

    @classmethod
    def from_torch(cls, net: torch.nn.Module) -> "NumpyQNet":
        return cls({k: v.detach().cpu().numpy() for k, v in net.state_dict().items()})

    def _lin(self, x, name):
        return x @ self.w[name + ".weight"].T + self.w[name + ".bias"]

    def __call__(self, light, deep, probed, glob, step, mask):
        B, L, _ = light.shape
        p = probed[..., None]
        ctx = np.broadcast_to(np.concatenate([glob, step[:, None]], -1)[:, None, :], (B, L, glob.shape[1] + 1))
        x = np.concatenate([light, deep * p, p, ctx], -1)
        h = np.maximum(self._lin(np.maximum(self._lin(x, "enc.0"), 0), "enc.2"), 0)
        pooled = np.concatenate([h.mean(1), h.max(1)], -1)
        hp = np.concatenate([h, np.broadcast_to(pooled[:, None, :], (B, L, pooled.shape[1]))], -1)
        a_link = self._lin(np.maximum(self._lin(hp, "head.0"), 0), "head.2").reshape(B, L * K)
        a_norm = self._lin(np.maximum(self._lin(pooled, "a_norm.0"), 0), "a_norm.2")
        adv = np.concatenate([a_link, a_norm], -1)
        m = mask.astype(np.float32)
        adv = adv - (adv * m).sum(-1, keepdims=True) / np.maximum(m.sum(-1, keepdims=True), 1)
        q = self._lin(np.maximum(self._lin(pooled, "v.0"), 0), "v.2") + adv
        return np.where(mask, q, -1e4)

    def save(self, path: str):
        np.savez(path, **self.w)


class Policy:
    def __init__(self, agent: Agent, enc: FeatureEncoder, engine: str = "torch"):
        self.agent = agent
        self.enc = enc
        self.topo = enc.topo
        self.L = enc.L
        self.engine = engine
        self.np_net = NumpyQNet.from_torch(agent.net) if engine == "numpy" else None
        torch.set_num_threads(1)              # 推理单线程：时延稳定、资源占用低
        self.latencies: List[float] = []
        self.masked: List[int] = []           # 本部署测量不到、因而不可取的动作（见 set_capabilities）
        self._warmup()

    def set_capabilities(self, noprobe: Sequence[str] = (), nobw: Sequence[str] = ()):
        """真实网络上测量能力可能不全：不能精测的链路屏蔽其精测动作，与"无精测能力"时一样在判定动作中取最优
        （不能把"未测"编码成精测结果：无应答的精测表示两端不通）；没有带宽估计的链路屏蔽带宽劣化结论，
        否则结论既无从证实也无从恢复。虚拟后端两者皆空，研判不变。"""
        self.masked = [self.enc.lidx[l] * K + PROBE for l in noprobe] + \
                      [self.enc.lidx[l] * K + FAULT_TYPES.index("bandwidth") for l in nobw]

    def keepalive(self):
        """保活前向（不计时）：研判数分钟才发生一次，内存紧张时推理库代码页会被系统回收，
        下一次研判因缺页而出现秒级尖峰；每轮一次空前向（约 0.5ms）使其常驻。"""
        self._warmup(1)

    def _warmup(self, n: int = 3):
        """首次前向含算子库初始化（约 1s），启动时预热，不计入时延统计。"""
        L, (F1, F2, G) = self.L, self.enc.dims
        for _ in range(n):
            self._q(np.zeros((L, F1), np.float32), np.zeros((L, F2), np.float32),
                    np.zeros(L, dtype=bool), np.zeros(G, np.float32), 0)

    def _q(self, light, deep, probed, glob, step):
        mask = action_mask(self.agent.base_mask, torch.as_tensor(probed[None]),
                           torch.tensor([step]), self.agent.cfg.max_steps)
        stepf = np.array([step / self.agent.cfg.max_steps], dtype=np.float32)
        if self.np_net is not None:
            return self.np_net(light[None], deep[None], probed[None].astype(np.float32), glob[None], stepf,
                               mask.numpy())[0]
        with torch.no_grad():
            q = self.agent.net(torch.as_tensor(light[None]), torch.as_tensor(deep[None]),
                               torch.as_tensor(probed[None].astype(np.float32)), torch.as_tensor(glob[None]),
                               torch.as_tensor(stepf), mask)
        return q[0].numpy()

    def _finite_bw(self, dp: DeepProbe, li: int) -> DeepProbe:
        """精测带宽未测得（NaN，真实协议栈 iperf3 失败）：无应答记 0（仿真后端链路不通时即为 0），
        有应答记为基线带宽（不作带宽证据）。其余情况原样返回。"""
        if math.isfinite(dp.bw_mbps):
            return dp
        return dataclasses.replace(dp, bw_mbps=0.0 if dp.recv == 0 else float(self.enc.base.deep_bw[li]))

    def decode(self, a: int):
        if a == self.L * K:
            return "normal", None
        l, k = divmod(a, K)
        return ("probe" if k == PROBE else FAULT_TYPES[k]), self.enc.lids[l]

    def diagnose(self, window: Sequence[RoundResult], probe_fn: Optional[Callable[[str], DeepProbe]] = None,
                 ref: Optional[ResidualRef] = None, gate: bool = False, exclude=()) -> Dict:
        """ref：残差研判（扣除已确认告警效应后再判一次）；exclude：不参与决策（含精测）的链路，即已告警链路，
        研判"其余链路还有没有故障"；gate：交叉错配校验——首选结论在其链路上缺少该类型的直接证据时，
        改取前 3 个候选中第一个有证据的（都没有则保留首选，evident=False）；可传入自定义校验
        gate(该链路轻量特征, 类型, 精测特征或 None, 链路)。"""
        ex = [self.enc.lidx[l] * K + k for l in exclude for k in range(K)] + self.masked
        t_all = time.perf_counter()
        light, glob = self.enc.light(window, ref)
        feat_ms = (time.perf_counter() - t_all) * 1000
        deep = np.zeros((self.L, self.enc.dims[1]), dtype=np.float32)
        probed = np.zeros(self.L, dtype=bool)
        probes, infer = [], []
        for step in range(self.agent.cfg.max_steps):
            t0 = time.perf_counter()
            q = self._q(light, deep, probed, glob, step)
            if not np.isfinite(q).all():          # 输入含非有限值时的防护：非有限 Q 视同屏蔽，全部无效取"正常"
                q = np.where(np.isfinite(q), q, Q_MASKED)
                if not (q > Q_MASKED).any():
                    q[self.L * K] = Q_MASKED + 1
            if ex:
                q[ex] = -1e4
            infer.append((time.perf_counter() - t0) * 1000)
            a = int(q.argmax())
            kind, lid = self.decode(a)
            if kind == "probe" and probe_fn is not None:
                dp = probe_fn(lid)
                li = self.enc.lidx[lid]
                deep[li] = self.enc.deep(self._finite_bw(dp, li), ref)
                probed[li] = True
                probes.append(dict(link=lid, loss=_num(dp.loss), rtt=_num(dp.rtt_avg), mdev=_num(dp.rtt_mdev),
                                   bw=_num(dp.bw_mbps)))
                continue
            if kind == "probe":             # 无精测能力时退化为在判定动作中取最优
                q2 = q.copy()
                q2[[i * K + PROBE for i in range(self.L)]] = -1e4
                a = int(q2.argmax())
                kind, lid = self.decode(a)
            break
        diag_q = q.copy()
        diag_q[[i * K + PROBE for i in range(self.L)]] = -1e4
        order = np.argsort(-diag_q)[:3]
        evident = True
        if gate and kind != "normal":
            chk = gate if callable(gate) else link_evidence
            ok = lambda k, l: chk(light[self.enc.lidx[l]], k, deep[self.enc.lidx[l]] if probed[self.enc.lidx[l]] else None,
                                  l)
            evident = ok(kind, lid)
            if not evident:
                for c in order:
                    ck, cl = self.decode(int(c))
                    if ck != "normal" and ok(ck, cl):
                        a, kind, lid, evident = int(c), ck, cl, True
                        break
        cands = []
        for c in order:
            ck, cl = self.decode(int(c))
            cands.append(dict(type=ck, link=cl, q=round(float(diag_q[c]), 3)))
        qbest = float(q[a])
        conf = float(np.clip((qbest + 1) / 2, 0, 1))
        self.latencies.extend(infer)
        if len(self.latencies) > 5000:
            self.latencies = self.latencies[-5000:]
        res = dict(type=kind, type_cn=TYPE_LABEL.get(kind, kind), link=lid, q=round(qbest, 3),
                   confidence=round(conf, 3), candidates=cands, probes=probes,
                   infer_ms=round(max(infer), 3), infer_ms_total=round(sum(infer), 3),
                   feature_ms=round(feat_ms, 3), steps=len(infer))
        if gate:
            res["evident"] = evident
        if lid is not None:
            res["evidence"] = self.evidence(light, deep, probed, lid, kind)
        return res

    # 各故障类型展示的证据类别（其余类别即使有数值也不展示，避免绕行等次生现象干扰判读）
    EVIDENCE_KINDS = {
        "link_down": ("unreach", "adj", "paths"),
        "route_drift": ("cost", "paths", "adj"),
        "delay": ("rtt",), "loss": ("loss",), "jitter": ("jitter", "rtt"), "bandwidth": ("bw",),
    }

    def evidence(self, light, deep, probed, lid, kind: str = "") -> List[str]:
        li = self.enc.lidx[lid]
        f = dict(zip(LIGHT_FEATURES, light[li].tolist()))
        l = self.topo.link(lid)
        b = self.enc.base
        cand = []
        if f["end_unreach_both"] > 0.5:
            cand.append(("unreach", f"链路两端接口地址 {l.a_ip.split('/')[0]} / {l.b_ip.split('/')[0]} 均不可达"))
        elif f["end_unreach_mean"] > 0.3:
            cand.append(("unreach", "链路一端接口地址不可达"))
        if f["adj_down"] > 0.5:
            cand.append(("adj", f"OSPF 邻接 {l.a}↔{l.b} 断开"))
        if abs(f["cost_ratio"]) > 0.05:
            cand.append(("cost", f"OSPF 开销由基线 {b.cost[li]:g} 变为约 {b.cost[li] * math.exp(f['cost_ratio']):.0f}"))
        if f["paths_lost"] > 0.05:
            cand.append(("paths", f"基线经过该链路的监测路径中 {f['paths_lost']:.0%} 已改道"))
        if f["diff_drtt"] > 0.7:
            cand.append(("rtt", f"远端接口相对近端 RTT 增加 {unlog(f['diff_drtt']):.1f} ms"))
        if f["tomo_drtt"] > 0.7:
            cand.append(("rtt", f"经过该链路的目标 RTT 比其余目标平均高 {unlog(f['tomo_drtt']):.1f} ms"))
        if f["diff_loss"] > 0.02 or f["tomo_loss"] > 0.02:
            cand.append(("loss", f"经过该链路的目标丢包率高出 {max(f['diff_loss'], f['tomo_loss']):.1%}"))
        if f["diff_djit"] > 0.5 or f["tomo_djit"] > 0.5:
            cand.append(("jitter", f"抖动比基线增加约 {unlog(max(f['diff_djit'], f['tomo_djit'])):.1f} ms"))
        if f["has_bw"] > 0.5 and f["bw_ratio"] < -0.2:
            cand.append(("bw", f"轻量带宽估计为基线的 {math.exp(f['bw_ratio']):.0%}"))
        keep = self.EVIDENCE_KINDS.get(kind)
        ev = [t for c, t in cand if keep is None or c in keep]
        if probed[li]:
            d = deep[li]
            if d[1] > 0.5:
                ev.append("精测：链路两端互 ping 不通")
            else:
                ev.append(f"精测：丢包 {d[2]:.1%}，RTT {'+' if d[3] >= 0 else ''}{unlog(float(d[3])):.1f} ms，"
                          f"抖动 +{max(0.0, unlog(float(d[4]))):.1f} ms，带宽为基线的 {math.exp(float(d[5])):.0%}")
        return ev or ["多维特征综合研判"]

    def latency_stats(self) -> Dict:
        if not self.latencies:
            return {}
        x = np.array(self.latencies)
        return dict(n=len(x), mean_ms=round(float(x.mean()), 3), p99_ms=round(float(np.percentile(x, 99)), 3),
                    max_ms=round(float(x.max()), 3))
