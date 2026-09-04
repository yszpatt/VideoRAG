import os

import pytest

from app.config import Settings


def test_defaults():
    s = Settings(_env_file=None)
    assert s.data_dir == "/data"
    # LLM（OpenAI 兼容，默认 DeepSeek）
    assert s.llm_provider == "deepseek"
    assert s.llm_base_url == "https://api.deepseek.com"
    assert s.llm_model == "deepseek-v4-flash"
    # TTS 默认关闭
    assert s.tts_provider == ""
    assert s.tts_base_url == ""
    assert s.tts_api_key == ""
    assert s.tts_model == ""
    # Embedding
    assert s.embed_provider == "fastembed"
    assert s.embed_model == "BAAI/bge-small-zh-v1.5"
    assert s.whisper_model == "large-v3"
    assert s.mcp_api_key == ""
    assert s.max_concurrent_tasks == 1
    assert s.port == 8080


def test_llm_env_override_openai_compatible(monkeypatch):
    """LLM 支持 OpenAI 兼容格式：任意 base_url + key + model 三件套。"""
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("MAX_CONCURRENT_TASKS", "3")
    monkeypatch.setenv("MCP_API_KEY", "secret")
    s = Settings(_env_file=None)
    assert s.llm_provider == "openai"
    assert s.llm_base_url == "https://api.openai.com/v1"
    assert s.llm_api_key == "sk-test"
    assert s.llm_model == "gpt-4o-mini"
    assert s.llm_enabled is True
    assert s.max_concurrent_tasks == 3
    assert s.mcp_api_key == "secret"


def test_tts_env_override(monkeypatch):
    monkeypatch.setenv("TTS_PROVIDER", "openai")
    monkeypatch.setenv("TTS_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("TTS_API_KEY", "sk-tts")
    monkeypatch.setenv("TTS_MODEL", "tts-1")
    s = Settings(_env_file=None)
    assert s.tts_provider == "openai"
    assert s.tts_base_url == "https://api.openai.com/v1"
    assert s.tts_api_key == "sk-tts"
    assert s.tts_model == "tts-1"
    assert s.tts_enabled is True


def test_enabled_flags_default(monkeypatch):
    monkeypatch.delenv("TTS_PROVIDER", raising=False)
    monkeypatch.delenv("TTS_BASE_URL", raising=False)
    s = Settings(_env_file=None)
    assert s.tts_enabled is False  # 未配置 TTS
    assert s.llm_enabled is False  # 无 key
    assert s.embed_enabled is True  # fastembed 默认启用


def test_env_file_loading(tmp_path, monkeypatch):
    """.env 文件作为默认配置来源（环境变量优先于 .env）。"""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLM_BASE_URL=https://envfile.example.com/v1\n"
        "LLM_MODEL=envfile-model\n"
        "TTS_PROVIDER=envfile-tts\n"
        "EMBED_MODEL=envfile-embed\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("TTS_PROVIDER", raising=False)
    monkeypatch.delenv("EMBED_MODEL", raising=False)
    s = Settings(_env_file=str(env_file))
    assert s.llm_base_url == "https://envfile.example.com/v1"
    assert s.llm_model == "envfile-model"
    assert s.tts_provider == "envfile-tts"
    assert s.embed_model == "envfile-embed"


def test_env_priority_over_env_file(tmp_path, monkeypatch):
    """环境变量 > .env 文件。"""
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_BASE_URL=https://envfile.example.com/v1\n", encoding="utf-8")
    monkeypatch.setenv("LLM_BASE_URL", "https://real.example.com/v1")
    s = Settings(_env_file=str(env_file))
    assert s.llm_base_url == "https://real.example.com/v1"


def test_paths_derived_from_data_dir(monkeypatch):
    monkeypatch.setenv("DATA_DIR", "/tmp/videorag-test")
    s = Settings(_env_file=None)
    assert s.db_path == "/tmp/videorag-test/db/videorag.db"
    assert s.lancedb_path == "/tmp/videorag-test/lancedb"
    assert s.models_dir == "/tmp/videorag-test/models"
