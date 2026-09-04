import { useEffect, useRef, useState } from "react";

/** 导入视频弹窗：输入链接 → 确认 → 关闭面板，处理进度在「视频收藏」页与侧边栏可见。 */
export default function ImportVideoDialog({ open, busy = false, onSubmit, onClose }) {
  const [url, setUrl] = useState("");
  const inputRef = useRef(null);

  useEffect(() => {
    if (open) {
      setUrl("");
      setTimeout(() => inputRef.current?.focus(), 40);
    }
  }, [open]);

  if (!open) return null;

  const submit = () => {
    const text = url.trim();
    if (!text || busy) return;
    onSubmit(text);
  };

  return (
    <div className="modal-backdrop" onClick={busy ? undefined : onClose}>
      <div className="modal modal-sm" onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true">
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
          粘贴视频链接，导入后会自动下载、转写并生成笔记。处理进度可在「视频收藏」中查看。
        </p>
        <div className="modal-body">
          <input
            ref={inputRef}
            className="input"
            type="text"
            placeholder="https://www.bilibili.com/video/..."
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                submit();
              }
              if (e.key === "Escape" && !busy) onClose();
            }}
            disabled={busy}
          />
          <div className="modal-foot-row">
            <button type="button" className="btn btn-ghost" onClick={onClose} disabled={busy}>
              取消
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={submit}
              disabled={busy || !url.trim()}
            >
              {busy ? "提交中…" : "确认导入"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
