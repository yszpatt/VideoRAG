import re
import time

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core.factory import build_embedder, build_llm, build_transcribers
from app.core.runtime_config import save_runtime_env

router = APIRouter(prefix="/api/settings")

# 掩码标记：GET 脱敏后回显，PUT 时识别到掩码值则不覆盖
MASK_PLACEHOLDER = "****"

# 视为密钥、需要脱敏回显的 env 键（CLOUD_ASR_KEY 虽不以 _API_KEY 结尾，但属密钥）
_SECRET_KEYS = {"CLOUD_ASR_KEY"}

# 由 _mask_secret 生成的「长 key 掩码」格式：前 4 字符 + "..." + 后 4 字符
# （仅 len>8 的 key 走此分支，例：sk-TESTDUMMYKEY1234567890 → sk-T...7890）。
# GET 脱敏回显用；PUT / probe 时识别到此类掩码值必须回落真实已保存 key，
# 绝不能把 "sk-T...7890" 当真实 key 发往第三方服务（会误判鉴权失败）。
_MASK_RE = re.compile(r"^.{4}\.\.\..{4}$")


def _is_mask_value(value: str) -> bool:
    """判断是否为脱敏回显值（**** 或 前4...后4 长格式）。"""
    if not value:
        return False
    return value == MASK_PLACEHOLDER or bool(_MASK_RE.match(value))

# 前端可在线调整的三组服务字段（env 键名）。
# 说明：本项目不需要 TTS（语音合成），原 tts 组已废弃；此处改为 asr 组，
# 直接连到实际的集中式转写配置（CLOUD_ASR_*），保存后立即热重载 transcribers。
GROUPS = {
    "llm": ["LLM_PROVIDER", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"],
    "asr": ["CLOUD_ASR_PROVIDER", "CLOUD_ASR_BASE_URL", "CLOUD_ASR_MODEL", "CLOUD_ASR_KEY"],
    "embedding": ["EMBED_PROVIDER", "EMBED_MODEL", "EMBED_BASE_URL", "EMBED_API_KEY"],
    # 视觉旁路画面描述层（VLM，OpenAI 兼容视觉模型）；留空 = 不启用
    "vlm": ["VLM_BASE_URL", "VLM_API_KEY", "VLM_MODEL"],
    # 视觉旁路（E3）：无语音视频的画面信息采集开关与阈值
    "visual": ["VISUAL_PIPELINE", "VISUAL_MIN_WPM", "VISUAL_MAX_FRAMES"],
    # 本地降级（Local Fallback）：开关/端点/手动路径由 GET 回显；
    # 模型下载管理走 /api/models（见 app/core/local_models/manager.py）。
    "local": [
        "ASR_FALLBACK", "LOCAL_ASR_BASE_URL", "LOCAL_ASR_MODEL_DIR",
        "LOCAL_EMBED_MODEL_DIR", "EMBED_DOWNLOAD_ENDPOINT",
    ],
}

FIELD_DISPLAY = {
    "LLM_PROVIDER": "provider",
    "LLM_API_KEY": "api_key",
    "LLM_BASE_URL": "base_url",
    "LLM_MODEL": "model",
    "CLOUD_ASR_PROVIDER": "provider",
    "CLOUD_ASR_BASE_URL": "base_url",
    "CLOUD_ASR_MODEL": "model",
    "CLOUD_ASR_KEY": "api_key",
    "EMBED_PROVIDER": "provider",
    "EMBED_MODEL": "model",
    "EMBED_BASE_URL": "base_url",
    "EMBED_API_KEY": "api_key",
    "VLM_BASE_URL": "base_url",
    "VLM_API_KEY": "api_key",
    "VLM_MODEL": "model",
    "VISUAL_PIPELINE": "pipeline",
    "VISUAL_MIN_WPM": "min_wpm",
    "VISUAL_MAX_FRAMES": "max_frames",
    "ASR_FALLBACK": "asr_fallback",
    "LOCAL_ASR_BASE_URL": "local_asr_base_url",
    "LOCAL_ASR_MODEL_DIR": "local_asr_model_dir",
    "LOCAL_EMBED_MODEL_DIR": "local_embed_model_dir",
    "EMBED_DOWNLOAD_ENDPOINT": "embed_download_endpoint",
}


def _mask_secret(value: str) -> str:
    """api_key 脱敏：保留前 4 后 4，中间打码；空值原样返回。"""
    if not value:
        return ""
    if len(value) <= 8:
        return MASK_PLACEHOLDER
    return f"{value[:4]}...{value[-4:]}"


def _get_settings(request: Request):
    return request.app.state.components["settings"]


@router.get("")
async def get_settings(request: Request):
    s = _get_settings(request)
    out = {}
    for group, keys in GROUPS.items():
        item = {}
        for key in keys:
            env_name = key
            attr = key.lower()
            value = getattr(s, attr, "")
            if key.endswith("_API_KEY") or key in _SECRET_KEYS:
                value = _mask_secret(value)
            item[FIELD_DISPLAY[key]] = value
        out[group] = item
    # 检索策略参数（数值原样返回）
    out["retrieval"] = {
        field: getattr(s, key.lower()) for key, (field, *_ ) in RETRIEVAL_FIELDS.items()
    }
    return out


class ServiceConfig(BaseModel):
    """服务组配置（llm / asr / embedding 通用）。

    None = 未提供 → 不覆盖（保留 env/runtime 现值）；
    "" = 显式清空 → 写入 runtime.env 空值（清空远程配置即回落本地档，
    如清空 CLOUD_ASR_* → asr_mode 回落 local / 清空 EMBED_PROVIDER+BASE_URL → fastembed 本地）；
    非空 = 覆盖为新值。
    注意：整表保存必须由前端按「加载快照对比」只发送发生变更的组/字段，
    避免未编辑的空字段被误判为清空（见 SettingsView.jsx buildServicePayload）。
    """

    provider: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None


# 检索策略参数：env 键 → (前端字段名, 类型, 下界, 上界)
RETRIEVAL_FIELDS = {
    "RETRIEVAL_VECTOR_K": ("vector_k", int, 1, 100),
    "RETRIEVAL_FTS_K": ("fts_k", int, 1, 100),
    "RETRIEVAL_RRF_K": ("rrf_k", int, 1, 1000),
    "RETRIEVAL_VECTOR_WEIGHT": ("vector_weight", float, 0.0, 1.0),
    "RETRIEVAL_FTS_WEIGHT": ("fts_weight", float, 0.0, 1.0),
    "RETRIEVAL_PER_VIDEO_CAP": ("per_video_cap", int, 0, 20),
    "RETRIEVAL_MIN_SIM": ("min_sim", float, 0.0, 1.0),
    "RETRIEVAL_NEIGHBOR_GAP": ("neighbor_gap", float, 0.0, 30.0),
}


class RetrievalConfig(BaseModel):
    vector_k: int | None = None
    fts_k: int | None = None
    rrf_k: int | None = None
    vector_weight: float | None = None
    fts_weight: float | None = None
    per_video_cap: int | None = None
    min_sim: float | None = None
    neighbor_gap: float | None = None


# 视觉旁路参数：env 键 → (前端字段名, 类型, 约束)
# - str 类型：约束为允许取值元组
# - int 类型：约束为 (下界, 上界)
VISUAL_FIELDS = {
    "VISUAL_PIPELINE": ("pipeline", str, ("off", "auto", "always")),
    "VISUAL_MIN_WPM": ("min_wpm", int, (0, 600)),
    "VISUAL_MAX_FRAMES": ("max_frames", int, (1, 600)),
}


class VisualConfig(BaseModel):
    """视觉旁路配置（E3）。None = 不覆盖。"""

    pipeline: str | None = None
    min_wpm: int | None = None
    max_frames: int | None = None


class LocalConfig(BaseModel):
    """本地降级（Local Fallback）配置。

    None = 不覆盖（保持现状）；"" = 显式清空（回落到默认值）；
    非空 = 写入新值。与 /api/models 的 path PUT 共用同一 runtime.env 键。
    """

    asr_fallback: str | None = None
    local_asr_base_url: str | None = None
    local_asr_model_dir: str | None = None
    local_embed_model_dir: str | None = None
    embed_download_endpoint: str | None = None


class SettingsUpdate(BaseModel):
    llm: ServiceConfig | None = None
    asr: ServiceConfig | None = None
    embedding: ServiceConfig | None = None
    vlm: ServiceConfig | None = None
    visual: VisualConfig | None = None
    retrieval: RetrievalConfig | None = None
    local: LocalConfig | None = None


@router.put("")
async def update_settings(req: SettingsUpdate, request: Request):
    """在线更新三类服务配置：写 runtime.env 持久化 + 热更新组件（立即生效）。"""
    overrides: dict[str, str] = {}
    payload = req.model_dump()

    for group in GROUPS:
        if group in ("local", "visual"):
            continue  # 本地降级 / 视觉旁路组独立处理（字段结构不同，见下）
        cfg = payload.get(group)
        if cfg is None:
            continue  # 组未提供 → 全部不覆盖
        for key in GROUPS[group]:
            field = FIELD_DISPLAY[key]
            value = cfg.get(field)
            if value is None:
                continue  # 字段未提供 → 不覆盖（保留 env/runtime 现值）
            value = str(value).strip()
            if (key.endswith("_API_KEY") or key in _SECRET_KEYS) and value == MASK_PLACEHOLDER:
                continue  # 掩码占位：不覆盖原 key
            overrides[key] = value  # 含 ""：显式清空（清空远程 → 回落本地档）

    # 检索策略参数：None 不覆盖；None 字段跳过；越界报 400
    if req.retrieval is not None:
        rc = req.retrieval.model_dump()
        for key, (field, typ, lo, hi) in RETRIEVAL_FIELDS.items():
            value = rc.get(field)
            if value is None:
                continue
            if not (lo <= value <= hi):
                raise HTTPException(
                    400, f"{key} 超出允许范围 [{lo}, {hi}]：{value}"
                )
            overrides[key] = str(typ(value))

    # 视觉旁路组：None=不覆盖；枚举校验 + 数值范围校验（越界报 400）
    if req.visual is not None:
        vc = req.visual.model_dump()
        for key, (field, typ, bounds) in VISUAL_FIELDS.items():
            value = vc.get(field)
            if value is None:
                continue
            if typ is str:
                if value not in bounds:
                    raise HTTPException(
                        400, f"{key} 必须为 {'/'.join(bounds)}：{value}"
                    )
                overrides[key] = value
            else:
                lo, hi = bounds
                if not (lo <= value <= hi):
                    raise HTTPException(
                        400, f"{key} 超出允许范围 [{lo}, {hi}]：{value}"
                    )
                overrides[key] = str(typ(value))

    # 本地降级组：None=不覆盖；""=显式清空（回落默认）；其余写入
    if req.local is not None:
        lc = req.local.model_dump()
        for key in GROUPS["local"]:
            field = FIELD_DISPLAY[key]
            value = lc.get(field)
            if value is None:
                continue  # 未提供 → 不覆盖
            overrides[key] = value.strip()  # "" 也会写入（清空语义）

    if not overrides:
        raise HTTPException(400, "no settings provided")

    data_dir = _get_settings(request).data_dir
    save_runtime_env(data_dir, overrides)

    # 热更新：应用 runtime 覆盖 → 重建 LLM / Embedder / Transcribers
    c = request.app.state.components
    new_settings = c["settings"].apply_runtime(overrides)
    c["settings"] = new_settings
    c["llm"] = build_llm(new_settings)
    c["embedder"] = build_embedder(new_settings)
    c["transcribers"] = build_transcribers(new_settings)
    # 本地模型管理器同步 settings 引用（GET /api/models 的档位/路径判定用）
    if "model_manager" in c:
        c["model_manager"].refresh_settings(new_settings)
    request.app.state.settings = new_settings
    request.app.state.llm = c["llm"]
    request.app.state.embedder = c["embedder"]
    request.app.state.transcribers = c["transcribers"]

    return {"saved": list(overrides.keys()), "applied": True}


# ============ 连通性探测（设置页每个 Base URL 旁的「测试连通性」）============

# 探测的 URL 候选（按 kind；命中即认为服务可达）。浏览器无法直连容器内/内网
# 地址，探测由服务端代发：LLM / Embedding 均为 OpenAI 兼容 → GET {base}/models；
# ASR 额外试根 /health（SenseVoice / faster-whisper-server 等常见暴露点）。
_PROBE_TIMEOUT = httpx.Timeout(4.0)


def _probe_candidates(kind: str, base: str) -> list[str]:
    base = (base or "").strip().rstrip("/")
    if not base:
        return []
    if kind == "asr":
        root = base[:-3] if base.endswith("/v1") else base  # http://h:9991/v1 → http://h:9991
        out = [f"{root}/health", f"{base}/models"]
    else:  # llm / embedding：OpenAI 兼容均有 GET /models（Ollama / vLLM / DeepSeek…）
        out = [base] if base.endswith("/models") else [f"{base}/models"]
    return list(dict.fromkeys(out))


class ProbeRequest(BaseModel):
    kind: str = ""          # llm | asr | embedding | vlm
    base_url: str = ""
    model: str | None = None
    api_key: str | None = None  # 掩码 **** / 空 → 用当前已保存 key


async def _try_get(url: str, api_key: str) -> dict:
    """GET 单候选：返回 {url, reachable, status?, latency_ms, error?, body?}。"""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT, follow_redirects=True) as c:
            r = await c.get(url, headers=headers)
        return {
            "url": url, "reachable": True, "status": r.status_code,
            "latency_ms": round((time.monotonic() - t0) * 1000),
            "text": r.text[:200_000],
        }
    except Exception as e:  # noqa: BLE001（连接拒绝/超时 → 服务未起/地址错）
        return {
            "url": url, "reachable": False,
            "error": f"{type(e).__name__}: {e}",
            "latency_ms": round((time.monotonic() - t0) * 1000),
        }


def _parse_model_ids(body_text: str) -> list[str]:
    """从 /models 响应体提取模型 id（兼容 Ollama {models:[]} 与 OpenAI {data:[]}）。"""
    try:
        import json

        data = json.loads(body_text or "")
    except (ValueError, TypeError):
        return []
    arr = (data or {}).get("models") or (data or {}).get("data") or []
    ids = []
    for m in arr if isinstance(arr, list) else []:
        if isinstance(m, dict):
            mid = m.get("id") or m.get("model") or m.get("name")
            if mid:
                ids.append(str(mid))
    return ids


@router.post("/probe")
async def probe_service(req: ProbeRequest, request: Request):
    """连通性探测：服务端代发 HTTP 请求到填写的 Base URL（未保存也可测）。

    语义：
    - reachable：URL 能建立连接并返回 HTTP 响应；
    - ok：reachable 且返回 <400；若填了 model 且服务端模型列表里没有 → ok=False；
    - 401/403：已连通但鉴权失败（检查 API Key）；其他 4xx/5xx：路径/地址不对。
    """
    kind = (req.kind or "").strip().lower()
    if kind not in ("llm", "asr", "embedding", "vlm"):
        raise HTTPException(400, "kind 必须为 llm / asr / embedding / vlm")
    base = (req.base_url or "").strip()
    if not base:
        raise HTTPException(400, "请先填写 Base URL 再测试连通性")
    candidates = _probe_candidates(kind, base)
    if not candidates:
        raise HTTPException(400, f"Base URL 格式不正确：{base}")

    # API Key：显式传入且非掩码 → 用传入值；掩码（**** 或 sk-T...7890）/
    # 空 → 回落当前已保存的真实 key（绝不能把掩码当真实 key 发往第三方）。
    s = _get_settings(request)
    saved_key = {
        "llm": s.llm_api_key, "asr": s.cloud_asr_key,
        "embedding": s.embed_api_key, "vlm": s.vlm_api_key,
    }[kind]
    key = (req.api_key or "").strip()
    if _is_mask_value(key):
        key = saved_key

    best, last = None, None
    for url in candidates:
        item = await _try_get(url, key)
        last = item
        if item["reachable"] and (best is None or item["status"] < best["status"]):
            best = item  # 多个候选都可达时取状态码最小（如 /health 200 优于 /models 404）
    if best is None:
        best = last

    reachable = bool(best and best["reachable"])
    status = best.get("status") if reachable else None
    endpoint = best.get("url", "")
    latency_ms = best.get("latency_ms", 0)

    # /models 候选的模型列表（llm / embedding 用于核对目标模型是否就绪）
    model_ids = []
    if reachable and status is not None and status < 500 and best.get("text"):
        model_ids = _parse_model_ids(best.get("text", ""))
    model_count = len(model_ids) if model_ids else None
    req_model = (req.model or "").strip()
    model_found = None
    if model_count:
        model_found = any(
            req_model == mid or req_model.lower() in str(mid).lower()
            for mid in model_ids
        ) if req_model else None

    ok = reachable and status is not None and status < 400
    if ok and req_model and model_found is False:
        ok = False  # 服务可达但目标模型不存在 → 仍需提示

    if not reachable:
        message = f"无法连接 {endpoint}：{best.get('error') or '无响应'}"
    elif status in (401, 403):
        message = f"已连通但鉴权失败（HTTP {status}，{latency_ms} ms）——检查 API Key"
    elif status >= 400:
        message = (
            f"已连通但接口返回 HTTP {status}（{endpoint}，{latency_ms} ms）"
            f"——检查 Base URL 是否为服务地址（如 http://host:8000 或 …/v1）"
        )
    elif model_found is False:
        message = f"连接成功（HTTP {status}，{latency_ms} ms），但未找到模型 {req_model}（共 {model_count} 个）"
    else:
        message = f"连接成功（HTTP {status}，{latency_ms} ms）"
        if model_count:
            message += f" · 服务端共 {model_count} 个模型"
        if req_model and model_found:
            message += f" · 目标模型 {req_model} 已就绪"

    return {
        "kind": kind,
        "ok": ok,
        "reachable": reachable,
        "status": status,
        "endpoint": endpoint,
        "latency_ms": latency_ms,
        "model_count": model_count,
        "model_found": model_found,
        "message": message,
        "error": None if reachable else best.get("error"),
    }
