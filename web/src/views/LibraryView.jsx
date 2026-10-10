import { useCallback, useEffect, useMemo, useState } from "react";
import VideoCard from "../components/VideoCard.jsx";
import ConfirmDialog from "../components/ConfirmDialog.jsx";
import { isActive } from "../lib/format.js";
import {
  createCollection,
  deleteCollection,
  getCollections,
  renameCollection,
  setVideoCollections,
} from "../api.js";

const FILTERS = [
  { key: "all", label: "全部" },
  { key: "active", label: "处理中" },
  { key: "done", label: "已完成" },
  { key: "failed", label: "失败" },
];

export default function LibraryView({
  videos,
  onOpen,
  onRefresh,
  onGoSubmit,
  onImport,
  onDelete,
  refreshing,
  searchRef,
  activeCollection = null,
  onSelectCollection,
  notify,
}) {
  const [keyword, setKeyword] = useState("");
  const [filter, setFilter] = useState("all");
  const [sort, setSort] = useState("newest");
  const [pending, setPending] = useState(null); // 待二次确认删除的视频
  const [collections, setCollections] = useState([]);
  const [totalVideos, setTotalVideos] = useState(0);
  const [colError, setColError] = useState(null);
  // 收藏夹栏是否收起（本地记忆；窄屏/宽屏共用同一状态）
  const [sideCollapsed, setSideCollapsed] = useState(() => {
    try {
      return localStorage.getItem("vrag-lib-side") === "collapsed";
    } catch (_) {
      return false;
    }
  });

  const toggleSide = () =>
    setSideCollapsed((prev) => {
      const next = !prev;
      try {
        localStorage.setItem("vrag-lib-side", next ? "collapsed" : "open");
      } catch (_) {
        /* 隐私模式下 localStorage 可能不可写，忽略 */
      }
      return next;
    });

  const loadCollections = useCallback(async () => {
    try {
      const d = await getCollections();
      setCollections(d.collections || []);
      setTotalVideos(d.total_videos ?? 0);
      setColError(null);
      return d.collections || [];
    } catch (e) {
      setColError(`收藏夹加载失败：${e.message}`);
      return [];
    }
  }, []);

  useEffect(() => {
    loadCollections();
  }, [loadCollections]);

  const counts = useMemo(
    () => ({
      all: videos.length,
      active: videos.filter((v) => isActive(v.status)).length,
      done: videos.filter((v) => v.status === "done").length,
      failed: videos.filter((v) => v.status === "failed").length,
    }),
    [videos],
  );

  const list = useMemo(() => {
    const q = keyword.trim().toLowerCase();
    let out = videos.filter((v) => {
      if (filter === "active" && !isActive(v.status)) return false;
      if (filter === "done" && v.status !== "done") return false;
      if (filter === "failed" && v.status !== "failed") return false;
      if (!q) return true;
      return [v.title, v.author, v.url, v.platform]
        .filter(Boolean)
        .some((f) => String(f).toLowerCase().includes(q));
    });
    out = [...out].sort((a, b) => {
      if (sort === "title") return String(a.title || a.url).localeCompare(String(b.title || b.url), "zh");
      return new Date(b.created_at || 0) - new Date(a.created_at || 0);
    });
    return out;
  }, [videos, keyword, filter, sort]);

  const activeName =
    activeCollection == null
      ? "全部视频"
      : collections.find((c) => c.id === activeCollection)?.name || "收藏夹";

  const onCreate = async () => {
    const name = window.prompt("新建收藏夹，输入名称：");
    if (name == null) return;
    try {
      const r = await createCollection(name);
      await loadCollections();
      notify?.(`已创建收藏夹「${r.collection.name}」`, "success");
    } catch (e) {
      notify?.(`创建失败：${e.message}`, "error");
    }
  };

  const onRename = async (coll) => {
    const name = window.prompt(`重命名「${coll.name}」：`, coll.name);
    if (name == null || name.trim() === coll.name) return;
    try {
      await renameCollection(coll.id, name);
      await loadCollections();
    } catch (e) {
      notify?.(`重命名失败：${e.message}`, "error");
    }
  };

  const onDeleteCollection = async (coll) => {
    if (
      !window.confirm(
        `删除收藏夹「${coll.name}」？\n其中的视频不会被删除，仍保留在其它收藏夹（默认收藏夹始终保留）。`,
      )
    ) {
      return;
    }
    try {
      await deleteCollection(coll.id);
      if (activeCollection === coll.id) onSelectCollection?.(null);
      await loadCollections();
      notify?.(`已删除收藏夹「${coll.name}」`, "success");
    } catch (e) {
      notify?.(`删除失败：${e.message}`, "error");
    }
  };

  // 卡片上勾选收藏夹：整表替换语义（默认夹由服务端强制保留），写完刷新计数与列表
  const onAssign = async (videoId, collectionIds) => {
    try {
      await setVideoCollections(videoId, collectionIds);
      await loadCollections();
      await onRefresh?.();
    } catch (e) {
      notify?.(`加入收藏夹失败：${e.message}`, "error");
    }
  };

  return (
    <div className={`view library-view${sideCollapsed ? " is-collapsed" : ""}`}>
      {!sideCollapsed && (
      <aside className="lib-side">
        <div className="lib-side-head">
          <h3 className="lib-side-title">
            收藏夹
            <span className="lib-side-count">{collections.length}</span>
          </h3>
          <div className="lib-side-actions">
            <button className="icon-btn" onClick={onCreate} title="新建收藏夹（可双击条目重命名）" aria-label="新建收藏夹">
              ＋
            </button>
            <button className="icon-btn" onClick={toggleSide} title="收起收藏夹栏" aria-label="收起收藏夹栏">
              ‹
            </button>
          </div>
        </div>

        <ul className="lib-col-list">
          <li className="lib-col-row">
            <button
              className={`lib-col-item${activeCollection == null ? " is-active" : ""}`}
              onClick={() => onSelectCollection?.(null)}
            >
              <span className="lib-col-name">全部视频</span>
              <span className="lib-col-count">{totalVideos}</span>
            </button>
            {/* 与可删除行等宽的占位：让各行数字的右边界对齐 */}
            <span className="lib-col-del is-ghost" aria-hidden="true" />
          </li>
          <li className="lib-col-sep" aria-hidden="true" />
          {collections.map((c) => (
            <li key={c.id} className="lib-col-row">
              <button
                className={`lib-col-item${activeCollection === c.id ? " is-active" : ""}`}
                onClick={() => onSelectCollection?.(c.id)}
                onDoubleClick={() => onRename(c)}
                title={c.is_default ? "默认收藏夹：导入的视频自动进这里" : "双击重命名"}
              >
                <span className="lib-col-name">{c.name}</span>
                {c.is_default && <span className="lib-col-tag">自动</span>}
                <span className="lib-col-count">{c.count}</span>
              </button>
              {c.is_default ? (
                <span className="lib-col-del is-ghost" aria-hidden="true" />
              ) : (
                <button
                  className="lib-col-del"
                  onClick={() => onDeleteCollection(c)}
                  title="删除收藏夹"
                  aria-label="删除收藏夹"
                >
                  ×
                </button>
              )}
            </li>
          ))}
        </ul>

        {colError && <p className="lib-col-err">{colError}</p>}
        <p className="lib-side-foot muted small" title="导入的视频自动进入默认收藏夹；用视频卡片页脚的「收藏夹」按钮加入其它收藏夹">
          导入的视频自动进「默认收藏夹」
        </p>
      </aside>
      )}

      <section className="panel">
        <div className="panel-head">
          <div>
            <h2 className="panel-title">{activeName}</h2>
            <p className="panel-sub">
              共 {counts.all} 个视频 · {counts.done} 个已入库
              {activeCollection != null && ` · 当前收藏夹内 ${counts.all} 个`}
            </p>
          </div>
          <div className="panel-head-actions">
            <button
              type="button"
              className="btn btn-ghost btn-sm lib-toggle"
              onClick={toggleSide}
              title={sideCollapsed ? "展开收藏夹栏" : "收起收藏夹栏"}
              aria-expanded={!sideCollapsed}
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" />
              </svg>
              {sideCollapsed ? "收藏夹" : "收起收藏夹"}
            </button>
            {onImport && (
              <button type="button" className="btn btn-gradient btn-sm" onClick={onImport}>
                ＋ 导入视频
              </button>
            )}
            <button className="btn btn-ghost btn-sm" onClick={onRefresh} disabled={refreshing}>
              {refreshing ? "刷新中…" : "刷新"}
            </button>
          </div>
        </div>

        <div className="library-tools">
          <input
            ref={searchRef}
            className="input"
            type="text"
            placeholder="搜索标题、作者或链接…"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            onKeyDown={(e) => e.key === "Escape" && setKeyword("")}
          />
          <div className="segmented">
            {FILTERS.map((f) => (
              <button
                key={f.key}
                className={`seg-btn${filter === f.key ? " is-active" : ""}`}
                onClick={() => setFilter(f.key)}
              >
                {f.label}
                <span className="seg-count">{counts[f.key]}</span>
              </button>
            ))}
          </div>
          <select className="select" value={sort} onChange={(e) => setSort(e.target.value)}>
            <option value="newest">最新优先</option>
            <option value="title">按标题</option>
          </select>
        </div>

        {list.length === 0 ? (
          <div className="empty">
            {videos.length === 0 ? (
              <>
                <p>{activeCollection == null ? "还没有视频" : "这个收藏夹还是空的"}</p>
                <button className="btn btn-gradient btn-sm" onClick={onGoSubmit}>
                  导入第一个视频
                </button>
              </>
            ) : (
              <p>没有符合条件的视频</p>
            )}
          </div>
        ) : (
          <div className="video-grid">
            {list.map((v) => (
              <VideoCard
                key={v.id}
                video={v}
                onOpen={onOpen}
                onDelete={(vid) => setPending(vid)}
                collections={collections}
                onAssign={onAssign}
              />
            ))}
          </div>
        )}
      </section>

      <ConfirmDialog
        open={!!pending}
        title="删除视频"
        message={
          pending
            ? `确定要删除《${pending.title || pending.url}》吗？此操作不可撤销，将同时删除该视频的笔记、字幕与转写等关联数据。`
            : ""
        }
        confirmText="删除"
        danger
        onCancel={() => setPending(null)}
        onConfirm={async () => {
          const target = pending;
          setPending(null);
          if (target) await onDelete?.(target.id);
        }}
      />
    </div>
  );
}
