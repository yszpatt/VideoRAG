import { useCallback, useEffect, useRef, useState } from "react";
import {
  cancelModel,
  deleteModel,
  downloadModel,
  getModels,
  getSettings,
  getVectorRebuildStatus,
  probeModelHealth,
  rebuildVectorStore,
  retryModel,
  saveSettings,
  setModelPath,
} from "../api.js";

/* ---------- 工具 ---------- */

const ACTIVE = new Set(["downloading", "verifying"]);

function fmtBytes(n) {
  if (!n) return "0 MB";
  const mb = n / 1048576;
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`;
}

function fmtSpeed(bps) {
  if (!bps) return "";
  return `${(bps / 1048576).toFixed(1)} MB/s`;
}

const FALLBACK_OPTS = [
  { value: "sensevoice", label: "SenseVoice（推荐）", hint: "sherpa-onnx + SenseVoiceSmall int8，离线转写" },
  { value: "whisper", label: "Whisper（兼容档）", hint: "faster-whisper 本地转写" },
  { value: "none", label: "禁用本地降级", hint: "远程未配置时转写直接报「未配置 ASR」" },
];

/* 徽章：由状态派生 tone/文案 */
function badgeOf(m) {
  const j = m.job;
  if (j && j.state === "downloading")
    return { tone: "warn", text: `下载中 ${j.pct ?? 0}%` };
  if (j && j.state === "verifying") return { tone: "warn", text: "校验中…" };
  if (j && j.state === "failed") return { tone: "err", text: "下载失败" };
  if (j && j.state === "cancelled") return { tone: "dim", text: "已取消" };
  if (m.installed) return { tone: "ok", text: m.manual ? "就绪（手动路径）" : "就绪" };
  return { tone: "dim", text: "未下载" };
}

function modeText(m, kind) {
  if (kind === "asr") {
    if (m.mode === "cloud") return "当前走远程 ASR（已配置 CLOUD_ASR_*）";
    if (m.mode === "none") return "未配置远程且本地降级已禁用 → 转写不可用";
    return "未配置远程 → 自动使用本地 SenseVoice";
  }
  if (m.mode === "remote") return "当前走远程 Embedding（已配置 EMBED_*）";
  if (m.mode === "none") return "未配置远程且无本地模型 → 向量化不可用";
  return "当前使用本地 fastembed（bge-small-zh-v1.5）";
}

/* ---------- 模型卡（asr / embedding 共用骨架）---------- */

function ModelCard({
  m,
  kind,
  pathDraft,
  onPathDraft,
  busy,
  run,
  notify,
}) {
  const specLabel =
    kind === "asr"
      ? "SenseVoice Small int8 · model.int8.onnx 229 MB + tokens.txt"
      : "bge-small-zh-v1.5 · ONNX 512 维（约 92 MB）";
  const badge = badgeOf(m);
  const j = m.job;
  const active = j && ACTIVE.has(j.state);

  const doDownload = () => run(async () => { await downloadModel(kind); }, `开始下载 ${kind === "asr" ? "ASR" : "Embedding"} 模型…`);
  const doCancel = () => run(async () => { await cancelModel(kind); }, "已发送取消…");
  const doRetry = () => run(async () => { await retryModel(kind); }, "重试中…");
  const doDelete = async () => {
    const label = kind === "asr" ? "SenseVoice" : "bge-small-zh";
    if (!window.confirm(`删除已下载的 ${label} 模型文件？删除后需重新下载（约 229 MB / 92 MB）。`)) return;
    await run(async () => { await deleteModel(kind); }, "删除中…");
  };
  const savePath = async () => {
    await run(async () => {
      await setModelPath(kind, (pathDraft || "").trim());
    }, pathDraft ? "已保存手动路径" : "已清除手动路径");
  };

  return (
    <div className="settings-group">
      <div className="settings-group-head lm-card-head">
        <div>
          <div className="lm-title-row">
            <h3 className="sub-title" style={{ margin: 0 }}>
              {kind === "asr" ? "本地 ASR（语音转写）" : "本地 Embedding（向量化）"}
            </h3>
            <span className={`lm-badge lm-badge-${badge.tone}`}>{badge.text}</span>
          </div>
          <p className="muted small">{specLabel}</p>
          <p className="muted small lm-mode">{modeText(m, kind)}</p>
        </div>
      </div>

      <div className="settings-fields">
        {m.installed && (
          <div className="lm-installed">
            <span>已安装</span>
            <span className="muted small">{fmtBytes(m.installed_bytes)}</span>
          </div>
        )}

        {active && (
          <div className="progress-track is-bar-only">
            <div className="pt-head">
              <span className="pt-label">
                {j.stage || "下载中"}
                {j.file_total > 1 ? `（${j.file_current}/${j.file_total}）` : ""}
              </span>
              <span className="pt-pct">
                {j.pct}% · {fmtBytes(j.bytes_done)}/{fmtBytes(j.bytes_total)}
                {j.speed_bps ? ` · ${fmtSpeed(j.speed_bps)}` : ""}
              </span>
            </div>
            <div className="pt-bar">
              <div
                className={`pt-fill${j.state === "failed" ? " is-err" : ""}`}
                style={{ width: `${Math.min(j.pct ?? 0, 100)}%` }}
              />
            </div>
          </div>
        )}

        {j && j.state === "failed" && (
          <div className="alert alert-error" style={{ padding: "6px 10px", fontSize: 12 }}>
            下载失败：{j.error || "未知原因"}（可重试，断点续传）
          </div>
        )}

        <div className="lm-actions">
          {!m.installed && !active && (
            <button className="btn btn-primary btn-sm" onClick={doDownload} disabled={busy}>
              下载模型（{kind === "asr" ? "229 MB" : "92 MB"}）
            </button>
          )}
          {active && (
            <button className="btn btn-ghost btn-sm" onClick={doCancel} disabled={busy}>
              取消下载
            </button>
          )}
          {j && j.state === "failed" && (
            <button className="btn btn-ghost btn-sm" onClick={doRetry} disabled={busy}>
              重试（续传）
            </button>
          )}
          {m.installed && !m.manual && !active && (
            <button className="btn btn-danger btn-sm" onClick={doDelete} disabled={busy}>
              删除模型
            </button>
          )}
          {m.installed && m.manual && (
            <span className="muted small">手动路径模型不可删除；清空下方路径后生效</span>
          )}
        </div>

        <label className="field">
          <span className="field-label">手动模型目录（可选）</span>
          <div className="lm-path-row">
            <input
              className="input"
              value={pathDraft ?? ""}
              placeholder={
                kind === "asr"
                  ? "含 model.int8.onnx + tokens.txt 的目录，如 /data/models/asr"
                  : "含 model_optimized.onnx 的目录或 HF 缓存根，如 /data/models/bge-small-zh"
              }
              onChange={(e) => onPathDraft(e.target.value)}
            />
            <button
              className="btn btn-ghost btn-sm"
              onClick={savePath}
              disabled={busy}
            >
              {m.manual_path ? "更新 / 清除" : "指定"}
            </button>
          </div>
          <span className="muted small">
            有值时跳过内置下载，仅做文件完整性校验（外部 NFS / 预置模型目录）
          </span>
        </label>
      </div>
    </div>
  );
}

/* ---------- 面板 ---------- */

export default function LocalModelsPanel({ form, setField, onLocalSynced, notify }) {
  const [models, setModels] = useState([]);
  const [loadErr, setLoadErr] = useState(null);
  const [busyKind, setBusyKind] = useState(null);
  const [pathDrafts, setPathDrafts] = useState({ asr: "", embedding: "" });
  const [seeded, setSeeded] = useState({});
  const [probe, setProbe] = useState(null); // {state, text, ok}
  // 向量库重建任务：null | {state:'running'|'done'|'failed', done,total,pct,error}
  const [rebuild, setRebuild] = useState(null);
  const local = form.local || {};
  const timerRef = useRef(null);
  const rebuilding = rebuild?.state === "running";

  const refresh = useCallback(async () => {
    const [ms, st] = await Promise.all([getModels(), getSettings()]);
    setModels(ms.models || []);
    if (onLocalSynced) onLocalSynced(st.local || {});
    return ms.models || [];
  }, [onLocalSynced]);

  useEffect(() => {
    let dead = false;
    refresh().catch((e) => !dead && setLoadErr(e.message));
    return () => { dead = true; };
  }, [refresh]);

  /* 下载/校验期间 2.5s 轮询进度；结束后停止 */
  useEffect(() => {
    if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null; }
    if (models.some((m) => m.job && ACTIVE.has(m.job.state))) {
      timerRef.current = setInterval(() => {
        getModels()
          .then((r) => setModels(r.models || []))
          .catch(() => {});
      }, 2500);
    }
    return () => { if (timerRef.current) clearInterval(timerRef.current); };
  }, [models.some((m) => m.job && ACTIVE.has(m.job.state))]);

  /* 向量库重建：running 期间 1.2s 轮询进度；done/failed 后停止并刷新模型状态 */
  useEffect(() => {
    if (!rebuilding) return;
    let dead = false;
    const t = setInterval(async () => {
      try {
        const st = await getVectorRebuildStatus();
        if (dead) return;
        if (st.state === "running") {
          setRebuild((r) => ({ state: "running", ...st }));
        } else if (st.state === "done") {
          clearInterval(t);
          setRebuild({ state: "done", ...st });
          try {
            await refresh();
            notify?.("重建完成：已重嵌入 " + st.total + " 条切片，向量库模型已更新");
          } catch (e) {
            notify?.(`重建完成但刷新失败：${e.message}`, true);
          }
        } else {
          clearInterval(t);
          setRebuild({ state: "failed", ...st });
          notify?.(`重建失败：${st.error || "未知原因"}`, true);
        }
      } catch (e) {
        if (dead) return;
        clearInterval(t);
        setRebuild({ state: "failed", error: e.message, done: 0, total: 0, pct: 0 });
        notify?.(`重建失败：${e.message}`, true);
      }
    }, 1200);
    return () => { dead = true; clearInterval(t); };
  }, [rebuilding, refresh, notify]);

  /* 手动路径草稿：仅在用户未编辑时由服务端 manual_path 回填一次 */
  useEffect(() => {
    setPathDrafts((d) => {
      const next = { ...d };
      for (const m of models) {
        if (!seeded[m.kind] && m.manual_path) {
          next[m.kind] = m.manual_path;
          setSeeded((s) => ({ ...s, [m.kind]: true }));
        }
      }
      return next;
    });
  }, [models, seeded]);

  const run = async (fn, okMsg) => {
    setLoadErr(null);
    setBusyKind(true);
    try {
      await fn();
      await refresh();
      if (okMsg) notify?.(okMsg);
    } catch (e) {
      setLoadErr(e.message);
      notify?.(`操作失败：${e.message}`, true);
    } finally {
      setBusyKind(false);
    }
  };

  /* 本地降级参数（asr_fallback / 端点 / 镜像）——即时写入并回同步到父表单 */
  const applyLocal = async (patch, okMsg) => {
    setLoadErr(null);
    try {
      await saveSettings({ local: patch });
      await refresh();
      notify?.(okMsg || "本地配置已应用");
    } catch (e) {
      setLoadErr(e.message);
      notify?.(`保存失败：${e.message}`, true);
    }
  };

  const doProbe = async (kind) => {
    setProbe({ state: "probing", kind, text: "探测中…" });
    try {
      const r = await probeModelHealth(kind);
      setProbe(
        r.ok
          ? { state: "ok", kind, text: `服务可达（${r.latency_ms} ms）` }
          : { state: "err", kind, text: r.error || "不可达" },
      );
    } catch (e) {
      setProbe({ state: "err", kind, text: e.message });
    }
  };

  /* 重建知识库（重置向量库）：以当前 embedding 模型重嵌入全部切片 */
  const doRebuild = async () => {
    const n = emb?.store_rows ?? 0;
    if (
      !window.confirm(
        `重建知识库：将以当前 embedding 模型（${emb?.active_model || "?"}）重嵌入` +
          `${n ? "全部 " + n + " 条" : ""}切片并重建向量库。` +
          "切片原文保留在数据库，无需重跑转写；过程中检索 / 入库会短暂不可用。确定继续？",
      )
    ) {
      return;
    }
    setRebuild({ state: "running", done: 0, total: 0, pct: 0, error: "" });
    try {
      await rebuildVectorStore(); // 后台任务，进度由上方 1.2s 轮询驱动
    } catch (e) {
      setRebuild({ state: "failed", error: e.message, done: 0, total: 0, pct: 0 });
      notify?.(`重建失败：${e.message}`, true);
    }
  };

  const byKind = (k) => models.find((m) => m.kind === k) || null;
  const asr = byKind("asr");
  const emb = byKind("embedding");

  return (
    <div className="settings-groups">
      <div className="info-block" style={{ marginBottom: 14 }}>
        <div className="info-block-head">
          <span>本地降级链路</span>
          <span className="muted small">
            以下为保存后的当前生效档位：远程参数已配置则优先远程，未配置自动回落本地
          </span>
        </div>
        <div className="lm-preview">
          <span className="chip chip-btn" style={{ cursor: "default" }}>
            ASR：{asr ? (asr.mode === "cloud" ? "远程" : asr.mode === "none" ? "已禁用" : "本地 SenseVoice") : "…"}
          </span>
          <span className="chip chip-btn" style={{ cursor: "default" }}>
            Embedding：{emb ? (emb.mode === "remote" ? "远程" : emb.mode === "none" ? "不可用" : "本地 fastembed") : "…"}
          </span>
          <span className="muted small">
            {local.asr_fallback === "none"
              ? "ASR 本地降级已关闭"
              : `ASR 本地降级档：${FALLBACK_OPTS.find((o) => o.value === local.asr_fallback)?.label || "SenseVoice"}`}
          </span>
        </div>
      </div>

      {loadErr && <div className="alert alert-error">{loadErr}</div>}

      {!models.length && !loadErr && (
        <div className="loading-block"><span className="spinner" />加载模型状态…</div>
      )}

      {asr && (
        <ModelCard
          m={asr}
          kind="asr"
          pathDraft={pathDrafts.asr}
          onPathDraft={(v) => setPathDrafts((d) => ({ ...d, asr: v }))}
          busy={busyKind}
          run={run}
          notify={notify}
        />
      )}

      {/* ASR 降级档位 + 端点 */}
      {asr && (
        <div className="settings-group">
          <div className="settings-group-head">
            <h3 className="sub-title">本地 ASR 服务</h3>
            <p className="muted small">
              docker 形态由 compose 注入 LOCAL_ASR_BASE_URL=http://asr:9991；裸机默认 127.0.0.1:9991
            </p>
          </div>
          <div className="settings-fields">
            <label className="field">
              <span className="field-label">启用本地降级（远程 ASR 未配置时）</span>
              <select
                className="input"
                value={local.asr_fallback || "sensevoice"}
                onChange={(e) => applyLocal({ asr_fallback: e.target.value }, `本地降级档位已切换为 ${e.target.value}`)}
              >
                {FALLBACK_OPTS.map((o) => (
                  <option key={o.value} value={o.value}>{o.label} — {o.hint}</option>
                ))}
              </select>
            </label>

            <label className="field">
              <span className="field-label">生效端点</span>
              <div className="lm-path-row">
                <input
                  className="input"
                  value={asr.endpoint || ""}
                  readOnly
                />
                <button
                  className={`btn btn-ghost btn-sm${probe?.kind === "asr" && probe.state === "probing" ? " is-busy" : ""}`}
                  onClick={() => doProbe("asr")}
                >
                  测试连通性
                </button>
              </div>
              {probe && probe.kind === "asr" && (
                <span className={`muted small lm-probe ${probe.state === "err" ? "is-err" : ""}`}>
                  {probe.state === "ok" ? "✓ " : probe.state === "err" ? "✗ " : ""}{probe.text}
                </span>
              )}
              <span className="muted small">
                浏览器无法直连容器内 asr:9991，连通性由服务端代探；转写时不可达会给出可读错误
              </span>
            </label>
          </div>
        </div>
      )}

      {emb && (
        <ModelCard
          m={emb}
          kind="embedding"
          pathDraft={pathDrafts.embedding}
          onPathDraft={(v) => setPathDrafts((d) => ({ ...d, embedding: v }))}
          busy={busyKind}
          run={run}
          notify={notify}
        />
      )}

      {/* Embedding 下载镜像 + 切库警示 */}
      {emb && (
        <div className="settings-group">
          <div className="settings-group-head">
            <h3 className="sub-title">Embedding 下载与兼容性</h3>
            <p className="muted small">
              bge-small-zh-v1.5 为 512 维；向量库按模型指纹（provider+model）识别，维度/模型不一致会拒写入
            </p>
          </div>
          <div className="settings-fields">
            <label className="field">
              <span className="field-label">下载镜像端点（可选）</span>
              <input
                className="input"
                value={local.embed_download_endpoint || ""}
                placeholder="留空 = 官方 huggingface.co；国内可设 https://hf-mirror.com"
                onBlur={(e) => {
                  const v = (e.target.value || "").trim();
                  if (v !== (local.embed_download_endpoint || "")) applyLocal({ embed_download_endpoint: v });
                }}
              />
              <span className="muted small">仅影响内置下载（首次下载前设置生效），手动路径不受影响</span>
            </label>

            {emb.store_model ? (
              emb.needs_rebuild ? (
                <div className="alert alert-error" style={{ padding: "8px 10px" }}>
                  <div>
                    向量库由 <b>{emb.store_model}</b>
                    {emb.store_dim ? `（${emb.store_dim} 维）` : ""} 写入（共{" "}
                    {emb.store_rows} 条），当前 embedding 为 <b>{emb.active_model}</b>
                    {emb.active_provider === "openai" ? "（远程）" : "（本地）"} ——
                    模型不一致，新入库 / 检索会被拒。以当前模型重建后即可恢复
                    （切片原文保留，无需重跑转写）。
                  </div>
                  {rebuild?.state === "failed" && (
                    <div className="muted small" style={{ marginTop: 6 }}>
                      重建失败：{rebuild.error}
                    </div>
                  )}
                </div>
              ) : (
                <div className="alert alert-ok" style={{ padding: "8px 10px" }}>
                  向量库与当前模型一致（{emb.store_model}
                  {emb.store_dim ? `，${emb.store_dim} 维` : ""}，共 {emb.store_rows}{" "}
                  条）——可直接入库 / 检索
                </div>
              )
            ) : (
              <div className="muted small">
                向量库尚未写入数据（无模型指纹）——首次入库后自动建档
              </div>
            )}

            {emb.needs_rebuild && !rebuilding && (
              <button
                className="btn btn-primary btn-sm"
                onClick={doRebuild}
                disabled={busyKind}
                style={{ marginTop: 8 }}
              >
                重建知识库（重置向量库）
              </button>
            )}

            {rebuilding && (
              <div className="progress-track is-bar-only" style={{ marginTop: 8 }}>
                <div className="pt-head">
                  <span className="pt-label">
                    重建中：已嵌入 {rebuild.done}/{rebuild.total || "…"} 条
                  </span>
                  <span className="pt-pct">{rebuild.pct ?? 0}%</span>
                </div>
                <div className="pt-bar">
                  <div
                    className="pt-fill"
                    style={{ width: `${Math.min(rebuild.pct ?? 0, 100)}%` }}
                  />
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
