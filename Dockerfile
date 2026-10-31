# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
# EdgeFDL 演示镜像：仿真后端 + 随仓库发布的模型 + Web 大屏，CPU 版 PyTorch，非 root 运行。
#   docker compose up      然后打开 http://127.0.0.1:8460
# 基础镜像按 digest 固定（更新时改这里，tools/dev/lock.py 从这里读取），Python 依赖按
# requirements-lock.txt 安装（版本与哈希全部固定，--no-deps：不解析、不多装）。
# 构建参数：REGISTRY 基础镜像仓库前缀；PIP_INDEX_URL PyPI 镜像；PIP_FIND_LINKS 轮子的附加来源。
# 哈希校验保证换镜像源也得到同样的文件。
ARG REGISTRY=docker.io/library
FROM ${REGISTRY}/python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_FIND_LINKS=
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements-lock.txt /tmp/requirements-lock.txt
RUN pip install --no-cache-dir --no-deps --require-hashes -r /tmp/requirements-lock.txt \
 && pip check \
 && rm /tmp/requirements-lock.txt

# 固定 UID，便于挂载卷授权；/data 放测量与告警日志，容器内其余位置只读也能运行
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin edgefdl \
 && mkdir /data && chown edgefdl:edgefdl /data

WORKDIR /app
COPY LICENSE NOTICE requirements-lock.txt ./
COPY configs/demo_topology.yaml configs/
COPY results/model.pt results/
COPY edgefdl edgefdl/

USER 10001:10001
VOLUME /data
EXPOSE 8460
# 服务收到 SIGINT 才会停止总控并落盘后退出；PID 1 的 Python 默认不响应 SIGTERM
STOPSIGNAL SIGINT
# 接口可答且测量循环已走完第一轮才算健康；绕开容器里可能注入的代理变量
HEALTHCHECK --interval=10s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import json, urllib.request as u; o = u.build_opener(u.ProxyHandler({})); s = json.load(o.open('http://127.0.0.1:8460/api/status', timeout=4)); raise SystemExit(0 if s.get('round', 0) >= 1 else 1)"]

ENTRYPOINT ["python", "-m", "edgefdl"]
# 监听 0.0.0.0 是容器内的地址；对外端口由 compose.yaml 只发布到宿主机回环地址
CMD ["serve", "--model", "results/model.pt", "--backend", "sim", "--host", "0.0.0.0", "--port", "8460", \
     "--interval", "2", "--log-dir", "/data/live"]
