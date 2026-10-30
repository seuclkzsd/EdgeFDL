#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
# 实验容器管理：build | shell | smoke | run <cmd...>
# 容器以 --privileged 运行（netns / tc / FRR 需要 CAP_NET_ADMIN 与 CAP_SYS_ADMIN），
# 仓库挂载到 /work；宿主机无需 sudo，只需在 docker 组。
set -euo pipefail
cd "$(dirname "$0")/.."
IMG=${EDGEFDL_LAB_IMAGE:-edgefdl-lab:latest}
RUN=(docker run --rm --privileged --network none -v "$PWD":/work -w /work
     -e PYTHONPATH=/work -e NO_PROXY='*' -e no_proxy='*')
case "${1:-shell}" in
  build) NO_PROXY='*' no_proxy='*' docker build --network host \
           --build-arg MIRROR="${EDGEFDL_APT_MIRROR:-deb.debian.org}" -t "$IMG" scripts/lab ;;
  shell) "${RUN[@]}" -it "$IMG" bash ;;
  smoke) shift; "${RUN[@]}" "$IMG" python3 scripts/netns_smoke.py "$@" ;;
  run)   shift; "${RUN[@]}" "$IMG" "$@" ;;
  *) echo "用法: $0 build|shell|smoke|run <cmd>"; exit 1 ;;
esac
