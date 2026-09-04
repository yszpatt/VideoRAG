"""提示词模板 API（设计文档 E2 / 2.2d）。

- GET    /api/prompts          五键的当前值/默认值/是否自定义
- PUT    /api/prompts          {key: value} 部分键更新（校验后落 prompts.json）
- POST   /api/prompts/reset    {key} 或 {}（全部），删除自定义键回落默认
- POST   /api/prompts/preview  {key, value?}，用样例数据渲染返回拼接效果

热更新：每次请求经 request.app.state.components["prompts"] 现取，
PromptRegistry 内部 mtime 缓存保证保存即生效（含手工编辑文件）。
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core.prompts import DEFAULTS, PromptRegistry, render

router = APIRouter(prefix="/api/prompts")

MAX_PROMPT_CHARS = 8000

# 变量说明（前端「插入变量」与侧栏说明共用此口径，与设计文档 2.1 变量表一致）
TEMPLATE_META = {
    "note_system": {"label": "笔记·系统指令", "vars": []},
    "note_context": {
        "label": "笔记·上下文",
        "vars": ["title", "author", "description", "top_comments"],
    },
    "note_map_system": {"label": "笔记·分块（map）", "vars": ["title", "index", "total"]},
    "note_reduce_system": {"label": "笔记·合并（reduce）", "vars": ["title"]},
    "qa_system": {"label": "问答·系统指令", "vars": ["question", "n_references"]},
}

# preview 用的样例变量（体现"最终发给 LLM 的效果"）
SAMPLE_VARS: dict[str, dict] = {
    "note_system": {"title": "示例视频：十分钟了解 RAG"},
    "note_context": {
        "title": "示例视频：十分钟了解 RAG",
        "author": "示例 UP 主",
        "description": "这是一段示例视频简介，用于预览提示词模板的渲染效果。",
        "top_comments": "1.（赞 1284）讲得很清楚，感谢分享！\n2.（赞 866）求 up 出进阶版",
    },
    "note_map_system": {"title": "示例视频：十分钟了解 RAG", "index": 1, "total": 3},
    "note_reduce_system": {"title": "示例视频：十分钟了解 RAG"},
    "qa_system": {"question": "这个视频的核心结论是什么？", "n_references": 5},
}


def _registry(request: Request) -> PromptRegistry:
    return request.app.state.components["prompts"]


def _validate_key(key: str) -> None:
    if key not in DEFAULTS:
        raise HTTPException(422, f"未知模板键：{key}")


@router.get("")
async def list_prompts(request: Request):
    reg = _registry(request)
    templates = {
        key: {
            "value": reg.get(key),
            "default": DEFAULTS[key],
            "customized": key in reg.customized_keys(),
            "label": TEMPLATE_META[key]["label"],
            "vars": TEMPLATE_META[key]["vars"],
        }
        for key in DEFAULTS
    }
    return {"templates": templates}


@router.put("")
async def update_prompts(request: Request):
    """body 为 {key: value}（可含多个键）；值 ≤ 8000 字符且非纯空白。"""
    reg = _registry(request)
    body = await request.json()
    if not isinstance(body, dict) or not body:
        raise HTTPException(422, "请求体应为非空的 {key: value} 对象")

    updates: dict[str, str] = {}
    for key, value in body.items():
        _validate_key(key)
        if not isinstance(value, str):
            raise HTTPException(422, f"{key} 的值必须是字符串")
        if not value.strip():
            raise HTTPException(422, f"{key} 的值不能为纯空白（恢复默认请用 reset）")
        if len(value) > MAX_PROMPT_CHARS:
            raise HTTPException(422, f"{key} 超过 {MAX_PROMPT_CHARS} 字符上限")
        updates[key] = value

    for key, value in updates.items():
        reg.set(key, value)
    return {"saved": list(updates.keys())}


class ResetRequest(BaseModel):
    key: str | None = None  # None / 缺省 = 全部重置


@router.post("/reset")
async def reset_prompts(req: ResetRequest, request: Request):
    reg = _registry(request)
    if req.key is None:
        reg.reset()
        return {"reset": list(DEFAULTS.keys())}
    _validate_key(req.key)
    reg.reset(req.key)
    return {"reset": [req.key]}


class PreviewRequest(BaseModel):
    key: str
    value: str | None = None  # 不传则用当前生效值预览


@router.post("/preview")
async def preview_prompt(req: PreviewRequest, request: Request):
    """用样例数据渲染模板（优先用传入的 value），返回渲染结果。"""
    _validate_key(req.key)
    reg = _registry(request)
    tpl = req.value if req.value is not None else reg.get(req.key)
    rendered = render(tpl, **SAMPLE_VARS.get(req.key, {}))
    return {"key": req.key, "rendered": rendered}
