# ---- 阶段 1：前端构建 ----
FROM node:20-alpine AS web
WORKDIR /web
# 镜像源可经 --build-arg 覆盖：国内默认 npmmirror，CI / 海外可用官方源。
# 带重试：这两个源在国内/代理环境下偶发 SSL 中断，一次失败就整个构建失败太脆。
ARG NPM_REGISTRY=https://registry.npmmirror.com
COPY web/package.json web/package-lock.json ./
RUN npm ci --registry="$NPM_REGISTRY" --fetch-retries=5 --fetch-retry-maxtimeout=120000
COPY web ./
RUN npm run build

# ---- 阶段 2：Python 依赖 ----
# 单独成阶段是为了既能用本地 wheel 缓存（抗中断），又不把 ~1GB wheels 打进最终镜像
# （在同一阶段里删掉也没用：COPY 出的层仍然占体积）。
FROM python:3.11-slim AS pydeps

# 包索引可经 --build-arg 覆盖：国内默认清华源，CI / 海外可用 https://pypi.org/simple
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /srv/app
# 本地 wheel 缓存：scripts/prefetch_wheels.sh 生成（可反复重跑、断点续传）。
# 目录为空也没关系——--find-links 找不到就回落到索引源，只是慢。
# 这是「中断白下」的解药：下载发生在宿主侧、可续传，构建阶段通常不再走网络。
COPY packaging/wheels /wheels
COPY pyproject.toml ./
COPY app ./app
# 有锁定清单就按清单装依赖（版本与 uv.lock 一致，且全部命中本地 wheel、构建期零下载），
# 再单独装项目本体（--no-deps，不重新解析依赖）；没有清单时退回按 pyproject 解析。
RUN set -eux; \
    if [ -f /wheels/requirements.txt ]; then \
        # --no-index：只吃本地 wheel。实测仅用 --find-links 时，同版本下 pip 仍会
        # 选索引里的链接（白下），加 --no-index 才是真正的「零网络依赖安装」。
        pip install --no-cache-dir --no-index --find-links=/wheels \
            --prefix=/install -r /wheels/requirements.txt; \
        # 项目本体单独装（不重新解析依赖）；这一步需要构建后端 hatchling，走索引（很小）
        pip install --no-cache-dir --no-deps --retries 10 --timeout 60 \
            -i "$PIP_INDEX_URL" --prefix=/install .; \
    else \
        pip install --no-cache-dir --retries 10 --timeout 60 --find-links=/wheels \
            -i "$PIP_INDEX_URL" --prefix=/install .; \
    fi; \
    rm -rf /wheels

# ---- 阶段 3：后端运行时 ----
FROM python:3.11-slim

# 容器运行用户：UID/GID 经构建参数与宿主用户对齐（compose 从 VIDEORAG_UID/GID 注入，
# 默认 1000:1000）。这样绑定挂载的数据卷不会产生 root 属主文件，Docker 与裸机两种
# 运行形态可共用同一份数据，也不会出现「容器 root 写过、裸机普通用户写不进」的权限冲突。
# 注：passwd 属 Debian required 包，slim 基础镜像自带 groupadd/useradd。
ARG APP_UID=1000
ARG APP_GID=1000

# XDG_CACHE_HOME 与 HOME 解耦：yt-dlp 等第三方工具把缓存写到 /tmp/.cache，
# 即使运行期用 `--user` 覆盖了 UID（HOME 不可写）也不会因此报错。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/app \
    XDG_CACHE_HOME=/tmp/.cache

# /data 预创建并归属 app：具名/匿名卷首次初始化会继承该属主，
# 未绑定宿主目录时也能以非 root 写入（绑定挂载则以宿主目录属主为准）。
RUN set -eux; \
    groupadd --gid "${APP_GID}" app; \
    useradd --create-home --uid "${APP_UID}" --gid "${APP_GID}" \
            --shell /usr/sbin/nologin app; \
    mkdir -p /data; \
    chown app:app /data

# ffmpeg 供音频提取与视觉旁路抽帧使用；
# libgl1 / libglib2.0-0 供 RapidOCR 依赖的 opencv 导入（slim 基础镜像默认缺失，
# 缺库会导致 cv2 import 失败进而 OCR 层不可用）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/app

# 依赖来自 pydeps 阶段（等价于原来的 `pip install .`，但走本地 wheel 缓存）。
# 放在最前面：只改 app/ 或前端时，这一层不受影响，不必重装依赖树。
COPY --from=pydeps /install /usr/local

COPY app ./app
COPY --from=web /web/dist ./web/dist

# COPY 会原样保留构建上下文里的文件模式（不改权限），而运行期是**非 root 的 app 用户**，
# 文件属主在镜像里是 root：只要有一个源文件是 0600，容器就会起不来，报
# `PermissionError: .../app/api/xxx.py`（已真实踩到：本地 umask 收紧后新建的文件即 0600）。
# 统一补「他人可读 + 目录可进入」，不依赖检出时的 umask；属主保持 root（app 用户不该改自己的代码）。
RUN chmod -R a+rX /srv/app

# 以非 root 用户运行（UID/GID 与宿主一致，见上方构建参数）
USER app

VOLUME ["/data"]
EXPOSE 8566

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8566"]
