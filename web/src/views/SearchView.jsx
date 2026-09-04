import { useState } from "react";
import { fmtTs, platformOf, watchUrl } from "../lib/format.js";

export default function SearchView({ query, setQuery, onSearch, searching, hits, topK, setTopK, inputRef }) {
  return (
    <div className="view">
      <section className="panel">
        <div className="panel-head">
          <div>
            <h2 className="panel-title">混合检索</h2>
            <p className="panel-sub">向量检索 + 全文检索融合排序，直接定位到片段</p>
          </div>
        </div>

        <div className="ask-row">
          <input
            ref={inputRef}
            className="input"
            type="text"
            placeholder="关键词或问题，如：向量数据库 选型"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              // 仅普通 Enter 提交；⌘/Ctrl+Enter 交由全局快捷键处理，避免重复触发
              if (e.key === "Enter" && !e.metaKey && !e.ctrlKey) {
                e.preventDefault();
                onSearch();
              }
            }}
          />
          <div className="topk">
            <label htmlFor="topk">返回</label>
            <select id="topk" className="select" value={topK} onChange={(e) => setTopK(Number(e.target.value))}>
              {[5, 8, 15, 30].map((n) => (
                <option key={n} value={n}>
                  {n} 条
                </option>
              ))}
            </select>
          </div>
          <button
            className="btn btn-primary"
            onClick={() => onSearch()}
            disabled={searching || !query.trim()}
          >
            {searching ? "检索中…" : "检索"}
          </button>
        </div>

        {searching && (
          <div className="loading-block">
            <span className="spinner" />
            正在检索…
          </div>
        )}

        {!searching && hits.length > 0 && (
          <div className="hit-list">
            {hits.map((h, i) => {
              const isMeta = h.kind === "meta";
              const href = isMeta ? h.url || null : watchUrl(h.url, h.start_sec);
              const p = platformOf(h.platform);
              return (
                <a
                  className="hit-card"
                  key={h.chunk_id || i}
                  href={href || "#"}
                  target="_blank"
                  rel="noreferrer"
                  onClick={(e) => !href && e.preventDefault()}
                >
                  <div className="hit-head">
                    <span className="hit-rank">#{i + 1}</span>
                    <span className="platform-dot" style={{ "--p-color": p.color }} />
                    <span className="hit-title">《{h.title || "未知视频"}》</span>
                    <span className={`hit-time${isMeta ? " is-meta" : ""}`}>
                      {isMeta ? "视频简介" : `${fmtTs(h.start_sec)} – ${fmtTs(h.end_sec)}`}
                    </span>
                    <span className="hit-score" title="融合评分">
                      <span className="score-bar">
                        <span
                          className="score-fill"
                          style={{ width: `${Math.max(4, Math.min(100, (h.score || 0) * 100))}%` }}
                        />
                      </span>
                      {(h.score ?? 0).toFixed(3)}
                    </span>
                  </div>
                  <p className="hit-text">{h.content}</p>
                </a>
              );
            })}
          </div>
        )}

        {!searching && hits.length === 0 && query && (
          <p className="muted">没有命中片段，换个说法或先提交视频。</p>
        )}
      </section>
    </div>
  );
}
