from types import SimpleNamespace

import httpx
import pytest
from openai import APIError

from app.core.llm import LLMClient


class FakeCompletions:
    def __init__(self, fail_times=0, content='{"ok": true}', contents=None):
        self.fail_times = fail_times
        self.calls = 0
        self.last_kwargs = None
        self.content = content
        self.contents = contents or []  # 按调用次数返回不同内容

    async def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        if self.calls <= self.fail_times:
            raise APIError(
                "boom",
                request=httpx.Request("POST", "https://x"),
                body=None,
            )
        content = self.contents[self.calls - 1] if self.contents else self.content
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


class FakeClient:
    def __init__(self, completions):
        self.chat = SimpleNamespace(completions=completions)


def _make_client(completions):
    return LLMClient(client=FakeClient(completions))


async def test_chat_returns_content_and_passes_json_mode():
    comp = FakeCompletions()
    llm = _make_client(comp)
    text = await llm.chat(
        [{"role": "user", "content": "hi"}], json_mode=True, max_tokens=100
    )
    assert text == '{"ok": true}'
    assert comp.last_kwargs["response_format"] == {"type": "json_object"}
    assert comp.last_kwargs["max_tokens"] == 100
    assert comp.calls == 1


async def test_chat_retries_on_api_error():
    comp = FakeCompletions(fail_times=2)
    llm = _make_client(comp)
    text = await llm.chat([{"role": "user", "content": "hi"}])
    assert text == '{"ok": true}'
    assert comp.calls == 3  # 1 次失败 + 1 次失败 + 成功


async def test_chat_raises_after_max_retries():
    comp = FakeCompletions(fail_times=99)
    llm = _make_client(comp)
    with pytest.raises(APIError):
        await llm.chat([{"role": "user", "content": "hi"}])
    assert comp.calls == 3


async def test_chat_json_parses_response():
    comp = FakeCompletions(content='{"summary": "s"}')
    llm = _make_client(comp)
    data = await llm.chat_json([{"role": "user", "content": "hi"}])
    assert data == {"summary": "s"}


async def test_chat_json_extracts_fenced_json():
    comp = FakeCompletions(content='```json\n{"summary": "s"}\n```')
    llm = _make_client(comp)
    data = await llm.chat_json([{"role": "user", "content": "hi"}])
    assert data == {"summary": "s"}


async def test_chat_json_retries_on_bad_json():
    # 第一次返回截断的 JSON（未闭合），重试后返回合法 JSON
    comp = FakeCompletions(contents=['{"summary": "s', '{"summary": "s"}'])
    llm = _make_client(comp)
    data = await llm.chat_json([{"role": "user", "content": "hi"}])
    assert data == {"summary": "s"}
    assert comp.calls == 2


async def test_chat_disables_thinking_for_deepseek():
    comp = FakeCompletions()
    llm = LLMClient(
        base_url="https://api.deepseek.com", api_key="k", model="deepseek-v4-flash",
        client=FakeClient(comp),
    )
    await llm.chat([{"role": "user", "content": "hi"}])
    assert comp.last_kwargs["extra_body"] == {"thinking": {"type": "disabled"}}


async def test_chat_skips_thinking_for_other_providers():
    comp = FakeCompletions()
    llm = LLMClient(
        base_url="https://api.openai.com/v1", api_key="k", model="gpt-4o-mini",
        client=FakeClient(comp),
    )
    await llm.chat([{"role": "user", "content": "hi"}])
    assert "extra_body" not in comp.last_kwargs
