import { useEffect, useRef, useState } from "react";

/** 从粘贴的多行文本里挑出链接（每行一个，也容忍空格分隔），去重并保持顺序。 */
export function parseUrls(text) {
  const found = (text || "")
    .split(/\s+/)
    .map((s) => s.trim())
    .filter((s) => /^https?:\/\/\S+$/i.test(s));
  return [...new Set(found)];
}

const FILE_ACCEPT = "video/*,audio/*,.mp4,.mkv,.webm,.mov,.avi,.flv,.m4v,.ts,.mp3,.m4a,.wav,.flac,.aac,.ogg";

function fmtSize(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v < 10 && i > 0 ? v.toFixed(1) : Math.round(v)} ${u[i]}`;
}

/**
 * 导入视频弹窗，两种来源：
 * 1. 在线链接（多行文本，一次多条）；
 * 2. **本地视频/音频文件**：按钮打开文件管理器多选，或直接把文件拖进来（支持多个）。
 *
 * 提交后：链接走 onSubmitUrls、文件走 onSubmitFiles（带上传进度），两者都完成后关闭并汇报结果。
 * 弹窗在上传期间保持打开，大文件能看到进度，避免"点了没反应"。
 */
export default function ImportVideoDialog({
  open,
  busy = false,
  onSubmitUrls,
  onSubmitFiles,
  notify,
  onClose,
}) {
  const [text, setText] = useState("");
  const [files, setFiles] = useState([]);
  const [dragging, setDragging] = useState(false);
  const [progress, setProgress] = useState({}); // 文件名 → 0..1
  const [working, setWorking] = useState(false);
  const [err, setErr] = useState(null);
  const taRef = useRef(null);
  const fileRef = useRef(null);

  useEffect(() => {
    if (open) {
      setText("");
      setFiles([]);
      setProgress({});
      setWorking(false);
      setErr(null);
      setDragging(false);
      setTimeout(() => taRef.current?.focus(), 40);
    }
  }, [open]);

  if (!open) return null;

  const urls = parseUrls(text);
  const total = urls.length + files.length;
  const isBusy = working || busy;

  const addFiles = (list) => {
    const incoming = [...(list || [])].filter((f) => f && f.size > 0);
    if (!incoming.length) return;
    setFiles((prev) => {
      const seen = new Set(prev.map((f) => `${f.name}:${f.size}`));
      return [...prev, ...incoming.filter((f) => !seen.has(`${f.name}:${f.size}`))];
    });
    setErr(null);
  };

  const removeFile = (name) => setFiles((prev) => prev.filter((f) => f.name !== name));

  const submit = async () => {
    if (!total || isBusy) return;
    setErr(null);
    setWorking(true);
    const okUrls = [];
    const failUrls = [];
    const okFiles = [];
    const failFiles = [];

    if (urls.length) {
      try {
        const r = await onSubmitUrls?.(urls);
        okUrls.push(...(r?.ok || []));
        failUrls.push(...(r?.failed || []));
      } catch (e) {
        failUrls.push(e.message);
      }
    }

    if (files.length) {
      try {
        const r = await onSubmitFiles?.(files, (name, p) =>
          setProgress((prev) => ({ ...prev, [name]: p })),
        );
        okFiles.push(...(r?.ok || []));
        failFiles.push(...(r?.failed || []));
      } catch (e) {
        failFiles.push({ name: "（全部文件）", msg: e.message });
      }
    }

    setWorking(false);

    const okCount = okUrls.length + okFiles.length;
    const fails = [...failUrls, ...failFiles.map((f) => `${f.name}：${f.msg}`)];
    if (!okCount) {
      setErr(`导入失败：${fails[0] || "未知错误"}`);
      return; // 弹窗留开，便于修改后重试
    }
    if (fails.length) {
      notify?.(`成功 ${okCount} 个，失败 ${fails.length} 个：${fails[0]}`, "error");
    } else {
      notify?.(`已导入 ${okCount} 个，开始处理`, "success");
    }
    onClose?.();
  };

  return (
    <div className="modal-backdrop" onClick={isBusy ? undefined : onClose}>
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
          <button className="icon-btn" onClick={onClose} aria-label="关闭" disabled={isBusy}>
            ×
          </button>
        </div>
        <p className="modal-desc">
          粘贴视频链接（一行一个，可多条）导入在线视频，或把
          <b>本地视频文件拖进来 / 点击选择</b>（支持多选）。导入后会自动转写并生成笔记。
        </p>
        <div className="modal-body">
          <textarea
            ref={taRef}
            className="input import-textarea"
            rows={3}
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
              if (e.key === "Escape" && !isBusy) onClose();
            }}
            disabled={isBusy}
          />
          <p className="muted small import-hint">
            Enter 换行 · Ctrl/⌘ + Enter 提交
            {urls.length > 0 && ` · 已识别 ${urls.length} 个链接`}
            {text.trim() && urls.length === 0 && " · 没识别到 http(s) 链接"}
          </p>

          {/* 本地文件：点击选择 / 拖入 */}
          <div
            className={`import-drop${dragging ? " is-dragging" : ""}`}
            onClick={() => !isBusy && fileRef.current?.click()}
            onDragOver={(e) => {
              e.preventDefault();
              if (!isBusy) setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              if (!isBusy) addFiles(e.dataTransfer?.files);
            }}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                fileRef.current?.click();
              }
            }}
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M12 16V4m0 0L8 8m4-4 4 4" />
              <path d="M4 16v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" />
            </svg>
            <span>
              {dragging ? "松手即可添加文件" : "把本地视频/音频拖到这里，或点击选择文件"}
            </span>
            <span className="muted small">支持多选（mp4 / mkv / mov / webm / mp3 …）</span>
            <input
              ref={fileRef}
              type="file"
              accept={FILE_ACCEPT}
              multiple
              hidden
              onChange={(e) => {
                addFiles(e.target.files);
                e.target.value = ""; // 允许重复选择同一文件
              }}
            />
          </div>

          {files.length > 0 && (
            <ul className="import-file-list">
              {files.map((f) => {
                const p = progress[f.name] || 0;
                return (
                  <li key={`${f.name}:${f.size}`}>
                    <div className="import-file-row">
                      <span className="import-file-name" title={f.name}>
                        {f.name}
                      </span>
                      <span className="muted small">{fmtSize(f.size)}</span>
                      <button
                        className="lib-col-del"
                        style={{ opacity: 1 }}
                        onClick={() => removeFile(f.name)}
                        disabled={isBusy}
                        title="移除"
                        aria-label="移除"
                      >
                        ×
                      </button>
                    </div>
                    {p > 0 && (
                      <div className="import-file-bar">
                        <span style={{ width: `${Math.round(p * 100)}%` }} />
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          )}

          {err && <div className="alert alert-error">{err}</div>}

          <div className="modal-foot-row">
            <span className="muted small">
              {total === 0
                ? "还没有待导入的内容"
                : `待导入：${urls.length ? `${urls.length} 个链接` : ""}${
                    urls.length && files.length ? " + " : ""
                  }${files.length ? `${files.length} 个本地文件` : ""}`}
            </span>
            <button type="button" className="btn btn-ghost" onClick={onClose} disabled={isBusy}>
              取消
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={submit}
              disabled={isBusy || total === 0}
            >
              {working ? "上传中…" : `确认导入${total > 1 ? ` ${total} 个` : ""}`}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
