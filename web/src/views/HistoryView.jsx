import { useCallback, useEffect, useMemo, useState } from "react";
import { getHistory, listHistory } from "../api.js";
import { fmtRelative } from "../lib/format.js";
import Markdown from "../components/Markdown.jsx";
import CiteCard from "../components/CiteCard.jsx";

/** 历史提问：左侧二级侧边栏列出全部历史问题，右侧展示该问题当时的答案与引用。 */
export default function HistoryView({ refreshKey = 0 }) {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [keyword, setKeyword] = useState("");
  const [activeId, setActiveId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await listHistory("ask", 100);
      // 后端返回 { items: [...] }（容错直接数组形态）
      const rows = Array.isArray(data) ? data : data?.items || [];
      setItems(rows);
      setError("");
    } catch (e) {
      setError(e.message || "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  const list = useMemo(() => {
    const q = keyword.trim().toLowerCase();
    if (!q) return items;
    return items.filter((it) => (it.query || "").toLowerCase().includes(q));
  }, [items, keyword]);

  // 选中某条历史：拉取完整答案（不发新 LLM 请求，回看当时结果）
  useEffect(() => {
    if (!activeId) {
      setDetail(null);
      return undefined;
    }
    let alive = true;
    setDetailLoading(true);
    getHistory(activeId)
      .then((d) => {
        if (alive) setDetail(d);
      })
      .catch(() => {
        if (alive) setDetail(null);
      })
      .finally(() => {
        if (alive) setDetailLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [activeId]);

  // 默认选中第一条
  useEffect(() => {
    if (!activeId && list.length > 0) setActiveId(list[0].id);
    if (activeId && list.length > 0 && !list.some((i) => i.id === activeId)) {
      setActiveId(list[0].id);
    }
    if (list.length === 0) setActiveId(null);
  }, [list, activeId]);

  const active = list.find((i) => i.id === activeId) || null;

  return (
    <div className="hist-page">
      <aside className="hist-side">
        <div className="hist-side-head">
          <div className="hist-side-title">
            <h2>历史提问</h2>
            <span className="hist-side-count">{items.length}</span>
          </div>
          <input
            className="input input-sm"
            type="text"
            placeholder="搜索历史问题…"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
          />
        </div>

        <div className="hist-side-list">
          {loading && <p className="hist-empty">加载中…</p>}
          {!loading && error && <p className="hist-empty is-error">{error}</p>}
          {!loading && !error && list.length === 0 && (
            <p className="hist-empty">{items.length === 0 ? "还没有提问记录" : "没有匹配的问题"}</p>
          )}
          {!loading &&
            list.map((it) => (
              <button
                key={it.id}
                className={`hist-side-item${it.id === activeId ? " is-active" : ""}`}
                onClick={() => setActiveId(it.id)}
                title={it.query}
              >
                <span className="hist-side-query">{it.query}</span>
                <span className="hist-side-meta">
                  <span>{fmtRelative(it.last_used_at)}</span>
                  {it.hit_count > 1 && <span className="hist-side-tag">问过 {it.hit_count} 次</span>}
                </span>
              </button>
            ))}
        </div>
      </aside>

      <section className="hist-main">
        {!active ? (
          <div className="empty">
            <p>选择左侧的历史问题查看当时的答案</p>
          </div>
        ) : (
          <div className="hist-detail">
            <header className="hist-detail-head">
              <h2 className="hist-detail-title" title={active.query}>
                {active.query}
              </h2>
              <div className="hist-detail-meta">
                <span>{fmtRelative(active.last_used_at)}</span>
                {active.hit_count > 1 && <span>问过 {active.hit_count} 次</span>}
                {detail?.citations?.length ? (
                  <span>{detail.citations.length} 条引用</span>
                ) : null}
              </div>
            </header>

            {detailLoading && (
              <div className="loading-block">
                <span className="spinner" />
                正在载入答案…
              </div>
            )}

            {!detailLoading && detail && (
              <>
                <div className="answer-card">
                  <div className="answer-text">
                    <Markdown className="answer-md">{detail.answer}</Markdown>
                  </div>
                </div>

                {detail.citations?.length > 0 && (
                  <div className="cite-list">
                    <h3 className="sub-title">引用来源</h3>
                    {detail.citations.map((c, i) => (
                      <CiteCard index={i} citation={c} key={i} />
                    ))}
                  </div>
                )}
              </>
            )}

            {!detailLoading && !detail && (
              <div className="empty">
                <p>这条记录没有可回看的答案</p>
              </div>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
