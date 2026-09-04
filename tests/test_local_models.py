"""本地降级 L1：download/manager/registry 单测 + /api/models 契约。

mock 网络层（真实下载由手动验收覆盖）：
- downloader 测试用本地 http 服务器模拟 200/206 + 校验 sha256 失败 + 取消保留 .part。
- manager 测试 patch _run_download 为快速假实现，验证状态机/互斥/取消/重试/删除。

注意：start()/cancel() 内部用 asyncio.create_task 调度后台任务，await start() 返回后
任务未必已执行——断言状态前必须先 await asyncio.sleep(0) 让出事件循环。
"""

import asyncio
import hashlib
import http.server
import socketserver
import threading
from pathlib import Path

import pytest

from app.config import Settings
from app.core.local_models import registry
from app.core.local_models.downloader import DownloadError, download_file
from app.core.local_models.manager import (
    DownloadBusy,
    DownloadConflict,
    DownloadNotFound,
    ModelManager,
)


# ========== registry ==========

def test_registry_specs():
    assert set(registry.KINDS) == {"asr", "embedding"}
    asr = registry.get_spec("asr")
    assert asr.source == "modelscope"
    assert asr.repo == "poloniumrock/SenseVoiceSmallOnnx"
    assert [f.path for f in asr.files] == ["model.int8.onnx", "tokens.txt"]
    assert asr.required == ("model.int8.onnx", "tokens.txt")
    assert all(f.sha256 for f in asr.files)  # 完整性基准已填
    assert asr.total_size > 200_000_000  # 229MB 级
    emb = registry.get_spec("embedding")
    assert emb.hf_repo == "Qdrant/bge-small-zh-v1.5"
    assert "model_optimized.onnx" in emb.hf_files


# ========== downloader（本地 http mock）==========

class _Handler(http.server.BaseHTTPRequestHandler):
    """1KB payload；支持 Range（206）与越界（416）；sha 由各测试显式指定。"""

    payload = b"0123456789abcdef" * 64  # 1024 B
    sha = ""

    def log_message(self, *a):  # 静默
        pass

    def do_GET(self):  # noqa: N802
        if "Range" in self.headers:
            start = int(self.headers["Range"].split("=")[1].split("-")[0])
            if start >= len(self.payload):
                self.send_response(416)
                self.end_headers()
                return
            body = self.payload[start:]
            self.send_response(206)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Range", f"bytes {start}-{len(self.payload)-1}/{len(self.payload)}")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)


@pytest.fixture
def http_server():
    _Handler.sha = ""  # 类属性跨测试共享 → 每次复位避免污染
    srv = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/file.bin"
    srv.shutdown()


async def test_downloader_fresh_and_sha_ok(http_server, tmp_path):
    dest = tmp_path / "model.bin"
    _Handler.sha = hashlib.sha256(_Handler.payload).hexdigest()
    n = await download_file(http_server, dest, expected_sha256=_Handler.sha)
    assert dest.is_file()
    assert n == len(_Handler.payload)
    assert dest.stat().st_size == len(_Handler.payload)
    assert not dest.with_suffix(dest.suffix + ".part").exists()
    # 幂等：已完整下载且 sha 通过 → 直接返回不再请求
    n2 = await download_file(http_server, dest, expected_sha256=_Handler.sha)
    assert n2 == len(_Handler.payload)


async def test_downloader_resume_from_part(http_server, tmp_path):
    """.part 已有一半 → Range 续传补齐，最终 sha 一致。"""
    dest = tmp_path / "model.bin"
    part = dest.with_suffix(dest.suffix + ".part")
    part.write_bytes(_Handler.payload[: len(_Handler.payload) // 2])
    _Handler.sha = hashlib.sha256(_Handler.payload).hexdigest()
    n = await download_file(http_server, dest, expected_sha256=_Handler.sha)
    assert n == len(_Handler.payload)
    assert dest.read_bytes() == _Handler.payload
    assert not part.exists()


async def test_downloader_sha_mismatch_cleans_part(http_server, tmp_path):
    """sha256 不符 → DownloadError 且 .part 被清理（下次可重下）。"""
    dest = tmp_path / "model.bin"
    _Handler.sha = hashlib.sha256(b"totally-different").hexdigest()
    with pytest.raises(DownloadError, match="sha256 mismatch"):
        await download_file(http_server, dest, expected_sha256=_Handler.sha)
    assert not dest.exists()
    assert not dest.with_suffix(dest.suffix + ".part").exists()


async def test_downloader_cancel_keeps_part(http_server, tmp_path):
    """取消 → CancelledError 且已有 .part 内容保留（供 retry 续传）。"""
    dest = tmp_path / "model.bin"
    part = dest.with_suffix(dest.suffix + ".part")
    seed = b"already-downloaded-half"
    part.write_bytes(seed)
    cancel = asyncio.Event()
    cancel.set()  # 预置取消：首个 chunk 到达即中断
    with pytest.raises(asyncio.CancelledError):
        await download_file(http_server, dest, cancel_event=cancel)
    assert not dest.exists()
    assert part.read_bytes() == seed  # 原内容未被清空


# ========== manager（patch 下载协程）==========

@pytest.fixture
def manager(tmp_path):
    s = Settings(_env_file=None, data_dir=str(tmp_path))
    return ModelManager(s), tmp_path


async def _fake_run_success(self, kind, spec, model_dir, job):
    """快速假下载：直接落盘 spec 文件 → done（同步跑完，无 await）。"""
    model_dir.mkdir(parents=True, exist_ok=True)
    if kind == "asr":
        for f in spec.files:
            (model_dir / f.path).write_bytes(b"0" * 100)
        job.bytes_done = spec.total_size
    else:
        job.bytes_done = 1000
    job.state, job.stage = "done", ""


async def _fake_run_never(self, kind, spec, model_dir, job):
    """挂起型假下载：等待 cancel；被取消时把状态收敛为 cancelled
    （真实 _run_download 的 except CancelledError 处理在包装层，patch 后需自行负责）。"""
    try:
        while not job._cancel.is_set():
            await asyncio.sleep(0.01)
    except asyncio.CancelledError:
        pass
    job.state, job.stage = "cancelled", ""


async def test_manager_start_done(monkeypatch, manager):
    m, tmp = manager
    monkeypatch.setattr(ModelManager, "_run_download", _fake_run_success)
    job = await m.start("asr")
    await asyncio.sleep(0)  # 让后台假下载任务跑完
    assert job.state == "done"
    assert job.bytes_done == job.bytes_total
    # 幂等：已安装再 start 直接 done（不重复任务）
    job2 = await m.start("asr")
    assert job2.state == "done"
    # 文件已落盘到 models_dir/asr
    assert (Path(tmp) / "models" / "asr" / "model.int8.onnx").is_file()
    assert (Path(tmp) / "models" / "asr" / "tokens.txt").is_file()


async def test_manager_mutex(monkeypatch, manager):
    """同一 kind 下载中 → 二次 start 抛 DownloadBusy（409 语义）。"""
    m, _ = manager
    monkeypatch.setattr(ModelManager, "_run_download", _fake_run_never)
    job = await m.start("asr")
    assert job.state == "downloading"  # start 同步置 downloading
    await asyncio.sleep(0)  # 让假任务真正开始执行（未启动即 cancel 的协程体不会运行）
    with pytest.raises(DownloadBusy):
        await m.start("asr")
    await m.cancel("asr")
    await asyncio.sleep(0.05)  # 等任务收敛状态
    assert m.job("asr").state == "cancelled"


async def test_manager_cancel(monkeypatch, manager):
    m, _ = manager
    monkeypatch.setattr(ModelManager, "_run_download", _fake_run_never)
    await m.start("asr")
    await asyncio.sleep(0)  # 让假任务先跑起来再 cancel
    job = await m.cancel("asr")
    await asyncio.sleep(0.05)
    assert job.state == "cancelled"
    with pytest.raises(DownloadNotFound):
        await m.cancel("embedding")  # 无进行中任务


async def test_manager_retry_only_failed(monkeypatch, manager):
    m, _ = manager

    async def _fail(self, kind, spec, model_dir, job):
        job.state, job.stage = "failed", ""
        job.error = "network down"

    monkeypatch.setattr(ModelManager, "_run_download", _fail)
    await m.start("asr")
    await asyncio.sleep(0)
    assert m.job("asr").state == "failed"

    with pytest.raises(DownloadConflict):
        await m.retry("embedding")  # idle 态不可重试

    monkeypatch.setattr(ModelManager, "_run_download", _fake_run_success)
    job = await m.retry("asr")  # failed → 重试
    await asyncio.sleep(0)
    assert job.state == "done"


async def test_manager_manual_path_blocks_download(monkeypatch, manager, tmp_path):
    m, _ = manager
    nfs = tmp_path / "nfs-asr"
    nfs.mkdir()
    (nfs / "model.int8.onnx").write_bytes(b"x")
    (nfs / "tokens.txt").write_bytes(b"x")
    await m.set_manual_path("asr", str(nfs))
    with pytest.raises(DownloadConflict):
        await m.start("asr")  # 已手动路径 → 不下载


async def test_manager_manual_path_validation(manager, tmp_path):
    m, _ = manager
    bad = tmp_path / "bad"
    bad.mkdir()
    with pytest.raises(DownloadConflict, match="缺少必需文件"):
        await m.set_manual_path("asr", str(bad))
    with pytest.raises(DownloadConflict, match="不存在"):
        await m.set_manual_path("asr", str(tmp_path / "no-such-dir"))


async def test_manager_delete(monkeypatch, manager):
    m, tmp = manager
    monkeypatch.setattr(ModelManager, "_run_download", _fake_run_success)
    await m.start("asr")
    await asyncio.sleep(0)
    assert (Path(tmp) / "models" / "asr" / "model.int8.onnx").is_file()
    await m.delete("asr")
    assert not (Path(tmp) / "models" / "asr").exists()
    # 已删除再删 → conflict
    with pytest.raises(DownloadConflict):
        await m.delete("asr")


async def test_manager_manual_path_delete_forbidden(manager, tmp_path):
    m, _ = manager
    nfs = tmp_path / "nfs-asr"
    nfs.mkdir()
    (nfs / "model.int8.onnx").write_bytes(b"x")
    (nfs / "tokens.txt").write_bytes(b"x")
    await m.set_manual_path("asr", str(nfs))
    with pytest.raises(DownloadConflict, match="手动路径"):
        await m.delete("asr")


# ========== /api/models 契约 ==========

async def test_api_models_get_default(client):
    resp = await client.get("/api/models")
    assert resp.status_code == 200
    models = resp.json()["models"]
    kinds = {m["kind"] for m in models}
    assert kinds == {"asr", "embedding"}
    asr = next(m for m in models if m["kind"] == "asr")
    assert asr["installed"] is False
    assert asr["mode"] == "local"  # 默认本地档
    assert asr["job"] is None
    assert asr["total_bytes"] > 200_000_000


async def test_api_models_download_conflict_when_manual(client, tmp_path):
    """已配手动路径 → download 400（不内置下载）。"""
    nfs = tmp_path / "nfs-asr"
    nfs.mkdir()
    (nfs / "model.int8.onnx").write_bytes(b"x")
    (nfs / "tokens.txt").write_bytes(b"x")
    r = await client.put("/api/models/asr/path", json={"path": str(nfs)})
    assert r.status_code == 200
    r2 = await client.post("/api/models/asr/download")
    assert r2.status_code == 400
    assert "手动" in r2.json()["detail"]


async def test_api_models_unknown_kind(client):
    assert (await client.post("/api/models/bogus/download")).status_code == 404
    assert (await client.put("/api/models/bogus/path", json={"path": "/x"})).status_code == 404
    assert (await client.get("/api/models/bogus/health")).status_code == 404


async def test_api_models_health_asr_ok(client, http_server):
    """服务端代探：端点指向可用的本地 http mock → ok。"""
    r = await client.put(
        "/api/settings",
        json={"local": {"local_asr_base_url": http_server.rstrip("/")}},
    )
    assert r.status_code == 200
    body = (await client.get("/api/models/asr/health")).json()
    assert body["kind"] == "asr"
    assert body["ok"] is True
    assert body["endpoint"].rstrip("/") == http_server.rstrip("/")
    assert body["latency_ms"] >= 0


async def test_api_models_health_asr_down(client):
    """端点无服务（端口 1 拒绝连接）→ ok False + 可读 error。"""
    await client.put(
        "/api/settings",
        json={"local": {"local_asr_base_url": "http://127.0.0.1:1"}},
    )
    body = (await client.get("/api/models/asr/health")).json()
    assert body["ok"] is False
    assert body["error"]


async def test_api_models_health_embedding(client):
    """embedding 无外部服务 → 回显 installed。"""
    body = (await client.get("/api/models/embedding/health")).json()
    assert body["kind"] == "embedding"
    assert body["ok"] is False  # 默认未安装


async def test_api_models_path_persists_runtime_env(client, tmp_path):
    nfs = tmp_path / "nfs-asr"
    nfs.mkdir()
    (nfs / "model.int8.onnx").write_bytes(b"x")
    (nfs / "tokens.txt").write_bytes(b"x")
    r = await client.put("/api/models/asr/path", json={"path": str(nfs)})
    assert r.status_code == 200
    # runtime.env 已持久化
    from app.core.runtime_config import load_runtime_env

    assert load_runtime_env(str(tmp_path))["LOCAL_ASR_MODEL_DIR"] == str(nfs)
    # GET /api/models 回显 manual
    g = (await client.get("/api/models")).json()["models"]
    asr = next(m for m in g if m["kind"] == "asr")
    assert asr["manual_path"] == str(nfs)
    # GET /api/settings local 组同步
    gs = (await client.get("/api/settings")).json()["local"]
    assert gs["local_asr_model_dir"] == str(nfs)


async def test_api_models_path_clear(client, tmp_path):
    """空串清除手动路径。"""
    nfs = tmp_path / "nfs-asr"
    nfs.mkdir()
    (nfs / "model.int8.onnx").write_bytes(b"x")
    (nfs / "tokens.txt").write_bytes(b"x")
    await client.put("/api/models/asr/path", json={"path": str(nfs)})
    r = await client.put("/api/models/asr/path", json={"path": ""})
    assert r.status_code == 200
    g = (await client.get("/api/models")).json()["models"]
    asr = next(m for m in g if m["kind"] == "asr")
    assert asr["manual_path"] is None or asr["manual_path"] == ""


# ========== embedding installed_bytes 统计（不误计兄弟目录）==========

def _seed_asr_model(models_dir):
    """在 models_dir/asr 落 ASR 文件（污染源：早期 bug 把整目录计入 embedding）。"""
    d = models_dir / "asr"
    d.mkdir(parents=True, exist_ok=True)
    (d / "model.int8.onnx").write_bytes(b"A" * 1024)
    (d / "tokens.txt").write_bytes(b"B" * 256)
    return 1024 + 256


async def test_manager_status_all_embedding_size_ignores_sibling(manager):
    """未安装 embedding 时 installed_bytes 应为 0，不得计入兄弟目录（models/asr）。"""
    m, tmp = manager
    models_dir = Path(tmp) / "models"
    asr_bytes = _seed_asr_model(models_dir)
    assert asr_bytes > 0
    status = {s["kind"]: s for s in m.status_all()}
    emb = status["embedding"]
    assert emb["installed"] is False
    assert emb["installed_bytes"] == 0  # 修复前会误返回 asr_bytes
    asr = status["asr"]
    assert asr["installed"] is True  # ASR 文件齐 → 已安装
    assert asr["installed_bytes"] == asr_bytes  # ASR 自己仍应正确统计


async def test_manager_status_all_embedding_hf_snapshot_size(manager):
    """HF 快照命中时只统计 models--* 缓存，兄弟大目录不计入。"""
    m, tmp = manager
    models_dir = Path(tmp) / "models"
    _seed_asr_model(models_dir)  # 1280 B 兄弟目录（不应计入 embedding）
    snap = (
        models_dir
        / "models--Qdrant--bge-small-zh-v1.5"
        / "snapshots"
        / "deadbeef"
    )
    snap.mkdir(parents=True)
    (snap / "model_optimized.onnx").write_bytes(b"O" * 2048)
    (snap / "tokenizer.json").write_bytes(b"T" * 64)
    (snap / "config.json").write_bytes(b"C" * 32)
    status = {s["kind"]: s for s in m.status_all()}
    emb = status["embedding"]
    assert emb["installed"] is True
    assert emb["installed_bytes"] == 2048 + 64 + 32  # 仅快照，不含 asr 兄弟
