import { useEffect, useRef, useState } from "react";
import { fmtRelative } from "../lib/format.js";
import Markdown from "../components/Markdown.jsx";
import CiteCard from "../components/CiteCard.jsx";

// 单轮问答：问题气泡 + 回答气泡（含引用）。pending 时只渲染「思考中」。
function Turn({ turn, asking, onAsk, copied, onCopy }) {
  return (
    <div className="ask-turn">
      <div className="ask-bubble is-question">
        <span className="ask-bubble-avatar" aria-hidden="true">你</span>
        <div className="ask-bubble-body">
          <p className="ask-bubble-text">{turn.question || "…"}</p>
        </div>
      </div>

      {asking ? (
        <div className="ask-bubble is-answer">
          <span className="ask-bubble-avatar is-ai" aria-hidden="true">
            <span className="ask-avatar-dot" />
          </span>
          <div className="ask-bubble-body">
            <div className="loading-block">
              <span className="spinner" />
              正在检索并生成答案…
            </div>
          </div>
        </div>
      ) : (
        <div className="ask-bubble is-answer">
          <span className="ask-bubble-avatar is-ai" aria-hidden="true">
            <span className="ask-avatar-dot" />
          </span>
          <div className="ask-bubble-body">
            <div className="answer-card">
              <div className="answer-head">
                <span className="answer-label">回答</span>
                <span className="answer-meta">
                  {turn.citations?.length ? `${turn.citations.length} 条引用` : "无引用"}
                </span>
                <button
                  className="btn btn-ghost btn-sm"
                  onClick={() => onAsk(turn.question)} // 重新提问：走新请求（追加为新一轮）
                >
                  重新提问
                </button>
                <button
                  className="btn btn-ghost btn-sm"
                  onClick={() => onCopy(turn.answer)}
                >
                  {copied ? "已复制" : "复制"}
                </button>
              </div>
              <div className="answer-text">
                <Markdown className="answer-md">{turn.answer}</Markdown>
              </div>
            </div>

            {turn.citations?.length > 0 && (
              <div className="cite-list">
                <h3 className="sub-title">引用来源</h3>
                {turn.citations.map((c, i) => (
                  <CiteCard index={i} citation={c} key={i} />
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export default function AskView({
  question,
  setQuestion,
  onAsk,
  asking,
  thread,
  pending,
  onNewChat,
  hist,
  inputRef,
  onImport,
}) {
  const [copied, setCopied] = useState(false);
  // 线程滚动容器引用：新轮次出现时自动滚到底部，使最新内容进入可视区
  const scrollRef = useRef(null);

  // 历史回看优先（遗留单条记录分支，当前主流程不触发）；否则渲染多轮会话线程。
  const isHistory = !!hist;
  const result = hist;
  const active = isHistory || asking || pending != null || (thread?.length || 0) > 0;

  const copyAnswer = async (text) => {
    try {
      await navigator.clipboard.writeText(text || "");
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch (_) {}
  };

  const submit = () => onAsk(); // 历史回看由 App.onAsk 统一清除

  // 输入框键盘约定：Enter 发送；⌘/Ctrl+Enter（或 Shift+Enter）换行。
  // IME 组合态（如中文选词时的 Enter）一律放行，避免打断输入。
  const onComposerKeyDown = (e) => {
    if (e.key !== "Enter" || e.nativeEvent?.isComposing) return;
    const el = e.currentTarget;
    if (e.metaKey || e.ctrlKey || e.shiftKey) {
      e.preventDefault();
      const start = el.selectionStart ?? question.length;
      const end = el.selectionEnd ?? start;
      setQuestion(`${question.slice(0, start)}\n${question.slice(end)}`);
      // 受控组件：等重渲染后再把光标放到新换行之后
      requestAnimationFrame(() => {
        el.selectionStart = el.selectionEnd = start + 1;
      });
      return;
    }
    e.preventDefault();
    submit();
  };

  // 换行后输入框自动增高（底部 dock 默认仅一行），上限 220px 避免挤占答案区
  useEffect(() => {
    const el = inputRef?.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 220)}px`;
  }, [question, active, inputRef]);

  const turns = thread || [];
  const pendingTurn = pending ? { id: "pending", question: pending.question, answer: "", citations: [] } : null;

  // 新轮次（thread 增长）/ 在途轮次（pending）/ 思考态变化时，自动滚到线程底部，
  // 让最新提问与回答进入可视区，避免连续追问后需手动翻页才能看到新内容。
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [turns.length, pending, asking]);

  return (
    <div className={`ask-page${active ? " is-conversation" : ""}`}>
      <div className="ask-scroll" ref={scrollRef}>
        {!active && (
          <div className="ask-hero">
            <div className="ask-hero-mark" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 3a6 6 0 0 1 6 6v3l2 3H4l2-3V9a6 6 0 0 1 6-6z" />
                <path d="M9.5 19a2.5 2.5 0 0 0 5 0" />
              </svg>
            </div>
            <h1 className="ask-hero-title">向你的视频知识库提问</h1>
            <p className="ask-hero-sub">
              基于全部已入库视频检索作答，答案附时间戳引用，可一键跳回原片
            </p>

            <div className="ask-composer ask-composer-hero">
              <textarea
                ref={inputRef}
                className="input input-area"
                rows={2}
                placeholder="例如：视频里讲 RAG 的检索流程是什么？"
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                onKeyDown={onComposerKeyDown}
              />
              <div className="ask-composer-foot">
                {onImport && (
                  <button type="button" className="btn btn-gradient-ghost" onClick={onImport}>
                    ＋ 导入视频
                  </button>
                )}
                <span className="ask-hint">↵ 发送 · ⌘/Ctrl + ↵ 换行</span>
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={submit}
                  disabled={asking || !question.trim()}
                >
                  {asking ? "思考中…" : "提问"}
                </button>
              </div>
            </div>
          </div>
        )}

        {active && isHistory && (
          <div className="ask-thread">
            <div className="ask-bubble is-question">
              <span className="ask-bubble-avatar" aria-hidden="true">你</span>
              <div className="ask-bubble-body">
                <p className="ask-bubble-text">{hist.query || "…"}</p>
              </div>
            </div>
            {result && (
              <div className="ask-bubble is-answer">
                <span className="ask-bubble-avatar is-ai" aria-hidden="true">
                  <span className="ask-avatar-dot" />
                </span>
                <div className="ask-bubble-body">
                  <div className="answer-card">
                    <div className="answer-head">
                      <span className="answer-label">回答</span>
                      <span className="answer-hist-badge" title={hist.last_used_at}>
                        来自历史记录 · {fmtRelative(hist.last_used_at)}
                      </span>
                      <span className="answer-meta">
                        {result.citations?.length ? `${result.citations.length} 条引用` : "无引用"}
                      </span>
                      <button
                        className="btn btn-ghost btn-sm"
                        onClick={() => onAsk(hist.query)} // 重新提问：走新请求
                      >
                        重新提问
                      </button>
                      <button
                        className="btn btn-ghost btn-sm"
                        onClick={() => copyAnswer(result.answer)}
                      >
                        {copied ? "已复制" : "复制"}
                      </button>
                    </div>
                    <div className="answer-text">
                      <Markdown className="answer-md">{result.answer}</Markdown>
                    </div>
                  </div>

                  {result.citations?.length > 0 && (
                    <div className="cite-list">
                      <h3 className="sub-title">引用来源</h3>
                      {result.citations.map((c, i) => (
                        <CiteCard index={i} citation={c} key={i} />
                      ))}
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>
        )}

        {active && !isHistory && (
          <div className="ask-thread">
            <div className="ask-thread-head">
              <span className="ask-thread-count">
                对话 · {turns.length + (pendingTurn ? 1 : 0)} 轮
              </span>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={onNewChat}
                disabled={asking && turns.length === 0}
              >
                新对话
              </button>
            </div>

            {turns.map((t) => (
              <Turn
                key={t.id}
                turn={t}
                onAsk={onAsk}
                copied={copied}
                onCopy={copyAnswer}
              />
            ))}

            {pendingTurn && (
              <Turn turn={pendingTurn} asking onAsk={onAsk} copied={copied} onCopy={copyAnswer} />
            )}
          </div>
        )}
      </div>

      {active && !isHistory && (
        <div className="ask-dock">
          <div className="ask-dock-inner">
            <div className="ask-composer ask-composer-dock">
              <textarea
                ref={inputRef}
                className="input input-area"
                rows={1}
                placeholder="继续追问…（↵ 发送 · ⌘/Ctrl + ↵ 换行）"
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                onKeyDown={onComposerKeyDown}
              />
              <div className="ask-composer-foot">
                {onImport && (
                  <button type="button" className="btn btn-gradient-ghost" onClick={onImport}>
                    ＋ 导入视频
                  </button>
                )}
                <span className="ask-hint">↵ 发送 · ⌘/Ctrl + ↵ 换行</span>
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={submit}
                  disabled={asking || !question.trim()}
                >
                  {asking ? "思考中…" : "提问"}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
