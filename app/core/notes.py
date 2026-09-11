import json
import re
from dataclasses import dataclass, field

from app.core.prompts import DEFAULTS, PromptRegistry, render
from app.core.transcribers.base import Transcript

# note_context 变量缺省值：E1 元数据采集（M2）就绪前，author/description/评论
# 无真实数据，以占位文本注入（LLM 可理解为"无此信息"）。
_META_PLACEHOLDER = "（未采集）"

# map-reduce 长文笔记：超过该字符数时先分块摘要（map）再合并（reduce），
# 避免整段转写超出本地 LLM 上下文（如 ollama 默认 num_ctx=4096 会静默截断，
# 导致模型只看到开头片段并幻觉出不符结构的 JSON）。
MAX_TRANSCRIPT_CHARS = 60_000
_MAP_CHUNK_CHARS = 6_000

# 注：quotes（金句）已下线——提示词不再要求、markdown 不再渲染，
# 仅保留 NoteData.quotes 字段以兼容存量笔记数据（前端也不再展示）。
_NOTE_FIELDS = ("summary", "chapters", "key_points", "glossary")

_SCHEMA_REMIND = (
    "上一条输出不符合要求的 JSON 结构。请重新输出：只输出一个 JSON 对象，"
    '必须包含 "summary"（150-200字中文摘要字符串）、"chapters"（数组，可为空）、'
    '"key_points"（数组）、"glossary"（数组）。'
    "禁止输出 summary/chapters/key_points/glossary 以外的顶层字段，"
    "不要输出任何解释文字或代码围栏。"
)


@dataclass
class NoteData:
    summary: str
    chapters: list[dict] = field(default_factory=list)  # [{title,start_sec,end_sec,points}]
    key_points: list[str] = field(default_factory=list)
    quotes: list[dict] = field(default_factory=list)  # [{text,start_sec}]
    glossary: list[dict] = field(default_factory=list)  # [{term,explanation}]


def _resolve_template(prompts: PromptRegistry | None, key: str) -> str:
    """取模板文本：接线了 PromptRegistry 用自定义值，否则用内置默认。"""
    return prompts.get(key) if prompts is not None else DEFAULTS[key]


def build_note_prompt(
    title: str,
    text_with_ts: str,
    prompts: PromptRegistry | None = None,
    note_vars: dict | None = None,
) -> list[dict]:
    sys = _resolve_template(prompts, "note_system")

    # note_context（E2）：渲染后拼入 user 段正文之前。
    # 仅在接线了 PromptRegistry 时启用；prompts=None 保持 v1 行为（回归保护）。
    context = ""
    if prompts is not None:
        context_vars = {
            "title": title,
            "author": _META_PLACEHOLDER,
            "description": _META_PLACEHOLDER,
            "top_comments": _META_PLACEHOLDER,
            **(note_vars or {}),  # 调用方（E1 元数据）覆盖占位值
        }
        # 过滤 None：调用方缺省维度留空时回落占位，避免渲染成字面 "None"
        context_vars = {k: v for k, v in context_vars.items() if v is not None}
        context = render(prompts.get("note_context"), **context_vars).strip()

    user = (
        f"视频标题：{title}\n"
        "转写全文（每行开头 [秒数] 表示该句开始时间，章节与要点请标注对应秒数）：\n\n"
        f"{text_with_ts}"
    )
    if context:
        user = f"{context}\n\n{user}"
    return [
        {"role": "system", "content": sys},
        {"role": "user", "content": user},
    ]


def build_note_map_prompt(
    title: str, part_text: str, index: int, total: int,
    prompts: PromptRegistry | None = None,
) -> list[dict]:
    """长文转写的分块（map）提示词：对单块内容生成分部笔记。"""
    sys = render(
        _resolve_template(prompts, "note_map_system"),
        title=title, index=index, total=total,
    )
    user = (
        f"视频标题：{title}\n"
        f"转写全文第 {index}/{total} 部分：\n\n{part_text}"
    )
    return [
        {"role": "system", "content": sys},
        {"role": "user", "content": user},
    ]


def build_note_reduce_prompt(
    title: str, part_notes: list[str],
    prompts: PromptRegistry | None = None,
) -> list[dict]:
    """长文转写的合并（reduce）提示词：把各分部笔记合并为最终笔记。"""
    sys = render(_resolve_template(prompts, "note_reduce_system"), title=title)
    parts = "\n\n".join(f"分部笔记 {i}：\n{p}" for i, p in enumerate(part_notes, 1))
    user = f"视频标题：{title}\n\n{parts}"
    return [
        {"role": "system", "content": sys},
        {"role": "user", "content": user},
    ]


# 视觉旁路来源在笔记正文行内的标注（note_system 默认模板有对应说明）。
# 注意：这是「送给 LLM 的文本」的标注，不改写 Segment.text 本身（DB/检索/前端保持干净）。
_SRC_MARKERS = {"ocr": "[画面] ", "vlm": "[画面描述] "}


def _src_marker(source: str | None) -> str:
    return _SRC_MARKERS.get(source or "", "")


def _text_with_timestamps(transcript: Transcript) -> str:
    lines = [
        f"[{seg.start_sec:.0f}] {_src_marker(seg.source)}{seg.text}"
        for seg in transcript.segments
    ]
    text = "\n".join(lines) or transcript.raw_text
    return text[:MAX_TRANSCRIPT_CHARS]


def _split_text_by_chars(text: str, limit: int) -> list[str]:
    """按行边界把文本切成不超过 limit 字符的块（单行超长时硬切）。"""
    chunks: list[str] = []
    cur = ""
    for line in text.splitlines():
        if len(line) > limit:  # 单行超长，硬切
            if cur:
                chunks.append(cur)
                cur = ""
            for i in range(0, len(line), limit):
                chunks.append(line[i : i + limit])
            continue
        if cur and len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks or [text]


def _note_is_valid(data: object) -> bool:
    """校验 LLM 输出是否为符合结构的笔记 JSON（防止幻觉出无关结构后存空笔记）。"""
    if not isinstance(data, dict):
        return False
    summary = data.get("summary")
    if isinstance(summary, str) and summary.strip():
        return True
    for key in ("chapters", "key_points"):
        v = data.get(key)
        if isinstance(v, list) and v:
            return True
    return False


async def _chat_note_json(llm, messages: list[dict]) -> dict:
    """chat_json + 结构校验：结构不符时带纠错指令重试一次，仍不符则抛错。

    抛错会让 pipeline 将视频标记为 failed，而不是静默落一份空笔记。
    """
    data = await llm.chat_json(messages)
    if _note_is_valid(data):
        return data
    retry = messages + [{"role": "user", "content": _SCHEMA_REMIND}]
    data = await llm.chat_json(retry)
    if _note_is_valid(data):
        return data
    raise ValueError("note LLM output does not match expected schema")


def _note_from_data(data: dict) -> NoteData:
    return NoteData(
        summary=data.get("summary", "") or "",
        chapters=data.get("chapters", []) or [],
        key_points=data.get("key_points", []) or [],
        quotes=data.get("quotes", []) or [],
        glossary=data.get("glossary", []) or [],
    )


async def generate_note(
    title: str,
    transcript: Transcript,
    llm,
    prompts: PromptRegistry | None = None,
    note_vars: dict | None = None,
) -> NoteData:
    """用 LLM 生成结构化笔记。llm 需有 chat_json(messages) -> dict。

    转写全文超过 _MAP_CHUNK_CHARS 时走 map-reduce：
    分块摘要（保留 [秒数] 绝对时间戳）→ 合并为最终笔记，
    避免超出本地 LLM 上下文导致静默截断与幻觉。

    prompts（E2）：接线 PromptRegistry 时，笔记 prompt 走用户自定义模板
    （note_context 附加上下文同时启用）；None 保持内置默认行为。
    """
    text_with_ts = _text_with_timestamps(transcript)
    if len(text_with_ts) <= _MAP_CHUNK_CHARS:
        data = await _chat_note_json(
            llm, build_note_prompt(title, text_with_ts, prompts, note_vars)
        )
    else:
        chunks = _split_text_by_chars(text_with_ts, _MAP_CHUNK_CHARS)
        part_notes: list[str] = []
        for i, chunk in enumerate(chunks, 1):
            d = await _chat_note_json(
                llm, build_note_map_prompt(title, chunk, i, len(chunks), prompts)
            )
            part_notes.append(json.dumps(d, ensure_ascii=False))
        data = await _chat_note_json(
            llm, build_note_reduce_prompt(title, part_notes, prompts)
        )
    return _note_from_data(data)


def _fmt_ts(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 60:02d}:{sec % 60:02d}"


def render_markdown(note: NoteData, title: str) -> str:
    lines = [f"# {title}", "", "## 摘要", note.summary or "（无摘要）", ""]
    lines.append("## 章节")
    for i, ch in enumerate(note.chapters, 1):
        ts = f"（{_fmt_ts(ch.get('start_sec', 0))}–{_fmt_ts(ch.get('end_sec', 0))}）"
        lines.append(f"### {i}. {ch.get('title', '')}{ts}")
        for p in ch.get("points", []) or []:
            lines.append(f"- {p}")
        lines.append("")
    if note.key_points:
        lines.append("## 要点")
        lines.extend(f"- {kp}" for kp in note.key_points)
        lines.append("")
    if note.glossary:
        lines.append("## 术语")
        lines.extend(f"- **{g.get('term', '')}**：{g.get('explanation', '')}" for g in note.glossary)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# 金句（quotes）已下线：新笔记不再生成该章节，但存量笔记的 markdown 里仍带着，
# 对外输出（页面展示 / 复制 / 下载 / MCP）前统一剥离，避免"页面没有、复制出来有"。
_QUOTES_SECTION_RE = re.compile(
    r"^[ \t]*##[ \t]*金句[ \t]*\r?\n(?:[ \t]*(?!##[ \t]).*\r?\n?)*",
    re.MULTILINE,
)


def strip_quotes_section(md: str | None) -> str:
    """剥离笔记 markdown 中的「## 金句」章节（金句功能已下线）。"""
    if not md:
        return md or ""
    return _QUOTES_SECTION_RE.sub("", md).strip() + "\n"
