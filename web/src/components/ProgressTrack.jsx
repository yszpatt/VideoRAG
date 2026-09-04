import { platformOf } from "../lib/format.js";

export function PlatformBadge({ platform, showLabel = true }) {
  const p = platformOf(platform);
  return (
    <span className="platform-badge" style={{ "--p-color": p.color }} title={p.label}>
      <span className="platform-dot" />
      {showLabel && <span className="platform-label">{p.label}</span>}
    </span>
  );
}

const STATUS_TEXT = {
  pending: "排队中",
  fetching: "下载中",
  transcribing: "转写中",
  noting: "生成笔记",
  embedding: "向量入库",
  done: "已完成",
  failed: "失败",
};

export function StatusChip({ status }) {
  const cls =
    status === "done"
      ? "ok"
      : status === "failed"
        ? "err"
        : status === "pending"
          ? "idle"
          : "busy";
  return (
    <span className={`status-chip is-${cls}`}>
      {cls === "busy" && <span className="pulse-dot" />}
      {STATUS_TEXT[status] || status}
    </span>
  );
}

export default function ProgressTrack({ progress, compact = false, barOnly = false }) {
  if (!progress) return null;
  const { stages, percent, stage_label, status, failed } = progress;

  const label =
    status === "done"
      ? "处理完成"
      : failed
        ? `${stage_label}失败`
        : `${stage_label}中…`;

  return (
    <div
      className={`progress-track${compact ? " is-compact" : ""}${barOnly ? " is-bar-only" : ""}`}
    >
      {!barOnly && (
        <div className="pt-head">
          <span className={`pt-label${failed ? " is-err" : ""}`}>{label}</span>
          <span className="pt-pct">{percent}%</span>
        </div>
      )}
      <div className="pt-bar">
        <div
          className={`pt-fill${failed ? " is-err" : ""}${status === "done" ? " is-done" : ""}`}
          style={{ width: `${percent}%` }}
        />
      </div>
      {!compact && !barOnly && (
        <ol className="pt-steps">
          {stages.map((s) => (
            <li key={s.key} className={`pt-step is-${s.state}`}>
              <span className="pt-dot" />
              <span className="pt-step-label">{s.label}</span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
