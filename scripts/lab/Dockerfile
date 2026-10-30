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
WORKDIR /work
CMD ["bash"]
