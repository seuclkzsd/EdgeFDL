#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""组装回放演示的静态站点：docs/demo/ 的页面与录制帧，加上大屏页面（edgefdl/core/web/index.html 原样复制为
dashboard.html，git 中不保存第二份）。站点只用相对路径，可放在任意子路径下（如 /EdgeFDL/），由任何静态文件服务器提供。

  python tools/demo/build_site.py                    # 输出到 build/site
  python tools/demo/build_site.py --out DIR
"""
import argparse
import os
import shutil
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEMO = os.path.join(ROOT, "docs", "demo")
DASHBOARD = os.path.join(ROOT, "edgefdl", "core", "web", "index.html")
PAGES = ("index.html", "replay.js", "frames.json")
MAX_FRAMES_BYTES = 3 * 1000 * 1000


def build(out: str) -> list:
    size = os.path.getsize(os.path.join(DEMO, "frames.json"))
    if size > MAX_FRAMES_BYTES:
        raise SystemExit(f"frames.json 有 {size} 字节，超过上限 {MAX_FRAMES_BYTES}")
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(out)
    for name in PAGES:
        shutil.copyfile(os.path.join(DEMO, name), os.path.join(out, name))
    shutil.copyfile(DASHBOARD, os.path.join(out, "dashboard.html"))
    return sorted(os.listdir(out))


def main():
    ap = argparse.ArgumentParser(description="组装回放演示的静态站点")
    ap.add_argument("--out", default=os.path.join(ROOT, "build", "site"))
    args = ap.parse_args()
    names = build(args.out)
    print(f"{args.out}：" + "、".join(f"{n}（{os.path.getsize(os.path.join(args.out, n))} 字节）" for n in names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
