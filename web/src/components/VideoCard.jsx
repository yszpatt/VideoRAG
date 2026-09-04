import { useState } from "react";
import ProgressTrack, { PlatformBadge, StatusChip } from "./ProgressTrack.jsx";
import {
  fmtCount,
  fmtDuration,
  fmtRelative,
  fmtUploadDate,
  platformOf,
} from "../lib/format.js";

function Thumb({ video, onDelete }) {
  const pf = platformOf(video.platform);
  const [failed, setFailed] = useState(false);
  // 有 has_thumbnail 时渲染 <img>，由后端 /thumbnail 端点按需代理下载并缓存；
  // 下载失败时 onError 回落到平台色占位，避免破图。
  // 处理状态浮标覆盖在封面右上角，便于一眼看到进度/失败。
  const DelBtn = onDelete ? (
    <button
      type="button"
      className="vc-del-btn"
      title="删除视频"
      aria-label="删除视频"
      onClick={(e) => {
        e.stopPropagation();
        onDelete(video);
      }}
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m2 0v14a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6m4 5v6m6-6v6" />
      </svg>
    </button>
  ) : null;
  if (video.has_thumbnail && !failed) {
    return (
      <div className="vc-thumb">
        <img
          className="vc-thumb-img"
          src={`/api/videos/${video.id}/thumbnail`}
          alt=""
          loading="lazy"
          onError={() => setFailed(true)}
        />
        <StatusChip status={video.status} />
        {DelBtn}
      </div>
    );
  }
  // 无封面 / 加载失败：平台色 SVG 占位（不请求网络）
  return (
    <div className="vc-thumb">
      <div
        className="vc-thumb-ph"
        style={{ background: `${pf.color}26`, color: pf.color }}
      >
        {(pf.short || "?").slice(0, 1)}
      </div>
      <StatusChip status={video.status} />
      {DelBtn}
    </div>
  );
}

export default function VideoCard({ video, onOpen, onDelete, selected }) {
  const title = video.title || video.url;
  const dur = fmtDuration(video.duration_sec);
  const views = fmtCount(video.view_count);
  const date = fmtUploadDate(video.upload_date);

  return (
    <article
      className={`video-card${selected ? " is-selected" : ""}`}
      onClick={() => onOpen(video.id)}
      onKeyDown={(e) => {
        // 焦点在内层交互元素（如删除按钮）时不触发打开详情
        if (e.target !== e.currentTarget) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpen(video.id);
        }
      }}
      role="button"
      tabIndex={0}
    >
      <Thumb video={video} onDelete={onDelete} />
      <div className="vc-body">
        <header className="vc-head">
          <PlatformBadge platform={video.platform} />
        </header>

        <h3 className="vc-title" title={title}>
          {title}
        </h3>

        <div className="vc-meta">
          {video.author && <span className="vc-author">{video.author}</span>}
          {dur && <span>{dur}</span>}
          {date && <span>{date}</span>}
          {views && <span>{views} 播放</span>}
        </div>

        <ProgressTrack progress={video.progress} barOnly />

        {video.error && <p className="vc-error">{video.error}</p>}

        <footer className="vc-foot">
          <span className="vc-open">查看笔记 →</span>
        </footer>
      </div>
    </article>
  );
}
