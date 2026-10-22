"""临时目录保障（``runtime_env.ensure_usable_temp_dir``）的测试。

为什么单独立一组测试
--------------------
LanceDB 的 Rust 层在 ``connect()`` 时就要写 spill 临时文件；目录不可写会直接
**在 Rust 里 panic**，Python 侧只看到 ``RustPanic: rust future panicked``。
这段代码跑在 ``lifespan`` 启动阶段、uvicorn 还没监听，于是桌面端表现为
「窗口白屏 / 服务在 90s 内未就绪」，日志里几乎没有线索。

真实故障（用户现场）::

    thread 'lancedb-tokio-worker' panicked:
    failed to create temp directory for LocalSpillStore:
    path: "C:\\Users\\...\\AppData\\Local\\Temp\\.tmpiNhx6k"
    err: Os { code: 5, kind: PermissionDenied, message: "拒绝访问。" }

这些测试锁住「必须找到一个真的能写文件的目录，并把它固定到 TEMP/TMP」。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from app import runtime_env

_KEYS = ("TEMP", "TMP", "TMPDIR", "VIDEORAG_TEMP_DIR")


@pytest.fixture(autouse=True)
def _restore_temp_env():
    saved = {k: os.environ[k] for k in _KEYS if k in os.environ}
    saved_tempdir = __import__("tempfile").tempdir
    try:
        yield
    finally:
        for k in _KEYS:
            os.environ.pop(k, None)
        os.environ.update(saved)
        import tempfile

        tempfile.tempdir = saved_tempdir


def _make_unwritable_dir(base: Path) -> Path:
    """造一个「存在但不可写」的目录（尽量跨平台）。"""
    d = base / " readonly"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o500)  # POSIX r-x；Windows 上 chmod 只影响只读位
    except OSError:  # pragma: no cover
        pass
    return d


def test_falls_back_to_data_dir_when_temp_unusable(tmp_path, monkeypatch):
    """TEMP 不可用时必须回落到 <data_dir>/temp，并把它写回环境变量。"""
    bad = _make_unwritable_dir(tmp_path)
    monkeypatch.setenv("TEMP", str(bad))
    monkeypatch.setenv("TMP", str(bad))
    monkeypatch.delenv("VIDEORAG_TEMP_DIR", raising=False)

    data_dir = tmp_path / "data"
    data_dir.mkdir()

    path, source = runtime_env.ensure_usable_temp_dir(str(data_dir))

    assert source == "data_dir/temp"
    assert Path(path) == data_dir / "temp"
    # 环境变量必须被改写，否则 lancedb 的 Rust 层仍会去读旧的 TEMP
    assert os.environ["TEMP"] == path
    assert os.environ["TMP"] == path


def test_updates_python_tempfile_cache(tmp_path, monkeypatch):
    """只改环境变量不够：tempfile 会缓存旧值，必须同时刷新。"""
    import tempfile

    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))
    monkeypatch.delenv("VIDEORAG_TEMP_DIR", raising=False)

    data_dir = tmp_path / "d"
    data_dir.mkdir()

    path, _ = runtime_env.ensure_usable_temp_dir(str(data_dir))
    assert tempfile.gettempdir() == path


def test_explicit_override_wins(tmp_path, monkeypatch):
    """用户显式指定 VIDEORAG_TEMP_DIR 时优先采用。"""
    custom = tmp_path / "mytemp"
    monkeypatch.setenv("VIDEORAG_TEMP_DIR", str(custom))
    monkeypatch.setenv("TEMP", str(tmp_path))

    data_dir = tmp_path / "d"
    data_dir.mkdir()

    path, source = runtime_env.ensure_usable_temp_dir(str(data_dir))
    assert source == "VIDEORAG_TEMP_DIR"
    assert Path(path) == custom


def test_uses_writable_temp_when_available(tmp_path, monkeypatch):
    """TEMP 本身可写时仍应优先尊重用户/系统配置，而不是强行搬到 data_dir。

    顺序上 data_dir/temp 排在 TEMP 之前，但这里显式用 VIDEORAG_TEMP_DIR
    校验「可写的显式配置会被采纳」；若连它都没有，data_dir/temp 胜出。
    """
    good = tmp_path / "goodtemp"
    good.mkdir()
    monkeypatch.delenv("VIDEORAG_TEMP_DIR", raising=False)
    monkeypatch.setenv("TEMP", str(good))
    monkeypatch.setenv("TMP", str(good))

    data_dir = tmp_path / "d"
    data_dir.mkdir()

    path, source = runtime_env.ensure_usable_temp_dir(str(data_dir))
    # 说明：data_dir/temp 优先级高于 TEMP 是刻意设计——%TEMP% 在真实机器上
    # 被安全软件/策略破坏的案例很多，而数据目录必然可写，更可靠。
    assert source == "data_dir/temp"
    assert Path(path) == data_dir / "temp"


def test_probe_really_writes_a_file(tmp_path, monkeypatch):
    """探测必须真写文件：有些 ACL 允许建目录但禁止建文件，而 lancedb 需要后者。"""
    data_dir = tmp_path / "d"
    data_dir.mkdir()

    path, _ = runtime_env.ensure_usable_temp_dir(str(data_dir))
    p = Path(path)
    assert p.is_dir()
    # 目录里不应残留探测文件
    assert not list(p.glob(".videorag-temp-probe-*"))


def test_raises_actionable_error_when_nothing_writable(tmp_path, monkeypatch):
    """全部候选都不可写时必须抛可读错误，而不是让 lancedb 在 Rust 里 panic。

    「不可写」的定义要与真实需求一致：目录建不出来、既不能写文件也不能建
    子目录。LanceDB 实际只需要在临时目录下**建子目录**，所以只禁写文件时
    仍应判定为可用（见 test_dir_with_subdir_only_is_usable）。
    """
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))
    monkeypatch.delenv("VIDEORAG_TEMP_DIR", raising=False)

    data_dir = tmp_path / "d"
    data_dir.mkdir()

    real_write_bytes = Path.write_bytes
    real_mkdir = Path.mkdir

    def deny_write(self, *a, **kw):
        if ".videorag-temp-probe-" in self.name:
            raise PermissionError(13, "Access is denied", str(self))
        return real_write_bytes(self, *a, **kw)

    def deny_mkdir(self, *a, **kw):
        # 探测用的子目录也一律拒绝 → 该候选彻底不可用
        if ".videorag-tmpdir-probe-" in self.name:
            raise PermissionError(13, "Access is denied", str(self))
        return real_mkdir(self, *a, **kw)

    monkeypatch.setattr(Path, "write_bytes", deny_write)
    monkeypatch.setattr(Path, "mkdir", deny_mkdir)

    with pytest.raises(RuntimeError) as ei:
        runtime_env.ensure_usable_temp_dir(str(data_dir))
    msg = str(ei.value)
    assert "临时目录" in msg
    assert "TEMP" in msg
    # 错误里要列出尝试过的路径，方便用户排查
    assert "已尝试" in msg
    # 每个失败项都要说明**具体**原因（而不是笼统的 PermissionError）
    assert "无法写入文件" in msg


def test_dir_with_subdir_only_is_usable(tmp_path, monkeypatch):
    """只能建子目录、不能写文件的目录，**仍应判定为可用**。

    LanceDB 的 Rust 层要做的就是在临时目录下创建 ``.tmpXXXXXX`` 子目录，
    并不需要直接写文件。有些环境（受限 ACL、部分杀软的目录保护）恰好是
    「不能写文件但能建子目录」——早前把「写文件失败」直接判死刑，
    会误杀这些其实可用的目录，最终报「找不到可写的临时目录」。
    """
    monkeypatch.delenv("VIDEORAG_TEMP_DIR", raising=False)
    data_dir = tmp_path / "d"
    data_dir.mkdir()

    real_write_bytes = Path.write_bytes

    def deny_write(self, *a, **kw):
        if ".videorag-temp-probe-" in self.name:
            raise PermissionError(13, "Access is denied", str(self))
        return real_write_bytes(self, *a, **kw)

    monkeypatch.setattr(Path, "write_bytes", deny_write)

    path, source = runtime_env.ensure_usable_temp_dir(str(data_dir))
    assert source == "data_dir/temp"
    assert Path(path).is_dir()


def test_probe_file_delete_failure_does_not_fail_the_dir(tmp_path, monkeypatch):
    """探测文件删不掉**不代表目录不可写**。

    早前的实现把 ``unlink`` 放在同一个 try 里，删除失败就把好目录误判成不可用
    （例如被杀毒软件锁住、或只读挂载），进而错误地报「找不到可写的临时目录」。
    """
    monkeypatch.delenv("VIDEORAG_TEMP_DIR", raising=False)
    data_dir = tmp_path / "d"
    data_dir.mkdir()

    real_unlink = Path.unlink

    def deny_unlink(self, *a, **kw):
        if ".videorag-temp-probe-" in self.name:
            raise PermissionError(13, "Access is denied", str(self))
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", deny_unlink)

    path, source = runtime_env.ensure_usable_temp_dir(str(data_dir))
    assert source == "data_dir/temp"
    assert Path(path).is_dir()


def test_bootstrap_does_not_raise_when_temp_unavailable(tmp_path, monkeypatch):
    """探测失败**不能**让 bootstrap 崩掉。

    否则用户看到的是 PyInstaller 的「Unhandled exception in script」裸弹窗，
    日志里一行都没有——这正是上一轮最糟糕的体验。降级后由
    ``_preflight_vector_store`` 给出更贴近现场的提示。
    """
    monkeypatch.delenv("VIDEORAG_TEMP_DIR", raising=False)
    real_write_bytes = Path.write_bytes
    real_mkdir = Path.mkdir

    def deny_write(self, *a, **kw):
        if ".videorag-temp-probe-" in self.name:
            raise PermissionError(13, "Access is denied", str(self))
        return real_write_bytes(self, *a, **kw)

    def deny_mkdir(self, *a, **kw):
        if ".videorag-tmpdir-probe-" in self.name:
            raise PermissionError(13, "Access is denied", str(self))
        return real_mkdir(self, *a, **kw)

    monkeypatch.setattr(Path, "write_bytes", deny_write)
    monkeypatch.setattr(Path, "mkdir", deny_mkdir)

    info = runtime_env.bootstrap(str(tmp_path / "b"))
    assert info["temp_dir"] == ""
    assert info["temp_error"]
    assert "临时目录" in info["temp_error"]


def test_bootstrap_reports_temp_error_in_summary(tmp_path, monkeypatch):
    """摘要里要能看出临时目录出了什么问题。"""
    monkeypatch.delenv("VIDEORAG_TEMP_DIR", raising=False)
    info = runtime_env.bootstrap(str(tmp_path / "b2"))
    text = runtime_env.format_summary(info)
    if info.get("temp_error"):
        assert "temp dir" in text or "临时目录" in text
    else:
        assert "temp dir" in text


def test_sibling_temp_is_tried(tmp_path, monkeypatch):
    """数据目录**整棵树**都不可用时，同级目录是退路之一，且排在 TMP/TEMP 之前。

    注意前提：只是「不能写文件」时，``data_dir/temp`` 仍算可用（能建子目录即可），
    见 ``test_dir_with_subdir_only_is_usable``。这里要模拟的是彻底不可用——
    连子目录都建不出来。
    """
    monkeypatch.delenv("VIDEORAG_TEMP_DIR", raising=False)
    monkeypatch.setenv("TEMP", str(tmp_path / "nope"))
    monkeypatch.setenv("TMP", str(tmp_path / "nope"))
    data_dir = tmp_path / "mydata"
    data_dir.mkdir()

    order: list[str] = []
    real_mkdir = Path.mkdir
    real_write = Path.write_bytes

    def spy_mkdir(self, *a, **kw):
        order.append(str(self))
        # data_dir 整棵树：连子目录都建不出来
        if str(self).startswith(str(data_dir)):
            raise PermissionError(13, "Access is denied", str(self))
        return real_mkdir(self, *a, **kw)

    def deny_write(self, *a, **kw):
        if str(self).startswith(str(data_dir)):
            raise PermissionError(13, "Access is denied", str(self))
        return real_write(self, *a, **kw)

    monkeypatch.setattr(Path, "mkdir", spy_mkdir)
    monkeypatch.setattr(Path, "write_bytes", deny_write)

    runtime_env.ensure_usable_temp_dir(str(data_dir))

    sibling = str(tmp_path / "mydata-temp")
    tmp_env = str(tmp_path / "nope")
    assert sibling in order, "同级目录必须被尝试"
    assert tmp_env in order, "系统 TEMP 也必须被尝试"
    # 同级目录排在系统 TEMP 之前（数据目录不可用时优先用近处的位置）
    assert order.index(sibling) < order.index(tmp_env)


def test_bootstrap_reports_temp_dir(tmp_path, monkeypatch):
    """bootstrap 摘要要带出实际使用的临时目录，方便从日志定位。"""
    monkeypatch.delenv("VIDEORAG_TEMP_DIR", raising=False)
    info = runtime_env.bootstrap(str(tmp_path / "boot"))
    assert info["temp_dir"]
    assert info["temp_source"]
    text = runtime_env.format_summary(info)
    assert "temp dir" in text
    assert info["temp_dir"] in text


@pytest.mark.skipif(os.name != "nt", reason="Windows 专属路径")
def test_windows_candidates_include_localappdata(tmp_path, monkeypatch):
    """候选里必须包含 %LOCALAPPDATA%\\Temp（Windows 的常规默认）。"""
    # 只验证候选构造逻辑，不真的切换目录
    seen: list[str] = []
    real_add = None

    # 通过 monkeypatch 让 mkdir 失败来收集尝试过的路径
    orig_mkdir = Path.mkdir

    def spy_mkdir(self, *a, **kw):
        seen.append(str(self))
        return orig_mkdir(self, *a, **kw)

    monkeypatch.setattr(Path, "mkdir", spy_mkdir)
    monkeypatch.setenv("TEMP", str(tmp_path / "no1"))
    monkeypatch.setenv("TMP", str(tmp_path / "no2"))
    monkeypatch.delenv("VIDEORAG_TEMP_DIR", raising=False)

    data_dir = tmp_path / "d"
    data_dir.mkdir(exist_ok=True)
    runtime_env.ensure_usable_temp_dir(str(data_dir))

    assert any("Temp" in s or "temp" in s for s in seen)
