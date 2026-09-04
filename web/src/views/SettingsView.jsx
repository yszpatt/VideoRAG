import { useEffect, useRef, useState } from "react";
import { clearHistory, getSettings, probeService, saveSettings } from "../api.js";
import ConfirmDialog from "../components/ConfirmDialog.jsx";
import LocalModelsPanel from "../components/LocalModelsPanel.jsx";
import PromptTab from "../components/PromptTab.jsx";

const GROUPS = [
  {
    key: "llm",
    title: "LLM（大语言模型）",
    desc: "OpenAI 兼容格式，任意服务可切（DeepSeek / OpenAI / Ollama / vLLM）",
    fields: [
      { key: "provider", label: "服务标识", placeholder: "ollama / deepseek / openai" },
      { key: "base_url", label: "Base URL", placeholder: "http://192.168.x.x:11434/v1" },
      { key: "api_key", label: "API Key", placeholder: "留空则不修改" },
      { key: "model", label: "模型", placeholder: "qwen3-vl:30b-a3b-instruct-q4_K_M" },
    ],
  },
  {
    key: "asr",
    title: "ASR（语音转写）",
    desc: "OpenAI 兼容 /v1/audio/transcriptions；集中式转写服务（SenseVoice / faster-whisper）",
    fields: [
      { key: "provider", label: "服务标识", placeholder: "sensevoice / whisper" },
      { key: "base_url", label: "Base URL", placeholder: "http://192.168.x.x:9991" },
      { key: "api_key", label: "API Key", placeholder: "留空则不修改（如 local）" },
      { key: "model", label: "模型", placeholder: "sensevoice / large-v3" },
    ],
  },
  {
    key: "embedding",
    title: "Embedding（向量化）",
    desc: "fastembed 本地 / openai 远程（Ollama bge-m3 等）",
    fields: [
      { key: "provider", label: "服务标识", placeholder: "fastembed / openai" },
      { key: "base_url", label: "Base URL", placeholder: "http://192.168.x.x:11434/v1" },
      { key: "api_key", label: "API Key", placeholder: "留空则不修改" },
      { key: "model", label: "模型", placeholder: "bge-m3:latest" },
    ],
  },
];

const TABS = [
  { key: "services", label: "服务配置" },
  { key: "models", label: "本地模型" },
  { key: "prompts", label: "提示词" },
];

// 检索策略参数（数值型，后端 settings API retrieval 组）
const RETRIEVAL_FIELDS = [
  {
    key: "vector_k",
    label: "向量召回条数",
    type: "number",
    min: 1,
    max: 100,
    hint: "每路宽召回的候选量，越大召回越全但稍慢",
  },
  {
    key: "fts_k",
    label: "全文召回条数",
    type: "number",
    min: 1,
    max: 100,
    hint: "关键词/术语的全文检索候选量",
  },
  {
    key: "rrf_k",
    label: "RRF 平滑常数",
    type: "number",
    min: 1,
    max: 1000,
    hint: "融合平滑度，60 为常用值；越大名次差异越平缓",
  },
  {
    key: "vector_weight",
    label: "向量权重",
    type: "number",
    min: 0,
    max: 1,
    step: 0.05,
    hint: "语义相关性权重（0~1），与全文权重共同决定排序",
  },
  {
    key: "fts_weight",
    label: "全文权重",
    type: "number",
    min: 0,
    max: 1,
    step: 0.05,
    hint: "关键词命中权重（0~1）；专有名词多时可调高",
  },
  {
    key: "per_video_cap",
    label: "单视频配额",
    type: "number",
    min: 0,
    max: 20,
    hint: "每个视频最多占的结果条数（0 = 不限），防止霸榜",
  },
  {
    key: "min_sim",
    label: "相似度门槛",
    type: "number",
    min: 0,
    max: 1,
    step: 0.05,
    hint: "丢弃纯向量命中中相似度低于此值的结果（0 = 关闭；建议 0.3）",
  },
  {
    key: "neighbor_gap",
    label: "相邻合并窗口（秒）",
    type: "number",
    min: 0,
    max: 30,
    step: 0.5,
    hint: "同视频时间相邻/重叠的切片只保留排名最高一条（0 = 关闭）",
  },
];

export default function SettingsView() {
  const [tab, setTab] = useState("services");
  const [form, setForm] = useState({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState(null);
  const [err, setErr] = useState(null);
  // 服务模式开关（仅 asr / embedding；llm 恒远程）：显式「使用本地模型」选择。
  // true=本地档（远程字段禁用并清空）；false=远程档（可填写远程参数）。
  const [localMode, setLocalMode] = useState({});
  // 连通性探测结果：{ [groupKey]: { state: "ok"|"err"|"probing", text } }
  const [probes, setProbes] = useState({});
  const [probing, setProbing] = useState(null); // 正在探测的组 key（按钮 busy 态）
  // 模式切换确认弹窗：{ title, message, danger, onOk }
  const [confirm, setConfirm] = useState(null);
  // 加载时的配置快照：保存时按「快照对比」只发送用户实际变更的字段，
  // 未编辑字段不发送（后端 None=不覆盖），用户清空的字段发送 ""（后端清空 → 回落本地档）。
  // 防止整表保存用陈旧空串覆盖未编辑组 / 手动路径等已应用配置。
  const snapshotRef = useRef(null);

  useEffect(() => {
    getSettings()
      .then((d) => {
        setForm(d);
        snapshotRef.current = JSON.parse(JSON.stringify(d));
        // 服务模式初值：按已保存配置推导（provider+base_url 齐 → 远程档）
        const m = {};
        for (const g of GROUPS) {
          if (g.key !== "llm") m[g.key] = !(d[g.key]?.provider && d[g.key]?.base_url);
        }
        setLocalMode(m);
        setLoading(false);
      })
      .catch((e) => {
        setErr(`加载配置失败：${e.message}`);
        setLoading(false);
      });
  }, []);

  const setField = (group, field, value) =>
    setForm((f) => ({
      ...f,
      [group]: { ...(f[group] || {}), [field]: value },
    }));

  // 归一化检索参数：空串/非数字 → null（后端 None = 不覆盖）
  const normalizeRetrieval = () => {
    const retrieval = { ...(form.retrieval || {}) };
    for (const f of RETRIEVAL_FIELDS) {
      const v = retrieval[f.key];
      retrieval[f.key] =
        v === "" || v === null || v === undefined || Number.isNaN(Number(v))
          ? null
          : Number(v);
    }
    return retrieval;
  };

  // 服务组保存：只发送「相对加载快照」发生变更的字段。
  // 掩码判定只看「当前字段值 cv」：cv 仍是掩码占位（****）才视为未编辑 → 不发送、
  // 后端据此保留原 Key。注意：不能拿「快照 sv === ****」当掩码——否则首次填真实
  // Key（此时快照是 **** 占位，实为 .env 占位值 ollama 的掩码）会被误判为「未变更」
  // 而丢弃，导致真实 Key 永远写不进 runtime.env（本 bug 根因）。
  const buildServicePayload = () => {
    const snap = snapshotRef.current || {};
    const out = {};
    for (const g of GROUPS) {
      const cur = form[g.key] || {};
      const s = snap[g.key] || {};
      const changed = {};
      for (const f of g.fields) {
        const k = f.key;
        const cv = cur[k] ?? "";
        const sv = s[k] ?? "";
        const isMaskedKey = f.key === "api_key" && cv === "****";
        if (cv === sv || isMaskedKey) continue; // 未变更 / 掩码占位 → 不覆盖
        changed[k] = cv; // 变更：新值或 ""（清空远程 → 回落本地档）
      }
      if (Object.keys(changed).length) out[g.key] = changed;
    }
    return out;
  };

  const onSave = async (groupsPayload) => {
    const retrieval = normalizeRetrieval();
    const payload = { ...(groupsPayload || {}), retrieval };
    const hasGroup = Object.keys(groupsPayload || {}).length > 0;
    const hasRetrieval = Object.values(retrieval).some((v) => v !== null);
    if (!hasGroup && !hasRetrieval) {
      setMsg("没有需要保存的变更");
      return;
    }
    setSaving(true);
    setErr(null);
    setMsg(null);
    try {
      const r = await saveSettings(payload);
      // 保存成功 → 以「当前表单 + 本次发送的变更」重建快照基线：
      // 避免后续对比把已清空的远程字段（本地档）误判为待保存的陈旧变更。
      const base = JSON.parse(JSON.stringify(form));
      const merged = { ...base };
      for (const [gk, fields] of Object.entries(groupsPayload || {})) {
        merged[gk] = { ...(merged[gk] || {}), ...fields };
      }
      snapshotRef.current = merged;
      setMsg(
        `已保存并立即生效（${r.saved.length} 项）：${r.saved.join(", ")}`,
      );
    } catch (e) {
      setErr(`保存失败：${e.message}`);
    } finally {
      setSaving(false);
    }
  };

  // 服务页保存：服务组（llm/asr/embedding）按快照对比发送变更 + 检索参数；
  // 本地降级组（local）不经此保存（由「本地模型」Tab 独立即时生效）。
  const onSaveServices = () => onSave(buildServicePayload());

  // ---- 「使用本地模型 / 远程服务」模式切换（#1：选定后确认再切换）----

  const remoteActive = (key) => !!(form[key]?.provider && form[key]?.base_url);

  // 切到远程档：仅启用远程字段（后续由用户填写并保存），无需确认
  const goRemote = (g) => setLocalMode((m) => ({ ...m, [g.key]: false }));

  // 切到本地档：存在远程配置时先弹确认（说明清空范围与向量库重建影响）
  const goLocal = (g) => {
    if (localMode[g.key]) return;
    const cur = form[g.key] || {};
    const snap = snapshotRef.current?.[g.key] || {};
    const savedRemote = !!(snap.provider && snap.base_url);
    const draftRemote = remoteActive(g.key);
    if (!savedRemote && !draftRemote) {
      setLocalMode((m) => ({ ...m, [g.key]: true }));
      return;
    }
    const p = cur.provider || snap.provider || "?";
    const md = cur.model || snap.model || "?";
    const isEmb = g.key === "embedding";
    setConfirm(
      isEmb
        ? {
            title: "切换到本地 Embedding（fastembed）？",
            danger: true,
            message: (
              <>
                将清空远程 Embedding 配置（<b>{p}</b> / <b>{md}</b>），改由本地
                fastembed bge-small-zh-v1.5（512 维）向量化。
                <br />
                <br />
                若向量库由远程模型写入，维度 / 语义与本地模型不一致——切换后检索会提示
                「模型已切换」，需在「设置 → 本地模型」点「重建知识库」，以本地模型
                重嵌入全部切片（原文保留，无需重跑转写）。
              </>
            ),
            onOk: () => doSwitchLocal(g),
          }
        : {
            title: "切换到本地 ASR（SenseVoice）？",
            danger: false,
            message: (
              <>
                将清空远程 ASR 配置（<b>{p}</b> / <b>{md}</b>），转写回落本地
                SenseVoice 档位。
                <br />
                <br />
                需先在「设置 → 本地模型」下载 SenseVoice 模型并启用 local-asr
                侧车（docker compose --profile local-asr up -d）。
              </>
            ),
            onOk: () => doSwitchLocal(g),
          },
    );
  };

  // 确认后：清空远程字段（保留 api_key 便于日后切回远程）+ 立即保存生效
  const doSwitchLocal = async (g) => {
    setConfirm(null);
    const patch = {};
    for (const f of g.fields) {
      if (f.key === "api_key") continue; // 密钥保留（掩码回显，不随清空丢失）
      patch[f.key] = "";
      setField(g.key, f.key, "");
    }
    setLocalMode((m) => ({ ...m, [g.key]: true }));
    await onSave({ [g.key]: patch });
  };

  // ---- 连通性探测（#2：每个 Base URL 旁的「测试连通性」）----

  const doProbe = async (g) => {
    const cur = form[g.key] || {};
    const url = (cur.base_url || "").trim();
    if (!url) {
      setProbes((p) => ({ ...p, [g.key]: { state: "err", text: "请先填写 Base URL 再测试" } }));
      return;
    }
    setProbing(g.key);
    setProbes((p) => ({ ...p, [g.key]: { state: "probing", text: "探测中…" } }));
    try {
      const curKey = (cur.api_key || "").trim();
      // 掩码占位（**** 或 sk-T...7890 前4...后4 格式）→ 不发真实值，
      // 由后端回落已保存的真实 key；仅当用户重新手填真实 key 时才随请求发送。
      const isMasked =
        curKey === "****" || /^.{4}\.\.\..{4}$/.test(curKey);
      const r = await probeService({
        kind: g.key,
        base_url: url,
        model: (cur.model || "").trim() || undefined,
        api_key: isMasked ? undefined : (curKey || undefined),
      });
      setProbes((p) => ({ ...p, [g.key]: { state: r.ok ? "ok" : "err", text: r.message } }));
    } catch (e) {
      setProbes((p) => ({ ...p, [g.key]: { state: "err", text: e.message } }));
    } finally {
      setProbing(null);
    }
  };

  // 模型面板操作成功后，回同步最新 local 组到表单（防止服务配置整表保存时覆盖）
  const onLocalSynced = (localGroup) => {
    setForm((f) => ({ ...f, local: localGroup }));
    if (localGroup && localGroup.local_asr_base_url) {
      setErr(null);
    }
  };

  // E5：历史记录清理（服务端持久化，跨设备生效）
  const [clearing, setClearing] = useState(false);
  const onClearHistory = async (kindLabel, kind) => {
    if (!window.confirm(`确定清空全部${kindLabel}历史？该操作不可恢复。`)) return;
    setClearing(true);
    setMsg(null);
    setErr(null);
    try {
      const r = await clearHistory(kind);
      setMsg(`已清空${kindLabel}历史（${r.rows} 条）`);
    } catch (e) {
      setErr(`清空失败：${e.message}`);
    } finally {
      setClearing(false);
    }
  };

  return (
    <div className="view">
      <section className="panel">
        <div className="panel-head">
          <div>
            <h2 className="panel-title">设置</h2>
            <p className="panel-sub">
              {tab === "services"
                ? "在线调整模型服务，保存后立即生效（写入运行时配置，重启不丢失）"
                : tab === "models"
                  ? "本地降级模型（ASR / Embedding）的下载、手动指定与连通性检查；未配置远程服务参数时自动回落本地"
                  : "自定义笔记与问答的提示词，保存后下一次生成 / 提问即生效"}
            </p>
          </div>
        </div>

        <div className="settings-tabs">
          {TABS.map((t) => (
            <button
              key={t.key}
              className={`settings-tab${tab === t.key ? " active" : ""}`}
              onClick={() => setTab(t.key)}
            >
              {t.label}
            </button>
          ))}
        </div>

        {tab === "prompts" && <PromptTab />}

        {tab === "models" && !loading && (
          <>
            {err && <div className="alert alert-error">{err}</div>}
            {msg && <div className="alert alert-ok">{msg}</div>}
            <LocalModelsPanel
              form={form}
              setField={setField}
              onLocalSynced={onLocalSynced}
              notify={(m, isErr) => (isErr ? setErr(m) : setMsg(m))}
            />
          </>
        )}

        {tab === "models" && loading && (
          <div className="loading-block">
            <span className="spinner" />
            加载配置…
          </div>
        )}

        {tab === "services" && (
          <>
            {loading && (
              <div className="loading-block">
                <span className="spinner" />
                加载配置…
              </div>
            )}

            {err && <div className="alert alert-error">{err}</div>}
            {msg && <div className="alert alert-ok">{msg}</div>}

            {!loading && (
              <div className="settings-groups">
                {GROUPS.map((g) => {
                  const switchable = g.key !== "llm"; // asr / embedding 可切本地档
                  const local = !!localMode[g.key];
                  return (
                    <div className="settings-group" key={g.key}>
                      <div className="settings-group-head">
                        <h3 className="sub-title">{g.title}</h3>
                        <p className="muted small">{g.desc}</p>
                      </div>

                      {switchable && (
                        <div className="settings-fields" style={{ paddingBottom: 0 }}>
                          <label className="field">
                            <span className="field-label">服务模式</span>
                            <div className="lm-path-row">
                              <div className="segmented">
                                <button
                                  type="button"
                                  className={`seg-btn${local ? " is-active" : ""}`}
                                  onClick={() => goLocal(g)}
                                >
                                  使用本地模型
                                </button>
                                <button
                                  type="button"
                                  className={`seg-btn${!local ? " is-active" : ""}`}
                                  onClick={() => goRemote(g)}
                                >
                                  使用远程服务
                                </button>
                              </div>
                            </div>
                            <span className="muted small">
                              {local
                                ? g.key === "embedding"
                                  ? "本地档：fastembed bge-small-zh-v1.5（512 维）。远程字段已禁用；若向量库由远程模型写入，切换后需在「本地模型」重建知识库"
                                  : "本地档：SenseVoice 本地转写。远程字段已禁用；需已下载模型并启用 local-asr 侧车"
                                : "远程档：填写下方远程服务参数并点「保存并生效」即切换"}
                            </span>
                          </label>
                        </div>
                      )}

                      <div className="settings-fields">
                        {g.fields.map((f) => (
                          <label className="field" key={f.key}>
                            <span className="field-label">{f.label}</span>
                            {f.key === "base_url" ? (
                              <div className="lm-path-row">
                                <input
                                  className="input"
                                  type="text"
                                  value={(form[g.key] || {})[f.key] || ""}
                                  placeholder={f.placeholder}
                                  disabled={switchable && local}
                                  onChange={(e) => setField(g.key, f.key, e.target.value)}
                                />
                                <button
                                  type="button"
                                  className={`btn btn-ghost btn-sm${probing === g.key ? " is-busy" : ""}`}
                                  onClick={() => doProbe(g)}
                                  disabled={switchable && local}
                                >
                                  {probing === g.key ? "测试中…" : "测试连通性"}
                                </button>
                              </div>
                            ) : (
                              <input
                                className="input"
                                type={f.key === "api_key" ? "password" : "text"}
                                value={(form[g.key] || {})[f.key] || ""}
                                placeholder={f.placeholder}
                                disabled={switchable && local}
                                onChange={(e) => setField(g.key, f.key, e.target.value)}
                              />
                            )}
                            {f.key === "base_url" && probes[g.key] && (
                              <span className={`muted small lm-probe${probes[g.key].state === "err" ? " is-err" : ""}`}>
                                {probes[g.key].state === "ok" ? "✓ " : probes[g.key].state === "err" ? "✗ " : ""}
                                {probes[g.key].text}
                              </span>
                            )}
                          </label>
                        ))}
                      </div>
                    </div>
                  );
                })}

                <div className="settings-group">
                  <div className="settings-group-head">
                    <h3 className="sub-title">检索策略</h3>
                    <p className="muted small">
                      混合检索流水线：宽召回 → 加权融合 → 相关性过滤 → 去重与配额；保存后下一次提问 / 检索即生效
                    </p>
                  </div>
                  <div className="settings-fields">
                    {RETRIEVAL_FIELDS.map((f) => (
                      <label className="field" key={f.key}>
                        <span className="field-label">{f.label}</span>
                        <input
                          className="input"
                          type="number"
                          min={f.min}
                          max={f.max}
                          step={f.step}
                          value={form.retrieval?.[f.key] ?? ""}
                          placeholder="留空表示不修改"
                          onChange={(e) =>
                            setField("retrieval", f.key, e.target.value)
                          }
                        />
                        <span className="muted small">{f.hint}</span>
                      </label>
                    ))}
                  </div>
                </div>

                <div className="settings-actions">
                  <button
                    className="btn btn-primary"
                    onClick={onSaveServices}
                    disabled={saving}
                  >
                    {saving ? "保存中…" : "保存并生效"}
                  </button>
                  <span className="muted small">
                    API Key 显示为掩码；留空表示不修改（掩码占位亦不覆盖原 Key）
                  </span>
                </div>

                <div className="settings-group">
                  <div className="settings-group-head">
                    <h3 className="sub-title">数据管理</h3>
                    <p className="muted small">
                      提问 / 检索历史保存在服务端（跨浏览器、清缓存不丢）
                    </p>
                  </div>
                  <div className="settings-actions">
                    <button
                      className="btn btn-ghost btn-sm"
                      onClick={() => onClearHistory("提问", "ask")}
                      disabled={clearing}
                    >
                      清空提问历史
                    </button>
                    <button
                      className="btn btn-ghost btn-sm"
                      onClick={() => onClearHistory("检索", "search")}
                      disabled={clearing}
                    >
                      清空检索历史
                    </button>
                  </div>
                </div>
              </div>
            )}
          </>
        )}

        <ConfirmDialog
          open={!!confirm}
          title={confirm?.title}
          message={confirm?.message}
          danger={confirm?.danger}
          confirmText="切换"
          onConfirm={() => {
            const c = confirm;
            setConfirm(null);
            c?.onOk?.();
          }}
          onCancel={() => setConfirm(null)}
        />
      </section>
    </div>
  );
}
