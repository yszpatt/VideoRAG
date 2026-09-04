import { useEffect, useRef, useState } from "react";
import {
  getPrompts,
  previewPrompt,
  resetPrompts,
  updatePrompts,
} from "../api.js";

// 模板展示顺序：常用在前，map/reduce 归入「高级」折叠
const ORDER = [
  "note_system",
  "note_context",
  "qa_system",
  "note_map_system",
  "note_reduce_system",
];
const ADVANCED = new Set(["note_map_system", "note_reduce_system"]);

const VAR_HINTS = {
  title: "视频标题",
  author: "作者（元数据采集后生效）",
  description: "视频简介（元数据采集后生效）",
  top_comments: "热评 top5（元数据采集后生效）",
  question: "用户问题",
  n_references: "本次检索命中条数",
  index: "当前分块序号",
  total: "分块总数",
};

export default function PromptTab() {
  const [templates, setTemplates] = useState(null);
  const [activeKey, setActiveKey] = useState("note_system");
  const [draft, setDraft] = useState("");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [saving, setSaving] = useState(false);
  const [preview, setPreview] = useState(null);
  const [previewing, setPreviewing] = useState(false);
  const [msg, setMsg] = useState(null);
  const [err, setErr] = useState(null);
  const taRef = useRef(null);

  const load = async () => {
    try {
      const d = await getPrompts();
      setTemplates(d.templates);
      // 首屏挂载时 draft 尚未初始化（selectKey 只在点击时触发），需从已加载模板
      // 回填当前选中项，否则文本框为空、且 dirty 被误判为 true。
      // 仅在 draft 为空时回填，避免覆盖后续保存/重置流程中的编辑态。
      setDraft((prev) => prev || d.templates[activeKey]?.value || "");
      setErr(null);
      return d.templates;
    } catch (e) {
      setErr(`加载提示词失败：${e.message}`);
      return null;
    }
  };

  useEffect(() => {
    load();
  }, []);

  const active = templates ? templates[activeKey] : null;
  const dirty = active ? draft !== active.value : false;

  const selectKey = (key) => {
    setActiveKey(key);
    setDraft(templates[key].value);
    setPreview(null);
    setMsg(null);
    setErr(null);
  };

  // 当前模板在列表中的展示顺序（含折叠逻辑）
  const visibleKeys = templates
    ? ORDER.filter(
        (k) => !ADVANCED.has(k) || showAdvanced || k === activeKey || templates[k].customized,
      )
    : [];

  const onEdit = (v) => setDraft(v);

  const flash = (text) => {
    setMsg(text);
    setErr(null);
  };

  const onSave = async () => {
    setSaving(true);
    setMsg(null);
    setErr(null);
    try {
      await updatePrompts({ [activeKey]: draft });
      await load(); // 刷新已保存值 → dirty 自动归零
      setPreview(null);
      flash(`「${active.label}」已保存，下一次生成 / 提问即生效`);
    } catch (e) {
      setErr(`保存失败：${e.message}`);
    } finally {
      setSaving(false);
    }
  };

  const onPreview = async () => {
    setPreviewing(true);
    setMsg(null);
    setErr(null);
    try {
      // 有未保存改动时预览草稿（保存前可确认效果）
      const r = await previewPrompt(activeKey, dirty ? draft : null);
      setPreview(r);
    } catch (e) {
      setErr(`预览失败：${e.message}`);
    } finally {
      setPreviewing(false);
    }
  };

  const onResetOne = async () => {
    try {
      await resetPrompts(activeKey);
      const t = await load();
      if (t) setDraft(t[activeKey].value); // 编辑框同步回落默认值
      setPreview(null);
      flash(`「${active.label}」已恢复默认`);
    } catch (e) {
      setErr(`恢复默认失败：${e.message}`);
    }
  };

  const onResetAll = async () => {
    if (!window.confirm("确定恢复全部提示词为默认值？所有自定义修改将被清除。")) return;
    try {
      await resetPrompts(null);
      const t = await load();
      if (t) setDraft(t[activeKey].value);
      setPreview(null);
      flash("已恢复全部默认提示词");
    } catch (e) {
      setErr(`恢复默认失败：${e.message}`);
    }
  };

  // 把 {{var}} 插入 textarea 光标处
  const insertVar = (name) => {
    const token = `{{${name}}}`;
    const ta = taRef.current;
    if (!ta) {
      setDraft(draft + token);
      return;
    }
    const start = ta.selectionStart ?? draft.length;
    const end = ta.selectionEnd ?? draft.length;
    const next = draft.slice(0, start) + token + draft.slice(end);
    setDraft(next);
    requestAnimationFrame(() => {
      ta.focus();
      const pos = start + token.length;
      ta.setSelectionRange(pos, pos);
    });
  };

  if (err && !templates) {
    return (
      <div className="prompt-tab">
        <div className="alert alert-error">{err}</div>
      </div>
    );
  }
  if (!templates || !active) {
    return (
      <div className="loading-block">
        <span className="spinner" />
        加载提示词…
      </div>
    );
  }

  return (
    <div className="prompt-tab">
      <div className="prompt-layout">
        <aside className="prompt-list">
          {visibleKeys.map((key) => (
            <button
              key={key}
              className={`prompt-item${key === activeKey ? " active" : ""}`}
              onClick={() => selectKey(key)}
            >
              <span>{templates[key].label}</span>
              {templates[key].customized && (
                <span className="prompt-badge">自定义</span>
              )}
            </button>
          ))}
          <button
            className="prompt-item prompt-advanced-toggle"
            onClick={() => setShowAdvanced((v) => !v)}
          >
            {showAdvanced ? "收起高级项" : "展开高级项"}
          </button>

          <div className="prompt-vars">
            <h4 className="prompt-vars-title">可用变量（点击插入）</h4>
            {active.vars.length === 0 && (
              <p className="muted small">该模板不支持变量</p>
            )}
            {active.vars.map((v) => (
              <button
                key={v}
                className="prompt-var-chip"
                onClick={() => insertVar(v)}
                title={VAR_HINTS[v] || v}
              >
                <code>{`{{${v}}}`}</code>
                <span>{VAR_HINTS[v] || v}</span>
              </button>
            ))}
            <p className="muted small prompt-vars-note">
              未识别的变量会原样保留在提示词中，可借此发现拼写错误。
            </p>
          </div>
        </aside>

        <div className="prompt-editor">
          <div className="prompt-editor-head">
            <h3 className="sub-title">{active.label}</h3>
            {active.customized ? (
              <span className="prompt-badge">已自定义</span>
            ) : (
              <span className="muted small">使用默认值</span>
            )}
          </div>

          <textarea
            ref={taRef}
            className="prompt-textarea"
            value={draft}
            onChange={(e) => onEdit(e.target.value)}
            spellCheck={false}
          />

          <div className="prompt-actions">
            <button
              className="btn btn-primary"
              onClick={onSave}
              disabled={saving || !dirty}
            >
              {saving ? "保存中…" : "保存并生效"}
            </button>
            <button
              className="btn btn-ghost"
              onClick={onPreview}
              disabled={previewing}
            >
              {previewing ? "渲染中…" : "预览渲染效果"}
            </button>
            <button
              className="btn btn-ghost"
              onClick={onResetOne}
              disabled={!active.customized}
            >
              恢复默认
            </button>
            <button className="btn btn-ghost prompt-danger" onClick={onResetAll}>
              全部恢复默认
            </button>
            {dirty && (
              <span className="muted small">有未保存的修改</span>
            )}
          </div>

          {err && <div className="alert alert-error">{err}</div>}
          {msg && <div className="alert alert-ok">{msg}</div>}

          {preview && (
            <div className="prompt-preview-wrap">
              <p className="muted small prompt-preview-caption">
                渲染预览（样例数据；实际发送时正文由转写内容拼接）：
              </p>
              <pre className="prompt-preview">{preview.rendered}</pre>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
