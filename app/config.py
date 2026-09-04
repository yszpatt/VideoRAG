from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置：环境变量优先，其次 .env 文件，最后默认值。

    - 环境变量（容器注入 / docker-compose environment / export）优先级最高
    - 项目根目录 .env 文件为默认配置来源（.env 不入库，模板见 .env.example）
    - 未设置时使用下方默认值
    - 在线修改的配置（$DATA_DIR/runtime.env）经 apply_runtime 应用后优先级最高
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ============ LLM（OpenAI 兼容格式，默认 DeepSeek）============
    # provider 仅为服务标识（deepseek / openai / 本地 vLLM 等），
    # 只要 base_url + api_key + model 三件套即可接入任意 OpenAI 兼容服务。
    llm_provider: str = "deepseek"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-v4-flash"

    # ============ TTS（可选，OpenAI 兼容 /v1/audio/speech）============
    # provider 为空表示未启用；如 openai / azure / edge-tts 等。
    tts_provider: str = ""
    tts_api_key: str = ""
    tts_base_url: str = ""
    tts_model: str = ""

    # ============ Embedding ============
    # fastembed（本地 ONNX，默认）/ openai（远程 OpenAI 兼容 /v1/embeddings，如 Ollama bge-m3）
    embed_provider: str = "fastembed"
    embed_model: str = "BAAI/bge-small-zh-v1.5"
    embed_base_url: str = ""   # provider=openai 时必填，如 http://192.168.x.x:11434/v1
    embed_api_key: str = ""    # 远程服务无鉴权时留空即可
    # 手动指定本地 fastembed 模型目录（空=用 models_dir 的 HF hub 缓存自动管理）
    local_embed_model_dir: str = ""
    # Embedding 模型下载镜像端点；空=官方 huggingface.co（国内可设 https://hf-mirror.com）
    embed_download_endpoint: str = ""

    # ============ ASR（转写）============
    whisper_model: str = "large-v3"  # 本地回退模型（ASR_FALLBACK=whisper 档使用）
    # 可选 provider（配置了才启用）：局域网集中转写服务
    cloud_asr_provider: str = ""
    cloud_asr_key: str = ""
    cloud_asr_base_url: str = ""  # 集中转写服务根地址，如 http://host:8000
    cloud_asr_model: str = ""     # 可选，指定远程模型名（默认由服务决定）

    # ============ 本地降级（未配置远程参数时启用）============
    # 本地降级档位：sensevoice（默认，sherpa-onnx 服务）/ whisper（保留档）/ none（禁用降级）
    asr_fallback: str = "sensevoice"
    # 本地 SenseVoice OpenAI 兼容端点。空=按形态默认（asr_local_endpoint 属性）：
    #   Docker 环境由 compose 注入 http://asr:9991；裸机回退 http://127.0.0.1:9991
    local_asr_base_url: str = ""
    # 手动指定本地 SenseVoice 模型目录（含 model.int8.onnx + tokens.txt；空=用 models_dir 自动管理）
    local_asr_model_dir: str = ""

    # ============ MCP ============
    mcp_api_key: str = ""

    # ============ 其他可选 provider ============
    gemini_api_key: str = ""

    # ============ 目录与运行 ============
    data_dir: str = "/data"
    cookie_dir: str = "/data/cookies"
    port: int = 8080
    max_concurrent_tasks: int = 1

    # E5 历史记录：每类（ask/search）保留条数上限，超限环形淘汰最旧
    history_limit: int = 200

    # ============ 检索策略（设置页「检索」组在线可调，写 runtime.env 持久化）============
    # 流水线：宽召回 → 加权 RRF 融合 → 相关性过滤 → 相邻切片合并 → 单视频配额
    retrieval_vector_k: int = 24        # 向量召回条数
    retrieval_fts_k: int = 24           # 全文召回条数
    retrieval_rrf_k: int = 60           # RRF 平滑常数，越大名次差异越平缓
    retrieval_vector_weight: float = 0.7  # 向量路权重（语义相关性为主）
    retrieval_fts_weight: float = 0.3     # 全文路权重（关键词/术语佐证）
    retrieval_per_video_cap: int = 3     # 单视频最多占的结果位数（0 = 不限）
    retrieval_min_sim: float = 0.0       # 向量路最低余弦相似度门槛（0 = 关闭；建议 0.3）
    retrieval_neighbor_gap: float = 2.0  # 相邻切片合并窗口（秒）；0 = 关闭

    @property
    def db_path(self) -> str:
        return f"{self.data_dir}/db/videorag.db"

    @property
    def lancedb_path(self) -> str:
        return f"{self.data_dir}/lancedb"

    @property
    def models_dir(self) -> str:
        return f"{self.data_dir}/models"

    # ---- 便捷属性：判断各服务是否已配置 ----

    @property
    def llm_api_key_effective(self) -> str:
        """实际使用的 LLM key。

        本地/自定义 OpenAI 兼容服务（如 Ollama）通常无需鉴权，
        用户显式配置了非默认 base_url 时用占位 key 通过 SDK 检查。
        """
        if self.llm_api_key:
            return self.llm_api_key
        if self.llm_base_url != "https://api.deepseek.com":
            return "ollama"
        return ""

    @property
    def llm_enabled(self) -> bool:
        """LLM 可用：OpenAI 兼容三件套齐全（至少要有 key 与 base_url）。"""
        return bool(self.llm_api_key_effective and self.llm_base_url)

    @property
    def tts_enabled(self) -> bool:
        """TTS 可用：provider 与 base_url 已配置。"""
        return bool(self.tts_provider and self.tts_base_url)

    @property
    def embed_enabled(self) -> bool:
        """Embedding 可用：fastembed 默认启用；openai 模式需有 base_url。"""
        if self.embed_provider == "fastembed":
            return True
        return bool(self.embed_base_url)

    # ---- 本地降级档位判定（"已配置优先，未配置降级"）----

    @property
    def embed_provider_effective(self) -> str:
        """实际使用的 embedding provider：空串回落 fastembed（清空远程配置 → 本地档）。"""
        return self.embed_provider or "fastembed"

    @property
    def embed_model_effective(self) -> str:
        """实际使用的 embedding 模型名：空串回落默认 bge-small-zh-v1.5（本地档必需）。"""
        return self.embed_model or "BAAI/bge-small-zh-v1.5"

    @property
    def embed_remote_configured(self) -> bool:
        """远程 embedding 已配置：provider 非 fastembed 且 base_url 非空。

        空 provider（清空远程配置）经 embed_provider_effective 回落 fastembed → 非远程。
        """
        return self.embed_provider_effective != "fastembed" and bool(self.embed_base_url)

    @property
    def embed_mode(self) -> str:
        """embedding 运行档位："remote" | "local" | "none"。

        remote：远程 openai 兼容参数配齐；local：本地 fastembed（含清空远程配置回落）；
        none：显式 embed_provider="none" 禁用向量化。
        """
        if self.embed_remote_configured:
            return "remote"
        if self.embed_provider == "none":
            return "none"
        return "local"  # fastembed 本地（含默认与清空远程后的回落）

    @property
    def asr_mode(self) -> str:
        """ASR 运行档位："cloud" | "local" | "none"。

        cloud：集中式 ASR 参数配齐（provider + base_url）；
        local：未配置远程，按 asr_fallback 使用本地档（sensevoice/whisper）；
        none：asr_fallback=none 且未配置远程（仅字幕档，禁用转写降级）。
        """
        if self.cloud_asr_provider and self.cloud_asr_base_url:
            return "cloud"
        if self.asr_fallback == "none":
            return "none"
        return "local"  # sensevoice / whisper 均为 local 形态

    @property
    def asr_local_endpoint(self) -> str:
        """本地 SenseVoice OpenAI 兼容端点：显式配置优先，空则回退 127.0.0.1:9991。"""
        return (self.local_asr_base_url or "").strip().rstrip("/") or "http://127.0.0.1:9991"

    @property
    def asr_model_dir_effective(self) -> str:
        """本地 SenseVoice 模型目录：手动指定优先，否则 models_dir/asr（下载器默认落点）。"""
        return self.local_asr_model_dir.rstrip("/") or f"{self.models_dir}/asr"

    @property
    def embed_model_dir_effective(self) -> str:
        """本地 embedding 模型目录：手动指定优先，否则 models_dir（fastembed HF 缓存根）。"""
        return self.local_embed_model_dir.rstrip("/") or self.models_dir

    def apply_runtime(self, overrides: dict[str, str]) -> "Settings":
        """应用在线修改的配置（$DATA_DIR/runtime.env），优先级最高。

        overrides 形如 {"LLM_BASE_URL": "...", "LLM_MODEL": "..."}（env 风格键名），
        只更新存在的字段，返回新实例（不修改原对象）。
        runtime.env 里的值均为字符串；int/float 字段在此强转，
        非法数字忽略该键（保持原值），避免 model_copy 绕过类型校验。
        """
        updates: dict[str, object] = {}
        fields = type(self).model_fields
        env2field = {f.upper(): f for f in fields}
        for key, value in overrides.items():
            field = env2field.get(key.upper())
            if field is None:  # 未知字段忽略
                continue
            if isinstance(value, str) and fields[field].annotation in (int, float):
                if value == "":  # 空值对数字字段无意义，忽略
                    continue
                try:
                    updates[field] = fields[field].annotation(value)
                except (TypeError, ValueError):
                    continue  # 非法数字：忽略该键，保持原值
            else:
                updates[field] = value
        if not updates:
            return self
        return self.model_copy(update=updates)
