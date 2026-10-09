# ---- 阶段 1：前端构建 ----
FROM node:20-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --registry=https://registry.npmmirror.com
COPY web ./
RUN npm run build

# ---- 阶段 2：后端运行时 ----
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

COPY pyproject.toml ./
COPY app ./app
COPY --from=web /web/dist ./web/dist

# COPY 会原样保留构建上下文里的文件模式（不改权限），而运行期是**非 root 的 app 用户**，
# 文件属主在镜像里是 root：只要有一个源文件是 0600，容器就会起不来，报
# `PermissionError: .../app/api/xxx.py`（已真实踩到：本地 umask 收紧后新建的文件即 0600）。
# 统一补「他人可读 + 目录可进入」，不依赖检出时的 umask；属主保持 root（app 用户不该改自己的代码）。
RUN chmod -R a+rX /srv/app

# 国内镜像加速
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple .

# 以非 root 用户运行（UID/GID 与宿主一致，见上方构建参数）
USER app

VOLUME ["/data"]
EXPOSE 8566

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8566"]
