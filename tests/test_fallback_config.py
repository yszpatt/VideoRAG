"""本地降级（Local Fallback）L0：config 档位判定 + factory 装配测试。

对应 docs/plans/2026-09-03-local-fallback-design.md §3.1/§3.2/§5.1。
"""

import pytest

from app.config import Settings
from app.core.factory import build_embedder, build_transcribers


# ---------- config 档位判定 ----------

def test_asr_mode_default_local():
    """未配置远程 ASR → local（默认 sensevoice 档）。"""
    s = Settings(_env_file=None, data_dir="/tmp/t")
    assert s.asr_mode == "local"
    assert s.asr_fallback == "sensevoice"


def test_asr_mode_cloud_when_remote_configured():
    """远程 provider+base_url 配齐 → cloud（远程优先）。"""
    s = Settings(_env_file=None, data_dir="/tmp/t",
                 cloud_asr_provider="sensevoice", cloud_asr_base_url="http://192.168.x.x:9991")
    assert s.asr_mode == "cloud"


def test_asr_mode_none_when_fallback_disabled():
    """asr_fallback=none 且无远程 → none（禁用降级）。"""
    s = Settings(_env_file=None, data_dir="/tmp/t", asr_fallback="none")
    assert s.asr_mode == "none"


def test_asr_local_endpoint_default_and_override():
    s = Settings(_env_file=None, data_dir="/tmp/t")
    assert s.asr_local_endpoint == "http://127.0.0.1:9991"  # 裸机缺省
    s2 = Settings(_env_file=None, data_dir="/tmp/t", local_asr_base_url="http://asr:9991")
    assert s2.asr_local_endpoint == "http://asr:9991"        # Docker 注入
    s3 = Settings(_env_file=None, data_dir="/tmp/t", local_asr_base_url=" http://asr:9991/ ")
    assert s3.asr_local_endpoint == "http://asr:9991"        # 去空白/斜杠


def test_embed_mode_remote_when_openai_configured():
    s = Settings(_env_file=None, data_dir="/tmp/t",
                 embed_provider="openai", embed_base_url="http://192.168.x.x:11434/v1")
    assert s.embed_mode == "remote"


def test_embed_mode_local_by_default():
    """默认 fastembed → local；local_embed_model_dir 手动目录生效。"""
    s = Settings(_env_file=None, data_dir="/tmp/t")
    assert s.embed_mode == "local"
    assert s.embed_model_dir_effective == "/tmp/t/models"
    s2 = Settings(_env_file=None, data_dir="/tmp/t", local_embed_model_dir="/nfs/models")
    assert s2.embed_model_dir_effective == "/nfs/models"


def test_asr_model_dir_effective():
    s = Settings(_env_file=None, data_dir="/tmp/t")
    assert s.asr_model_dir_effective == "/tmp/t/models/asr"   # 自动管理落点
    s2 = Settings(_env_file=None, data_dir="/tmp/t", local_asr_model_dir="/nfs/asr")
    assert s2.asr_model_dir_effective == "/nfs/asr"           # 手动指定优先


def test_runtime_env_override_local_fields(monkeypatch, tmp_path):
    """runtime.env（apply_runtime）可覆盖本地降级字段。"""
    s = Settings(_env_file=None, data_dir=str(tmp_path))
    s2 = s.apply_runtime({"LOCAL_ASR_BASE_URL": "http://asr:9991",
                          "ASR_FALLBACK": "whisper",
                          "LOCAL_EMBED_MODEL_DIR": "/x/models"})
    assert s2.asr_local_endpoint == "http://asr:9991"
    assert s2.asr_fallback == "whisper"
    assert s2.local_embed_model_dir == "/x/models"


# ---------- factory 装配 ----------

def test_build_transcribers_default_local_sensevoice():
    """未配置远程：Subtitle + CloudASR 指向本地端点（provider=local-sensevoice）。"""
    s = Settings(_env_file=None, data_dir="/tmp/t")
    chain = build_transcribers(s)
    assert [c.name for c in chain] == ["subtitle", "cloud_asr"]
    last = chain[-1]
    assert last._provider == "local-sensevoice"
    assert last._base == "http://127.0.0.1:9991"


def test_build_transcribers_local_sensevoice_respects_endpoint():
    s = Settings(_env_file=None, data_dir="/tmp/t", local_asr_base_url="http://asr:9991")
    chain = build_transcribers(s)
    assert chain[-1]._base == "http://asr:9991"


def test_build_transcribers_cloud_unchanged():
    """远程配置齐全 → cloud 路径与现状一致。"""
    s = Settings(_env_file=None, data_dir="/tmp/t",
                 cloud_asr_provider="sensevoice", cloud_asr_base_url="http://192.168.x.x:9991",
                 cloud_asr_model="sensevoice")
    chain = build_transcribers(s)
    assert [c.name for c in chain] == ["subtitle", "cloud_asr"]
    last = chain[-1]
    assert last._provider == "sensevoice"
    assert last._base == "http://192.168.x.x:9991"
    assert last._model == "sensevoice"


def test_build_transcribers_whisper_fallback():
    s = Settings(_env_file=None, data_dir="/tmp/t", asr_fallback="whisper")
    chain = build_transcribers(s)
    assert [c.name for c in chain] == ["subtitle", "whisper"]
    assert chain[-1]._model_size == "large-v3"


def test_build_transcribers_none_only_subtitle():
    """asr_fallback=none：仅字幕档（无转写 provider 时 pipeline 报可读错误）。"""
    s = Settings(_env_file=None, data_dir="/tmp/t", asr_fallback="none")
    chain = build_transcribers(s)
    assert [c.name for c in chain] == ["subtitle"]


def test_build_embedder_local_default():
    s = Settings(_env_file=None, data_dir="/tmp/t")
    e = build_embedder(s)
    assert e._provider == "fastembed"
    assert e._local_model_dir is None
    assert e._models_dir == "/tmp/t/models"


def test_build_embedder_local_manual_dir():
    s = Settings(_env_file=None, data_dir="/tmp/t", local_embed_model_dir="/nfs/models")
    e = build_embedder(s)
    assert e._local_model_dir == "/nfs/models"


def test_build_embedder_remote():
    s = Settings(_env_file=None, data_dir="/tmp/t",
                 embed_provider="openai", embed_model="bge-m3:latest",
                 embed_base_url="http://192.168.x.x:11434/v1")
    e = build_embedder(s)
    assert e._provider == "openai"
    assert e._base_url == "http://192.168.x.x:11434/v1"


def test_embed_mode_falls_back_local_when_remote_cleared():
    """清空远程 embedding 配置（空串）→ 回落本地 fastembed（而非 none）。"""
    s = Settings(
        _env_file=None, data_dir="/tmp/t",
        embed_provider="", embed_base_url="", embed_model="",
    )
    assert s.embed_mode == "local"
    assert s.embed_provider_effective == "fastembed"
    assert s.embed_model_effective == "BAAI/bge-small-zh-v1.5"


def test_embed_mode_none_only_when_explicit():
    """embed_mode=none 仅由显式 embed_provider='none' 触发。"""
    s = Settings(_env_file=None, data_dir="/tmp/t", embed_provider="none")
    assert s.embed_mode == "none"


def test_asr_mode_falls_back_local_when_remote_cleared():
    """清空集中式 ASR 参数 → asr_mode 回落 local（sensevoice 本地档）。"""
    s = Settings(
        _env_file=None, data_dir="/tmp/t",
        cloud_asr_provider="", cloud_asr_base_url="", asr_fallback="sensevoice",
    )
    assert s.asr_mode == "local"
    s2 = Settings(
        _env_file=None, data_dir="/tmp/t",
        cloud_asr_provider="sensevoice", cloud_asr_base_url="",  # 只留 provider
    )
    assert s2.asr_mode == "local"  # base_url 空即不算远程
