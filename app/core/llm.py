import asyncio
import json
import re

from openai import APIError, AsyncOpenAI


def _extract_json(text: str) -> str:
    """从 LLM 输出中提取 JSON 子串（容忍 ```json 围栏、前后缀文本）。"""
    text = text.strip()
    # 优先取第一个 { 到最后一个 }（对象），其次 [ 到 ]（数组）
    for start_ch, end_ch in (("{", "}"), ("[", "]")):
        s = text.find(start_ch)
        e = text.rfind(end_ch)
        if s != -1 and e > s:
            return text[s : e + 1]
    return text


class LLMClient:
    """OpenAI 兼容 LLM 客户端（DeepSeek / Gemini / OpenAI 等），带重试与 JSON 容错。"""

    def __init__(
        self,
        base_url: str = "https://api.deepseek.com",
        api_key: str = "",
        model: str = "deepseek-v4-flash",
        client: AsyncOpenAI | None = None,
        thinking: bool = False,
    ):
        self._base_url = base_url
        self._api_key = api_key
        self._model = model
        self._client = client
        self._thinking = thinking

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            if not self._api_key:
                raise ValueError("LLM_API_KEY is not configured")
            self._client = AsyncOpenAI(base_url=self._base_url, api_key=self._api_key)
        return self._client

    async def chat(
        self,
        messages: list[dict],
        temperature: float = 0.2,
        max_tokens: int = 8192,
        json_mode: bool = False,
    ) -> str:
        kwargs: dict = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        # DeepSeek V4 默认开启 thinking：笔记/问答场景关闭以获得更稳定完整的输出
        if not self._thinking and "deepseek" in self._base_url:
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        client = self._get_client()
        for attempt in range(3):
            try:
                resp = await client.chat.completions.create(**kwargs)
                return resp.choices[0].message.content
            except APIError:
                if attempt == 2:
                    raise
                await asyncio.sleep(2**attempt)  # 1s, 2s 指数退避
        raise APIError("unreachable", request=None)  # pragma: no cover

    async def chat_json(self, messages: list[dict], **kw) -> dict:
        kw.setdefault("max_tokens", 8192)
        text = await self.chat(messages, json_mode=True, **kw)
        try:
            return json.loads(_extract_json(text))
        except json.JSONDecodeError:
            # 输出被截断/非纯 JSON 时，追加指令重试一次
            retry = messages + [
                {
                    "role": "user",
                    "content": "上一条输出不是合法的 JSON。请重新输出：只输出一个合法 JSON 对象，"
                    "不要任何解释、代码围栏或多余文字。",
                }
            ]
            text2 = await self.chat(retry, json_mode=True, **kw)
            return json.loads(_extract_json(text2))
