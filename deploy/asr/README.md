# 本地降级 ASR 侧车（sherpa-onnx + SenseVoice int8）

videoRAG「本地降级」功能的 ASR 运行时。当远程（集中式）转写服务参数未配置时，
videorag 自动回落到本服务。相比旧 `deploy/sensevoice/`（FunASR+torch，镜像 3-4GB），
本实现只依赖 sherpa-onnx（自带 ONNX 内核），无 torch/funasr/modelscope，开箱即离线。

实测（2026-09-03，见设计文档 §5.1b）：24.9 分钟音频 RTF≈0.078（6 线程）、
稳态 RSS≈670MB、加载 2.49s、词级时间戳聚合为句子级 segments，长音频 60s 分块无缝。

## 文件

| 文件 | 说明 |
|------|------|
| `server.py` | OpenAI 兼容转写服务（POST /v1/audio/transcriptions、GET /health、GET /v1/models） |
| `requirements.txt` | 运行时依赖（sherpa-onnx / fastapi / uvicorn / numpy） |
| `Dockerfile` | 侧车镜像（python:3.11-slim，模型经卷挂载不入镜像） |
| `models/` | 本地模型（**不入库**，见 .gitignore；首次用 videorag 下载或手动放置） |

模型 = `model.int8.onnx`（229MB，sha256 见 `app/core/local_models/registry.py`）+ `tokens.txt`
（315KB）。来源：ModelScope `poloniumrock/SenseVoiceSmallOnnx`（等价 GitHub
`sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17`）。

## 裸机起法

```bash
cd deploy/asr
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 模型：方式 A 用 videorag「设置 → 本地模型」下载后软链/拷贝；
#       方式 B 手动放 models/（ModelScope resolve 直链，见 registry.py）
python server.py --model models/model.int8.onnx --tokens models/tokens.txt \
    --port 9991 --num-threads 4
curl http://127.0.0.1:9991/health        # → {"status":"ok",...}
```

默认 videorag 侧本地端点 `http://127.0.0.1:9991`（`LOCAL_ASR_BASE_URL` 留空即此值）。

## Docker（compose profile）

```bash
# videorag 容器负责下载模型到 ./videorag-data/models/asr；侧车只读共享该目录
docker compose --profile local-asr up -d
```

- 侧车不暴露宿主端口，videorag 经容器网 `http://asr:9991` 访问（videorag 环境已注入
  `LOCAL_ASR_BASE_URL=http://asr:9991`，仅在本 fallback 启用时被消费）；
- 模型真源 = `./videorag-data/models/asr`（videorag 写、asr 只读挂载 `/data:ro`）；
- `restart: unless-stopped` + `/health` healthcheck；模型缺失时容器无法启动（打印指引后退出，
  避免静默挂起）——先在 videorag UI 下载模型再启 profile，或把模型预置到共享目录；
- 端口/线程可经环境覆盖：`PORT`、`NUM_THREADS`、`MODEL_DIR`（默认 `/data/models/asr`）。

## 手动验证（与 videorag 联调）

```bash
curl -F "file=@/tmp/a.mp3" -F "model=sensevoice" -F "response_format=verbose_json" \
    http://127.0.0.1:9991/v1/audio/transcriptions | python -m json.tool | head
```

返回 `text` + `segments[]`（句子级 start/end/text）；videorag 的 `CloudASRTranscriber`
按 OpenAI 兼容格式消费，自动兜底无 segments 场景为整段。

## 与旧 FunASR 方案的关系

`deploy/sensevoice/` 保留（Windows GPU 集中式服务的参考实现，指向 192.168.x.x 部署）。
本侧车是**本地降级**形态的轻量运行时，两者可并存：远程优先、本地兜底。
