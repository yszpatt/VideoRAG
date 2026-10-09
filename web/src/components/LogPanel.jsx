import { useCallback, useEffect, useRef, useState } from "react";
import { clearLogs, getLogs } from "../api.js";

const LEVELS = [
  { value: "", label: "全部级别" },
  { value: "DEBUG", label: "DEBUG 及以上" },
  { value: "INFO", label: "INFO 及以上" },
  { value: "WARNING", label: "WARNING 及以上" },
  { value: "ERROR", label: "ERROR 及以上" },
];

const LEVEL_CLASS = {
  DEBUG: "is-debug",
  INFO: "is-info",
  WARNING: "is-warn",
  ERROR: "is-err",
  CRITICAL: "is-err",
};

/**
 * 设置页「日志」：读服务进程内环形缓冲（/api/logs），支持级别/关键词过滤、自动刷新、复制。
 * 日志可能含标题与 URL，复制出来贴到 issue 前请自行检查。
 */
export default function LogPanel() {
  const [entries, setEntries] = useState([]);
  const [meta, setMeta] = useState({ capacity: 0, buffered: 0, matched: 0 });
  const [level, setLevel] = useState("");
  const [q, setQ] = useState("");
  const [auto, setAuto] = useState(true);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(null);
  const [msg, setMsg] = useState(null);
  const [stick, setStick] = useState(true);
  const boxRef = useRef(null);

  const load = useCallback(
    async (opts = {}) => {
      try {
        const d = await getLogs({ limit: 300, level, q: q.trim() });
        setEntries(d.entries || []);
        setMeta({ capacity: d.capacity, buffered: d.buffered, matched: d.matched });
        setErr(null);
        if (opts.scroll !== false) setStick(true);
      } catch (e) {
        setErr(`读取日志失败：${e.message}`);
      } finally {
        setLoading(false);
      }
    },
    [level, q],
  );

  useEffect(() => {
    load();
  }, [load]);

  // 自动刷新：2s 一次（与视频列表轮询同节奏）；关掉后可手动刷新
  useEffect(() => {
    if (!auto) return undefined;
    const t = setInterval(() => load({ scroll: false }), 2000);
    return () => clearInterval(t);
  }, [auto, load]);

  // 新日志到达时贴底（用户手动往上滚了就不要再拽回去）
  useEffect(() => {
    const box = boxRef.current;
    if (!box || !stick) return;
    box.scrollTop = box.scrollHeight;
  }, [entries, stick]);

  const onScroll = () => {
    const box = boxRef.current;
    if (!box) return;
    setStick(box.scrollHeight - box.scrollTop - box.clientHeight < 40);
  };

  const copyAll = async () => {
    const text = entries
      .map((e) => `${e.date} ${e.ts} ${e.level.padEnd(8)} ${e.logger} — ${e.msg}`)
      .join("\n");
    try {
      await navigator.clipboard.writeText(text);
      setMsg(`已复制 ${entries.length} 条日志到剪贴板`);
      setErr(null);
    } catch (_) {
      setErr("复制失败，请检查浏览器权限");
    }
  };

  const onClear = async () => {
    if (!window.confirm("清空服务端日志缓冲？已写入容器 stdout 的日志不受影响（docker logs 仍可查）。")) {
      return;
    }
    try {
      const r = await clearLogs();
      setMsg(`已清空 ${r.cleared} 条`);
      setErr(null);
      await load();
    } catch (e) {
      setErr(`清空失败：${e.message}`);
    }
  };

  return (
    <div className="log-panel">
      <div className="settings-group">
        <div className="settings-group-head">
          <h3 className="sub-title">运行日志</h3>
          <p className="muted small">
            显示服务进程最近 {meta.capacity || 1000} 条应用日志（内存环形缓冲，重启即清空）。
            含抓取 / 转写 / 队列 / 模型下载等 logger 记录，用于定位「提交失败 / 下载失败 / 处理卡住」；
            前端轮询产生的访问日志（GET /api/videos 等）与 httpx 逐条请求不在这里，仍在
            <code>docker logs</code> 里。
          </p>
        </div>

        <div className="log-tools">
          <select className="input input-sm log-level" value={level} onChange={(e) => setLevel(e.target.value)}>
            {LEVELS.map((l) => (
              <option key={l.value} value={l.value}>
                {l.label}
              </option>
            ))}
          </select>
          <input
            className="input input-sm log-search"
            type="text"
            placeholder="关键词过滤（内容 / logger 名）"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          <button className="btn btn-ghost btn-sm" onClick={() => load()}>
            刷新
          </button>
          <label className="log-auto">
            <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
            自动刷新
          </label>
          <button className="btn btn-ghost btn-sm" onClick={copyAll} disabled={!entries.length}>
            复制
          </button>
          <button className="btn btn-danger btn-sm" onClick={onClear} disabled={!meta.buffered}>
            清空
          </button>
          <span className="muted small">
            显示 {entries.length} / 匹配 {meta.matched} / 缓冲 {meta.buffered}
          </span>
        </div>

        {err && <div className="alert alert-error">{err}</div>}
        {msg && <div className="alert alert-ok">{msg}</div>}

        <div className="log-box" ref={boxRef} onScroll={onScroll}>
          {loading && <p className="muted small">加载中…</p>}
          {!loading && entries.length === 0 && (
            <p className="muted small">没有符合条件的日志。</p>
          )}
          {entries.map((e) => (
            <div className={`log-line ${LEVEL_CLASS[e.level] || ""}`} key={e.seq}>
              <span className="log-ts" title={e.date}>
                {e.ts}
              </span>
              <span className="log-level-tag">{e.level}</span>
              <span className="log-logger">{e.logger}</span>
              <span className="log-msg">{e.msg}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
