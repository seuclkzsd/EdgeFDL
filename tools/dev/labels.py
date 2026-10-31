# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""检查 .github/labels.yml 并同步到 GitHub。

    python tools/dev/labels.py           # 只检查并打印 gh 命令，不改动任何东西
    python tools/dev/labels.py --apply   # 用 gh label create --force 创建或更新（只增改，不删除）
"""
import argparse
import re
import shlex
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
GROUPS = ("type", "area", "priority", "community")
REQUIRED = ("good first issue", "help wanted")


def load(path: Path = ROOT / ".github" / "labels.yml") -> list:
    """读取标签集并校验；不合格时抛 ValueError。"""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if set(data) != set(GROUPS):
        raise ValueError(f"分组须为 {GROUPS}，实际 {sorted(data)}")
    labels = [x for g in GROUPS for x in data[g]]
    names = [x["name"] for x in labels]
    if len(set(n.lower() for n in names)) != len(names):
        raise ValueError("标签重名")
    for x in labels:
        if not re.fullmatch(r"[0-9a-f]{6}", str(x["color"])):
            raise ValueError(f"{x['name']}: 颜色须为 6 位十六进制")
        if not 0 < len(x["description"]) <= 100:                 # GitHub 的长度上限
            raise ValueError(f"{x['name']}: 说明须为 1–100 个字符")
    missing = [n for n in REQUIRED if n not in names]
    if missing:
        raise ValueError(f"缺少标签 {missing}")
    return labels


def commands(labels: list) -> list:
    return [["gh", "label", "create", x["name"], "--color", str(x["color"]), "--description", x["description"], "--force"]
            for x in labels]


def main() -> int:
    ap = argparse.ArgumentParser(description="检查 .github/labels.yml 并同步到 GitHub")
    ap.add_argument("--apply", action="store_true", help="真正执行 gh 命令（默认只打印）")
    args = ap.parse_args()
    labels = load()
    for cmd in commands(labels):
        print(" ".join(shlex.quote(c) for c in cmd))
        if args.apply:
            subprocess.run(cmd, check=True)
    print(f"labels: {len(labels)} 个标签" + ("已同步" if args.apply else "，未改动 GitHub（--apply 才执行）"), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
