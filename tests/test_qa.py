from app.core.rag.qa import Answer, answer_question, build_qa_prompt
from app.core.rag.retriever import Hit


def _hit(cid, title="视频A", start=5.0, platform="bilibili", kind="content"):
    return Hit(
        chunk_id=cid, video_id="v1", content="RAG 把检索和生成结合起来",
        start_sec=start, end_sec=start + 3.0, title=title, score=0.8,
        platform=platform, kind=kind,
    )


def test_build_qa_prompt_includes_citations():
    msgs = build_qa_prompt("什么是RAG", [_hit("c1")])
    joined = "".join(m["content"] for m in msgs)
    assert "[1]" in joined
    assert "视频A" in joined
    assert "5s" in joined
    assert "RAG 把检索和生成结合起来" in joined
    assert "不知道" in msgs[0]["content"]  # 系统提示要求不乱编


def test_build_qa_prompt_empty_hits():
    msgs = build_qa_prompt("问", [])
    user = msgs[1]["content"]
    assert "问题：问" in user


async def test_answer_question_returns_citations():
    class FakeLLM:
        async def chat(self, messages, **kw):
            return "RAG 是把检索和生成结合的方法[1]。"

    answer = await answer_question("什么是RAG", [_hit("c1"), _hit("c2", title="视频B")], FakeLLM())
    assert answer.answer == "RAG 是把检索和生成结合的方法[1]。"
    assert len(answer.citations) == 2
    assert answer.citations[0]["title"] == "视频A"
    assert answer.citations[0]["start_sec"] == 5.0
    assert answer.citations[0]["video_id"] == "v1"
    assert answer.citations[0]["platform"] == "bilibili"
    assert answer.citations[0]["kind"] == "content"


def test_meta_hit_renders_as_intro_without_timestamp():
    meta = _hit("m1", title="视频M", start=0.0, kind="meta")
    meta.content = "视频标题：视频M\n视频简介：讲 RAG 与向量库选型"
    msgs = build_qa_prompt("这个视频讲了什么", [meta])
    user = msgs[1]["content"]
    # meta 引用走「·视频简介」分支，不出现时间戳
    assert "[1] (视频《视频M》·视频简介) 视频标题：视频M" in user
    assert "s-" not in user


async def test_answer_question_citations_carry_kind():
    class FakeLLM:
        async def chat(self, messages, **kw):
            return "视频背景见[1]。"

    meta = _hit("m1", title="视频M", start=0.0, kind="meta")
    answer = await answer_question("背景", [meta], FakeLLM())
    assert answer.citations[0]["kind"] == "meta"
    assert answer.citations[0]["start_sec"] == 0.0
