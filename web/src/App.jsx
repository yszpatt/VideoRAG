import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ask, listVideos, searchKnowledge, submitVideo, deleteVideo } from "./api.js";
import { isActive } from "./lib/format.js";
import { ToastProvider, useToast } from "./hooks/useToast.jsx";
import { go, openVideo, parseHash, useHash } from "./hooks/useHash.js";
import VideoDetail from "./components/VideoDetail.jsx";
import ProgressTrack, { StatusChip } from "./components/ProgressTrack.jsx";
import AskView from "./views/AskView.jsx";
import SearchView from "./views/SearchView.jsx";
import HistoryView from "./views/HistoryView.jsx";
import LibraryView from "./views/LibraryView.jsx";
import SettingsView from "./views/SettingsView.jsx";
import ImportVideoDialog from "./components/ImportVideoDialog.jsx";

// 主页 = 提问；导入视频改为弹窗（主页 / 视频收藏均可唤起）
const NAV = [
  { key: "ask", label: "提问", hint: "向知识库提问" },
  { key: "search", label: "检索", hint: "定位具体片段" },
  { key: "history", label: "历史提问", hint: "回看往期问答" },
  { key: "library", label: "视频收藏", hint: "管理与查看笔记" },
  { key: "settings", label: "设置", hint: "模型服务在线配置" },
];

/** 侧边栏图标：统一线性描边 SVG（现代渐变风格） */
const NAV_ICONS = {
  ask: (
    <path d="M21 11.5a8.5 8.5 0 0 1-8.5 8.5 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7A8.4 8.4 0 0 1 4 11.5 8.5 8.5 0 0 1 12.5 3 8.5 8.5 0 0 1 21 11.5z" />
  ),
  search: (
    <>
      <circle cx="11" cy="11" r="7" />
      <path d="M20 20l-4.5-4.5" />
    </>
  ),
  history: (
    <>
      <path d="M3 12a9 9 0 1 0 3-6.7" />
      <path d="M3 4v5h5" />
      <path d="M12 8v4l3 2" />
    </>
  ),
  library: <path d="M6 4h12v17l-6-4-6 4V4z" />,
  settings: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .34 1.83l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.83-.34 1.7 1.7 0 0 0-1 1.55V21a2 2 0 1 1-4 0v-.09a1.7 1.7 0 0 0-1.11-1.55 1.7 1.7 0 0 0-1.83.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-1.55-1H3a2 2 0 1 1 0-4h.09A1.7 1.7 0 0 0 4.6 9a1.7 1.7 0 0 0-.34-1.83l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-1.55V3a2 2 0 1 1 4 0v.09a1.7 1.7 0 0 0 1 1.55 1.7 1.7 0 0 0 1.83-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.7 1.7 0 0 0 19.4 9v.09a1.7 1.7 0 0 0 1.55 1H21a2 2 0 1 1 0 4h-.09a1.7 1.7 0 0 0-1.51 1z" />
    </>
  ),
};

function NavIcon({ k }) {
  return (
    <svg
      className="nav-icon"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {NAV_ICONS[k]}
    </svg>
  );
}

const SHORTCUTS = [
  { keys: ["Ctrl/⌘", "K"], desc: "聚焦提问框" },
  { keys: ["↵"], desc: "发送提问 / 执行检索" },
  { keys: ["Ctrl/⌘", "↵"], desc: "输入框内换行" },
  { keys: ["Alt", "1–5"], desc: "切换工作区" },
  { keys: ["Esc"], desc: "关闭弹层 / 清空输入" },
  { keys: ["?"], desc: "显示快捷键" },
];

const THEME_META = { dark: "暗色", light: "亮色", system: "跟随系统" };

function useTheme() {
  const [theme, setThemeState] = useState(
    () => localStorage.getItem("vrag-theme") || "system",
  );
  useEffect(() => {
    const effective =
      theme === "system"
        ? window.matchMedia("(prefers-color-scheme: light)").matches
          ? "light"
          : "dark"
        : theme;
    document.documentElement.dataset.theme = effective;
    localStorage.setItem("vrag-theme", theme);
    const m = document.querySelector('meta[name="theme-color"]');
    if (m) m.content = effective === "light" ? "#f6f7fb" : "#0e1014";
  }, [theme]);
  const cycle = useCallback(() => {
    setThemeState((t) => (t === "dark" ? "light" : t === "light" ? "system" : "dark"));
  }, []);
  return { theme, cycle };
}

function Sidebar({ view, onNav, counts, themeLabel, onCycleTheme }) {
  return (
    <aside className="sidebar">
      <div className="brand">
        <span className="brand-mark">vR</span>
        <div className="brand-text">
          <strong>videoRAG</strong>
          <span>视频 → 笔记 → 知识库</span>
        </div>
      </div>

      <nav className="nav">
        {NAV.map((item) => (
          <button
            key={item.key}
            className={`nav-item${view === item.key ? " is-active" : ""}`}
            onClick={() => onNav(item.key)}
            title={item.hint}
          >
            <NavIcon k={item.key} />
            <span className="nav-label">{item.label}</span>
            {item.key === "library" &&
              (counts.active > 0 ? (
                <span className="nav-count is-busy" title={`${counts.active} 个处理中`}>
                  {counts.active}
                </span>
              ) : counts.all > 0 ? (
                <span className="nav-count" title={`共 ${counts.all} 个视频`}>
                  {counts.all}
                </span>
              ) : null)}
          </button>
        ))}
      </nav>

      <div className="sidebar-foot">
        <div className="stat-row">
          <span>已入库</span>
          <strong>{counts.done}</strong>
        </div>
        <div className="stat-row">
          <span>处理中</span>
          <strong>{counts.active}</strong>
        </div>
        <button
          className="theme-toggle"
          onClick={onCycleTheme}
          title="切换主题（暗色 / 亮色 / 跟随系统）"
        >
          主题 {THEME_META[themeLabel] || themeLabel}
        </button>
        <button className="shortcut-hint" onClick={() => window.dispatchEvent(new Event("vrag-help"))}>
          快捷键 <kbd>?</kbd>
        </button>
      </div>
    </aside>
  );
}

/** E4 移动端底部 Tab Bar（<768px 显示，复用 NAV） */
function MobileTabBar({ view, onNav }) {
  return (
    <nav className="mobile-tabbar" aria-label="主导航">
      {NAV.map((item) => (
        <button
          key={item.key}
          className={`tabbar-item${view === item.key ? " is-active" : ""}`}
          onClick={() => onNav(item.key)}
          aria-label={item.hint}
        >
          <span className="tabbar-icon">
            <NavIcon k={item.key} />
          </span>
          <span className="tabbar-label">{item.label}</span>
        </button>
      ))}
    </nav>
  );
}

function HelpDialog({ onClose }) {
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal modal-sm" onClick={(e) => e.stopPropagation()}>
        <header className="modal-head">
          <h3 className="modal-title">快捷键</h3>
          <button className="icon-btn" onClick={onClose} aria-label="关闭">
            ×
          </button>
        </header>
        <div className="modal-body">
          <ul className="shortcut-list">
            {SHORTCUTS.map((s) => (
              <li key={s.desc}>
                <span className="kbd-group">
                  {s.keys.map((k) => (
                    <kbd key={k}>{k}</kbd>
                  ))}
                </span>
                <span>{s.desc}</span>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}

function Shell() {
  const { push } = useToast();
  const { theme, cycle } = useTheme();

  // E4：hash 为唯一事实源；route.videoId 打开详情时保留最近主视图
  const hash = useHash();
  const route = parseHash(hash);
  const lastView = useRef(route.view || "ask");
  if (route.view) lastView.current = route.view; // 保持与 URL 同步
  const mainView = route.videoId ? lastView.current : route.view;
  const activeId = route.videoId || null;

  const [videos, setVideos] = useState([]);
  const [refreshing, setRefreshing] = useState(false);
  const [online, setOnline] = useState(true);

  const [showImport, setShowImport] = useState(false);
  const [importing, setImporting] = useState(false);
  const [question, setQuestion] = useState("");
  const [asking, setAsking] = useState(false);
  // 多轮会话线程：每轮 { id, question, answer, citations }；pending 为在途轮次的问题文本。
  // 与旧版「单值 qa 整体覆盖」不同——连续追问时前一轮不再被丢弃，全部累积可回看。
  const [thread, setThread] = useState([]);
  const [pending, setPending] = useState(null);
  const [histSeq, setHistSeq] = useState(0); // 每次提问成功后自增，用于刷新「历史提问」页
  const [query, setQuery] = useState("");
  const [topK, setTopK] = useState(8);
  const [searching, setSearching] = useState(false);
  const [hits, setHits] = useState([]);

  const [showHelp, setShowHelp] = useState(false);

  const askRef = useRef(null);
  const searchRef = useRef(null);
  const librarySearchRef = useRef(null);

  const refreshVideos = useCallback(
    async (silent = false) => {
      if (!silent) setRefreshing(true);
      try {
        const data = await listVideos();
        setVideos(data);
        setOnline(true);
      } catch (e) {
        setOnline(false);
        if (!silent) push(`刷新失败：${e.message}`, "error");
      } finally {
        if (!silent) setRefreshing(false);
      }
    },
    [push],
  );

  useEffect(() => {
    refreshVideos(true);
  }, [refreshVideos]);

  const hasActive = videos.some((v) => isActive(v.status));
  useEffect(() => {
    if (!hasActive) return undefined;
    const timer = setInterval(() => refreshVideos(true), 3000);
    return () => clearInterval(timer);
  }, [hasActive, refreshVideos]);

  const counts = useMemo(
    () => ({
      all: videos.length,
      active: videos.filter((v) => isActive(v.status)).length,
      done: videos.filter((v) => v.status === "done").length,
      failed: videos.filter((v) => v.status === "failed").length,
    }),
    [videos],
  );

  // 导入视频：弹窗输入链接 → 确认后面板关闭，进度在「视频收藏」页与侧边栏显示
  const onImportVideo = useCallback(
    async (url) => {
      setImporting(true);
      try {
        await submitVideo(url);
        push("已提交，开始处理", "success");
        setShowImport(false);
        await refreshVideos(false);
        go("library"); // 跳到视频收藏看处理进度
      } catch (e) {
        push(`提交失败：${e.message}`, "error");
      } finally {
        setImporting(false);
      }
    },
    [push, refreshVideos],
  );

  const openImport = useCallback(() => setShowImport(true), []);

  const onAsk = useCallback(
    async (q) => {
      const text = (q ?? question).trim();
      if (!text) return;
      setAsking(true);
      setPending({ question: text }); // 先占位，渲染在途「思考中」气泡
      try {
        const res = await ask(text, 8);
        // 追加一轮，而非整体覆盖——连续追问时前一轮问答保留可回看
        setThread((t) => [
          ...t,
          { id: `${Date.now()}-${t.length}`, question: text, answer: res.answer, citations: res.citations || [] },
        ]);
        setQuestion(""); // 清空输入框，准备下一次追问
        setHistSeq((n) => n + 1); // 新提问入历史，历史提问页需刷新
      } catch (e) {
        push(`提问失败：${e.message}`, "error");
      } finally {
        setPending(null);
        setAsking(false);
      }
    },
    [question, push],
  );

  // 新对话：清空整条线程与在途轮次、输入框；回到空态 hero
  const onNewChat = useCallback(() => {
    setThread([]);
    setPending(null);
    setQuestion("");
  }, []);

  const onSearch = useCallback(
    async (q, k) => {
      const text = (q ?? query).trim();
      if (!text) return;
      const kk = k ?? topK;
      setSearching(true);
      try {
        setHits((await searchKnowledge(text, kk)).hits || []);
      } catch (e) {
        push(`检索失败：${e.message}`, "error");
      } finally {
        setSearching(false);
      }
    },
    [query, topK, push],
  );

  // 打开视频详情：记录当前视图，hash 切到 #/v/{id}（浏览器返回键可回原视图）
  const onOpenVideo = useCallback((id) => {
    openVideo(id);
  }, []);
  const closeVideo = useCallback(() => {
    go(lastView.current || "ask");
  }, []);

  // 删除视频：调后端（连带笔记/字幕/向量等），成功后刷新列表；
  // 若正打开被删视频则先关闭详情。
  const onDeleteVideo = useCallback(
    async (id) => {
      try {
        await deleteVideo(id);
        push("视频已删除", "success");
        if (activeId === id) closeVideo();
        await refreshVideos(false);
      } catch (e) {
        push(`删除失败：${e.message}`, "error");
      }
    },
    [activeId, closeVideo, push, refreshVideos],
  );

  const focusAsk = useCallback(() => {
    go("ask");
    setTimeout(() => askRef.current?.focus(), 60);
  }, []);

  const clearInputs = useCallback(() => {
    const tag = document.activeElement?.tagName;
    if (tag === "TEXTAREA" || tag === "SELECT") return;
    if (mainView === "ask") setQuestion("");
    if (mainView === "search") setQuery("");
  }, [mainView]);

  useEffect(() => {
    const onHelp = () => setShowHelp(true);
    window.addEventListener("vrag-help", onHelp);
    return () => window.removeEventListener("vrag-help", onHelp);
  }, []);

  useEffect(() => {
    const onKey = (e) => {
      const mod = e.metaKey || e.ctrlKey;
      if (mod && e.key.toLowerCase() === "k") {
        e.preventDefault();
        focusAsk();
        return;
      }
      if (mod && e.key === "Enter") {
        // 焦点在输入框内时交给组件自己处理（Enter 发送 / ⌘Ctrl+Enter 换行），
        // 否则会同时触发「换行 + 提交」。非输入框场景仍保留快捷提交。
        const tag = document.activeElement?.tagName;
        if (tag === "TEXTAREA" || tag === "INPUT") return;
        e.preventDefault();
        if (mainView === "ask") onAsk();
        else if (mainView === "search") onSearch();
        return;
      }
      if (e.altKey && ["1", "2", "3", "4", "5"].includes(e.key)) {
        e.preventDefault();
        go(NAV[Number(e.key) - 1].key);
        return;
      }
      if (e.key === "Escape") {
        if (activeId) {
          closeVideo(); // 详情弹窗：返回原视图
          return;
        }
        if (showHelp) setShowHelp(false);
        else clearInputs();
        return;
      }
      const tag = document.activeElement?.tagName;
      const typing = tag === "INPUT" || tag === "TEXTAREA";
      if (e.key === "?" && !typing) {
        e.preventDefault();
        setShowHelp(true);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [mainView, activeId, onAsk, onSearch, focusAsk, clearInputs, showHelp, closeVideo]);

  const activeVideo = activeId ? videos.find((v) => v.id === activeId) || null : null;
  const current = NAV.find((n) => n.key === mainView) || NAV[0];

  // 提问页 / 历史提问页：整页填充（内部各自滚动，底部输入栏常驻）
  const fillView = mainView === "ask" || mainView === "history";

  return (
    <div className="app-shell">
      <Sidebar
        view={mainView}
        onNav={go}
        counts={counts}
        themeLabel={theme}
        onCycleTheme={cycle}
      />

      <main className="workspace">
        <header className="topbar">
          <div>
            <h1 className="topbar-title">{current.label}</h1>
            <p className="topbar-sub">{current.hint}</p>
          </div>
          <div className="topbar-right">
            <button
              type="button"
              className="btn btn-gradient btn-sm"
              onClick={openImport}
              title="粘贴链接导入视频"
            >
              ＋ 导入视频
            </button>
            <button
              className="theme-toggle theme-toggle-inline"
              onClick={cycle}
              title="切换主题（暗色 / 亮色 / 跟随系统）"
            >
              主题 {THEME_META[theme] || theme}
            </button>
            <span className={`conn-dot${online ? "" : " is-off"}`} title={online ? "服务正常" : "连接异常"} />
            <span className="conn-text">{online ? "服务正常" : "连接异常"}</span>
            <button
              className="btn btn-ghost btn-sm"
              onClick={() => window.dispatchEvent(new Event("vrag-help"))}
            >
              快捷键
            </button>
          </div>
        </header>

        <div className={`content${fillView ? " is-fill" : ""}`}>
          <div className="content-main">
            {mainView === "ask" && (
              <AskView
                question={question}
                setQuestion={setQuestion}
                onAsk={onAsk}
                asking={asking}
                thread={thread}
                pending={pending}
                onNewChat={onNewChat}
                inputRef={askRef}
                onImport={openImport}
              />
            )}
            {mainView === "search" && (
              <SearchView
                query={query}
                setQuery={setQuery}
                onSearch={onSearch}
                searching={searching}
                hits={hits}
                topK={topK}
                setTopK={setTopK}
                inputRef={searchRef}
              />
            )}
            {mainView === "history" && <HistoryView refreshKey={histSeq} />}
            {mainView === "library" && (
              <LibraryView
                videos={videos}
                onOpen={onOpenVideo}
                onRefresh={() => refreshVideos(false)}
                onGoSubmit={openImport}
                onImport={openImport}
                onDelete={onDeleteVideo}
                refreshing={refreshing}
                searchRef={librarySearchRef}
              />
            )}
            {mainView === "settings" && <SettingsView />}
          </div>
        </div>

        <MobileTabBar view={mainView} onNav={go} />
      </main>

      {activeVideo && (
        <VideoDetail video={activeVideo} onClose={closeVideo} />
      )}
      {showHelp && <HelpDialog onClose={() => setShowHelp(false)} />}
      <ImportVideoDialog
        open={showImport}
        busy={importing}
        onSubmit={onImportVideo}
        onClose={() => setShowImport(false)}
      />
    </div>
  );
}

export default function App() {
  return (
    <ToastProvider>
      <Shell />
    </ToastProvider>
  );
}
