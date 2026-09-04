import { useEffect, useMemo, useState } from "react";
import Markdown from "./Markdown.jsx";
import ProgressTrack, { PlatformBadge, StatusChip } from "./ProgressTrack.jsx";
import { getNote, getTranscript, getVideo } from "../api.js";
import { useToast } from "../hooks/useToast.jsx";
import {
  fmtCount,
  fmtDuration,
  fmtTs,
  fmtUploadDate,
  watchUrl,
} from "../lib/format.js";

function Chip({ children }) {
  return <span className="chip">{children}</span>;
}

/** E1：简介（4 行折叠）+ 标签；展示于笔记 Tab 顶部（热门评论已移至笔记下方） */
function VideoMeta({ video }) {
  const [expanded, setExpanded] = useState(false);
  if (!video) return null;
  const tags = video.tags || [];
  const desc = (video.description || "").trim();
  const noMeta = video.meta_source === "none";
  const longDesc = desc.length > 240 || desc.split("\n").length > 4;
  if (!(desc || tags.length || noMeta)) return null;

  return (
    <div className="video-info">
      {noMeta && (
        <p className="muted small">该视频元数据未采集，仅显示基础信息（标题可能为链接）。</p>
      )}

      {tags.length > 0 && (
        <div className="info-tags">
          {tags.map((t) => (
            <span className="chip" key={t}>
              {t}
            </span>
          ))}
        </div>
      )}

      {desc && (
        <div className="info-block">
          <div className="info-block-head">
            <h4>简介</h4>
            {longDesc && (
              <button
                className="link-btn"
                onClick={() => setExpanded((v) => !v)}
              >
                {expanded ? "收起" : "展开全文"}
              </button>
            )}
          </div>
          <p className={`info-desc-text${expanded ? " expanded" : ""}`}>{desc}</p>
        </div>
      )}
    </div>
  );
}

/** E1：热门评论 top5（按点赞降序）；按需求展示于笔记正文下方 */
function HotComments({ video }) {
  if (!video) return null;
  const comments = video.comments || [];
  if (comments.length === 0) return null;

  return (
    <div className="video-info is-foot">
      <div className="info-block">
        <div className="info-block-head">
          <h4>热门评论</h4>
          <span className="muted small">按点赞排序 · top {comments.length}</span>
        </div>
        {comments.map((c, i) => (
          <div className="info-comment" key={i}>
            <div className="info-comment-head">
              <span className="info-comment-author">{c.author || "匿名"}</span>
              <span className="info-comment-likes">
                {fmtCount(c.like_count)} 赞
              </span>
            </div>
            <p className="info-comment-text">{c.text}</p>
          </div>
        ))}
      </div>
    </div>
  );
}

function NoteBody({ note, title }) {
  const [showRaw, setShowRaw] = useState(false);
  const { push } = useToast();
  if (!note) return null;
  const chapters = note.chapters || [];
  const points = note.key_points || [];
  const glossary = note.glossary || [];

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(note.markdown || "");
      push("笔记已复制到剪贴板", "success");
    } catch (_) {
      push("复制失败，请检查浏览器权限", "error");
    }
  };

  const download = () => {
    const blob = new Blob([note.markdown || ""], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${(title || "note").slice(0, 40)}.md`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="note-body">
      {note.summary && (
        <section className="note-section">
          <h4>摘要</h4>
          <p className="note-summary">{note.summary}</p>
        </section>
      )}

      {chapters.length > 0 && (
        <section className="note-section">
          <h4>章节</h4>
          <ol className="chapter-list">
            {chapters.map((c, i) => (
              <li key={i}>
                <a
                  className="chapter-time"
                  href={watchUrl(note.url, c.start_sec) || "#"}
                  target="_blank"
                  rel="noreferrer"
                >
                  {fmtTs(c.start_sec)}
                </a>
                <div>
                  <div className="chapter-title">{c.title}</div>
                  {Array.isArray(c.points) && c.points.length > 0 && (
                    <ul className="chapter-points">
                      {c.points.map((p, j) => (
                        <li key={j}>{p}</li>
                      ))}
                    </ul>
                  )}
                </div>
              </li>
            ))}
          </ol>
        </section>
      )}

      {points.length > 0 && (
        <section className="note-section">
          <h4>关键要点</h4>
          <ul className="point-list">
            {points.map((p, i) => (
              <li key={i}>{typeof p === "string" ? p : p.text || JSON.stringify(p)}</li>
            ))}
          </ul>
        </section>
      )}

      {glossary.length > 0 && (
        <section className="note-section">
          <h4>术语表</h4>
          <div className="glossary">
            {glossary.map((g, i) => (
              <div className="glossary-item" key={i}>
                <Chip>{typeof g === "string" ? g : g.term}</Chip>
                <span>{typeof g === "string" ? "" : g.explanation}</span>
              </div>
            ))}
          </div>
        </section>
      )}

      {note.markdown && (
        <section className="note-section">
          <div className="note-raw-head">
            <h4 style={{ margin: 0 }}>完整笔记</h4>
            <div className="note-actions">
              <button className="btn btn-ghost btn-sm" onClick={() => setShowRaw((v) => !v)}>
                {showRaw ? "收起" : "展开"}
              </button>
              <button className="btn btn-ghost btn-sm" onClick={copy}>
                复制
              </button>
              <button className="btn btn-ghost btn-sm" onClick={download}>
                下载 .md
              </button>
            </div>
          </div>
          {showRaw && <Markdown>{note.markdown}</Markdown>}
        </section>
      )}
    </div>
  );
}

function TranscriptBody({ videoId, url }) {
  const [segments, setSegments] = useState(null);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState("");

  useEffect(() => {
    let alive = true;
    getTranscript(videoId)
      .then((d) => alive && setSegments(d.segments || []))
      .catch(() => alive && setSegments([]))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [videoId]);

  const filtered = useMemo(() => {
    if (!segments) return [];
    const q = filter.trim().toLowerCase();
    if (!q) return segments;
    return segments.filter((s) => (s.text || "").toLowerCase().includes(q));
  }, [segments, filter]);

  if (loading) return <p className="muted">正在加载转写…</p>;
  if (!segments || segments.length === 0)
    return <p className="muted">暂无转写内容（该视频可能仍在处理，或没有可用音轨）。</p>;

  return (
    <div className="transcript">
      <div className="transcript-tools">
        <input
          className="input input-sm"
          placeholder={`在 ${segments.length} 条字幕中搜索…`}
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
      </div>
      <ol className="seg-list">
        {filtered.map((s, i) => (
          <li key={i}>
            <a
              className="seg-time"
              href={watchUrl(url, s.start_sec) || "#"}
              target="_blank"
              rel="noreferrer"
            >
              {fmtTs(s.start_sec)}
            </a>
            <span className="seg-text">{s.text}</span>
          </li>
        ))}
      </ol>
      {filtered.length === 0 && <p className="muted">没有匹配的字幕。</p>}
    </div>
  );
}

export default function VideoDetail({ video, onClose }) {
  const [tab, setTab] = useState("note");
  const [note, setNote] = useState(null);
  const [loading, setLoading] = useState(true);
  const [detail, setDetail] = useState(null); // E1 富详情（含热评）

  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // 笔记是否就绪（done/failed 后才有意义），仅在该状态翻转时补拉一次，
  // 避免依赖整个 video 对象（每 3s 轮询会换新引用）导致笔记反复重载闪烁。
  const noteReady = video
    ? video.status === "done" || video.status === "failed"
    : false;

  useEffect(() => {
    if (!video) return;
    let alive = true;
    setDetail(null);
    getVideo(video.id)
      .then((d) => alive && setDetail(d))
      .catch(() => {});
    return () => {
      alive = false;
    };
    // 关键：依赖稳定的 video.id 与 meta_source（仅元数据采集完成时翻转一次），
    // 不依赖 video 对象本身，否则 3s 轮询每次换新引用都会重新拉取详情。
  }, [video?.id, video?.meta_source]);

  useEffect(() => {
    if (!video) return;
    let alive = true;
    setLoading(true);
    getNote(video.id)
      .then((n) => {
        if (!alive) return;
        if (n && (n.summary || n.markdown || n.chapters || n.key_points)) {
          setNote({ ...n, url: video.url });
        } else {
          setNote(null);
        }
      })
      .catch(() => {
        if (!alive) return;
        setNote(null);
      })
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
    // 关键：依赖 video.id 与 noteReady（status 翻转 done/failed 时触发一次补拉），
    // 不依赖 video 对象，避免 3s 轮询导致笔记每 3s 重载闪烁。
  }, [video?.id, noteReady]);

  if (!video) return null;
  // 合并：以列表里的实时字段（progress/status/title 等，每 3s 轮询会更新）为主，
  // 叠加一次性拉取的富详情（评论等）。这样详情页进度条实时刷新，但笔记不会每 3s 重载闪烁。
  const v = { ...(detail || {}), ...video };
  const dur = fmtDuration(v.duration_sec);
  const views = fmtCount(v.view_count);
  const likes = fmtCount(v.like_count);
  const date = fmtUploadDate(v.upload_date);

  return (
    <div className="modal-backdrop modal-backdrop-sheet" onClick={onClose}>
      <div
        className="modal modal-sheet"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
      >
        <div className="sheet-head">
          <header className="modal-head">
            <div className="modal-title-wrap">
              <PlatformBadge platform={v.platform} />
              <StatusChip status={v.status} />
              <h3 className="modal-title">{v.title || v.url}</h3>
            </div>
            <button className="icon-btn" onClick={onClose} aria-label="关闭">
              ×
            </button>
          </header>

          <div className="modal-sub">
            {v.author && <span>{v.author}</span>}
            {date && <span>{date}</span>}
            {views && <span>{views} 播放</span>}
            {likes && <span>{likes} 赞</span>}
            {dur && <span>时长 {dur}</span>}
            <a href={v.url} target="_blank" rel="noreferrer" className="link">
              打开原视频 ↗
            </a>
          </div>

          <ProgressTrack progress={v.progress} />

          {v.error && <p className="vc-error">{v.error}</p>}

          <nav className="tabs">
            <button
              className={`tab${tab === "note" ? " is-active" : ""}`}
              onClick={() => setTab("note")}
            >
              结构化笔记
            </button>
            <button
              className={`tab${tab === "transcript" ? " is-active" : ""}`}
              onClick={() => setTab("transcript")}
            >
              转写文本
            </button>
          </nav>
        </div>

        <div className="sheet-body">
          {tab === "note" ? (
            <>
              <VideoMeta video={v} />
              {loading ? (
                <p className="muted">正在加载笔记…</p>
              ) : note ? (
                <NoteBody note={note} title={v.title || v.url} />
              ) : (
                <div className="empty-block">
                  <p className="muted">
                    {v.status === "failed"
                      ? "该视频处理失败，尚未生成笔记。修正问题后可重新提交。"
                      : "笔记尚未生成，请等待处理完成。"}
                  </p>
                  {v.status !== "failed" && (
                    <p className="muted small">可切换到「转写文本」查看已完成的字幕。</p>
                  )}
                </div>
              )}
              {/* 热门评论：按需求置于笔记正文之后 */}
              <HotComments video={v} />
            </>
          ) : (
            <TranscriptBody videoId={v.id} url={v.url} />
          )}
        </div>
      </div>
    </div>
  );
}
