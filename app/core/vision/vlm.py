"""画面描述层（VLM）——**本版仅预留接口，不发起真实视觉调用**。

背景：无语音视频的画面信息有两个来源层——
1. 本地 OCR（`ocr.py`，本次完整实现，零成本、纯 CPU）；
2. 云 VLM 画面描述（本模块，调用 OpenAI 兼容视觉模型，有 API 费用）。

本次交付只做第 1 层。第 2 层保留完整接口与配置（`VLM_BASE_URL` / `VLM_API_KEY`
/ `VLM_MODEL`，见 `Settings.vlm_enabled`），后续接入时只需：
1. 在 `describe_frame` 内改为真实调用（复用 `openai.AsyncOpenAI`，messages 用
   `image_url` 传 base64 图片）；
2. 在 `app/jobs/pipeline.py` 构造并传入 vlm 实例（当前传 None → 编排层直接跳过）。

这样配置、设置页连通性探测、前端展示都已就绪，接实现时无需再动其它模块。
"""

# 画面描述的文本前缀（视觉段在转写/笔记中以此区分来源；由 notes.py 统一加）
class VLMNotImplemented(NotImplementedError):
    """VLM 层尚未接入真实实现（本版预期行为）。"""


def is_vlm_configured(base_url: str, model: str) -> bool:
    """VLM 是否已配置：base_url + model 齐全即可（本地服务可无 api_key）。"""
    return bool((base_url or "").strip() and (model or "").strip())


async def describe_frame(
    image_path: str,
    *,
    base_url: str = "",
    api_key: str = "",
    model: str = "",
) -> str:
    """对单帧画面生成一句关键信息描述（**预留接口，本版未实现**）。

    Args:
        image_path: 关键帧图片路径（jpg）。
        base_url / api_key / model: OpenAI 兼容视觉模型三件套。

    Raises:
        VLMNotImplemented: 本版固定抛出；编排层捕获后跳过画面描述层。
    """
    raise VLMNotImplemented(
        "画面描述层（VLM）为预留接口，当前版本未接入真实视觉模型调用；"
        "本地 OCR 层不受影响。"
    )
