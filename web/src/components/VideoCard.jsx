import { useState } from "react";
import ProgressTrack, { PlatformBadge, StatusChip } from "./ProgressTrack.jsx";
import {
  fmtCount,
  fmtDuration,
  fmtRelative,
  fmtUploadDate,
  platformOf,
} from "../lib/format.js";

/** 收藏夹勾选面板：点一下即写入（整表替换语义，默认夹不可取消）。 */
function CollectionPicker({ video, collections, onAssign }) {
  const current = new Set(video.collection_ids || []);
  const [busy, setBusy] = useState(false);

  const toggle = async (coll) => {
    if (coll.is_default || busy) return; // 默认收藏夹：自动归属、不可取消
    const next = new Set(current);
    if (next.has(coll.id)) next.delete(coll.id);
    else next.add(coll.id);
    setBusy(true);
    try {
      await onAssign(video.id, [...next]);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="vc-col-pop" onClick={(e) => e.stopPropagation()}>
      <div className="vc-col-pop-head">
        加入收藏夹
        <span className="muted small">点击即保存</span>
      </div>
      <ul className="vc-col-pop-list">
        {collections.map((c) => {
          const on = current.has(c.id);
          return (
            <li key={c.id}>
              <label className={`vc-col-item${c.is_default ? " is-locked" : ""}`}>
                <input
                  type="checkbox"
                  checked={on}
                  disabled={c.is_default || busy}
                  onChange={() => toggle(c)}
                />
                <span className="vc-col-name">{c.name}</span>
                {c.is_default && <span className="vc-col-tag">自动</span>}
              </label>
            </li>
          );
        })}
        {collections.length === 0 && <li className="muted small">还没有收藏夹</li>}
      </ul>
    </div>
  );
}

function Thumb({ video, onDelete, collections = [], onAssign, onTogglePicker, pickerOpen }) {
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
  const ColBtn = onAssign ? (
    <button
      type="button"
      className={`vc-col-btn${pickerOpen ? " is-active" : ""}`}
      title="加入收藏夹"
      aria-label="加入收藏夹"
      onClick={(e) => {
        e.stopPropagation();
        onTogglePicker();
      }}
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" />
      </svg>
    </button>
  ) : null;
  const Picker = pickerOpen ? (
    <CollectionPicker video={video} collections={collections} onAssign={onAssign} />
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
        {ColBtn}
        {DelBtn}
        {Picker}
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
      {ColBtn}
      {DelBtn}
      {Picker}
    </div>
  );
}

export default function VideoCard({
  video,
  onOpen,
  onDelete,
  selected,
  collections = [],
  onAssign,
}) {
  const [pickerOpen, setPickerOpen] = useState(false);
  const title = video.title || video.url;
  const dur = fmtDuration(video.duration_sec);
  const views = fmtCount(video.view_count);
  const date = fmtUploadDate(video.upload_date);
  const mine = collections.filter((c) => (video.collection_ids || []).includes(c.id));

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
      <Thumb
        video={video}
        onDelete={onDelete}
        collections={collections}
        onAssign={onAssign}
        pickerOpen={pickerOpen}
        onTogglePicker={() => setPickerOpen((v) => !v)}
      />
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

        {mine.length > 0 && (
          <div className="vc-cols" title="所属收藏夹">
            {mine.map((c) => (
              <span className={`vc-col-chip${c.is_default ? " is-default" : ""}`} key={c.id}>
                {c.name}
              </span>
            ))}
          </div>
        )}
      </div>
    </article>
  );
}
