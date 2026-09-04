import pytest


async def test_get_settings_defaults(client):
    resp = await client.get("/api/settings")
    assert resp.status_code == 200
    d = resp.json()
    assert set(d) == {"llm", "asr", "embedding", "retrieval", "local"}
    assert d["llm"]["provider"] == "deepseek"
    assert d["llm"]["base_url"] == "https://api.deepseek.com"
    assert d["llm"]["model"] == "deepseek-v4-flash"
    assert d["llm"]["api_key"] == ""  # 未配置为空
    assert d["asr"]["provider"] == ""
    assert d["embedding"]["provider"] == "fastembed"
    # 本地降级组默认值
    assert d["local"]["asr_fallback"] == "sensevoice"
    assert d["local"]["local_asr_base_url"] == ""
    assert d["local"]["local_asr_model_dir"] == ""
    assert d["local"]["local_embed_model_dir"] == ""
    assert d["local"]["embed_download_endpoint"] == ""


async def test_get_settings_masks_api_key(client, monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_API_KEY", "sk-1234567890abcdef")
    # 用带 key 的 Settings 重建 app（conftest 的 client 已用无 key settings 构造）
    from app.config import Settings
    from app.main import create_app

    settings = Settings(_env_file=None, data_dir=str(tmp_path))
    app = create_app(settings=settings)
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.get("/api/settings")
        d = resp.json()
        assert d["llm"]["api_key"] == "sk-1...cdef"  # 前4后4打码
        assert "1234567890" not in d["llm"]["api_key"]


async def test_put_settings_persists_and_masks(client, tmp_path):
    resp = await client.put(
        "/api/settings",
        json={
            "llm": {
                "provider": "ollama",
                "base_url": "http://192.168.x.x:11434/v1",
                "api_key": None,
                "model": "ornith-1.5:9b",
            },
            "embedding": {
                "provider": "openai",
                "model": "bge-m3:latest",
                "base_url": "http://192.168.x.x:11434/v1",
                "api_key": None,
            },
        },
    )
    assert resp.status_code == 200
    d = resp.json()
    assert d["applied"] is True
    assert set(d["saved"]) == {
        "LLM_PROVIDER", "LLM_BASE_URL", "LLM_MODEL",
        "EMBED_PROVIDER", "EMBED_MODEL", "EMBED_BASE_URL",
    }
    # 热更新已生效
    c = client._transport.app.state.components
    assert c["settings"].llm_provider == "ollama"
    assert c["settings"].llm_base_url == "http://192.168.x.x:11434/v1"
    assert c["settings"].embed_model == "bge-m3:latest"
    # LLM 组件已重建（使用占位 key）
    assert c["llm"]._api_key == "ollama"

    # GET 回读
    g = await client.get("/api/settings")
    gd = g.json()
    assert gd["llm"]["provider"] == "ollama"
    assert gd["embedding"]["model"] == "bge-m3:latest"


async def test_put_settings_mask_placeholder_keeps_key(client, monkeypatch, tmp_path):
    """PUT 传掩码占位 **** / null 时不覆盖已配置字段（null=未提供）。"""
    monkeypatch.setenv("LLM_API_KEY", "sk-abcdefghijklmnop")
    from app.config import Settings
    from app.main import create_app
    from httpx import ASGITransport, AsyncClient

    settings = Settings(_env_file=None, data_dir=str(tmp_path))
    app = create_app(settings=settings)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.put(
            "/api/settings",
            json={
                "llm": {
                    "provider": "deepseek",
                    "api_key": "****",   # 掩码占位：不覆盖
                    "base_url": None,     # null：未提供 → 不覆盖
                    "model": None,
                }
            },
        )
        assert resp.status_code == 200
        assert resp.json()["saved"] == ["LLM_PROVIDER"]
        # key 与 base_url 均保持不变（key 来自 env，base_url 为默认值）
        s = app.state.components["settings"]
        assert s.llm_api_key == "sk-abcdefghijklmnop"
        assert s.llm_base_url == "https://api.deepseek.com"


async def test_put_settings_no_payload_400(client):
    resp = await client.put("/api/settings", json={})
    assert resp.status_code == 400


async def test_get_settings_includes_retrieval_defaults(client):
    resp = await client.get("/api/settings")
    d = resp.json()
    assert "retrieval" in d
    r = d["retrieval"]
    assert r["vector_k"] == 24
    assert r["fts_k"] == 24
    assert r["rrf_k"] == 60
    assert r["vector_weight"] == 0.7
    assert r["fts_weight"] == 0.3
    assert r["per_video_cap"] == 3
    assert r["min_sim"] == 0.0
    assert r["neighbor_gap"] == 2.0


async def test_put_settings_retrieval_persists_and_applies(client, tmp_path):
    resp = await client.put(
        "/api/settings",
        json={"retrieval": {"vector_k": 32, "min_sim": 0.3, "per_video_cap": 2}},
    )
    assert resp.status_code == 200
    d = resp.json()
    assert d["applied"] is True
    assert set(d["saved"]) == {
        "RETRIEVAL_VECTOR_K", "RETRIEVAL_MIN_SIM", "RETRIEVAL_PER_VIDEO_CAP",
    }
    # runtime.env 已持久化
    from app.core.runtime_config import load_runtime_env

    overrides = load_runtime_env(str(tmp_path))
    assert overrides["RETRIEVAL_VECTOR_K"] == "32"
    assert overrides["RETRIEVAL_MIN_SIM"] == "0.3"
    # GET 回显新值（热生效）
    r = (await client.get("/api/settings")).json()["retrieval"]
    assert r["vector_k"] == 32 and r["min_sim"] == 0.3 and r["per_video_cap"] == 2


async def test_put_settings_retrieval_out_of_range_400(client):
    resp = await client.put(
        "/api/settings", json={"retrieval": {"vector_k": 999}}
    )
    assert resp.status_code == 400
    assert "范围" in resp.json()["detail"]


async def test_put_settings_retrieval_none_fields_skipped(client, tmp_path):
    """显式 null / 未提供的字段不覆盖；空 payload retrieval 全 None → 400。"""
    resp = await client.put(
        "/api/settings", json={"retrieval": {"vector_k": None, "fts_k": None}}
    )
    assert resp.status_code == 400  # 无有效覆盖项


async def test_put_settings_clear_asr_falls_back_local(client, tmp_path):
    """清空 ASR 远程配置（全字段 ""）→ 写入 runtime.env → asr_mode 回落 local。

    修复：删除 asr/embedding 配置保存不生效（原实现空串被跳过 → overrides 空 400）。
    """
    # 先配置远程（模拟 docker compose env / 曾配置的集中式 ASR）
    r1 = await client.put(
        "/api/settings",
        json={"asr": {
            "provider": "sensevoice", "base_url": "http://192.168.x.x:9991",
            "model": "sensevoice", "api_key": "local",
        }},
    )
    assert r1.status_code == 200
    c = client._transport.app.state.components
    assert c["settings"].asr_mode == "cloud"

    # 清空整组 → 应回落 local（sensevoice 本地档）
    r2 = await client.put(
        "/api/settings",
        json={"asr": {"provider": "", "base_url": "", "model": "", "api_key": ""}},
    )
    assert r2.status_code == 200
    assert set(r2.json()["saved"]) == {
        "CLOUD_ASR_PROVIDER", "CLOUD_ASR_BASE_URL", "CLOUD_ASR_MODEL", "CLOUD_ASR_KEY",
    }
    s = c["settings"]
    assert s.cloud_asr_provider == "" and s.cloud_asr_base_url == ""
    assert s.asr_mode == "local"
    # runtime.env 已持久化（重启容器后仍保持 local）
    from app.core.runtime_config import load_runtime_env

    env = load_runtime_env(str(tmp_path))
    assert env.get("CLOUD_ASR_PROVIDER") == ""
    # GET 回显已清空
    gd = (await client.get("/api/settings")).json()
    assert gd["asr"]["provider"] == ""
    # 转写链已重建为本地档（指向 asr_local_endpoint）
    assert any(getattr(t, "name", "") == "cloud_asr" for t in c["transcribers"])


async def test_put_settings_clear_embedding_falls_back_local(client, tmp_path):
    """清空 Embedding 远程配置 → embed_mode 回落 local（fastembed 本地档）。"""
    # 先配置远程
    r1 = await client.put(
        "/api/settings",
        json={"embedding": {
            "provider": "openai", "base_url": "http://192.168.x.x:11434/v1",
            "model": "bge-m3:latest", "api_key": "",
        }},
    )
    assert r1.status_code == 200
    c = client._transport.app.state.components
    assert c["settings"].embed_mode == "remote"

    # 清空整组 → 回落本地 fastembed（而非 none）
    r2 = await client.put(
        "/api/settings",
        json={"embedding": {"provider": "", "base_url": "", "model": "", "api_key": ""}},
    )
    assert r2.status_code == 200
    s = c["settings"]
    assert s.embed_provider == "" and s.embed_base_url == ""
    assert s.embed_mode == "local"
    assert s.embed_provider_effective == "fastembed"
    assert s.embed_model_effective == "BAAI/bge-small-zh-v1.5"
    # embedder 组件已重建为 fastembed 本地档（不走 remote 分支）
    e = c["embedder"]
    assert e._provider == "fastembed"
    assert e._model_name == "BAAI/bge-small-zh-v1.5"
