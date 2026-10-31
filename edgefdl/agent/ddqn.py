# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""改进型 DQN 故障定位智能体。

改进点（相对原始 DQN）：
  1. Double DQN：在线网络选动作、目标网络估值，抑制 Q 值过估计；
  2. Dueling：状态价值 V(s) 与动作优势 A(s,a) 分离；
  3. 链路共享编码器（置换等变）：每条链路用同一套权重编码，参数量与拓扑规模无关、样本效率高；
  4. 主动精测动作：除"判定(链路, 故障类型)"与"判定正常"外，智能体可选择"精测某链路"，
     奖励中计入测量开销，从而学会只在证据不足时花带宽；
  5. 优先经验回放（PER）+ 动作掩码（不可行动作不参与选择与目标计算）。

动作编号：a = l*K + k，k∈[0,6) 为对链路 l 判定第 k 类故障、k=6 为精测链路 l；a = L*K 为"判定正常"。
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Dict, Optional

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as Fn

from ..faults.model import CATEGORY, FAULT_TYPES

NT = len(FAULT_TYPES)
K = NT + 1                 # 每链路动作数：6 类判定 + 1 个精测
PROBE = NT
NORMAL_T = NT              # 标签中的正常类


@dataclass
class AgentConfig:
    hidden: int = 64
    max_steps: int = 4               # 每次研判最多 3 次精测 + 1 次判定
    probe_cost: float = 0.05
    gamma: float = 0.97
    lr: float = 1e-3
    batch: int = 512
    n_env: int = 512
    replay: int = 300_000
    iters: int = 12_000
    eps_start: float = 1.0
    eps_end: float = 0.02
    eps_frac: float = 0.4
    tau: float = 0.01
    per_alpha: float = 0.6
    per_beta0: float = 0.4
    reward_loc_only: float = 0.2     # 定位对、类型错
    reward_type_only: float = -0.4   # 类型对、定位错
    seed: int = 0


def mlp(i, h, o):
    return nn.Sequential(nn.Linear(i, h), nn.ReLU(), nn.Linear(h, o))


class QNet(nn.Module):
    def __init__(self, f_light: int, f_deep: int, f_glob: int, hidden: int = 64):
        super().__init__()
        d = f_light + f_deep + 1 + f_glob + 1
        self.enc = nn.Sequential(nn.Linear(d, hidden), nn.ReLU(), nn.Linear(hidden, hidden), nn.ReLU())
        self.head = mlp(hidden * 3, hidden, K)
        self.v = mlp(hidden * 2, hidden, 1)
        self.a_norm = mlp(hidden * 2, hidden, 1)

    def forward(self, light, deep, probed, glob, step, mask):
        B, L, _ = light.shape
        p = probed.unsqueeze(-1)
        ctx = torch.cat([glob, step.unsqueeze(-1)], -1).unsqueeze(1).expand(B, L, -1)
        h = self.enc(torch.cat([light, deep * p, p, ctx], -1))
        pooled = torch.cat([h.mean(1), h.max(1).values], -1)
        a_link = self.head(torch.cat([h, pooled.unsqueeze(1).expand(B, L, -1)], -1)).reshape(B, L * K)
        adv = torch.cat([a_link, self.a_norm(pooled)], -1)
        m = mask.float()
        adv = adv - (adv * m).sum(-1, keepdim=True) / m.sum(-1, keepdim=True).clamp(min=1)
        q = self.v(pooled) + adv
        return q.masked_fill(~mask, -1e4)


class Dataset:
    """训练/测试样本（torch 张量），由 faults/generator 产出的 npz 加载。"""

    def __init__(self, path_or_dict, device="cpu"):
        d = np.load(path_or_dict, allow_pickle=False) if isinstance(path_or_dict, str) else path_or_dict
        f = lambda k, dt=torch.float32: torch.as_tensor(np.asarray(d[k]), dtype=dt, device=device)
        self.light, self.glob, self.deep = f("light"), f("glob"), f("deep")
        self.y_type, self.y_link = f("y_type", torch.long), f("y_link", torch.long)
        self.y_value = f("y_value")
        self.noisy = f("noisy", torch.bool)
        n = len(d["y_type"])
        # 触发对齐采样的检测结果；旧数据集没有这两列时视为全部触发
        self.detected = f("detected", torch.bool) if "detected" in d else torch.ones(n, dtype=torch.bool)
        self.det_rounds = f("det_rounds", torch.long) if "det_rounds" in d else torch.zeros(n, dtype=torch.long)
        self.link_ids = [str(x) for x in d["link_ids"]]
        self.drift_links = np.asarray(d["drift_links"], dtype=int)
        self.N, self.L, self.F1 = self.light.shape
        self.F2, self.G = self.deep.shape[2], self.glob.shape[1]
        self.device = device

    KEYS = ("light", "glob", "deep", "y_type", "y_link", "y_value", "noisy", "detected", "det_rounds")

    def subset(self, mask) -> "Dataset":
        """按样本掩码取子集（训练时剔除"故障已注入但判决器未触发"的样本）。"""
        import copy
        ds = copy.copy(self)
        for k in self.KEYS:
            setattr(ds, k, getattr(self, k)[mask])
        ds.N = int(ds.light.shape[0])
        return ds

    def trainable(self) -> "Dataset":
        return self.subset(self.detected | (self.y_type == NORMAL_T))

    def to(self, device):
        for k in self.KEYS:
            setattr(self, k, getattr(self, k).to(device))
        self.device = device
        return self


def static_mask(L: int, drift_links, device="cpu") -> torch.Tensor:
    m = torch.ones(L * K + 1, dtype=torch.bool, device=device)
    allowed = set(int(x) for x in drift_links)
    for l in range(L):
        if l not in allowed:
            m[l * K + 5] = False           # route_drift 只对路由器间链路有意义
    return m


def action_mask(base_mask, probed, step, max_steps):
    """probed: (B,L) bool；step: (B,) long。"""
    B, L = probed.shape
    m = base_mask.unsqueeze(0).expand(B, -1).clone()
    pm = m[:, :L * K].reshape(B, L, K)
    can_probe = (~probed) & (step < max_steps - 1).unsqueeze(-1)
    pm[:, :, PROBE] &= can_probe
    return m


class Agent:
    def __init__(self, F1, F2, G, L, drift_links, cfg: Optional[AgentConfig] = None, device="cpu"):
        self.cfg = cfg or AgentConfig()
        self.F1, self.F2, self.G, self.L = F1, F2, G, L
        self.drift_links = list(int(x) for x in drift_links)
        self.device = device
        with torch.random.fork_rng(devices=[]):      # 权重初始化按 cfg.seed 确定，且不扰动全局随机状态
            torch.manual_seed(self.cfg.seed)
            self.net = QNet(F1, F2, G, self.cfg.hidden).to(device)
            self.tgt = QNet(F1, F2, G, self.cfg.hidden).to(device)
        self.tgt.load_state_dict(self.net.state_dict())
        self.base_mask = static_mask(L, self.drift_links, device)

    # ------------------------------------------------------------------ 观测
    def obs(self, ds: Dataset, idx, probed, step):
        light = ds.light[idx]
        deep = ds.deep[idx]
        glob = ds.glob[idx]
        stepf = step.float() / self.cfg.max_steps
        mask = action_mask(self.base_mask, probed, step, self.cfg.max_steps)
        return light, deep, probed.float(), glob, stepf, mask

    def reward(self, ds: Dataset, idx, a):
        cfg = self.cfg
        yt, yl = ds.y_type[idx], ds.y_link[idx]
        is_norm_a = a == self.L * K
        l, k = a // K, a % K
        probe = (~is_norm_a) & (k == PROBE)
        r = torch.full(a.shape, -1.0, device=a.device)
        fault = yt != NORMAL_T
        r[is_norm_a & ~fault] = 1.0
        diag = (~is_norm_a) & (~probe) & fault
        r[diag & (l == yl) & (k == yt)] = 1.0
        r[diag & (l == yl) & (k != yt)] = cfg.reward_loc_only
        r[diag & (l != yl) & (k == yt)] = cfg.reward_type_only
        r[probe] = -cfg.probe_cost
        return r, ~probe

    # ------------------------------------------------------------------ 训练
    def train(self, ds: Dataset, log_every: int = 1000, val: Optional[Dataset] = None) -> Dict:
        """全部随机抽取（回合样本、探索动作、ε 判定、PER 采样）都用 cfg.seed 播种的生成器：
        同一数据、同一种子、同一线程数下训练结果可复现（不同线程数下浮点累加顺序不同，不保证逐位一致）。"""
        cfg = self.cfg
        dev = self.device
        g = torch.Generator(device="cpu").manual_seed(cfg.seed)
        gd = g if torch.device(dev).type == "cpu" else torch.Generator(device=dev).manual_seed(cfg.seed + 1)
        opt = torch.optim.Adam(self.net.parameters(), lr=cfg.lr)
        cap, L = cfg.replay, self.L
        R = dict(idx=torch.zeros(cap, dtype=torch.long, device=dev),
                 pr=torch.zeros(cap, L, dtype=torch.bool, device=dev),
                 st=torch.zeros(cap, dtype=torch.long, device=dev),
                 a=torch.zeros(cap, dtype=torch.long, device=dev),
                 r=torch.zeros(cap, device=dev),
                 d=torch.zeros(cap, dtype=torch.bool, device=dev),
                 pr2=torch.zeros(cap, L, dtype=torch.bool, device=dev),
                 st2=torch.zeros(cap, dtype=torch.long, device=dev))
        prio = torch.zeros(cap, device=dev)
        ptr, size = 0, 0
        E = cfg.n_env
        idx = torch.randint(ds.N, (E,), generator=g).to(dev)
        probed = torch.zeros(E, L, dtype=torch.bool, device=dev)
        step = torch.zeros(E, dtype=torch.long, device=dev)
        hist = []
        t0 = time.time()
        ep_ret, ep_cnt = 0.0, 0
        for it in range(cfg.iters):
            eps = cfg.eps_end + (cfg.eps_start - cfg.eps_end) * max(0.0, 1 - it / (cfg.eps_frac * cfg.iters))
            # ---- 交互
            with torch.no_grad():
                o = self.obs(ds, idx, probed, step)
                q = self.net(*o)
                greedy = q.argmax(-1)
                rnd = torch.multinomial(o[-1].float(), 1, generator=gd).squeeze(-1)
                explore = torch.rand(E, device=dev, generator=gd) < eps
                a = torch.where(explore, rnd, greedy)
                r, done = self.reward(ds, idx, a)
                l = a // K
                probe_now = ~done
                probed2 = probed.clone()
                probed2[probe_now, l[probe_now]] = True
                step2 = step + 1
                done = done | (step2 >= cfg.max_steps)
                n = E
                sl = torch.arange(ptr, ptr + n, device=dev) % cap
                for k_, v_ in (("idx", idx), ("pr", probed), ("st", step), ("a", a), ("r", r), ("d", done),
                               ("pr2", probed2), ("st2", step2)):
                    R[k_][sl] = v_
                prio[sl] = prio[:size].max() if size else 1.0
                ptr = (ptr + n) % cap
                size = min(cap, size + n)
                ep_ret += float(r[done].sum()); ep_cnt += int(done.sum())
                # 重置结束的回合
                newi = torch.randint(ds.N, (E,), generator=g).to(dev)
                idx = torch.where(done, newi, idx)
                probed = torch.where(done.unsqueeze(-1), torch.zeros_like(probed2), probed2)
                step = torch.where(done, torch.zeros_like(step2), step2)
            # ---- 学习（PER）
            if size < cfg.batch * 4:
                continue
            beta = cfg.per_beta0 + (1 - cfg.per_beta0) * it / cfg.iters
            pa = prio[:size] ** cfg.per_alpha
            j = torch.multinomial(pa, cfg.batch, replacement=True, generator=gd)
            w = (size * pa[j] / pa.sum()) ** (-beta)
            w = w / w.max()
            o1 = self.obs(ds, R["idx"][j], R["pr"][j], R["st"][j])
            qa = self.net(*o1).gather(1, R["a"][j].unsqueeze(1)).squeeze(1)
            with torch.no_grad():
                o2 = self.obs(ds, R["idx"][j], R["pr2"][j], R["st2"][j].clamp(max=cfg.max_steps - 1))
                a2 = self.net(*o2).argmax(-1)
                q2 = self.tgt(*o2).gather(1, a2.unsqueeze(1)).squeeze(1)
                y = R["r"][j] + cfg.gamma * (~R["d"][j]).float() * q2
            td = y - qa
            loss = (w * Fn.smooth_l1_loss(qa, y, reduction="none")).mean()
            opt.zero_grad()
            loss.backward()
            nn.utils.clip_grad_norm_(self.net.parameters(), 10.0)
            opt.step()
            prio[j] = td.detach().abs() + 1e-3
            with torch.no_grad():
                for pt, pn in zip(self.tgt.parameters(), self.net.parameters()):
                    pt.mul_(1 - cfg.tau).add_(pn, alpha=cfg.tau)
            if log_every and (it + 1) % log_every == 0:
                rec = dict(iter=it + 1, eps=round(eps, 3), loss=round(float(loss.detach()), 4),
                           ep_return=round(ep_ret / max(ep_cnt, 1), 3), sec=round(time.time() - t0, 1))
                if val is not None:
                    m = self.evaluate(val, max_n=4000)
                    rec.update(val_joint=m["overall"]["joint_acc"], val_type=m["overall"]["type_acc"],
                               val_probes=m["overall"]["avg_probes"])
                hist.append(rec)
                print("  ", rec, flush=True)
                ep_ret, ep_cnt = 0.0, 0
        return {"history": hist, "train_sec": round(time.time() - t0, 1)}

    # ------------------------------------------------------------------ 评估
    @torch.no_grad()
    def rollout(self, ds: Dataset, idx: torch.Tensor):
        n = len(idx)
        probed = torch.zeros(n, self.L, dtype=torch.bool, device=self.device)
        step = torch.zeros(n, dtype=torch.long, device=self.device)
        final = torch.full((n,), -1, dtype=torch.long, device=self.device)
        qfin = torch.zeros(n, device=self.device)
        alive = torch.ones(n, dtype=torch.bool, device=self.device)
        for _ in range(self.cfg.max_steps):
            q = self.net(*self.obs(ds, idx, probed, step))
            a = q.argmax(-1)
            is_probe = (a < self.L * K) & (a % K == PROBE)
            fin = alive & ~is_probe
            final[fin] = a[fin]
            qfin[fin] = q.gather(1, a.unsqueeze(1)).squeeze(1)[fin]
            pm = alive & is_probe
            probed[pm, (a // K)[pm]] = True
            alive = alive & is_probe
            step = step + 1
            if not alive.any():
                break
        return final, probed.sum(1), qfin

    def evaluate(self, ds: Dataset, max_n: Optional[int] = None, interval_s: float = 5.0) -> Dict:
        """端到端口径：判决器未触发的样本不会唤醒智能体，按"判正常"计
        （故障样本即漏报，正常样本即无告警）。"""
        n = ds.N if max_n is None else min(ds.N, max_n)
        idx = torch.arange(n, device=self.device)
        acts, probes = [], []
        for s in range(0, n, 4096):
            a, p, _ = self.rollout(ds, idx[s:s + 4096])
            acts.append(a); probes.append(p)
        a = torch.cat(acts).cpu().numpy()
        p = torch.cat(probes).cpu().numpy()
        yt = ds.y_type[:n].cpu().numpy()
        yl = ds.y_link[:n].cpu().numpy()
        noisy = ds.noisy[:n].cpu().numpy()
        det = ds.detected[:n].cpu().numpy()
        drounds = ds.det_rounds[:n].cpu().numpy()
        L = self.L
        pt = np.where(a == L * K, NORMAL_T, a % K)
        pl = np.where(a == L * K, -1, a // K)
        pt = np.where(det, pt, NORMAL_T)
        pl = np.where(det, pl, -1)
        p = np.where(det, p, 0)
        names = list(FAULT_TYPES) + ["normal"]
        out = {"per_type": {}, "per_category": {}}
        for ti, name in enumerate(names):
            m = yt == ti
            if not m.any():
                continue
            rec = dict(n=int(m.sum()), type_acc=float((pt[m] == ti).mean()),
                       avg_probes=float(p[m].mean()), detect_rate=float(det[m].mean()))
            if ti != NORMAL_T:
                rec.update(loc_acc=float((pl[m] == yl[m]).mean()),
                           joint_acc=float(((pt[m] == ti) & (pl[m] == yl[m])).mean()),
                           detect_delay_s=float(drounds[m & det].mean() * interval_s) if (m & det).any() else None)
            out["per_type"][name] = rec
        cat_of = lambda t: CATEGORY[names[t]] if t < NT else "正常"
        cats = np.array([cat_of(t) for t in yt])
        pcats = np.array([cat_of(t) for t in pt])
        for c in ["链路中断", "性能劣化", "路由漂移", "正常"]:
            m = cats == c
            if m.any():
                rec = dict(n=int(m.sum()), category_acc=float((pcats[m] == c).mean()),
                           type_acc=float((pt[m] == yt[m]).mean()))
                if c != "正常":
                    rec.update(loc_acc=float((pl[m] == yl[m]).mean()),
                               joint_acc=float(((pt[m] == yt[m]) & (pl[m] == yl[m])).mean()))
                out["per_category"][c] = rec
        normal = yt == NORMAL_T
        out["overall"] = dict(
            n=int(n), type_acc=float((pt == yt).mean()),
            joint_acc=float(((pt == yt) & ((pl == yl) | normal)).mean()),
            false_alarm_rate=float((pt[normal] != NORMAL_T).mean()) if normal.any() else None,
            false_trigger_rate=float(det[normal].mean()) if normal.any() else None,
            false_alarm_rate_noisy=float((pt[normal & noisy] != NORMAL_T).mean()) if (normal & noisy).any() else None,
            miss_rate=float((pt[~normal] == NORMAL_T).mean()) if (~normal).any() else None,
            avg_probes=float(p.mean()))
        return out

    # ------------------------------------------------------------------ 存取
    def save(self, path: str, meta: Optional[dict] = None):
        torch.save({"state": self.net.state_dict(), "cfg": asdict(self.cfg),
                    "dims": [self.F1, self.F2, self.G, self.L], "drift_links": self.drift_links,
                    "meta": meta or {}}, path)

    @classmethod
    def load(cls, path: str, device="cpu") -> "Agent":
        """模型包只含张量与基本类型（dict/list/str/数值），按 weights_only=True 加载：文件不能借反序列化执行代码。"""
        import pickle
        try:
            ck = torch.load(path, map_location=device, weights_only=True)
        except pickle.UnpicklingError as e:
            lines = [x.strip() for x in str(e).split("WeightsUnpickler error:")[-1].splitlines() if x.strip()]
            why = next((x for x in lines if not x.startswith(("Weights only load failed", "Check the doc"))),
                       lines[0] if lines else "")
            raise ValueError(f"{path}: 拒绝加载：模型包只能含张量与基本类型（按 weights_only=True 安全加载，"
                             f"需 torch>=2.0，当前 {torch.__version__}）：{why}") from None
        F1, F2, G, L = ck["dims"]
        ag = cls(F1, F2, G, L, ck["drift_links"], AgentConfig(**ck["cfg"]), device)
        ag.net.load_state_dict(ck["state"])
        ag.tgt.load_state_dict(ck["state"])
        ag.net.eval()
        ag.meta = ck.get("meta", {})
        return ag
