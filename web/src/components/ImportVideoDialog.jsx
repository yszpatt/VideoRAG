import { useEffect, useRef, useState } from "react";

/** 从粘贴的多行文本里挑出链接（每行一个，也容忍空格分隔），去重并保持顺序。 */
export function parseUrls(text) {
  const found = (text || "")
    .split(/\s+/)
    .map((s) => s.trim())
    .filter((s) => /^https?:\/\/\S+$/i.test(s));
  return [...new Set(found)];
}

/**
 * 导入视频弹窗：多行输入，支持一次粘贴多个链接（每行一个）。
 *
 * - 输入框从单行 input 换成可换行的 textarea（原来一行放不下长链接、也无法多链接）；
 * - Enter 换行、Ctrl/⌘ + Enter 提交（与「支持换行」一致）；
 * - 提交时把识别到的链接数组交给上层批量提交，逐条报成败。
 */
export default function ImportVideoDialog({ open, busy = false, onSubmit, onClose }) {
  const [text, setText] = useState("");
  const taRef = useRef(null);

  useEffect(() => {
    if (open) {
      setText("");
      setTimeout(() => taRef.current?.focus(), 40);
    }
  }, [open]);

  if (!open) return null;

  const urls = parseUrls(text);
  const submit = () => {
    if (!urls.length || busy) return;
    onSubmit(urls);
  };

  return (
    <div className="modal-backdrop" onClick={busy ? undefined : onClose}>
      <div
        className="modal modal-import"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
      >
        <div className="modal-head">
          <div className="modal-title-wrap">
            <span className="modal-badge">＋</span>
            <h3 className="modal-title">导入视频</h3>
          </div>
          <button className="icon-btn" onClick={onClose} aria-label="关闭" disabled={busy}>
            ×
          </button>
        </div>
        <p className="modal-desc">
          粘贴视频链接，导入后会自动下载、转写并生成笔记。<b>一行一个链接</b>，可一次导入多条；
          处理进度可在「视频收藏」中查看。
        </p>
        <div className="modal-body">
          <textarea
            ref={taRef}
            className="input import-textarea"
            rows={5}
            placeholder={
              "https://www.bilibili.com/video/BV...\n" +
              "https://v.douyin.com/xxxxxx/\n" +
              "https://www.xiaohongshu.com/explore/...?xsec_token=..."
            }
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                submit();
              }
              if (e.key === "Escape" && !busy) onClose();
            }}
            disabled={busy}
          />
          <p className="muted small import-hint">
            Enter 换行 · Ctrl/⌘ + Enter 提交
            {urls.length > 0 && ` · 已识别 ${urls.length} 个链接`}
            {text.trim() && urls.length === 0 && " · 没识别到 http(s) 链接"}
          </p>
          <div className="modal-foot-row">
            <button type="button" className="btn btn-ghost" onClick={onClose} disabled={busy}>
              取消
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={submit}
              disabled={busy || urls.length === 0}
            >
              {busy
                ? "提交中…"
                : urls.length > 1
                  ? `确认导入 ${urls.length} 个`
                  : "确认导入"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
