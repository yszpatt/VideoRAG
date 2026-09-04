import { useMemo, useState } from "react";
import VideoCard from "../components/VideoCard.jsx";
import ConfirmDialog from "../components/ConfirmDialog.jsx";
import { isActive } from "../lib/format.js";

const FILTERS = [
  { key: "all", label: "全部" },
  { key: "active", label: "处理中" },
  { key: "done", label: "已完成" },
  { key: "failed", label: "失败" },
];

export default function LibraryView({ videos, onOpen, onRefresh, onGoSubmit, onImport, onDelete, refreshing, searchRef }) {
  const [keyword, setKeyword] = useState("");
  const [filter, setFilter] = useState("all");
  const [sort, setSort] = useState("newest");
  const [pending, setPending] = useState(null); // 待二次确认删除的视频

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

  return (
    <div className="view">
      <section className="panel">
        <div className="panel-head">
          <div>
            <h2 className="panel-title">视频收藏</h2>
            <p className="panel-sub">共 {counts.all} 个视频 · {counts.done} 个已入库</p>
          </div>
          <div className="panel-head-actions">
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
                <p>还没有视频</p>
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
