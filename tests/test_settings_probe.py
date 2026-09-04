"""POST /api/settings/probe 连通性探测测试。"""


async def test_probe_requires_base_url(client):
    r = await client.post("/api/settings/probe", json={"kind": "llm", "base_url": ""})
    assert r.status_code == 400
    assert "请先填写 Base URL" in r.json()["detail"]


async def test_probe_rejects_unknown_kind(client):
    r = await client.post(
        "/api/settings/probe", json={"kind": "tts", "base_url": "http://x:1"}
    )
    assert r.status_code == 400
    assert "kind 必须为" in r.json()["detail"]


async def test_probe_connection_refused(client):
    """连接被拒：ok=False + 中文错误（而非抛异常）。"""
    r = await client.post(
        "/api/settings/probe", json={"kind": "llm", "base_url": "http://127.0.0.1:1"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["reachable"] is False
    assert "无法连接" in body["message"]
    assert body["error"]


async def test_probe_asr_tries_health_then_models(client):
    """ASR 候选按 /health → /models 顺序探测；端口不可达时返回明确错误。"""
    r = await client.post(
        "/api/settings/probe",
        json={"kind": "asr", "base_url": "http://127.0.0.1:2", "model": "sensevoice"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "无法连接" in body["message"]


async def test_probe_long_mask_falls_back_to_saved_key(client, app, monkeypatch):
    """长 key 脱敏回显（sk-T...7890 前4...后4）不能当真实 key 发往服务，
    必须回落已保存的真实 key。回归：此前会把掩码当真实 key → 误报鉴权失败。"""
    real_key = "sk-realsecretvalue1234567890"
    app.state.components["settings"].llm_api_key = real_key

    captured = {}
    async def _fake_try_get(url, api_key):
        captured["api_key"] = api_key
        return {"url": url, "reachable": True, "status": 200, "latency_ms": 1, "text": '{"data":[]}'}
    monkeypatch.setattr("app.api.settings._try_get", _fake_try_get)

    r = await client.post(
        "/api/settings/probe",
        json={"kind": "llm", "base_url": "http://127.0.0.1:9999", "api_key": "sk-r...7890"},
    )
    assert r.status_code == 200
    assert captured.get("api_key") == real_key


async def test_probe_short_mask_falls_back_to_saved_key(client, app, monkeypatch):
    """短 key 脱敏（****）同样须回落真实已保存 key。"""
    real_key = "shortkey"
    app.state.components["settings"].llm_api_key = real_key

    captured = {}
    async def _fake_try_get(url, api_key):
        captured["api_key"] = api_key
        return {"url": url, "reachable": True, "status": 200, "latency_ms": 1, "text": '{"data":[]}'}
    monkeypatch.setattr("app.api.settings._try_get", _fake_try_get)

    r = await client.post(
        "/api/settings/probe",
        json={"kind": "llm", "base_url": "http://127.0.0.1:9999", "api_key": "****"},
    )
    assert r.status_code == 200
    assert captured.get("api_key") == real_key


async def test_probe_explicit_key_used_verbatim(client, app, monkeypatch):
    """手填的真实 key（非掩码）应原样透传，不回落已保存值。"""
    app.state.components["settings"].llm_api_key = "sk-saved-but-different-0000"

    captured = {}
    async def _fake_try_get(url, api_key):
        captured["api_key"] = api_key
        return {"url": url, "reachable": True, "status": 200, "latency_ms": 1, "text": '{"data":[]}'}
    monkeypatch.setattr("app.api.settings._try_get", _fake_try_get)

    r = await client.post(
        "/api/settings/probe",
        json={"kind": "llm", "base_url": "http://127.0.0.1:9999", "api_key": "sk-freshrealkey-1234"},
    )
    assert r.status_code == 200
    assert captured.get("api_key") == "sk-freshrealkey-1234"
