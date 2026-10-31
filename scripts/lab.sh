#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
# 实验容器管理：build | build-torch | shell | smoke | run <cmd...>
# 容器以 --privileged 运行（netns / tc / FRR 需要 CAP_NET_ADMIN 与 CAP_SYS_ADMIN），
# 仓库挂载到 /work；宿主机无需 sudo，只需在 docker 组。
# 容器带 --rm，只有仓库目录（/work）里的文件会保留：输出请写到仓库内，如
#   scripts/lab.sh run python3 -m edgefdl baseline --backend netns -o runs/lab/baseline.json
# run 结束后把容器（root）在仓库里新建的文件交还给宿主机当前用户。
# 可选环境变量：EDGEFDL_LAB_IMAGE 镜像；EDGEFDL_LAB_NAME 容器名；EDGEFDL_LAB_CPUSET 绑定的 CPU（如 24-27）；
# 构建时 EDGEFDL_LAB_TORCH=1 另装 CPU 版 PyTorch（scripts/realstack_eval.py 需要，默认镜像不含），
# EDGEFDL_PIP_INDEX、EDGEFDL_TORCH_WHEELS 换 pip 源与 PyTorch wheel 源。
set -euo pipefail
cd "$(dirname "$0")/.."
IMG=${EDGEFDL_LAB_IMAGE:-edgefdl-lab:latest}
RUN=(docker run --rm --privileged --network none -v "$PWD":/work -w /work
     -e PYTHONPATH=/work -e NO_PROXY='*' -e no_proxy='*' -e OMP_NUM_THREADS=1)
[ -z "${EDGEFDL_LAB_NAME:-}" ] || RUN+=(--name "$EDGEFDL_LAB_NAME")     # 给容器命名，便于并行跑时识别
[ -z "${EDGEFDL_LAB_CPUSET:-}" ] || RUN+=(--cpuset-cpus "$EDGEFDL_LAB_CPUSET")
BUILD=(--build-arg MIRROR="${EDGEFDL_APT_MIRROR:-deb.debian.org}" --build-arg REGISTRY="${EDGEFDL_REGISTRY:-docker.io/library}")
TORCH=(--build-arg TORCH=1)
[ -z "${EDGEFDL_PIP_INDEX:-}" ] || TORCH+=(--build-arg PIP_INDEX="$EDGEFDL_PIP_INDEX")
[ -z "${EDGEFDL_TORCH_WHEELS:-}" ] || TORCH+=(--build-arg TORCH_WHEELS="$EDGEFDL_TORCH_WHEELS")
[ "${EDGEFDL_LAB_TORCH:-0}" != 1 ] || BUILD+=("${TORCH[@]}")
case "${1:-shell}" in
  build) NO_PROXY='*' no_proxy='*' docker build --network host "${BUILD[@]}" -t "$IMG" scripts/lab ;;
  build-torch) NO_PROXY='*' no_proxy='*' docker build --network host "${BUILD[@]}" "${TORCH[@]}" \
           -t edgefdl-lab-torch:latest scripts/lab ;;
  shell) "${RUN[@]}" -it "$IMG" bash ;;
  smoke) shift; "${RUN[@]}" "$IMG" python3 scripts/netns_smoke.py "$@" ;;
  run)   shift
         for a in "$@"; do
           case "$a" in /tmp/*|*=/tmp/*) echo "lab.sh: 注意 $a 在容器内的 /tmp，容器退出即删除；请写到仓库内（如 runs/lab/）" >&2 ;; esac
         done
         "${RUN[@]}" -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" "$IMG" bash -c \
           'touch /tmp/.lab-t0; "$@"; rc=$?
            find /work -xdev -user 0 -newer /tmp/.lab-t0 -exec chown -h "$HOST_UID:$HOST_GID" {} + 2>/dev/null
            exit $rc' lab "$@" ;;
  *) echo "用法: $0 build|build-torch|shell|smoke|run <cmd>"; exit 1 ;;
esac
