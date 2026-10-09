#!/usr/bin/env bash
# 预下载镜像构建所需的 wheel 到 packaging/wheels/，**可反复重跑、断点续传**。
#
# 为什么需要：
#   本项目的 Dockerfile 用 `pip install --no-cache-dir`，而本机 docker 没装 buildx
#   （`docker buildx` 不存在），因此用不了 BuildKit 的 `--mount=type=cache`。
#   构建一旦中断，已下载的包全部作废——在慢镜像源上（实测 66 KB/s、依赖树 ~1GB）
#   代价极大，这正是「中断白下」的根因。
#
# 做法：
#   在 python:3.11-slim（与运行时同平台、同 Python 版本）里用 pip download 把依赖
#   下到宿主目录 packaging/wheels/。pip 会跳过目标目录里已存在的 wheel，所以中断后
#   重跑是**续传**；全部下完后 Dockerfile 用 `--find-links=/wheels` 直接从本地装，
#   构建阶段不再走网络（缺什么才回落索引源）。
#
# 用法：
#   scripts/prefetch_wheels.sh                  # 反复尝试直到下完
#   scripts/prefetch_wheels.sh --attempts 3     # 最多 3 次
#   PIP_INDEX_URL=https://pypi.org/simple scripts/prefetch_wheels.sh
#
# 产物目录 packaging/wheels/ 已在 .gitignore 中（体积大、不入库）。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WHEELS="${WHEELS_DIR:-${ROOT}/packaging/wheels}"
INDEX="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
PY_IMAGE="${PY_IMAGE:-python:3.11-slim}"
MAX_ATTEMPTS=0   # 0 = 不限次数

while [ $# -gt 0 ]; do
  case "$1" in
    --attempts) MAX_ATTEMPTS="${2:-0}"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "未知参数：$1（可用：--attempts N）" >&2; exit 2 ;;
  esac
done

mkdir -p "$WHEELS"
echo "wheel 缓存：$WHEELS"
echo "索引源    ：$INDEX"
echo "下载镜像  ：$PY_IMAGE"

# 用 uv.lock 导出运行时依赖清单（与镜像里 `pip install .` 的口径一致：不含 dev/desktop），
# 这样 wheelhouse 的内容是**锁定版本**，不会因为构建时重新解析而漂移。
REQ="$(mktemp -t videorag-req.XXXXXX.txt)"
trap 'rm -f "$REQ"' EXIT
if command -v uv >/dev/null 2>&1; then
  if (cd "$ROOT" && uv export --format requirements-txt --no-dev --no-emit-project --no-hashes > "$REQ" 2>/dev/null); then
    # 清单也放进 wheelhouse：镜像按它安装，保证「装的版本 == 下载的版本」，
    # 不会出现 pip 按 pyproject 解析出更新版本、又回头去网上补下的情况。
    cp "$REQ" "$WHEELS/requirements.txt"
    echo "依赖清单  ：uv.lock 导出（$(grep -c . "$REQ") 行），已写入 $WHEELS/requirements.txt"
  else
    echo "警告：uv export 失败，回退为按 pyproject 解析" >&2
    : > "$REQ"
  fi
else
  echo "未找到 uv：改为按 pyproject 解析依赖"
  : > "$REQ"
fi

attempt=0
while :; do
  attempt=$((attempt + 1))
  echo
  echo "── 第 ${attempt} 次尝试  $(date '+%H:%M:%S') ──"
  # 两种口径：有 requirements 就按它下；没有就直接对项目目录 pip download .
  if [ -s "$REQ" ]; then
    docker run --rm \
      -v "${WHEELS}:/wheels" \
      -v "${REQ}:/req.txt:ro" \
      -e PIP_INDEX_URL="$INDEX" \
      "$PY_IMAGE" \
      sh -c 'pip download --no-cache-dir --retries 5 --timeout 30 -i "$PIP_INDEX_URL" -r /req.txt -d /wheels'
  else
    docker run --rm \
      -v "${WHEELS}:/wheels" \
      -v "${ROOT}:/src" \
      -w /src \
      -e PIP_INDEX_URL="$INDEX" \
      "$PY_IMAGE" \
      sh -c 'pip download --no-cache-dir --retries 5 --timeout 30 -i "$PIP_INDEX_URL" -d /wheels .'
  fi
  code=$?

  files=$(find "$WHEELS" -maxdepth 1 -type f \( -name '*.whl' -o -name '*.tar.gz' \) | wc -l | tr -d ' ')
  size=$(du -sh "$WHEELS" 2>/dev/null | cut -f1)
  if [ "$code" -eq 0 ]; then
    echo "✓ 依赖已全部下载：${files} 个文件、${size}"
    exit 0
  fi
  echo "✗ 本次未完成（exit=${code}）：已下 ${files} 个文件、${size} 保留在缓存里，下次续传"
  if [ "$MAX_ATTEMPTS" -gt 0 ] && [ "$attempt" -ge "$MAX_ATTEMPTS" ]; then
    echo "达到最大尝试次数 ${MAX_ATTEMPTS}，退出（重跑本脚本即续传）"
    exit 1
  fi
  sleep 5
done
