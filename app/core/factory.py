"""服务组件工厂：按配置构造 LLM / Embedder / Transcribers。

供 main.py（启动装配）与 settings API（在线热更新重建）复用。

装配语义（"已配置优先，未配置降级"，见 docs/plans/2026-09-03-local-fallback-design.md）：
- ASR：远程集中式参数配齐（provider+base_url）→ cloud；否则按 asr_fallback 用本地档
  （默认 sensevoice：指向本地 sherpa-onnx OpenAI 兼容端点，复用 CloudASRTranscriber；
  whisper 保留档）；asr_fallback=none 且无远程 → 仅字幕档。
- Embedding：provider=openai 且 base_url 配齐 → remote；否则 fastembed 本地。
"""

from app.config import Settings
from app.core.embed.embedder import Embedder
from app.core.llm import LLMClient
from app.core.transcribers.cloud_asr import CloudASRTranscriber
from app.core.transcribers.subtitle import SubtitleTranscriber
from app.core.transcribers.whisper import WhisperTranscriber


def build_transcribers(settings: Settings) -> list:
    """按配置构造转写链。

    - asr_mode == "cloud"：Subtitle + CloudASR（远程集中式，现状路径不变）
    - asr_mode == "local"：
        - asr_fallback == "whisper"：Subtitle + Whisper（本地 faster-whisper，保留档）
        - 否则（sensevoice 默认）：Subtitle + CloudASR 指向本地端点
          （provider=local-sensevoice 不含 "whisper"，不发送 faster-whisper 专有参数；
           分块/二分重试/无 segments 兜底全部继承）
    - asr_mode == "none"：仅 Subtitle（视频无字幕时 pipeline 报可读错误）
    """
    chain = [SubtitleTranscriber()]

    if settings.asr_mode == "cloud":
        chain.append(
            CloudASRTranscriber(
                settings.cloud_asr_base_url,
                api_key=settings.cloud_asr_key or "local",
                model=settings.cloud_asr_model or None,
                provider=settings.cloud_asr_provider,
            )
        )
    elif settings.asr_mode == "local":
        if settings.asr_fallback == "whisper":
            # 保留档：本地 faster-whisper（large-v3 等），模型懒下载到 models_dir
            chain.append(WhisperTranscriber(settings.whisper_model, model_dir=settings.models_dir))
        else:
            # 默认：本地 sherpa-onnx SenseVoice OpenAI 兼容端点（deploy/asr/）
            chain.append(
                CloudASRTranscriber(
                    settings.asr_local_endpoint,
                    api_key="local",
                    model="sensevoice",
                    provider="local-sensevoice",
                )
            )
    # asr_mode == "none"：仅字幕档，不追加任何转写 provider

    return chain


def build_llm(settings: Settings) -> LLMClient:
    return LLMClient(
        settings.llm_base_url, settings.llm_api_key_effective, settings.llm_model
    )


def build_embedder(settings: Settings) -> Embedder:
    """按配置构造向量化器（fastembed 本地 / openai 远程二选一）。

    - embed_mode == "remote"：OpenAI 兼容远程 /v1/embeddings（Ollama bge-m3 等）
    - embed_mode == "local"：fastembed 本地 ONNX；
      local_embed_model_dir 非空时按手动目录加载（specific_model_path），否则 HF 缓存。
    """
    return Embedder(
        settings.embed_model_effective,
        provider=settings.embed_provider_effective,
        base_url=settings.embed_base_url,
        api_key=settings.embed_api_key,
        models_dir=settings.models_dir,
        local_model_dir=settings.local_embed_model_dir or None,
    )
