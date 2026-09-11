import pytest

from app.core.notes import (
    NoteData,
    build_note_prompt,
    generate_note,
    render_markdown,
    strip_quotes_section,
)
from app.core.transcribers.base import Segment, Transcript

SAMPLE_JSON = {
    "summary": "视频讲了一个故事",
    "chapters": [{"title": "开头", "start_sec": 0.0, "end_sec": 30.0, "points": ["介绍背景"]}],
    "key_points": ["要点一", "要点二"],
    "quotes": [{"text": "你好世界", "start_sec": 5.0}],
    "glossary": [{"term": "RAG", "explanation": "检索增强生成"}],
}


class FakeLLM:
    def __init__(self, data: dict):
        self._data = data
        self.last_messages = None
        self.calls = 0

    async def chat_json(self, messages, **kw):
        self.calls += 1
        self.last_messages = messages
        return self._data


class FakeSchemaLLM:
    """先返回错误结构，收到纠错指令（消息数 > 2）后返回正确结构。"""

    def __init__(self, bad: dict, good: dict):
        self._bad = bad
        self._good = good
        self.calls = 0

    async def chat_json(self, messages, **kw):
        self.calls += 1
        if len(messages) > 2:  # 追加了 _SCHEMA_REMIND 纠错消息
            return self._good
        return self._bad


class FakeMapReduceLLM:
    """map 提示词返回分部笔记，reduce 提示词返回最终笔记。"""

    def __init__(self, part: dict, final: dict):
        self._part = part
        self._final = final
        self.map_calls = 0
        self.reduce_calls = 0

    async def chat_json(self, messages, **kw):
        if "同一视频各部分的分部笔记" in messages[0]["content"]:
            self.reduce_calls += 1
            return self._final
        self.map_calls += 1
        return self._part


def _transcript():
    return Transcript(
        segments=[Segment(0.0, 2.0, "你好世界"), Segment(2.0, 4.0, "第二段")],
        raw_text="你好世界\n第二段",
        source="whisper",
    )


def test_build_note_prompt_includes_title_and_transcript():
    msgs = build_note_prompt("测试视频", "你好世界\n第二段")
    joined = "".join(m["content"] for m in msgs)
    assert "测试视频" in joined
    assert "你好世界" in joined
    assert "summary" in joined


def test_generate_note_parses_json():
    llm = FakeLLM(SAMPLE_JSON)
    note = _run_generate(llm)
    assert note.summary == "视频讲了一个故事"
    assert len(note.chapters) == 1
    assert note.chapters[0]["title"] == "开头"
    assert note.key_points == ["要点一", "要点二"]
    assert note.quotes[0]["text"] == "你好世界"
    assert note.glossary[0]["term"] == "RAG"


def test_generate_note_truncates_long_transcript():
    llm = FakeLLM(SAMPLE_JSON)
    note = _run_generate(llm, transcript_text="字" * 200_000)
    assert note.summary  # 仍能生成


def test_generate_note_raises_on_wrong_schema():
    """LLM 幻觉出不符结构的 JSON（如 title/hosts/Dialogue）时应抛错，不落空笔记。"""
    llm = FakeLLM({"title": "x", "hosts": "y", "Dialogue": []})
    with pytest.raises(ValueError):
        _run_generate(llm)
    assert llm.calls == 2  # 首次 + 纠错重试


def test_generate_note_retries_wrong_schema_and_recovers():
    """结构不符时带纠错指令重试一次，第二次返回正确结构则成功。"""
    llm = FakeSchemaLLM({"foo": "bar"}, SAMPLE_JSON)
    note = _run_generate(llm)
    assert note.summary == "视频讲了一个故事"
    assert llm.calls == 2


def test_generate_note_map_reduce_long_transcript():
    """超长转写走 map-reduce：多个 map 调用 + 一次 reduce 合并。"""
    segs = [
        Segment(float(i), float(i + 2), f"第{i}句" + "内容" * 30)
        for i in range(120)
    ]
    tr = Transcript(segments=segs, raw_text="", source="cloud_asr")
    llm = FakeMapReduceLLM(
        part={"summary": "分部摘要", "chapters": [], "key_points": ["p"], "quotes": [], "glossary": []},
        final=SAMPLE_JSON,
    )
    import asyncio

    note = asyncio.run(generate_note("测试视频", tr, llm))
    assert note.summary == "视频讲了一个故事"
    assert llm.map_calls >= 2
    assert llm.reduce_calls == 1


def test_split_text_by_chars_respects_line_boundary():
    from app.core.notes import _split_text_by_chars

    text = "\n".join(f"[{i}] " + "字" * 10 for i in range(100))
    chunks = _split_text_by_chars(text, 200)
    assert all(len(c) <= 200 for c in chunks)
    assert "".join(c.replace("\n", "\n", 1) for c in chunks)  # 不丢内容
    # 行数守恒
    total_lines = sum(len(c.splitlines()) for c in chunks)
    assert total_lines == 100


def test_text_with_timestamps_marks_visual_sources():
    """E3：画面来源段在笔记正文加 [画面]/[画面描述] 前缀，语音/字幕不加。"""
    from app.core.notes import _text_with_timestamps

    tr = Transcript(
        segments=[
            Segment(0, 1, "口播内容", source="speech"),
            Segment(1, 2, "字幕内容", source="subtitle"),
            Segment(2, 3, "PPT 上的文字", source="ocr"),
            Segment(3, 4, "画面里有一个人", source="vlm"),
        ],
        raw_text="",
        source="cloud_asr",
    )
    text = _text_with_timestamps(tr)
    assert text == (
        "[0] 口播内容\n"
        "[1] 字幕内容\n"
        "[2] [画面] PPT 上的文字\n"
        "[3] [画面描述] 画面里有一个人"
    )


def test_render_markdown():
    note = NoteData(**SAMPLE_JSON)
    md = render_markdown(note, "测试视频")
    assert "# 测试视频" in md
    assert "## 摘要" in md
    assert "## 章节" in md
    assert "### 1. 开头（00:00–00:30）" in md
    assert "## 要点" in md
    assert "- 要点一" in md
    # 金句章节已移除：即使存量数据带 quotes，也不再渲染
    assert "## 金句" not in md
    assert "> 你好世界（00:05）" not in md
    assert "## 术语" in md
    assert "**RAG**：检索增强生成" in md


def test_render_markdown_handles_empty_chapters():
    note = NoteData(summary="s", chapters=[], key_points=[], quotes=[], glossary=[])
    md = render_markdown(note, "t")
    assert "## 章节" in md  # 空章节也不崩


def _run_generate(llm, transcript_text=None):
    import asyncio

    tr = _transcript()
    if transcript_text:
        tr.raw_text = transcript_text
    return asyncio.run(generate_note("测试视频", tr, llm))


def test_strip_quotes_section_removes_only_quotes_block():
    """存量笔记 markdown 中的「## 金句」章节被剥离，其余章节完整保留。"""
    md = (
        "# 标题\n\n## 摘要\n摘要内容\n\n"
        "## 章节\n### 1. 开头（00:00–00:30）\n- 要点\n\n"
        "## 要点\n- 要点一\n\n"
        "## 金句\n> 你好世界（00:05）\n> 再来一句（01:02）\n\n"
        "## 术语\n- **RAG**：检索增强生成\n"
    )
    out = strip_quotes_section(md)
    assert "## 金句" not in out
    assert "你好世界" not in out
    assert "## 摘要" in out
    assert "## 章节" in out
    assert "## 要点" in out
    assert "## 术语" in out


def test_strip_quotes_section_handles_empty_and_absent():
    assert strip_quotes_section("") == ""
    assert strip_quotes_section(None) == ""
    assert strip_quotes_section("## 摘要\n只有摘要\n") == "## 摘要\n只有摘要\n"
