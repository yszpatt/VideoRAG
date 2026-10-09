import { useEffect, useRef, useState } from "react";
import {
  deleteCookie,
  getBrowsers,
  getCookies,
  importCookieFromBrowser,
  saveCookie,
} from "../api.js";

// 各平台徽标：必需（未配置就不能提交）/ 可选
function statusBadge(p) {
  if (!p.configured) {
    return { tone: p.required ? "err" : "dim", text: p.required ? "未配置（必需）" : "未配置" };
  }
  if (p.parse_error) return { tone: "err", text: "内容无法解析" };
  if (p.expired) return { tone: "warn", text: "已过期" };
  const miss = (p.key_cookies_missing || []).length;
  if (miss) return { tone: "warn", text: `已配置（缺 ${miss} 项关键 cookie）` };
  return { tone: "ok", text: "已配置" };
}

const EXPORT_STEPS = (
  <>
    <p className="cookie-step-title">
      <b>方式一 · 开发者工具（推荐，免装扩展）</b>
    </p>
    <ol className="cookie-steps">
      <li>在浏览器里登录目标站点（小红书 / 抖音 / B 站 / YouTube）。</li>
      <li>
        按 <kbd>F12</kbd> 打开开发者工具，切到「<b>网络 / Network</b>」标签。
      </li>
      <li>刷新页面或随便点一下，让请求列表里出现发往该站点的请求。</li>
      <li>
        右键其中任意一条请求 →「<b>复制 → 复制为 cURL (bash)</b>」；或点开它，在
        「标头 / Headers → 请求标头」里找到 <code>Cookie:</code> 那一行整行复制。
      </li>
      <li>
        粘贴到下面对应平台的输入框 → <b>保存</b>。两种都带 HttpOnly cookie，
        比在控制台敲 <code>document.cookie</code> 更全（后者拿不到登录态）。
      </li>
    </ol>
    <p className="cookie-step-title">
      <b>方式二 · 从本机浏览器读取</b>
    </p>
    <p className="muted small" style={{ margin: "2px 0 0" }}>
      服务器本机有浏览器时（桌面 / 裸机形态）可一键读取；容器里读不到宿主浏览器。
    </p>
    <p className="cookie-step-title">
      <b>方式三 · 浏览器扩展</b>
    </p>
    <p className="muted small" style={{ margin: "2px 0 0" }}>
      「Get cookies.txt LOCALLY」导出 Netscape 文本，或「Cookie-Editor」导出 JSON。
    </p>
  </>
);

export default function CookiePanel() {
  const [data, setData] = useState(null);
  const [browsers, setBrowsers] = useState([]);
  const [browser, setBrowser] = useState("");
  const [drafts, setDrafts] = useState({});
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState(null);
  const [err, setErr] = useState(null);
  const fileRefs = useRef({});

  const load = async () => {
    try {
      const d = await getCookies();
      setData(d);
      return d;
    } catch (e) {
      setErr(`加载 cookie 状态失败：${e.message}`);
      return null;
    }
  };

  useEffect(() => {
    load();
    // 浏览器可用性单独探测：容器形态通常一个都没有，此时前端给出替代方案提示
    getBrowsers()
      .then((d) => {
        const list = d.browsers || [];
        setBrowsers(list);
        const first = list.find((b) => b.available);
        setBrowser(first ? first.key : list[0]?.key || "chrome");
      })
      .catch(() => setBrowsers([]));
  }, []);

  const anyBrowserAvailable = browsers.some((b) => b.available);

  const patchPlatform = (status) =>
    setData((prev) =>
      prev
        ? {
            ...prev,
            platforms: prev.platforms.map((p) => (p.key === status.key ? status : p)),
          }
        : prev,
    );

  const onSave = async (key) => {
    const content = (drafts[key] || "").trim();
    if (!content) {
      setErr("请先粘贴 cookie 内容");
      return;
    }
    setBusy(key);
    setMsg(null);
    setErr(null);
    try {
      const r = await saveCookie(key, drafts[key]);
      patchPlatform(r.status);
      // 保存后立刻清空输入框：凭据不留在页面上
      setDrafts((prev) => ({ ...prev, [key]: "" }));
      const fmtLabel =
        { header: "Cookie 请求头", curl: "复制为 cURL", netscape: "Netscape", json: "JSON" }[
          r.format
        ] || r.format;
      const dom = r.assumed_domain ? `，域按 ${r.assumed_domain} 合成` : "";
      setMsg(`已保存 ${r.status.label}：${r.count} 条 cookie（来源：${fmtLabel}${dom}）`);
    } catch (e) {
      setErr(`保存失败：${e.message}`);
    } finally {
      setBusy("");
    }
  };

  const onImportBrowser = async (key) => {
    setBusy(key);
    setMsg(null);
    setErr(null);
    try {
      const r = await importCookieFromBrowser(key, browser);
      patchPlatform(r.status);
      setMsg(
        `已从 ${browser} 读取并保存 ${r.status.label}：${r.count} 条 cookie（只保留该平台的域）`,
      );
    } catch (e) {
      setErr(`从浏览器读取失败：${e.message}`);
    } finally {
      setBusy("");
    }
  };

  const onClear = async (platform) => {
    if (!window.confirm(`确定清除 ${platform.label} 的 cookie 文件？清除后该平台链接将无法提交或降级。`)) {
      return;
    }
    setBusy(platform.key);
    setMsg(null);
    setErr(null);
    try {
      const r = await deleteCookie(platform.key);
      patchPlatform(r.status);
      setMsg(r.removed ? `已清除 ${platform.label} cookie` : `${platform.label} 本来就没有 cookie 文件`);
    } catch (e) {
      setErr(`清除失败：${e.message}`);
    } finally {
      setBusy("");
    }
  };

  const onPickFile = async (key, file) => {
    if (!file) return;
    try {
      const text = await file.text();
      setDrafts((prev) => ({ ...prev, [key]: text }));
      setMsg(`已读取文件 ${file.name}（${text.length} 字符），确认无误后点「保存」`);
      setErr(null);
    } catch (e) {
      setErr(`读取文件失败：${e.message}`);
    } finally {
      // 允许重复选择同一个文件
      const input = fileRefs.current[key];
      if (input) input.value = "";
    }
  };

  if (err && !data) {
    return (
      <div className="cookie-panel">
        <div className="alert alert-error">{err}</div>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="loading-block">
        <span className="spinner" />
        加载 cookie 状态…
      </div>
    );
  }

  return (
    <div className="cookie-panel">
      <div className="settings-group">
        <div className="settings-group-head">
          <h3 className="sub-title">登录 cookie</h3>
          <p className="muted small">
            小红书 / 抖音<b>必须</b>配置 cookie（否则提交链接会被直接拒绝）；B 站 / YouTube
            为可选，但登录后才有高清画质、CC 字幕、年龄限制或会员内容。cookie 等同账号登录态，
            只写不读：本页不会回显已保存的内容。
          </p>
        </div>
        <p className="muted small cookie-dir">
          存放位置：<code>{data.cookie_dir}</code>
        </p>
        <details className="cookie-help">
          <summary>怎么导出 cookie（点击展开）</summary>
          {EXPORT_STEPS}
          <p className="muted small">
            注意：YouTube 的 cookie 会被轮换，官方建议在<b>无痕窗口</b>登录后导出并立即关闭窗口，
            否则 cookie 很快失效；另外请控制请求频率，用主账号高频下载有被限制的风险。
          </p>
        </details>
      </div>

      {err && <div className="alert alert-error">{err}</div>}
      {msg && <div className="alert alert-ok">{msg}</div>}

      {data.platforms.map((p) => {
        const badge = statusBadge(p);
        const found = new Set(p.key_cookies_found || []);
        return (
          <div className="settings-group" key={p.key}>
            <div className="settings-group-head lm-card-head">
              <div>
                <div className="lm-title-row">
                  <h3 className="sub-title" style={{ margin: 0 }}>
                    {p.label}
                  </h3>
                  <span className={`lm-badge lm-badge-${badge.tone}`}>{badge.text}</span>
                </div>
                <p className="muted small">{p.note}</p>
              </div>
            </div>

            <div className="settings-fields">
              {p.configured && (
                <div className="cookie-meta">
                  <span className="muted small">
                    {p.count} 条 · {p.domains?.length || 0} 个域
                    {p.saved_at ? ` · 保存于 ${p.saved_at}` : ""}
                  </span>
                  <span className="muted small">
                    关键 cookie：
                    {p.key_cookies.map((name) => (
                      <span
                        key={name}
                        className={`cookie-flag${found.has(name) ? "" : " is-miss"}`}
                        title={found.has(name) ? "已找到" : "未找到（可能登录态不完整）"}
                      >
                        {found.has(name) ? "✓" : "✗"} {name}
                      </span>
                    ))}
                  </span>
                </div>
              )}
              {p.parse_error && (
                <div className="alert alert-error" style={{ padding: "6px 10px", fontSize: 12 }}>
                  文件存在但无法解析：{p.parse_error}（请重新导出覆盖）
                </div>
              )}
              {p.configured && p.expired && (
                <div className="alert alert-error" style={{ padding: "6px 10px", fontSize: 12 }}>
                  关键 cookie 已过期
                  {p.key_cookies_expired?.length ? `（${p.key_cookies_expired.join("、")}）` : ""}
                  ，请重新登录导出。
                </div>
              )}
              {p.configured && !p.expired && p.key_expires_at > 0 && (
                <span className="muted small">
                  关键 cookie 有效期至{" "}
                  {new Date(p.key_expires_at * 1000).toLocaleDateString()}
                </span>
              )}
              {p.configured && p.stale_cookies > 0 && (
                <span className="muted small">
                  另有 {p.stale_cookies} 条无关 cookie 已过期
                  {p.stale_cookie_names?.length ? `（${p.stale_cookie_names.join("、")}）` : ""}
                  ，不影响登录态
                </span>
              )}

              <label className="field">
                <span className="field-label">
                  粘贴 cookie 内容（F12 复制为 cURL / Cookie 请求头 / Netscape 文本 / JSON 都认）
                </span>
                <textarea
                  className="cookie-textarea"
                  value={drafts[p.key] || ""}
                  placeholder={
                    p.key === "xhs"
                      ? "curl 'https://edith.xiaohongshu.com/api/…' -H 'cookie: web_session=…; a1=…'\n（或整行 Cookie: web_session=…; a1=…）"
                      : "curl '…' -H 'cookie: name=value; …'\n（或 # Netscape HTTP Cookie File 文本 / 扩展导出的 JSON）"
                  }
                  spellCheck={false}
                  onChange={(e) => setDrafts((prev) => ({ ...prev, [p.key]: e.target.value }))}
                />
                <span className="muted small">
                  内容不会回显；保存后输入框会被清空。也可以直接选择文件：
                </span>
              </label>

              <div className="cookie-actions">
                <button
                  className="btn btn-primary btn-sm"
                  onClick={() => onSave(p.key)}
                  disabled={busy === p.key || !(drafts[p.key] || "").trim()}
                >
                  {busy === p.key ? "保存中…" : "保存"}
                </button>
                <input
                  ref={(el) => {
                    fileRefs.current[p.key] = el;
                  }}
                  type="file"
                  accept=".txt,.json,text/plain,application/json"
                  className="cookie-file"
                  onChange={(e) => onPickFile(p.key, e.target.files?.[0])}
                />
                <button
                  className="btn btn-danger btn-sm"
                  onClick={() => onClear(p)}
                  disabled={busy === p.key || !p.configured}
                >
                  清除
                </button>
              </div>

              <div className="cookie-browser-row">
                <span className="field-label">或：从服务器本机浏览器直接读取</span>
                <div className="cookie-actions">
                  <select
                    className="input cookie-browser-select"
                    value={browser}
                    onChange={(e) => setBrowser(e.target.value)}
                    disabled={!anyBrowserAvailable}
                  >
                    {browsers.map((b) => (
                      <option key={b.key} value={b.key}>
                        {b.label}
                        {b.available ? "" : "（未检测到）"}
                      </option>
                    ))}
                  </select>
                  <button
                    className="btn btn-ghost btn-sm"
                    onClick={() => onImportBrowser(p.key)}
                    disabled={busy === p.key || !anyBrowserAvailable}
                  >
                    {busy === p.key ? "读取中…" : "从浏览器导入"}
                  </button>
                </div>
                <span className="muted small">
                  {anyBrowserAvailable
                    ? "只读取该平台的 cookie 落盘，不会把整库写入项目；浏览器正在运行通常也能读（Firefox 最稳，Chrome / Edge 新版可能因加密失败）。"
                    : "当前环境检测不到浏览器（容器形态属正常）：请用上面的「复制为 cURL / Cookie 头」，或把宿主浏览器 profile 挂进容器后再试。"}
                </span>
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
