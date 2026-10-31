#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
# 真实协议栈准确率测试的分片并行运行：每个分片一个容器（netns 与 FRR 按容器隔离），各跑 scripts/realstack_eval.py。
#   scripts/realstack_run.sh [输出目录] [传给 realstack_eval.py run 的参数...]
#   例：EDGEFDL_LAB_IMAGE=edgefdl-lab-torch:latest scripts/realstack_run.sh runs/realstack/a --sets single
# 环境变量：EDGEFDL_SHARDS 分片数（默认 6）；EDGEFDL_CPUSETS 空格分隔的各分片 CPU 绑定（默认不绑定，如
# "24-27 28-31"，用 cpuset 而不用 --cpus：CFS 配额的限流停顿会被测成网络时延）。容器名 edgefdl-realstack-<目录名>-<分片>，
# 脚本退出（含中断）时全部删除。
set -euo pipefail
cd "$(dirname "$0")/.."
out=${1:-runs/realstack/all}
[ $# -eq 0 ] || shift
shards=${EDGEFDL_SHARDS:-6}
read -r -a cpusets <<< "${EDGEFDL_CPUSETS:-}"
tag=$(basename "$out")
commit=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)
mkdir -p "$out"
names=()
cleanup() { [ ${#names[@]} -eq 0 ] || docker rm -f "${names[@]}" >/dev/null 2>&1 || true; }
trap cleanup EXIT
pids=()
for ((i = 0; i < shards; i++)); do
  name=edgefdl-realstack-$tag-$i
  names+=("$name")
  # 各分片的测量轮错开：traceroute 触发的 ICMP 差错受内核全局限速，这个限速不按命名空间隔离
  phase=$(python3 -c "print(round($i * 5 / $shards + ${EDGEFDL_PHASE:-0}, 2))")
  EDGEFDL_LAB_NAME=$name EDGEFDL_LAB_CPUSET=${cpusets[$i]:-} \
    scripts/lab.sh run python3 scripts/realstack_eval.py run --shard "$i" --shards "$shards" \
      --out "$out/shard$i" --phase "$phase" --commit "$commit" --image "${EDGEFDL_LAB_IMAGE:-edgefdl-lab:latest}" "$@" \
      > "$out/shard$i.log" 2>&1 &
  pids+=($!)
done
rc=0
for p in "${pids[@]}"; do wait "$p" || rc=$?; done
exit $rc
