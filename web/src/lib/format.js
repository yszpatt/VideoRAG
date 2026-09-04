export const PLATFORMS = {
  bilibili: { label: "哔哩哔哩", short: "B站", color: "#FB7299" },
  youtube: { label: "YouTube", short: "YT", color: "#FF4E45" },
  douyin: { label: "抖音", short: "抖音", color: "#25F4EE" },
  xiaohongshu: { label: "小红书", short: "小红书", color: "#FF2442" },
  generic: { label: "直链", short: "直链", color: "#8B93A7" },
};

export function platformOf(key) {
  return PLATFORMS[key] || { label: key || "未知", short: key || "?", color: "#8B93A7" };
}

export function fmtTs(sec) {
  const s = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = s % 60;
  const mm = String(m).padStart(h > 0 ? 2 : 2, "0");
  return h > 0
    ? `${h}:${mm}:${String(ss).padStart(2, "0")}`
    : `${mm}:${String(ss).padStart(2, "0")}`;
}

export function fmtDuration(sec) {
  if (!sec || sec <= 0) return null;
  return fmtTs(sec);
}

export function fmtRelative(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const diff = Math.max(0, Date.now() - then) / 1000;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 2592000) return `${Math.floor(diff / 86400)} 天前`;
  return new Date(then).toLocaleDateString("zh-CN");
}

/** yt-dlp 上传日期 "YYYYMMDD" → "2024-01-05"（非法/缺失返回空串） */
export function fmtUploadDate(d) {
  if (!d) return "";
  const s = String(d);
  const m = s.match(/^(\d{4})(\d{2})(\d{2})$/);
  return m ? `${m[1]}-${m[2]}-${m[3]}` : s;
}

/** 中文计数缩写：1.2万 / 3.4亿；未提供返回空串 */
export function fmtCount(n) {
  if (n == null) return "";
  if (n >= 1e8) return `${+(n / 1e8).toFixed(1)}亿`;
  if (n >= 1e4) return `${+(n / 1e4).toFixed(1)}万`;
  return String(n);
}

/** 构造跳回原视频并在指定秒数开始播放的链接 */
export function watchUrl(url, startSec) {
  if (!url) return null;
  const t = Math.floor(startSec || 0);
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}t=${t}`;
}

export function detectPlatformKey(url) {
  const u = (url || "").toLowerCase();
  if (u.includes("bilibili.com") || u.includes("b23.tv")) return "bilibili";
  if (u.includes("youtube.com") || u.includes("youtu.be")) return "youtube";
  if (u.includes("douyin.com")) return "douyin";
  if (u.includes("xiaohongshu.com") || u.includes("xhslink.com")) return "xiaohongshu";
  if (u.startsWith("http")) return "generic";
  return null;
}

export const ACTIVE_STATUSES = [
  "pending",
  "fetching",
  "transcribing",
  "noting",
  "embedding",
];

export const isActive = (status) => ACTIVE_STATUSES.includes(status);
