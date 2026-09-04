import asyncio
from pathlib import Path

# fastembed ONNX 就绪判定必需文件（与 registry.EMBEDDING_SPEC.hf_files 一致）。
# 手动目录直接含这些文件 → specific_model_path 直载；否则按 HF 缓存根（models--*）解析。
_LOCAL_REQUIRED_FILES = ("model_optimized.onnx", "tokenizer.json", "config.json")


class Embedder:
    """文本向量化：本地 fastembed（默认，CPU ONNX）或远程 OpenAI 兼容服务（如 Ollama）。

    - provider="fastembed"：本地懒加载 bge 系列 ONNX 模型，模型可能触发下载（约 100MB），
      必须在 to_thread 中执行，避免阻塞事件循环。
    - provider="openai"（或非 fastembed 值）：调用远程 /v1/embeddings（Ollama / vLLM 等），
      需配合 base_url（如 http://host:11434/v1）与 model_name（如 bge-m3:latest）。
    - local_model_dir：手动指定本地模型目录（fastembed≥0.6 specific_model_path）。
      非空时仅本地加载（不联网、不触发自动下载）；模型缺失抛可读错误，提示到设置页下载。
    """

    def __init__(
        self,
        model_name: str = "BAAI/bge-small-zh-v1.5",
        provider: str = "fastembed",
        base_url: str = "",
        api_key: str = "",
        model: object | None = None,
        models_dir: str | None = None,
        local_model_dir: str | None = None,
        client: object | None = None,
    ):
        self._model_name = model_name
        self._provider = provider
        self._base_url = base_url
        self._api_key = api_key
        self._model = model
        self._models_dir = models_dir
        self._local_model_dir = local_model_dir
        self._client = client

    def _get_model(self):
        if self._model is None:
            from fastembed import TextEmbedding

            kwargs = {"model_name": self._model_name}
            if self._local_model_dir:
                # 手动指定本地目录，双布局兼容（不联网、不触发自动下载）：
                # - 直接含 model_optimized.onnx 等的模型目录 → specific_model_path 直载；
                # - 否则视为 fastembed HF 缓存根（models--Qdrant--bge-small-zh-v1.5/…）
                #   → 走 cache_dir 本地快照解析。
                p = Path(self._local_model_dir)
                direct = all((p / f).is_file() for f in _LOCAL_REQUIRED_FILES)
                if direct:
                    kwargs["specific_model_path"] = str(p)
                else:
                    kwargs["cache_dir"] = str(p)
                kwargs["local_files_only"] = True
            else:
                kwargs["cache_dir"] = self._models_dir
            self._model = TextEmbedding(**kwargs)
        return self._model

    @property
    def fingerprint(self) -> dict:
        """模型指纹：向量库据此识别「换了 embedding 模型」（同维度不同模型也逃不掉）。"""
        return {"provider": self._provider, "model": self._model_name}

    async def embed_texts(
        self, texts: list[str], query: bool = False
    ) -> list[list[float]]:
        # 单一 provider，装配期已按 embed_mode 决定 remote/local，无运行期隐式降级：
        # embed_mode=remote 时 provider=openai（或非 fastembed）；embed_mode=local 时走 fastembed。
        if self._provider != "fastembed":
            return await self._embed_remote(texts)
        return await self._embed_local(texts, query=query)

    async def _embed_local(self, texts: list[str], query: bool = False):
        def _run():
            model = self._get_model()  # 懒加载（含模型下载），线程内执行
            return list(model.embed(texts, query=query))

        vecs = await asyncio.to_thread(_run)
        return [v.tolist() if hasattr(v, "tolist") else list(v) for v in vecs]

    async def _embed_remote(self, texts: list[str]) -> list[list[float]]:
        """OpenAI 兼容 /v1/embeddings（Ollama bge-m3 / vLLM 等）。"""
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                base_url=self._base_url or "http://localhost:11434/v1",
                api_key=self._api_key or "ollama",
            )
        resp = await self._client.embeddings.create(
            model=self._model_name, input=texts
        )
        data = sorted(resp.data, key=lambda d: d.index)
        return [d.embedding for d in data]
