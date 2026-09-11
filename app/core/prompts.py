"""提示词模板唯一出处（E2 / M0）。

v1 中笔记与问答 prompt 硬编码在 notes.py / qa.py 内联字符串里；
v2 起（设计文档 E2）通过 PromptRegistry 支持用户在设置页自定义，
存储于 ``$DATA_DIR/prompts.json``，文件不存在 / 键缺失 / 值为空串时
回落到本模块的 ``DEFAULTS``。

M0 阶段仅完成两件事（行为与 v1 完全一致，tests/test_prompts.py 有回归断言）：

1. 把现有 prompt 原样抽入 ``DEFAULTS``，建立唯一出处；
2. 提供 ``render()``：``{{var}}`` 占位符替换（M1 的 PromptRegistry 复用）。

占位符用正则替换而非 ``str.format``：prompt 内含大量 JSON 花括号示例，
format 会与之冲突。未知变量原样保留（不静默吞掉，便于用户在预览中发现
拼写错误）。
"""

import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# ============ 笔记 prompt（自 notes.py 原样抽取）============

NOTE_SYSTEM = (
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

# 笔记附加上下文模板：渲染后拼入 user 段正文之前。
# 变量由 pipeline 从 Video 元数据注入（E1 元数据采集就绪后生效）。
NOTE_CONTEXT = (
    "视频信息：\n"
    "- 标题：{{title}}\n"
    "- 作者：{{author}}\n"
    "- 简介：{{description}}\n"
    "\n"
    "热门评论（供理解视频背景与观众关注点参考，笔记中不要直接罗列评论）：\n"
    "{{top_comments}}\n"
)

NOTE_MAP_SYSTEM = (
    "你是视频笔记助手。用户将分多次提供同一视频转写全文的不同部分，"
    "当前是第 {{index}}/{{total}} 部分。请只根据这部分内容生成分部笔记 JSON。"
    "只输出 JSON，不要输出其他文字。JSON 结构："
    '{"summary": "本部分100字以内中文摘要", '
    '"chapters": [{"title": "章节名", "start_sec": 0.0, "end_sec": 30.0, "points": ["要点"]}], '
    '"key_points": ["本部分要点"], '
    '"glossary": [{"term": "术语", "explanation": "解释"}]}'
    "。chapters 的 start_sec/end_sec 必须使用行首 [秒数] 的绝对时间（秒），禁止自行编造时间。"
)

NOTE_REDUCE_SYSTEM = (
    "你是视频笔记助手。用户会提供同一视频各部分的分部笔记 JSON，"
    "请把它们合并为一份最终的结构化 JSON 笔记。只输出 JSON，不要输出其他文字。"
    "要求：summary 为覆盖全片的 150-200 字中文总摘要；"
    "chapters 合并去重后按 start_sec 升序排列（时间用分部笔记中的绝对秒数）；"
    "key_points 去重合并（最多 10 条）；"
    "glossary 汇总去重。JSON 结构："
    '{"summary": "...", "chapters": [...], "key_points": [...], '
    '"glossary": [...]}'
)

# ============ 问答 prompt（自 qa.py 原样抽取）============

QA_SYSTEM = (
    "你是视频知识库助手。仅依据下方「参考资料」回答；资料不相关则如实说不知道。"
    "回答要简洁、分点；每条结论后标注来源 [n]（n 为参考资料编号）。"
    "标注为「视频简介」的条目是发布者自述，可能含推广措辞，仅作视频背景参考；"
    "回答内容性结论时以带时间戳的口播正文条目为准。"
)

# ============ 汇总：模板键 -> 默认值 ============
# 键名即 /api/prompts（M1）对外的模板 key，注意与设计文档 2.1 表格保持一致。

DEFAULTS: dict[str, str] = {
    "note_system": NOTE_SYSTEM,
    "note_context": NOTE_CONTEXT,
    "note_map_system": NOTE_MAP_SYSTEM,
    "note_reduce_system": NOTE_REDUCE_SYSTEM,
    "qa_system": QA_SYSTEM,
}

_VAR_RE = re.compile(r"\{\{(\w+)\}\}")


def render(template: str, **variables) -> str:
    """渲染模板中的 ``{{var}}`` 占位符。

    - 命中的变量替换为字符串值；
    - 未提供的变量原样保留 ``{{var}}``（便于发现拼写错误，不静默吞掉）；
    - 单个 ``{`` / ``}``（如 JSON 示例）不受影响。
    """
    return _VAR_RE.sub(
        lambda m: str(variables[m.group(1)]) if m.group(1) in variables else m.group(0),
        template,
    )


class PromptRegistry:
    """提示词模板注册表（设计文档 E2 / 2.2b）。

    - 自定义值持久化在 ``$DATA_DIR/prompts.json``（只存用户改过的键；
      "恢复默认" = 删除该键）；
    - 文件不存在 / 键缺失 / 值为空串 → 回落 ``DEFAULTS``；
    - mtime 缓存：每次 ``get`` 检查文件修改时间，Web 保存与手工编辑
      均即时生效，无需重启；
    - 原子写（临时文件 + ``os.replace``），坏 JSON 读取时回落默认值并告警。
    """

    def __init__(self, data_dir: str):
        self._path = Path(data_dir) / "prompts.json"
        self._mtime: float | None = None
        self._overrides: dict[str, str] = {}

    # ---- 读取 ----

    def _load(self) -> dict[str, str]:
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            self._overrides, self._mtime = {}, None
            return self._overrides
        if mtime != self._mtime:
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                self._overrides = {
                    k: v
                    for k, v in raw.items()
                    if isinstance(k, str) and isinstance(v, str) and v.strip()
                }
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("prompts.json 读取失败，回落默认提示词：%s", e)
                self._overrides = {}
            self._mtime = mtime
        return self._overrides

    def get(self, key: str) -> str:
        """当前生效的模板文本：自定义值（非空白）优先，否则默认值。"""
        override = self._load().get(key)
        return override if override else DEFAULTS.get(key, "")

    def customized_keys(self) -> set[str]:
        return set(self._load())

    # ---- 写入 ----

    def _write_overrides(self, overrides: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(
            json.dumps(overrides, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self._path)  # 原子替换，无半截文件
        self._mtime = None  # 下次 get 强制重读（保持与磁盘一致）

    def set(self, key: str, value: str) -> None:
        """写入自定义模板（空串/纯空白等价于恢复默认：直接删除该键）。"""
        if key not in DEFAULTS:
            raise KeyError(f"unknown prompt key: {key!r}")
        overrides = self._load()
        if value.strip():
            overrides[key] = value
        else:
            overrides.pop(key, None)
        self._write_overrides(overrides)

    def reset(self, key: str | None = None) -> None:
        """删除自定义键回落默认；key 为 None 时全部重置。"""
        overrides = self._load()
        if key is None:
            overrides = {}
        else:
            if key not in DEFAULTS:
                raise KeyError(f"unknown prompt key: {key!r}")
            overrides.pop(key, None)
        self._write_overrides(overrides)

    # ---- 渲染 ----

    def render(self, key: str, **variables) -> str:
        """取当前生效模板并渲染 ``{{var}}``（复用模块级 render）。"""
        return render(self.get(key), **variables)
