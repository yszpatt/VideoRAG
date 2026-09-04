"""本地模型下载管理器：任务状态机 + 互斥 + 进度。

- 进程内单例（uvicorn 单进程，与现有组件模式一致）；任务对象存内存，
  下载中断重启容器 → 任务丢失但 `.part` 保留，用户 retry 即续传（不落库）。
- 互斥：同一 kind 同时只允许一个下载任务（409）。
- 取消 = 置 cancel_event，下载协程抛 CancelledError 中止并保留 `.part`。
- 状态机：idle → downloading → (done | failed | cancelled)；failed --retry--> downloading。

设计来源：docs/plans/2026-09-03-local-fallback-design.md §3.4。
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.config import Settings
from app.core.local_models import registry
from app.core.local_models.downloader import DownloadError, download_file
from app.core.local_models.registry import ModelSpec, get_spec

log = logging.getLogger(__name__)

JOB_STATES = ("idle", "downloading", "verifying", "done", "failed", "cancelled")


@dataclass
class DownloadJob:
    kind: str
    state: str = "idle"
    stage: str = ""                 # 当前阶段：downloading / verifying
    file_current: int = 0           # 第几个文件
    file_total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    speed_bps: float = 0.0
    error: str = ""
    started_at: float = 0.0
    updated_at: float = 0.0
    _task: asyncio.Task | None = field(default=None, repr=False)
    _cancel: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def snapshot(self) -> dict:
        return {
            "kind": self.kind,
            "state": self.state,
            "stage": self.stage,
            "file_current": self.file_current,
            "file_total": self.file_total,
            "bytes_done": self.bytes_done,
            "bytes_total": self.bytes_total,
            "speed_bps": int(self.speed_bps),
            "error": self.error,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "pct": round(self.bytes_done / self.bytes_total * 100, 1)
            if self.bytes_total else 0.0,
        }


class ModelManager:
    """单 kind 互斥的下载任务管理器（进程内单例）。"""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._jobs: dict[str, DownloadJob] = {
            k: DownloadJob(kind=k) for k in registry.KINDS
        }

    def refresh_settings(self, settings: Settings) -> None:
        """设置热更新后同步引用（/api/settings PUT 时调用）。"""
        self._settings = settings

    # ---- 状态查询 ----

    def job(self, kind: str) -> DownloadJob | None:
        return self._jobs.get(kind)

    def status_all(self) -> list[dict]:
        out = []
        for kind in registry.KINDS:
            spec = get_spec(kind)
            s = self._settings
            if kind == "asr":
                model_dir = Path(s.asr_model_dir_effective)
                manual = bool(s.local_asr_model_dir)
                installed, size = self._check_files(model_dir, spec.required)
                endpoint = s.asr_local_endpoint
            else:
                model_dir = Path(s.embed_model_dir_effective)
                manual = bool(s.local_embed_model_dir)
                installed = self._embedding_installed(model_dir, spec)
                size = self._embedding_size(model_dir, spec)
                endpoint = ""
            out.append({
                "kind": kind,
                "label": spec.label,
                "installed": installed,
                "installed_bytes": size,
                "manual_path": s.local_asr_model_dir if kind == "asr" else s.local_embed_model_dir,
                "manual": manual,
                "mode": s.asr_mode if kind == "asr" else s.embed_mode,
                "endpoint": endpoint,
                "total_bytes": spec.total_size,
                "job": self._jobs[kind].snapshot() if self._jobs[kind].state != "idle" else None,
            })
        return out

    @staticmethod
    def _check_files(model_dir: Path, required: tuple[str, ...]) -> tuple[bool, int]:
        if not model_dir.is_dir():
            return False, 0
        missing = [f for f in required if not (model_dir / f).is_file()]
        return (not missing), _dir_size(model_dir)

    @staticmethod
    def _embedding_installed(model_dir: Path, spec: ModelSpec) -> bool:
        """embedding 就绪判定（双布局）：直接含必需文件 或 HF 缓存命中。"""
        direct = all((model_dir / f).is_file() for f in spec.hf_files)
        return direct or ModelManager._hf_snapshot_present(model_dir, spec)

    @staticmethod
    def _embedding_size(model_dir: Path, spec: ModelSpec) -> int:
        """embedding 实际占用字节（双布局）：

        - 直接含必需文件 → 整个目录即模型目录（specific_model_path 布局），统计全量；
        - 否则视为 fastembed HF 缓存根 → 只统计 models--* 快照目录，
          避免把兄弟子目录（如 models/asr 的 SenseVoice 模型）误计入。
        - 均未命中 → 0（未安装）。
        """
        if not model_dir.is_dir():
            return 0
        if all((model_dir / f).is_file() for f in spec.hf_files):
            return _dir_size(model_dir)
        total = 0
        for child in model_dir.iterdir():
            if child.is_dir() and child.name.startswith("models--"):
                total += _dir_size(child)
        return total

    @staticmethod
    def _hf_snapshot_present(cache_dir: Path, spec: ModelSpec) -> bool:
        """fastembed HF hub 缓存命中判定：cache_dir/models--{org}--{name}/ 下必需文件齐。"""
        if not cache_dir.is_dir():
            return False
        for child in cache_dir.iterdir():
            if child.is_dir() and child.name.startswith("models--"):
                # 递归找快照内必需文件（blobs 以 sha 命名，只按文件名存在性粗判即可：
                # 命中即认为已下载——精确校验交给 fastembed 加载期）
                found = {f: False for f in spec.hf_files}
                for p in child.rglob("*"):
                    if p.is_file() and p.name in found:
                        found[p.name] = True
                if all(found.values()):
                    return True
        return False

    # ---- 动作 ----

    async def start(self, kind: str) -> DownloadJob:
        spec = get_spec(kind)
        if spec is None:
            raise KeyError(kind)
        job = self._jobs[kind]
        if job.state in ("downloading", "verifying"):
            raise DownloadBusy(kind)
        s = self._settings
        # 手动路径已配 → 不需要内置下载
        manual = bool(s.local_asr_model_dir) if kind == "asr" else bool(s.local_embed_model_dir)
        if manual:
            raise DownloadConflict(f"{kind} 已配置手动模型路径，无需内置下载")

        # 已安装 → 幂等返回 done（不重复下载）
        model_dir = Path(s.asr_model_dir_effective if kind == "asr" else s.embed_model_dir_effective)
        if kind == "asr":
            installed, _ = self._check_files(model_dir, spec.required)
        else:
            installed = self._embedding_installed(model_dir, spec)
        if installed:
            job.state, job.updated_at = "done", time.time()
            return job

        # 磁盘预检（不足 1.1x 直接拒绝，避免下到一半爆盘）
        need = spec.total_size * 1.1
        try:
            free = shutil.disk_usage(str(model_dir.parent if model_dir.parent.exists() else model_dir)).free
        except OSError:
            free = float("inf")
        if free < need:
            raise DownloadConflict(
                f"磁盘空间不足：需要 ≥{need / 1e6:.0f} MB，剩余 {free / 1e6:.0f} MB"
            )

        # 复位并启动后台任务
        job.state, job.stage = "downloading", "downloading"
        job.file_current, job.file_total = 0, len(spec.files) or 1
        job.bytes_done, job.bytes_total = 0, spec.total_size
        job.speed_bps, job.error = 0.0, ""
        job.started_at = job.updated_at = time.time()
        job._cancel = asyncio.Event()
        job._task = asyncio.create_task(
            self._run_download(kind, spec, model_dir, job)
        )
        return job

    async def _run_download(
        self, kind: str, spec: ModelSpec, model_dir: Path, job: DownloadJob
    ) -> None:
        """后台下载循环：按 spec 下载全部文件并校验。"""
        try:
            if kind == "asr":
                await self._download_modelscope(spec, model_dir, job)
            else:
                await self._download_hf(spec, model_dir, job)
            job.state, job.stage = "done", ""
        except asyncio.CancelledError:
            job.state, job.stage = "cancelled", ""
            job.error = "已取消（.part 保留，可重试续传）"
        except DownloadError as e:
            job.state, job.stage = "failed", ""
            job.error = str(e)
            log.warning("model download %s failed: %s", kind, e)
        except Exception as e:  # noqa: BLE001
            job.state, job.stage = "failed", ""
            job.error = f"{type(e).__name__}: {e}"
            log.exception("model download %s crashed", kind)
        job.updated_at = time.time()

    async def _download_modelscope(
        self, spec: ModelSpec, model_dir: Path, job: DownloadJob
    ) -> None:
        urls = spec.resolve_urls
        for i, (f, url) in enumerate(zip(spec.files, urls), start=1):
            if job._cancel.is_set():
                raise asyncio.CancelledError()
            job.file_current, job.stage = i, f"downloading {f.path}"
            dest = model_dir / f.path
            base_done = job.bytes_done
            t0 = time.time()

            def _prog(done: int, _i=i, _f=f, _t0=t0, _base=base_done):
                # done = 当前文件已下载字节；累计 = 本文件前已完成 + 本文件进度
                job.bytes_done = _base + done
                job.speed_bps = done / max(time.time() - _t0, 1e-6)
                job.updated_at = time.time()

            await download_file(
                url, dest, expected_sha256=f.sha256,
                cancel_event=job._cancel, progress=_prog,
            )
        job.bytes_done = spec.total_size
        job.stage = "done"

    async def _download_hf(
        self, spec: ModelSpec, cache_dir: Path, job: DownloadJob
    ) -> None:
        """Embedding：huggingface_hub snapshot_download 到 fastembed 缓存根。

        在线程中执行（同步库）；进度按"文件级"汇报（3~5 个文件，大头单文件）。
        """
        from huggingface_hub import snapshot_download

        cache_dir.mkdir(parents=True, exist_ok=True)
        endpoint = self._settings.embed_download_endpoint or None
        env: dict[str, str] = {}
        if endpoint:
            import os
            env = {**os.environ, "HF_ENDPOINT": endpoint}

        job.file_total = len(spec.hf_files)
        job.stage = "枚举模型文件"

        def _snap():
            return snapshot_download(
                repo_id=spec.hf_repo,
                cache_dir=str(cache_dir),
                local_files_only=False,
                # huggingface_hub 无进程内进度回调；以目录字节增量近似
            )

        await asyncio.to_thread(_snap)
        if job._cancel.is_set():
            raise asyncio.CancelledError()
        job.file_current = job.file_total
        job.stage = "校验文件"
        if not self._hf_snapshot_present(cache_dir, spec):
            raise DownloadError(
                f"embedding 模型下载后校验失败：缺少必需文件 {list(spec.hf_files)}"
            )
        job.bytes_done = _dir_size(cache_dir)
        job.bytes_total = max(job.bytes_total, job.bytes_done)

    async def cancel(self, kind: str) -> DownloadJob:
        job = self._jobs.get(kind)
        if job is None or job.state not in ("downloading", "verifying"):
            raise DownloadNotFound(kind)
        job._cancel.set()
        if job._task:
            job._task.cancel()
        return job

    async def retry(self, kind: str) -> DownloadJob:
        job = self._jobs.get(kind)
        if job is None or job.state != "failed":
            raise DownloadConflict(f"{kind} 当前不可重试（state={job.state if job else 'none'}）")
        job.state, job.stage = "idle", ""
        return await self.start(kind)

    async def delete(self, kind: str) -> None:
        """删除已下载模型文件（仅自动管理路径；手动路径 / 下载中不可删）。"""
        spec = get_spec(kind)
        if spec is None:
            raise KeyError(kind)
        job = self._jobs[kind]
        if job.state in ("downloading", "verifying"):
            raise DownloadConflict(f"{kind} 正在下载，无法删除")
        s = self._settings
        manual = bool(s.local_asr_model_dir) if kind == "asr" else bool(s.local_embed_model_dir)
        if manual:
            raise DownloadConflict(f"{kind} 为手动路径，不可删除（可清空手动路径后删除）")
        model_dir = Path(s.asr_model_dir_effective if kind == "asr" else s.embed_model_dir_effective)
        if kind == "asr":
            removed = _remove_dir_children(model_dir, spec)
        else:
            removed = _remove_hf_cache(model_dir)
        if not removed:
            raise DownloadConflict(f"{kind} 模型未安装，无需删除")
        job.state = "idle"

    async def set_manual_path(self, kind: str, path: str) -> Settings:
        """手动指定本地模型目录（写 runtime.env 持久化；空串清除）。

        返回应用覆盖后的 Settings 新实例（调用方负责同步组件重建）。
        """
        if kind not in registry.KINDS:
            raise KeyError(kind)
        job = self._jobs[kind]
        if job.state in ("downloading", "verifying"):
            raise DownloadConflict(f"{kind} 正在下载，请先取消再改手动路径")
        path = (path or "").strip().rstrip("/")
        key = "LOCAL_ASR_MODEL_DIR" if kind == "asr" else "LOCAL_EMBED_MODEL_DIR"
        from app.core.runtime_config import save_runtime_env

        # 校验：非空路径须存在且文件齐（空 = 清除手动路径）
        if path:
            spec = get_spec(kind)
            p = Path(path)
            if not p.is_dir():
                raise DownloadConflict(f"路径不存在或不是目录：{path}")
            if kind == "asr":
                ok, _ = self._check_files(p, spec.required)
                if not ok:
                    raise DownloadConflict(
                        f"目录缺少必需文件 {list(spec.required)}：{path}"
                    )
            else:
                # embedding 双布局：直接含 onnx/tokenizer/config 的模型目录
                # （specific_model_path 直载），或 fastembed HF 缓存根（models--*）。
                if not self._embedding_installed(p, spec):
                    raise DownloadConflict(
                        f"目录既不是含 {list(spec.hf_files)} 的模型目录，"
                        f"也不是 fastembed HF 缓存根（models--*）：{path}"
                    )
        save_runtime_env(self._settings.data_dir, {key: path})
        # 返回应用覆盖后的新 settings（不修改 components，由调用方同步重建）
        updated = self._settings.apply_runtime({key: path})
        self._settings = updated
        return updated


# ---- 工具 ----

def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                total += p.stat().st_size
    except OSError:
        pass
    return total


def _remove_dir_children(model_dir: Path, spec: ModelSpec) -> bool:
    """删除 asr 模型目录下的 spec 文件（及残留 .part）。返回是否有删除动作。"""
    if not model_dir.is_dir():
        return False
    removed = False
    for f in spec.files:
        for p in (model_dir / f.path, model_dir / f"{f.path}.part"):
            if p.is_file():
                p.unlink()
                removed = True
    # 目录空则连目录一起清
    try:
        if model_dir.is_dir() and not any(model_dir.iterdir()):
            model_dir.rmdir()
    except OSError:
        pass
    return removed


def _remove_hf_cache(cache_dir: Path) -> bool:
    """删除 HF hub 缓存（models--* 目录）。返回是否有删除动作。"""
    if not cache_dir.is_dir():
        return False
    removed = False
    for child in cache_dir.iterdir():
        if child.is_dir() and child.name.startswith("models--"):
            shutil.rmtree(child, ignore_errors=True)
            removed = True
    return removed


# ---- 异常 ----

class DownloadConflict(RuntimeError):
    """业务冲突（409 语义）：已在下载 / 手动路径 / 磁盘不足 / 不可重试。"""


class DownloadBusy(DownloadConflict):
    """同 kind 已有进行中任务。"""


class DownloadNotFound(RuntimeError):
    """无此任务（404 语义）。"""
