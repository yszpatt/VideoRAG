const API = "";

async function request(path, options = {}) {
  let resp;
  try {
    resp = await fetch(`${API}${path}`, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
  } catch (e) {
    throw new Error(`网络请求失败：${e.message}`);
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      detail = body.detail || body.message || detail;
    } catch (_) {
      // 非 JSON 错误体（如裸 500 文本）→ 读原文，保留后端真实信息
      try {
        const text = await resp.text();
        if (text) detail = text;
      } catch (_) {}
    }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return resp.json();
}

export const submitVideo = (url) =>
  request("/api/videos", { method: "POST", body: JSON.stringify({ url }) });

export const listVideos = () => request("/api/videos");

export const getVideo = (id) => request(`/api/videos/${id}`);

export const getNote = (id) => request(`/api/videos/${id}/note`);

export const getTranscript = (id) => request(`/api/videos/${id}/transcript`);

export const deleteVideo = (id) =>
  request(`/api/videos/${id}`, { method: "DELETE" });

export const searchKnowledge = (query, topK = 8) =>
  request("/api/search", { method: "POST", body: JSON.stringify({ query, top_k: topK }) });

export const ask = (question, topK = 8) =>
  request("/api/ask", { method: "POST", body: JSON.stringify({ question, top_k: topK }) });

export const getSettings = () => request("/api/settings");

export const saveSettings = (settings) =>
  request("/api/settings", { method: "PUT", body: JSON.stringify(settings) });

// ---- 本地降级：本地模型下载 / 手动路径 / 连通性（/api/models）----

export const getModels = () => request("/api/models");

export const downloadModel = (kind) =>
  request(`/api/models/${kind}/download`, { method: "POST" });

export const cancelModel = (kind) =>
  request(`/api/models/${kind}/cancel`, { method: "POST" });

export const retryModel = (kind) =>
  request(`/api/models/${kind}/retry`, { method: "POST" });

export const deleteModel = (kind) =>
  request(`/api/models/${kind}`, { method: "DELETE" });

export const setModelPath = (kind, path) =>
  request(`/api/models/${kind}/path`, {
    method: "PUT",
    body: JSON.stringify({ path: path || "" }),
  });

export const probeModelHealth = (kind) => request(`/api/models/${kind}/health`);

// ---- 设置页连通性探测（Base URL 旁「测试连通性」，服务端代发）----
export const probeService = (payload) =>
  request("/api/settings/probe", { method: "POST", body: JSON.stringify(payload) });

// ---- 向量库重建（换 embedding 模型后「重建知识库」，后台任务 + 轮询进度）----
export const rebuildVectorStore = () =>
  request("/api/vectorstore/rebuild", { method: "POST" });

export const getVectorRebuildStatus = () => request("/api/vectorstore/rebuild/status");

export const getPrompts = () => request("/api/prompts");

export const updatePrompts = (updates) =>
  request("/api/prompts", { method: "PUT", body: JSON.stringify(updates) });

export const resetPrompts = (key = null) =>
  request("/api/prompts/reset", {
    method: "POST",
    body: JSON.stringify(key ? { key } : {}),
  });

export const previewPrompt = (key, value = null) =>
  request("/api/prompts/preview", {
    method: "POST",
    body: JSON.stringify(value !== null ? { key, value } : { key }),
  });

export const listHistory = (kind, limit = 20) =>
  request(`/api/history?kind=${kind}&limit=${limit}`);

export const getHistory = (id) => request(`/api/history/${id}`);

export const deleteHistory = (id) =>
  request(`/api/history/${id}`, { method: "DELETE" });

export const clearHistory = (kind = null) =>
  request(`/api/history${kind ? `?kind=${kind}` : ""}`, { method: "DELETE" });
