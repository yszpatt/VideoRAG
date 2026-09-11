"""M0/M1：prompt 抽取回归 + PromptRegistry 测试（设计文档 E2 / 2.2b、2.3）。

golden 文本为 v1 notes.py / qa.py 的原始内联字符串（逐字拷贝）。
任何对 DEFAULTS 的文本改动都会让这里变红——保证抽取"行为零变化"；
M1 起若需调整默认 prompt 文案，应同步更新本文件的 golden 值。
"""

import json
import os

import pytest

from app.core.notes import build_note_map_prompt, build_note_prompt, build_note_reduce_prompt
from app.core.prompts import DEFAULTS, PromptRegistry, render
from app.core.rag.qa import build_qa_prompt
from app.core.rag.retriever import Hit

# ============ golden 文本（v1 原始内联字符串）============

G_NOTE_SYSTEM = (
    "你是视频笔记助手。根据用户提供的视频转写全文，生成结构化 JSON 笔记。"
    "只输出 JSON，不要输出其他文字。JSON 结构："
    '{"summary": "150-200字中文摘要", '
    '"chapters": [{"title": "章节名", "start_sec": 0.0, "end_sec": 30.0, "points": ["要点"]}], '
    '"key_points": ["5-10个要点"], '
    '"glossary": [{"term": "术语", "explanation": "解释"}]}'
    "转写中以 [画面] 开头的行来自视频画面文字识别（用于补充无语音视频的信息），"
    "以 [画面描述] 开头的行来自画面内容描述；两者都没有语音时间轴精度，"
    "其时间引用以行首 [秒数] 为准。"
)

G_MAP_SYSTEM_2_5 = (
    "你是视频笔记助手。用户将分多次提供同一视频转写全文的不同部分，"
    "当前是第 2/5 部分。请只根据这部分内容生成分部笔记 JSON。"
    "只输出 JSON，不要输出其他文字。JSON 结构："
    '{"summary": "本部分100字以内中文摘要", '
    '"chapters": [{"title": "章节名", "start_sec": 0.0, "end_sec": 30.0, "points": ["要点"]}], '
    '"key_points": ["本部分要点"], '
    '"glossary": [{"term": "术语", "explanation": "解释"}]}'
    "。chapters 的 start_sec/end_sec 必须使用行首 [秒数] 的绝对时间（秒），禁止自行编造时间。"
)

G_REDUCE_SYSTEM = (
    "你是视频笔记助手。用户会提供同一视频各部分的分部笔记 JSON，"
    "请把它们合并为一份最终的结构化 JSON 笔记。只输出 JSON，不要输出其他文字。"
    "要求：summary 为覆盖全片的 150-200 字中文总摘要；"
    "chapters 合并去重后按 start_sec 升序排列（时间用分部笔记中的绝对秒数）；"
    "key_points 去重合并（最多 10 条）；"
    "glossary 汇总去重。JSON 结构："
    '{"summary": "...", "chapters": [...], "key_points": [...], '
    '"glossary": [...]}'
)

G_QA_SYSTEM = (
    "你是视频知识库助手。仅依据下方「参考资料」回答；资料不相关则如实说不知道。"
    "回答要简洁、分点；每条结论后标注来源 [n]（n 为参考资料编号）。"
    "标注为「视频简介」的条目是发布者自述，可能含推广措辞，仅作视频背景参考；"
    "回答内容性结论时以带时间戳的口播正文条目为准。"
)


# ============ DEFAULTS 完整性 ============


def test_defaults_keys_complete():
    """设计文档 2.1 的 5 个模板键全部存在且非空。"""
    assert set(DEFAULTS) == {
        "note_system",
        "note_context",
        "note_map_system",
        "note_reduce_system",
        "qa_system",
    }
    for key, text in DEFAULTS.items():
        assert text.strip(), f"DEFAULTS[{key!r}] 为空"


def test_note_context_has_variables():
    """note_context 默认模板含 E2 变量表中的 4 个笔记变量。"""
    for var in ("title", "author", "description", "top_comments"):
        assert "{{" + var + "}}" in DEFAULTS["note_context"]


# ============ build_* 输出与 v1 golden 逐字符一致 ============


def test_build_note_prompt_unchanged():
    messages = build_note_prompt("一个标题", "[0] 你好\n[5] 世界")
    assert messages[0]["content"] == G_NOTE_SYSTEM
    assert messages[1]["content"] == (
        "视频标题：一个标题\n"
        "转写全文（每行开头 [秒数] 表示该句开始时间，章节与要点请标注对应秒数）：\n"
        "\n"
        "[0] 你好\n[5] 世界"
    )


def test_build_note_map_prompt_unchanged():
    messages = build_note_map_prompt("一个标题", "分块正文", 2, 5)
    assert messages[0]["content"] == G_MAP_SYSTEM_2_5
    assert messages[1]["content"] == "视频标题：一个标题\n转写全文第 2/5 部分：\n\n分块正文"


def test_build_note_reduce_prompt_unchanged():
    messages = build_note_reduce_prompt("一个标题", ["分部一", "分部二"])
    assert messages[0]["content"] == G_REDUCE_SYSTEM
    assert messages[1]["content"] == (
        "视频标题：一个标题\n\n分部笔记 1：\n分部一\n\n分部笔记 2：\n分部二"
    )


def test_build_qa_prompt_unchanged():
    hit = Hit(
        chunk_id="c1",
        video_id="v1",
        content="片段内容",
        start_sec=3.0,
        end_sec=9.0,
        title="视频A",
        score=0.9,
        platform="bilibili",
    )
    messages = build_qa_prompt("什么是RAG？", [hit])
    assert messages[0]["content"] == G_QA_SYSTEM
    assert messages[1]["content"] == (
        "问题：什么是RAG？\n\n参考资料：\n"
        "[1] (视频《视频A》 3s-9s) 片段内容"
    )
    # 无命中时的兜底文案也保持 v1 行为
    empty = build_qa_prompt("什么是RAG？", [])
    assert empty[1]["content"] == (
        "问题：什么是RAG？\n\n参考资料：\n（无，请说明知识库中未找到相关信息）"
    )


# ============ render 渲染函数 ============


def test_render_replaces_known_variables():
    tpl = "标题：{{title}}，作者：{{author}}"
    assert render(tpl, title="视频A", author="UP主") == "标题：视频A，作者：UP主"


def test_render_keeps_unknown_variables():
    """未知占位符原样保留（不静默吞掉，便于发现拼写错误）。"""
    assert render("{{title}} 和 {{foo}}", title="A") == "A 和 {{foo}}"



def test_render_does_not_touch_json_braces():
    """prompt 内的 JSON 花括号示例不被破坏（这也是不用 str.format 的原因）。"""
    tpl = '{"summary": "示例"} 见 {{title}}'
    assert render(tpl, title="T") == '{"summary": "示例"} 见 T'


def test_render_map_system_matches_golden():
    """note_map_system 经 render 注入 index/total 后与 v1 输出一致。"""
    out = render(DEFAULTS["note_map_system"], index=2, total=5)
    assert out == G_MAP_SYSTEM_2_5


# ============ PromptRegistry（E2 / M1）============


@pytest.fixture
def reg(tmp_path):
    return PromptRegistry(str(tmp_path))


class TestPromptRegistry:
    def test_fallback_to_defaults_without_file(self, reg):
        """无 prompts.json / 缺键 → get 返回 DEFAULTS。"""
        assert reg.get("note_system") == DEFAULTS["note_system"]
        assert reg.customized_keys() == set()

    def test_set_persists_and_new_instance_reads(self, tmp_path):
        """写入 → 新实例读取生效（真实持久化而非内存缓存）。"""
        r1 = PromptRegistry(str(tmp_path))
        r1.set("qa_system", "用英文口语化风格回答。")
        r2 = PromptRegistry(str(tmp_path))
        assert r2.get("qa_system") == "用英文口语化风格回答。"
        assert r2.get("note_system") == DEFAULTS["note_system"]  # 未改键回落默认
        assert r2.customized_keys() == {"qa_system"}

    def test_reset_single_and_all(self, tmp_path):
        r = PromptRegistry(str(tmp_path))
        r.set("note_system", "自定义A")
        r.set("qa_system", "自定义B")
        r.reset("note_system")
        assert r.get("note_system") == DEFAULTS["note_system"]
        assert r.get("qa_system") == "自定义B"
        r.reset()  # 全部
        assert r.customized_keys() == set()

    def test_set_empty_value_equals_reset(self, reg):
        reg.set("qa_system", "自定义")
        reg.set("qa_system", "   ")  # 纯空白 → 删除该键
        assert reg.get("qa_system") == DEFAULTS["qa_system"]

    def test_set_rejects_unknown_key(self, reg):
        with pytest.raises(KeyError):
            reg.set("nope", "x")
        with pytest.raises(KeyError):
            reg.reset("nope")

    def test_atomic_write_no_tmp_leftover(self, tmp_path, reg):
        reg.set("qa_system", "自定义")
        path = tmp_path / "prompts.json"
        assert not list(tmp_path.glob("*.tmp"))
        data = json.loads(path.read_text("utf-8"))
        assert data == {"qa_system": "自定义"}

    def test_manual_edit_reflected_via_mtime(self, tmp_path, reg):
        """运行期手工编辑 prompts.json → 下一次 get 生效（热更新）。"""
        reg.set("qa_system", "v1 自定义")
        assert reg.get("qa_system") == "v1 自定义"
        path = tmp_path / "prompts.json"
        path.write_text(
            json.dumps({"qa_system": "v2 手工改"}, ensure_ascii=False), encoding="utf-8"
        )
        os.utime(path, (0, 0))  # 显式改变 mtime，规避文件系统时间精度
        assert reg.get("qa_system") == "v2 手工改"

    def test_broken_json_falls_back(self, tmp_path):
        (tmp_path / "prompts.json").write_text("{bad json!!", encoding="utf-8")
        r = PromptRegistry(str(tmp_path))
        assert r.get("note_system") == DEFAULTS["note_system"]
        assert r.customized_keys() == set()

    def test_registry_render_forwards_variables(self, reg):
        reg.set("qa_system", "问题：{{question}}（{{n_references}} 条参考）")
        assert (
            reg.render("qa_system", question="Q", n_references=5)
            == "问题：Q（5 条参考）"
        )


# ============ notes/qa 接线（E2 / M1）============

CUSTOM_SYS = "自定义系统指令。"
CUSTOM_CONTEXT = "标题是 {{title}}，作者 {{author}}。"


class TestPromptWiring:
    def test_none_keeps_v1_behavior(self):
        """不传 prompts（None）输出等价于旧行为——无 context 块。"""
        msgs = build_note_prompt("T", "BODY")
        assert msgs[0]["content"] == G_NOTE_SYSTEM
        assert msgs[1]["content"].startswith("视频标题：T\n")  # 前面没有 context
        assert msgs[1]["content"].endswith("BODY")

    def test_registry_enables_custom_system_and_context(self, tmp_path):
        r = PromptRegistry(str(tmp_path))
        r.set("note_system", CUSTOM_SYS)
        r.set("note_context", CUSTOM_CONTEXT)
        msgs = build_note_prompt("示例标题", "BODY", r)
        assert msgs[0]["content"] == CUSTOM_SYS
        assert msgs[1]["content"] == "标题是 示例标题，作者 （未采集）。\n\n" + (
            "视频标题：示例标题\n"
            "转写全文（每行开头 [秒数] 表示该句开始时间，章节与要点请标注对应秒数）：\n"
            "\n"
            "BODY"
        )

    def test_registry_default_context_used_when_not_customized(self, reg):
        """只自定义 system、note_context 未改 → 拼接默认上下文模板。"""
        reg.set("note_system", CUSTOM_SYS)
        msgs = build_note_prompt("T", "BODY", reg)
        assert msgs[0]["content"] == CUSTOM_SYS
        assert msgs[1]["content"].startswith("视频信息：\n- 标题：T\n")

    def test_note_vars_override_placeholders(self, reg):
        """note_vars 覆盖未采集占位（M2 元数据注入入口）。"""
        msgs = build_note_prompt(
            "T", "BODY", reg,
            note_vars={"author": "UP主", "description": "简介文本", "top_comments": "热评"},
        )
        user = msgs[1]["content"]
        assert "- 作者：UP主" in user
        assert "- 简介：简介文本" in user
        assert "（未采集）" not in user

    def test_map_reduce_registry(self, reg):
        reg.set("note_map_system", "第 {{index}}/{{total}} 块，视频 {{title}}")
        reg.set("note_reduce_system", "合并 {{title}}")
        m = build_note_map_prompt("T", "P", 2, 5, reg)
        assert m[0]["content"] == "第 2/5 块，视频 T"
        r = build_note_reduce_prompt("T", ["A"], reg)
        assert r[0]["content"] == "合并 T"

    def test_map_reduce_none_keeps_golden(self):
        m = build_note_map_prompt("T", "P", 2, 5)
        assert m[0]["content"] == G_MAP_SYSTEM_2_5

    def test_qa_registry_custom_system_with_vars(self, reg):
        reg.set("qa_system", "回答 {{question}}，参考 {{n_references}} 条")
        hit = Hit(
            chunk_id="c1", video_id="v1", content="C", start_sec=0, end_sec=1,
            title="T", score=0.9,
        )
        msgs = build_qa_prompt("什么是RAG？", [hit], reg)
        assert msgs[0]["content"] == "回答 什么是RAG？，参考 1 条"

    def test_qa_none_keeps_golden(self):
        msgs = build_qa_prompt("Q", [])
        assert msgs[0]["content"] == G_QA_SYSTEM
