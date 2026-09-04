"""E2 / M1：提示词 API 集成测试（/api/prompts 四端点）。"""

LONG = "x" * 8001


async def test_list_prompts_defaults(client):
    resp = await client.get("/api/prompts")
    assert resp.status_code == 200
    d = resp.json()["templates"]
    assert set(d) == {
        "note_system", "note_context", "note_map_system",
        "note_reduce_system", "qa_system",
    }
    for key, item in d.items():
        assert item["value"] == item["default"]
        assert item["customized"] is False
        assert item["label"]
        assert isinstance(item["vars"], list)


async def test_put_updates_and_persists(client):
    resp = await client.put(
        "/api/prompts", json={"qa_system": "用英文口语化风格回答。"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"saved": ["qa_system"]}

    d = (await client.get("/api/prompts")).json()["templates"]
    assert d["qa_system"]["value"] == "用英文口语化风格回答。"
    assert d["qa_system"]["customized"] is True
    assert d["note_system"]["customized"] is False

    # 持久化：数据目录下 prompts.json 落盘（经由 registry 读文件验证）
    reg = client._transport.app.state.components["prompts"]
    assert reg.get("qa_system") == "用英文口语化风格回答。"


async def test_put_validation_errors(client):
    for body, why in [
        ({"nope": "x"}, "未知键"),
        ({"qa_system": "   "}, "纯空白"),
        ({"qa_system": LONG}, "超长"),
        ({"qa_system": 123}, "非字符串"),
    ]:
        resp = await client.put("/api/prompts", json=body)
        assert resp.status_code == 422, f"{why} 应 422：{body}"
    resp = await client.put("/api/prompts", json={})
    assert resp.status_code == 422  # 空 body


async def test_reset_single_and_all(client):
    await client.put(
        "/api/prompts",
        json={"note_system": "自定义A", "qa_system": "自定义B"},
    )
    resp = await client.post("/api/prompts/reset", json={"key": "note_system"})
    assert resp.status_code == 200
    d = (await client.get("/api/prompts")).json()["templates"]
    assert d["note_system"]["customized"] is False
    assert d["qa_system"]["customized"] is True

    resp = await client.post("/api/prompts/reset", json={})  # 全部
    assert resp.status_code == 200
    d = (await client.get("/api/prompts")).json()["templates"]
    assert all(not item["customized"] for item in d.values())


async def test_reset_unknown_key(client):
    resp = await client.post("/api/prompts/reset", json={"key": "nope"})
    assert resp.status_code == 422


async def test_preview_with_default_value(client):
    resp = await client.post("/api/prompts/preview", json={"key": "note_context"})
    assert resp.status_code == 200
    d = resp.json()
    assert d["key"] == "note_context"
    assert "示例视频：十分钟了解 RAG" in d["rendered"]  # 样例变量已注入
    assert "{{title}}" not in d["rendered"]


async def test_preview_with_explicit_value(client):
    resp = await client.post(
        "/api/prompts/preview",
        json={"key": "qa_system", "value": "问题：{{question}} 参考 {{n_references}}"},
    )
    assert resp.status_code == 200
    assert resp.json()["rendered"] == "问题：这个视频的核心结论是什么？ 参考 5"


async def test_preview_map_vars(client):
    resp = await client.post("/api/prompts/preview", json={"key": "note_map_system"})
    assert resp.status_code == 200
    assert "1/3" in resp.json()["rendered"]


async def test_preview_unknown_key(client):
    resp = await client.post("/api/prompts/preview", json={"key": "nope"})
    assert resp.status_code == 422


async def test_components_registry_wired(client):
    """main.py 装配断言：components["prompts"] 就位（pipeline/search 接线依赖）。"""
    from app.core.prompts import PromptRegistry

    reg = client._transport.app.state.components["prompts"]
    assert isinstance(reg, PromptRegistry)
