# videorag 本地降级 ASR 侧车：sherpa-onnx + SenseVoice int8（OpenAI 兼容）。
# 设计来源：docs/plans/2026-09-03-local-fallback-design.md §3.6（D3）。
# 主镜像不装 sherpa-onnx（保持轻量）；模型经共享卷挂载（/data/models/asr，只读），
# 由 videorag 容器的「设置 → 本地模型」负责下载/校验。
FROM python:3.11-slim

# 运行用户：与主服务保持同一 UID/GID（compose 从 VIDEORAG_UID/GID 注入，默认 1000:1000），
# 保证共享模型卷 /data/models/asr 的属主一致、只读挂载也能正常读取。
ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/app \
    XDG_CACHE_HOME=/tmp/.cache

RUN set -eux; \
    groupadd --gid "${APP_GID}" app; \
    useradd --create-home --uid "${APP_UID}" --gid "${APP_GID}" \
            --shell /usr/sbin/nologin app

# ffmpeg：服务端把任意上传音频统一转 wav 16k mono（走 pipe:1，不落临时文件）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/asr

COPY requirements.txt ./
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

COPY server.py ./

# 以非 root 用户运行；上传音频的临时文件走 tempfile（/tmp，1777 可写）
USER app

EXPOSE 9991

# 模型目录/线程数经环境注入（compose 默认 /data/models/asr；裸机可 -e 覆盖）
CMD ["sh", "-c", \
     "python server.py --model ${MODEL_DIR:-/data/models/asr}/model.int8.onnx --tokens ${MODEL_DIR:-/data/models/asr}/tokens.txt \
      --port ${PORT:-9991} --num-threads ${NUM_THREADS:-4}"]
