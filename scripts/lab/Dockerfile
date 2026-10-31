# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
# EdgeFDL 实验环境：单容器内用 netns 起整张等价虚拟网络（真实内核协议栈 + FRR OSPF + tc）。
# 构建：scripts/lab.sh build    运行：scripts/lab.sh shell / scripts/lab.sh smoke
FROM debian:bookworm-slim
ARG MIRROR=deb.debian.org
ENV http_proxy= https_proxy= HTTP_PROXY= HTTPS_PROXY= no_proxy=* NO_PROXY=* \
    DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1
RUN sed -i "s#deb.debian.org#${MIRROR}#g; s#security.debian.org#${MIRROR}#g" \
        /etc/apt/sources.list.d/debian.sources \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
        frr iperf3 traceroute iproute2 iputils-ping procps \
        python3 python3-yaml python3-numpy ca-certificates \
 && rm -rf /var/lib/apt/lists/*
# 可选：TORCH=1 时另装 CPU 版 PyTorch 与 psutil，供 scripts/realstack_eval.py 在容器内跑在线引擎；默认不装，
# CI 与冒烟测试用的默认镜像内容不变。国内可用 EDGEFDL_PIP_INDEX、EDGEFDL_TORCH_WHEELS 换源（见 scripts/lab.sh）
ARG TORCH=0
ARG TORCH_SPEC=torch==2.9.1+cpu
ARG TORCH_WHEELS=https://download.pytorch.org/whl/cpu/torch/
ARG PIP_INDEX=https://pypi.org/simple
RUN if [ "$TORCH" = 1 ]; then \
      apt-get update \
   && apt-get install -y --no-install-recommends python3-pip python3-psutil \
   && rm -rf /var/lib/apt/lists/* \
   && python3 -m pip install --break-system-packages --no-cache-dir --index-url "$PIP_INDEX" \
        --find-links "$TORCH_WHEELS" "$TORCH_SPEC" \
   && python3 -c "import torch, numpy, yaml, psutil; print(torch.__version__, numpy.__version__)"; \
    fi
WORKDIR /work
CMD ["bash"]
