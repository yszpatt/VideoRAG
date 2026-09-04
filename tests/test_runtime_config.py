import pytest

from app.config import Settings
from app.core.runtime_config import load_runtime_env, save_runtime_env


def test_runtime_env_save_load_roundtrip(tmp_path):
    data_dir = str(tmp_path)
    save_runtime_env(data_dir, {"LLM_MODEL": "ornith-1.5:9b", "LLM_BASE_URL": "http://x:11434/v1"})
    loaded = load_runtime_env(data_dir)
    assert loaded["LLM_MODEL"] == "ornith-1.5:9b"
    assert loaded["LLM_BASE_URL"] == "http://x:11434/v1"
    # 再次保存保留旧键
    save_runtime_env(data_dir, {"TTS_PROVIDER": "openai"})
    loaded2 = load_runtime_env(data_dir)
    assert loaded2["LLM_MODEL"] == "ornith-1.5:9b"
    assert loaded2["TTS_PROVIDER"] == "openai"


def test_runtime_env_missing_file(tmp_path):
    assert load_runtime_env(str(tmp_path)) == {}


def test_runtime_env_ignores_comments_and_blanks(tmp_path):
    path = tmp_path / "runtime.env"
    path.write_text("# comment\n\nEMBED_MODEL=bge-m3:latest\n", encoding="utf-8")
    assert load_runtime_env(str(tmp_path)) == {"EMBED_MODEL": "bge-m3:latest"}


def test_apply_runtime_overrides_env(monkeypatch):
    """runtime.env 覆盖环境变量（在线配置优先级最高）。"""
    monkeypatch.setenv("LLM_MODEL", "env-model")
    s = Settings(_env_file=None)
    assert s.llm_model == "env-model"
    s2 = s.apply_runtime({"LLM_MODEL": "runtime-model", "LLM_BASE_URL": "http://x/v1"})
    assert s2.llm_model == "runtime-model"
    assert s2.llm_base_url == "http://x/v1"
    assert s.llm_model == "env-model"  # 原对象不变


def test_apply_runtime_ignores_unknown_keys():
    s = Settings(_env_file=None)
    s2 = s.apply_runtime({"NOT_A_FIELD": "x", "ALSO_BAD": "y"})
    assert s2 is s  # 无有效字段时返回原对象


def test_apply_runtime_empty_value_clears():
    s = Settings(_env_file=None)
    s2 = s.apply_runtime({"LLM_MODEL": ""})
    assert s2.llm_model == ""


def test_apply_runtime_coerces_numeric_fields():
    """runtime.env 数字均为字符串：int/float 字段须强转（检索参数依赖此行为）。"""
    s = Settings(_env_file=None)
    s2 = s.apply_runtime({
        "RETRIEVAL_VECTOR_K": "32",
        "RETRIEVAL_MIN_SIM": "0.35",
        "RETRIEVAL_FTS_WEIGHT": "bad",  # 非法数字 → 忽略保持原值
    })
    assert s2.retrieval_vector_k == 32 and isinstance(s2.retrieval_vector_k, int)
    assert s2.retrieval_min_sim == 0.35 and isinstance(s2.retrieval_min_sim, float)
    assert s2.retrieval_fts_weight == 0.3  # 非法值不生效
    assert s.retrieval_vector_k == 24  # 原对象不变
