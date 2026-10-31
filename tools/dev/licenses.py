# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""检查 Python 运行时依赖的许可证：读已安装包的元数据，遇到未知或强 copyleft 许可证时失败。

    python tools/dev/licenses.py                                # requirements.txt 的依赖闭包
    python tools/dev/licenses.py --lock requirements-lock.txt   # 以锁文件为准，版本与闭包须完全一致
    python tools/dev/licenses.py --lock requirements-lock.txt --markdown   # 输出 docs/dependencies.md 的表格

退出码：0 通过；1 有未知或强 copyleft 许可证，或环境与锁文件不一致；2 依赖没装全。
许可证的出处依次为 License-Expression、License 字段、Trove 分类、包内许可证文件的文本特征。
GPU 构建的 PyTorch 额外依赖 nvidia-* 专有运行库：它们不属于 CPU 安装（requirements-lock.txt），
只列出、不判定。锁文件之外的 CI 环境同样受检，新版本引入的未知许可证会在这里暴露。
"""
import argparse
import importlib.metadata as md
import re
import sys
from pathlib import Path

try:
    from packaging.requirements import Requirement
except ImportError:                                  # 开发环境未单独安装 packaging 时用 pip 自带的
    from pip._vendor.packaging.requirements import Requirement

PERMISSIVE, WEAK, STRONG, UNKNOWN = 0, 1, 2, 3
CATEGORY = {PERMISSIVE: "permissive", WEAK: "weak copyleft", STRONG: "STRONG COPYLEFT", UNKNOWN: "UNKNOWN"}
PERMISSIVE_IDS = {"mit", "mit-0", "bsd", "bsd-2-clause", "bsd-3-clause", "0bsd", "isc", "apache-2.0", "psf-2.0",
                  "python-2.0", "zlib", "unlicense", "cc0-1.0", "bsl-1.0", "hpnd"}
WEAK_PREFIX = ("lgpl", "mpl-", "epl-", "cddl-")
STRONG_PREFIX = ("gpl", "agpl", "sspl", "osl-", "eupl-")
GPU_ONLY = re.compile(r"^nvidia-")
# 写法不规范的 License 字段与 Trove 分类 → SPDX 标识
ALIASES = {"bsd": "BSD", "bsd license": "BSD", "mit license": "MIT", "the mit license (mit)": "MIT",
           "apache 2.0": "Apache-2.0", "apache license 2.0": "Apache-2.0", "apache software license": "Apache-2.0",
           "python software foundation license": "PSF-2.0", "psf": "PSF-2.0", "the unlicense (unlicense)": "Unlicense",
           "isc license (iscl)": "ISC", "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0"}
# 元数据完全没有许可证信息、但上游明确的包（conda 打包的 setuptools 以 egg-info 安装，不带 LICENSE）
UPSTREAM = {"setuptools": "MIT"}
MARKS = {"Permission is hereby granted, free of charge": "MIT",
         "Redistribution and use in source and binary forms": "BSD",
         "Apache License": "Apache-2.0",
         "GNU AFFERO GENERAL PUBLIC LICENSE": "AGPL-3.0-only",
         "GNU GENERAL PUBLIC LICENSE": "GPL"}
TOKEN = re.compile(r"\(|\)|[A-Za-z0-9.+-]+")


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def rank_id(lid: str, exception: str = "") -> int:
    x = lid.lower()
    if exception or x.endswith("-with-gcc-exception"):     # GCC 运行库例外等：不传染链接它的程序
        return WEAK if x.startswith(STRONG_PREFIX) and ("exception" in exception.lower() or "exception" in x) else UNKNOWN
    if x in PERMISSIVE_IDS:
        return PERMISSIVE
    if x.startswith(WEAK_PREFIX):
        return WEAK
    if x.startswith(STRONG_PREFIX):
        return STRONG
    return UNKNOWN


def rank_expression(expr: str) -> int:
    """SPDX 表达式的最优可选分类：AND 取最严，OR 取最宽（使用者可选其一）。语法错误算未知。"""
    toks = TOKEN.findall(expr)
    pos = 0

    def peek():
        return toks[pos] if pos < len(toks) else None

    def take():
        nonlocal pos
        pos += 1
        return toks[pos - 1]

    def atom():
        if peek() == "(":
            take()
            r = either()
            if peek() != ")":
                raise ValueError(expr)
            take()
            return r
        if peek() in (None, ")", "AND", "OR", "WITH"):
            raise ValueError(expr)
        lid = take()
        if peek() == "WITH":
            take()
            return rank_id(lid, take() if peek() else "invalid")
        return rank_id(lid)

    def both():
        r = atom()
        while peek() == "AND":
            take()
            r = max(r, atom())
        return r

    def either():
        r = both()
        while peek() == "OR":
            take()
            r = min(r, both())
        return r

    try:
        r = either()
        return r if pos == len(toks) else UNKNOWN
    except ValueError:
        return UNKNOWN


def fingerprint(text: str) -> str:
    """由许可证正文判断（取最先出现的条款：后面常附带第三方片段的许可证）；认不出返回空串。"""
    t = " ".join(text.split())
    marks = [(t.find(m), m) for m in MARKS if m in t]
    if not marks:
        return ""
    lid = MARKS[min(marks)[1]]
    if lid == "BSD":
        return "BSD-3-Clause" if re.search("neither the name", t, re.I) else "BSD-2-Clause"
    if lid == "GPL":
        return "GPL-3.0-only" if "Version 3" in t else "GPL-2.0-only"
    return lid


def license_files(dist) -> list:
    out = []
    for f in dist.files or []:
        if f.parts and f.parts[0].endswith(".dist-info") and re.search(r"licen[cs]e|copying", f.name, re.I):
            try:
                out.append(Path(f.locate()).read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
    return out


def declared(dist) -> str:
    """包声明的许可证（SPDX 表达式）；认不出返回空串。"""
    meta = dist.metadata
    expr = (meta.get("License-Expression") or "").strip()
    if expr:
        return expr
    field = (meta.get("License") or "").strip()
    single = field if field and "\n" not in field and len(field) < 64 else ""
    trove = [c.split(" :: ")[-1] for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    trove = " AND ".join(dict.fromkeys(ALIASES.get(t.lower(), t) for t in trove if t != "OSI Approved"))
    if trove and rank_expression(trove) != PERMISSIVE:     # 分类里出现非宽松许可证时从严，不看其他字段
        return trove
    guess = ALIASES.get(single.lower(), single)
    if guess and rank_expression(guess) == UNKNOWN:
        guess = ""
    if guess and guess != "BSD":
        return guess
    # 只写了 "BSD" 或完全没写：看正文，定到具体条款
    texts = ([field] if field and not single else []) + license_files(dist)
    found = next((x for x in map(fingerprint, texts) if x), "")
    if guess == "BSD" and not found.startswith("BSD-"):
        return guess
    return found or trove or UPSTREAM.get(norm(meta["Name"]), "")


def requirements(dist) -> list:
    """本环境下生效的依赖（跳过 extras 与不满足的环境标记）。"""
    out = []
    for r in dist.requires or []:
        q = Requirement(r)
        if q.marker is None or q.marker.evaluate({"extra": ""}):
            out.append(norm(q.name))
    return out


def walk(roots):
    """沿已安装元数据走依赖闭包，返回 {包: dist}、{包: 依赖它的包}、缺失的包。"""
    dists, parents, missing, gpu = {}, {}, set(), set()
    todo = list(roots)
    while todo:
        name = todo.pop()
        if GPU_ONLY.match(name):
            gpu.add(name)
            continue
        if name in dists or name in missing:
            continue
        try:
            dists[name] = md.distribution(name)
        except md.PackageNotFoundError:
            missing.add(name)
            continue
        for dep in requirements(dists[name]):
            parents.setdefault(dep, set()).add(name)
            todo.append(dep)
    return dists, parents, missing, gpu


def read_requirements(path: Path) -> list:
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if m and not line.lstrip().startswith(("#", "-")):
            names.append(norm(m.group(1)))
    return names


def read_lock(path: Path) -> dict:
    return {norm(n): v for n, v in re.findall(r"^([A-Za-z0-9._-]+)==(\S+)", path.read_text(encoding="utf-8"), re.M)}


def markdown(rows) -> str:
    out = ["| Package | Version | License | Required by |", "|---|---|---|---|"]
    out += [f"| {n} | {v} | {lic} | {by} |" for n, v, lic, by in rows]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="检查 Python 运行时依赖的许可证")
    ap.add_argument("--requirements", help="直接依赖清单（缺省为仓库根目录的 requirements.txt）")
    ap.add_argument("--lock", help="锁文件：环境须与之完全一致")
    ap.add_argument("--markdown", action="store_true", help="通过后只输出 Markdown 表格")
    args = ap.parse_args()

    lock = read_lock(Path(args.lock)) if args.lock else None
    if lock is not None:
        roots = list(lock)
    else:
        roots = read_requirements(Path(args.requirements or Path(__file__).resolve().parents[2] / "requirements.txt"))
    dists, parents, missing, gpu = walk(roots)
    if missing:
        print("licenses: 以下依赖未安装：" + ", ".join(sorted(missing)) + "（先 pip install -r requirements.txt）")
        return 2

    problems, rows = [], []
    if lock is not None:
        for name, ver in sorted(lock.items()):
            if dists[name].version != ver:
                problems.append(f"{name}: 已安装 {dists[name].version}，锁文件 {ver}")
        problems += [f"{n}: 在依赖闭包里但不在锁文件里" for n in sorted(set(dists) - set(lock))]
    for name in sorted(dists):
        d = dists[name]
        expr = declared(d)
        rank = rank_expression(expr) if expr else UNKNOWN
        by = ", ".join(sorted(parents.get(name, ()))) or "direct"
        rows.append((d.metadata["Name"], d.version, expr or "?", by))
        if rank in (STRONG, UNKNOWN):
            problems.append(f"{name} {d.version}: {CATEGORY[rank]}（{expr or '元数据没有许可证信息'}）")
        if not args.markdown:
            print(f"  {name:<20} {d.version:<14} {expr or '?':<28} {CATEGORY[rank]}")
    if problems:
        print("licenses: 未通过\n  " + "\n  ".join(problems))
        return 1
    if args.markdown:
        print(markdown(rows))
        return 0
    if gpu:
        print(f"  另有 {len(gpu)} 个 GPU 构建专属的 nvidia-* 运行库，不属于 CPU 安装，未判定")
    print(f"licenses: {len(rows)} 个包通过（未知 0，强 copyleft 0）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
