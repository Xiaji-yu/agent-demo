# syntax=docker/dockerfile:1
#
# agent-demo 容器镜像：与宿主机部署（scripts/deploy.py + systemd）跑同一份代码，
# 差别只在「谁当 supervisor」——容器里由 compose 的 restart: unless-stopped +
# init: true（tini）承担进程托管与信号转发。
#
# reboot 执行链在容器内的可用性：C（外部白名单命令）/ B（os.execve 自替换）两步
# 不需要 systemd，照常可用；A 步（退出等 supervisor）由容器重启等价替代。真正会
# 死的只有裸 nohup 部署（README 运维速查有警示）。
#
# 构建期即跑 deploy.py smoke 同款 import 冒烟——缺件在 build 就炸，好过上线后
# 启动失败。

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------- builder
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

# uv 从 PyPI 装进 builder：ghcr.io 的官方静态镜像实测拉取极慢（23MB 要几分钟），
# 而 files.pythonhosted.org 秒级。关键不是来源而是语义——uv sync --locked 会校验
# uv.lock 与 pyproject 一致，比 --frozen（只照现有 lock 装、不校验新鲜度）严格：
# CI 的 M8 注释记的就是只装不校验让 lock 悄悄过期的教训。版本与 dev extra 的
# pin 对齐（pyproject.toml）。
# 国内网络下 PyPI 官方源同样拉不动（腾讯云实测 uv 的 20MB wheel 下载 0 进度直至
# pip 超时）：传 --build-arg PIP_INDEX_URL=<国内镜像> 走国内源，例如
# http://mirrors.tencentyun.com/pypi/simple（腾讯云内网）或
# https://mirrors.aliyun.com/pypi/simple。默认空 = 官方源，不影响境外构建。
# 下方的 uv sync 也读它（RUN 内条件 export 成 UV_DEFAULT_INDEX_URL；刻意不进
# ENV——空字符串下 uv 的行为未定义，不能赌）。
ARG PIP_INDEX_URL=
RUN pip install --no-cache-dir --timeout 120 --retries 10 \
        ${PIP_INDEX_URL:+-i ${PIP_INDEX_URL}} uv==0.12.17

# 不让 uv 自行下载解释器（基础镜像已带 3.12，下载会引入不可预期版本）；
# HTTP 超时放宽到 120s——默认 15s 在跨境慢链路上会把下载判成失败
ENV UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_HTTP_TIMEOUT=120

WORKDIR /app

# 先只放依赖描述文件：依赖不变时这一层命中缓存，改业务代码不触发重装。
# README.md 是 hatchling 的 readme 字段来源，装项目本身时要读
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -n "$PIP_INDEX_URL" ]; then export UV_DEFAULT_INDEX_URL="$PIP_INDEX_URL"; fi; \
    uv sync --locked --no-dev --no-install-project

COPY agentcore ./agentcore
COPY plugins ./plugins
COPY bot.py config.yaml ./
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -n "$PIP_INDEX_URL" ]; then export UV_DEFAULT_INDEX_URL="$PIP_INDEX_URL"; fi; \
    uv sync --locked --no-dev

# venv 进 PATH：uv sync 把依赖装在 /app/.venv，不激活的话下面的冒烟会用系统
# 解释器，报出来的是「No module named 'dotenv'」这种缺件假象
ENV PATH="/app/.venv/bin:${PATH}"

# import 冒烟：nonebot init + plugins 全图加载无缺件。此阶段 USER 仍是 root，
# 不需要 data/ 可写（日志初始化对不可写目录是容错降级的）
RUN python -c "import bot"

# ---------------------------------------------------------------- pgtools
# PG 16 客户端工具（pg_dump / psql）从官方 postgres:16-bookworm 镜像取，**不要**用
# apt 的 postgresql-client：bookworm 自带的是 PG 15，而服务端是 pgvector:pg16（PG 16），
# pg_dump 会以 "aborting because of server version mismatch" 直接拒绝导出，备份
# 静默降级成 JSONL（实测复现）。
# tag 必须带 -bookworm：无后缀的 postgres:16 基于更新的 Debian（glibc 2.38+），
# 它的 libldap/krb5 在 bookworm（glibc 2.36）上直接 "GLIBC_2.38 not found"。
# 只有 libpq.so.5 从这儿取：它是 PGDG 的 16（bookworm apt 里是 15），是唯一
# 版本敏感的一环；其余依赖（libldap/gnutls/krb5/sasl2…）在 bookworm apt 里都有，
# 走 apt 装。
FROM postgres:16-bookworm AS pgtools

# ---------------------------------------------------------------- runtime
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

# 系统依赖。缺任何一样都是**静默降级**——不报错，只是功能消失：
# - ffmpeg：点歌 silk 转码探测 PATH 里的 ffmpeg（agentcore/music/silk.py），
#   缺失则点歌整体不注册；CI 亦显式安装（.github/workflows/ci.yml M1 注释）
# - tzdata：时区数据。默认 UTC 会让 tz.today_anchor() 的时效锚点对中文用户差
#   一天，故 compose 里显式给 TZ
# - gosu：容器以 root 启动时，修完挂载点属权后降权到 app
# - libpq 的依赖：libssl3/libzstd1/liblz4-1/libldap-2.5-0/libsasl2-2/libgnutls30/
#   libgssapi-krb5-2/libkeyutils1 走 apt（postgres:16-bookworm 本身就是 bookworm，
#   这些库同源）；只有 libpq.so.5 从 pgtools 取（版本敏感，见上）
# 可选：国内网络下 deb.debian.org 极慢（实测 ffmpeg 依赖要 20+ 分钟），
# 传 --build-arg APT_MIRROR=mirrors.aliyun.com 走国内源。默认空 = 官方源，
# 不影响境外/正常网络下的构建。
ARG APT_MIRROR=

RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" \
            /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" \
            /etc/apt/sources.list; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        tzdata \
        gosu \
        libzstd1 \
        liblz4-1 \
        libssl3 \
        libldap-2.5-0 \
        libsasl2-2 \
        libgnutls30 \
        libgssapi-krb5-2 \
        libkeyutils1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=pgtools /usr/lib/postgresql/16/bin/pg_dump /usr/local/pg16/pg_dump
COPY --from=pgtools /usr/lib/postgresql/16/bin/psql /usr/local/pg16/psql
COPY --from=pgtools /lib/x86_64-linux-gnu/libpq.so.5* /usr/local/pg16/lib/

# app 用户：bot 的**运行身份**（由 entrypoint 以 gosu 降权后取得，不是镜像默认
# USER——见下面「刻意不 USER app」的说明）。uid/gid 取 1000：自托管宿主用户多为
# 1000，bind mount 的文件属主天然匹配
RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app agentcore ./agentcore
COPY --chown=app:app plugins ./plugins
COPY --chown=app:app bot.py config.yaml ./
# data/ 只带 fonts：agentcore/render/fonts.py 按 parents[2]/data/fonts 解析字体，
# 遮掉它文字卡片/表格渲染全挂。skills 与 kb_samples 由 compose 挂载（用户的自定义
# 技能与语料不该烤进镜像），此处只预建空目录
COPY --chown=app:app data/fonts ./data/fonts

# 运行期目录预建并属主给 app：没挂卷时也写得进；挂了卷由 entrypoint 修属权
# （compose 的 bind mount 指向不存在的目录时 docker 以 root:root 创建它）
RUN mkdir -p data/logs data/archive data/backups data/budget data/workspace \
        data/cache data/sessions data/privacy data/skills data/kb_samples \
    && chown -R app:app data

COPY --chown=app:app scripts/docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV PATH="/usr/local/pg16:/app/.venv/bin:${PATH}" \
    LD_LIBRARY_PATH="/usr/local/pg16/lib" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 刻意**不**在这里 USER app：compose 的 bind mount 指向不存在的目录时，docker
# 以 root:root 创建它，此时进程已降权就改不了属主了（budget/archive 的写入是
# 容错降级，症状是「日志只剩控制台、账本静默不记」，极难排查）。统一由
# docker-entrypoint.sh 在 root 下修完属权再 gosu 降到 app 运行；若部署者用
# compose 的 user: 指定身份启动（宿主 uid 非 1000 时），则走 entrypoint 的
# 非 root 分支直接运行。
# 反向 WS 服务端与 web 总览共用这一个端口（fastapi driver）
EXPOSE 8080

# 只探端口就绪：协议端连没连上是另一回事，未连接时 bot 同样是健康的
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import socket;s=socket.create_connection(('127.0.0.1',8080),3);s.close()"

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "bot.py"]
