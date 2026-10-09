import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import ProgressTrack, { PlatformBadge, StatusChip } from "./ProgressTrack.jsx";
import {
  fmtCount,
  fmtDuration,
  fmtRelative,
  fmtUploadDate,
  platformOf,
} from "../lib/format.js";

const POP_WIDTH = 214;
const POP_MAX_HEIGHT = 260;

/**
 * 收藏夹勾选弹窗。
 *
 * 用 portal 挂到 document.body + position: fixed：卡片与封面都有 `overflow: hidden`
 * （圆角与封面裁剪需要），挂在卡片内部的弹窗会被裁掉、也会被相邻卡片盖住——这就是
 * 「层级错误被遮挡」的原因。挂到 body 后不受任何祖先裁剪与层叠上下文影响。
 *
 * 位置按触发按钮的 rect 计算：默认贴其下方，放不下则上翻 / 左右收进视口；滚动或
 * 缩放时直接关闭（fixed 定位跟随滚动反而容易飘）。
 */
function CollectionPicker({ anchorRef, video, collections, onAssign, onClose }) {
  const [pos, setPos] = useState(null);
  const current = new Set(video.collection_ids || []);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const place = () => {
      const a = anchorRef.current?.getBoundingClientRect();
      if (!a) return;
      const height = Math.min(POP_MAX_HEIGHT, 74 + collections.length * 30);
      let left = a.left;
      let top = a.bottom + 6;
      if (left + POP_WIDTH > window.innerWidth - 8) {
        left = Math.max(8, window.innerWidth - POP_WIDTH - 8);
      }
      if (top + height > window.innerHeight - 8) {
        top = Math.max(8, a.top - height - 6); // 下方放不下 → 翻到按钮上方
      }
      setPos({
        left,
        top,
        maxHeight: Math.max(120, Math.min(POP_MAX_HEIGHT, window.innerHeight - top - 16)),
      });
    };
    place();
    window.addEventListener("resize", place);
    return () => window.removeEventListener("resize", place);
  }, [anchorRef, collections.length]);

  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && onClose();
    const onScroll = () => onClose();
    window.addEventListener("keydown", onKey);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [onClose]);

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

  return createPortal(
    <>
      {/* 点空白处关闭：垫在弹窗下一层，顺带阻止点穿到卡片 */}
      <div className="vc-col-mask" onClick={onClose} />
      <div
        className="vc-col-pop"
        style={{
          left: pos?.left ?? -9999,
          top: pos?.top ?? -9999,
          width: POP_WIDTH,
          maxHeight: pos?.maxHeight ?? POP_MAX_HEIGHT,
        }}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-label="加入收藏夹"
      >
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
    </>,
    document.body,
  );
}

function Thumb({ video, onDelete }) {
  const pf = platformOf(video.platform);
  const [failed, setFailed] = useState(false);
  // 有 has_thumbnail 时渲染 <img>，由后端 /thumbnail 端点按需代理下载并缓存；
  // 下载失败时 onError 回落到平台色占位，避免破图。
  // 封面上只留两个浮层：删除=左上、状态=右上（收藏夹入口移到卡片页脚，
  // 避免小卡片上「上下两个角」的按钮挤在一起）
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

export default function VideoCard({
  video,
  onOpen,
  onDelete,
  selected,
  collections = [],
  onAssign,
}) {
  const [pickerOpen, setPickerOpen] = useState(false);
  const colBtnRef = useRef(null);
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
      <Thumb video={video} onDelete={onDelete} />
      <div className="vc-body">
        <h3 className="vc-title" title={title}>
          {title}
        </h3>

        {/* 平台徽标并进 meta 行：省掉一整行高度（卡片更紧凑） */}
        <div className="vc-meta">
          <PlatformBadge platform={video.platform} />
          {video.author && <span className="vc-author">{video.author}</span>}
          {dur && <span>{dur}</span>}
          {date && <span>{date}</span>}
          {views && <span>{views} 播放</span>}
        </div>

        <ProgressTrack progress={video.progress} barOnly />

        {video.error && <p className="vc-error">{video.error}</p>}

        <footer className="vc-foot">
          {onAssign && (
            <button
              type="button"
              ref={colBtnRef}
              className={`vc-foot-col${pickerOpen ? " is-open" : ""}`}
              title="加入收藏夹"
              aria-label="加入收藏夹"
              onClick={(e) => {
                e.stopPropagation();
                setPickerOpen((v) => !v);
              }}
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" />
              </svg>
              收藏夹
            </button>
          )}
          <span className="vc-open">查看笔记 →</span>
        </footer>

        {/* 只列「非默认」的收藏夹：默认夹人人都有，列出来只是噪声 */}
        {mine.some((c) => !c.is_default) && (
          <div className="vc-cols" title="所属收藏夹">
            {mine
              .filter((c) => !c.is_default)
              .map((c) => (
                <span className="vc-col-chip" key={c.id}>
                  {c.name}
                </span>
              ))}
          </div>
        )}
      </div>

      {pickerOpen && onAssign && (
        <CollectionPicker
          anchorRef={colBtnRef}
          video={video}
          collections={collections}
          onAssign={onAssign}
          onClose={() => setPickerOpen(false)}
        />
      )}
    </article>
  );
}
