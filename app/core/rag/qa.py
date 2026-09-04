from dataclasses import dataclass, field

from app.core.prompts import DEFAULTS, PromptRegistry, render
from app.core.rag.retriever import Hit

CITATION_MAX_CHARS = 200


@dataclass
class Answer:
    answer: str
    citations: list[dict] = field(default_factory=list)


def _fmt_ref(i: int, h: Hit) -> str:
    """单个参考条目的渲染：meta（视频简介）无时间戳，与口播引用区分。"""
    if h.kind == "meta":
        return f"[{i}] (视频《{h.title}》·视频简介) {h.content}"
    return f"[{i}] (视频《{h.title}》 {int(h.start_sec)}s-{int(h.end_sec)}s) {h.content}"


def build_qa_prompt(
    question: str, hits: list[Hit], prompts: PromptRegistry | None = None
) -> list[dict]:
    # E2：接线 PromptRegistry 时走自定义 qa_system（可注入 {{question}}/
    # {{n_references}}）；None 保持内置默认（DEFAULTS 无占位符，渲染后不变）。
    sys = render(
        prompts.get("qa_system") if prompts is not None else DEFAULTS["qa_system"],
        question=question,
        n_references=len(hits),
    )
    refs = [_fmt_ref(i, h) for i, h in enumerate(hits, 1)]
    user = "问题：{q}\n\n参考资料：\n{refs}".format(
        q=question, refs="\n".join(refs) if refs else "（无，请说明知识库中未找到相关信息）"
    )
    return [
        {"role": "system", "content": sys},
        {"role": "user", "content": user},
    ]


async def answer_question(
    question: str, hits: list[Hit], llm, prompts: PromptRegistry | None = None
) -> Answer:
    messages = build_qa_prompt(question, hits, prompts)
    text = await llm.chat(messages)
    citations = [
        {
            "title": h.title,
            "start_sec": h.start_sec,
            "end_sec": h.end_sec,
            "content": h.content[:CITATION_MAX_CHARS],
            "video_id": h.video_id,
            "platform": h.platform,
            "kind": h.kind,
        }
        for h in hits
    ]
    return Answer(answer=text, citations=citations)
