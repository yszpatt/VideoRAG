import { useEffect, useState } from "react";

/**
 * E4 hash 路由：以 location.hash 为唯一事实源。
 *
 * 路由表：
 *   #/ask | #/search | #/history | #/library | #/settings → 对应工作区
 *   #/v/{videoId}                                          → 详情弹窗
 *   空 / 未知                                              → ask（回落，主页即提问）
 *
 * 注：原 #/submit 已移除，导入视频改为弹窗（主页/视频收藏均可唤起）。
 * 旧链接会走「未知 → ask」回落到主页，不会白屏。
 */

export const VIEW_KEYS = ["ask", "search", "history", "library", "settings"];

export function parseHash(hash = window.location.hash) {
  const segs = (hash || "").replace(/^#\/?/, "").split("/");
  if (segs[0] === "v" && segs[1]) return { view: null, videoId: segs[1] };
  if (VIEW_KEYS.includes(segs[0])) return { view: segs[0], videoId: null };
  return { view: "ask", videoId: null }; // 主页 = 提问
}

/** 订阅 hashchange，返回当前 hash（字符串） */
export function useHash() {
  const [hash, setHash] = useState(() => window.location.hash);
  useEffect(() => {
    const on = () => setHash(window.location.hash);
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return hash;
}

/** 导航到某个工作区视图（#/key），重复导航幂等 */
export function go(viewKey) {
  const target = `#/${viewKey}`;
  if (window.location.hash === target) return;
  window.location.hash = target;
}

/** 打开视频详情（#/v/{id}），返回按钮可回到原视图 */
export function openVideo(id) {
  window.location.hash = `#/v/${id}`;
}
