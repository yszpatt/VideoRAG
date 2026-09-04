import { useState } from "react";
import { fmtTs, watchUrl } from "../lib/format.js";

/**
 * 引用来源卡片：默认折叠，仅显示头部（序号/时间/视频标题/展开按钮）；
 * 点击展开后渲染原文字段，并提供「跳转 ↗」到原片。
 *
 * 样式目标：弱化引用区，背景默认透明，展开后仅以一条淡紫色细线 + 极淡底色承载原文。
 */
export default function CiteCard({ index, citation }) {
  const [open, setOpen] = useState(false);
  // kind=meta：视频级简介分片，无时间戳；跳转不带定位秒数
  const isMeta = citation?.kind === "meta";
  const href = isMeta ? citation.url || null : watchUrl(citation.url, citation.start_sec);

  return (
    <div className={`cite-card${open ? " is-open" : ""}`}>
      <button
        type="button"
        className="cite-head"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className="cite-idx">[{index + 1}]</span>
        <span className={`cite-time${isMeta ? " is-meta" : ""}`}>
          {isMeta ? "视频简介" : `${fmtTs(citation.start_sec)} – ${fmtTs(citation.end_sec)}`}
        </span>
        <span className="cite-title" title={citation.title || "未知视频"}>
          《{citation.title || "未知视频"}》
        </span>
        <span className="cite-jump">{open ? "收起" : "展开全文"}</span>
      </button>
      {open &&
        (href ? (
          <a className="cite-text-wrap" href={href} target="_blank" rel="noreferrer">
            <p className="cite-text">{citation.content}</p>
          </a>
        ) : (
          <p className="cite-text cite-text-plain">{citation.content}</p>
        ))}
    </div>
  );
}