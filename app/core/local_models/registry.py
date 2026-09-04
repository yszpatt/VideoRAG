"""本地模型注册表（静态定义）。

每类本地模型定义下载来源、文件清单与完整性基准。
ASR = sherpa-onnx SenseVoice int8（ModelScope int8 镜像，2 文件，静态清单；
      sha256 为实测值，见 deploy/asr/models/）。
Embedding = fastembed bge-small-zh-v1.5（HF hub 缓存结构，snapshot 下载）。

设计来源：docs/plans/2026-09-03-local-fallback-design.md §3.4。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---- 常量 ----
MODELSCOPE_API = "https://modelscope.cn/api/v1/models/{repo}/repo/files"
MODELSCOPE_RESOLVE = "https://modelscope.cn/models/{repo}/resolve/{revision}/{path}"

KINDS = ("asr", "embedding")


@dataclass(frozen=True)
class ModelFile:
    """模型清单中的单个文件。size 用于进度/磁盘预检；sha256 用于完整性校验。"""

    path: str          # ModelScope 仓库内路径（本地文件名与之相同）
    size: int          # 字节（实测/官方）
    sha256: str = ""


@dataclass(frozen=True)
class ModelSpec:
    """一类本地模型的下载与校验定义。"""

    kind: str
    label: str
    source: str                        # "modelscope" | "huggingface"
    repo: str                          # 源仓库 id
    revision: str = "master"
    files: tuple[ModelFile, ...] = ()  # modelscope：静态文件清单
    hf_repo: str = ""                  # huggingface：模型仓库（如 Qdrant/bge-small-zh-v1.5）
    hf_files: tuple[str, ...] = ()     # huggingface：就绪判定必需文件（存在即视为已装）
    required: tuple[str, ...] = ()     # 就绪判定必需文件（相对目标目录）
    subdir: str = ""                   # 相对 models_dir 的目标子目录（空=根）

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def resolve_urls(self) -> list[str]:
        """ModelScope resolve 直链（按 files 顺序）。"""
        return [
            MODELSCOPE_RESOLVE.format(repo=self.repo, revision=self.revision, path=f.path)
            for f in self.files
        ]


ASR_SPEC = ModelSpec(
    kind="asr",
    label="SenseVoice int8（sherpa-onnx）",
    source="modelscope",
    repo="poloniumrock/SenseVoiceSmallOnnx",
    revision="master",
    files=(
        ModelFile("model.int8.onnx", 239_233_841,
                  "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51"),
        ModelFile("tokens.txt", 315_894,
                  "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc"),
    ),
    required=("model.int8.onnx", "tokens.txt"),
    subdir="asr",
)

EMBEDDING_SPEC = ModelSpec(
    kind="embedding",
    label="bge-small-zh-v1.5（fastembed ONNX, 512d）",
    source="huggingface",
    repo="",
    hf_repo="Qdrant/bge-small-zh-v1.5",
    # fastembed 加载必需文件（存在即视为已装；snapshot 全量下载）
    hf_files=("model_optimized.onnx", "tokenizer.json", "config.json"),
)

MODELS: dict[str, ModelSpec] = {s.kind: s for s in (ASR_SPEC, EMBEDDING_SPEC)}


def get_spec(kind: str) -> ModelSpec | None:
    return MODELS.get(kind)
