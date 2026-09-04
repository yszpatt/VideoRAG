"""把 video.status 派生为前端可直接渲染的进度结构。

流水线只有一条 process 任务，细粒度百分比无从获取，因此进度按「阶段」表达：
每个阶段给出 state（done/running/failed/pending），整体 percent 取当前阶段的
起始值，不假称已完成。
"""

# (key, 中文标签, 阶段起始百分比)
STAGES = (
    ("queued", "排队", 4),
    ("fetching", "下载", 12),
    ("transcribing", "转写", 35),
    ("noting", "笔记", 68),
    ("embedding", "入库", 88),
)

DONE_PERCENT = 100

# status → 当前所处阶段序号（0-based）；done 单独处理
_STATUS_INDEX = {
    "pending": 0,
    "fetching": 1,
    "transcribing": 2,
    "noting": 3,
    "embedding": 4,
}


def _reached_index(has_segments: bool, has_note: bool, has_chunks: bool) -> int:
    """失败时依据已落库的数据推断卡在哪一阶段。

    - 有 chunks：入库阶段产出过，失败发生在 embedding
    - 有 note：笔记已生成，失败发生在 embedding
    - 有 segments：转写已落库，失败发生在 noting
    - 都没有：只能判定失败发生在下载阶段（具体原因看 error 文案）
    """
    if has_chunks or has_note:
        return 4
    if has_segments:
        return 3
    return 1


def compute_progress(
    status: str,
    *,
    has_segments: bool = False,
    has_note: bool = False,
    has_chunks: bool = False,
) -> dict:
    """返回 {status, stage, stage_label, index, total, percent, failed, stages}。"""
    total = len(STAGES)
    failed = status == "failed"

    if status == "done":
        index = total
        percent = DONE_PERCENT
        stage_key = "done"
        stage_label = "完成"
    elif failed:
        index = _reached_index(has_segments, has_note, has_chunks)
        percent = STAGES[index][2]
        stage_key = STAGES[index][0]
        stage_label = STAGES[index][1]
    else:
        index = _STATUS_INDEX.get(status, 0)
        percent = STAGES[index][2]
        stage_key, stage_label, _ = STAGES[index]

    stages = []
    for i, (key, label, _pct) in enumerate(STAGES):
        if i < index:
            state = "done"
        elif i == index and not failed and status != "done":
            state = "running"
        elif i == index and failed:
            state = "failed"
        else:
            state = "pending"
        stages.append({"key": key, "label": label, "state": state})

    return {
        "status": status,
        "stage": stage_key,
        "stage_label": stage_label,
        "index": index,
        "total": total,
        "percent": percent,
        "failed": failed,
        "stages": stages,
    }


def progress_for_video(video) -> dict:
    """从 ORM Video 实例派生进度（segments/note/chunks 均为 selectin 预加载）。"""
    return compute_progress(
        video.status,
        has_segments=bool(getattr(video, "segments", None)),
        has_note=getattr(video, "note", None) is not None,
        has_chunks=bool(getattr(video, "chunks", None)),
    )
