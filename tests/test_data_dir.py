"""数据目录可用性保障（``runtime_env.ensure_usable_data_dir``）的测试。

为什么需要这一组
----------------
默认数据目录是 ``%LOCALAPPDATA%\\videoRAG``。它在受限公司环境里可能**不可写**
（组策略、杀软接管、ACL 异常）。此时应用会卡在 LanceDB 建库阶段，表现为
「窗口白屏 / 服务 90 秒未就绪」，而真正的原因不会出现在任何日志里——
上一轮用户反馈的「找不到可写的临时目录」就是同源问题。

这些测试锁住两件事：

1. 首选位置不可用时，**自动退到备选位置**并把决定记下来；
2. 退避过程本身不得抛未定义异常（曾经因为漏了 ``log`` 定义，抛
   ``NameError: name 'log' is not defined``，把降级逻辑变成新的崩溃点）。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from app import runtime_env

_KEYS = ("DATA_DIR", "VIDEORAG_DATA_DIR", "VIDEORAG_PORTABLE")


@pytest.fixture(autouse=True)
def _restore_env():
    saved = {k: os.environ[k] for k in _KEYS if k in os.environ}
    for k in _KEYS:
        os.environ.pop(k, None)
    try:
        yield
    finally:
        for k in _KEYS:
            os.environ.pop(k, None)
        os.environ.update(saved)


def _deny_writes_under(root: Path, marker: str):
    """让 ``root`` 下的写入与建子目录都失败。"""
    real_write = Path.write_bytes
    real_mkdir = Path.mkdir

    def deny_write(self, *a, **kw):
        if str(self).startswith(str(root)) and marker in self.name:
            raise PermissionError(13, "Access is denied", str(self))
        return real_write(self, *a, **kw)

    def deny_mkdir(self, *a, **kw):
        if str(self).startswith(str(root)) and marker in self.name:
            raise PermissionError(13, "Access is denied", str(self))
        return real_mkdir(self, *a, **kw)

    return deny_write, deny_mkdir


def test_preferred_dir_is_used_when_writable(tmp_path):
    target = tmp_path / "videoRAG"
    path, source = runtime_env.ensure_usable_data_dir(str(target))
    assert Path(path) == target
    assert source == "preferred"


def test_falls_back_when_preferred_unwritable(tmp_path, monkeypatch):
    """首选不可写时退到备选位置，并返回来源说明。"""
    preferred = tmp_path / "videoRAG"
    deny_write, deny_mkdir = _deny_writes_under(preferred, ".videorag-data-probe-")
    monkeypatch.setattr(Path, "write_bytes", deny_write)
    monkeypatch.setattr(Path, "mkdir", deny_mkdir)

    path, source = runtime_env.ensure_usable_data_dir(str(preferred))
    assert Path(path) != preferred
    assert source != "preferred"
    assert Path(path).is_dir()


def test_fallback_logs_the_decision(tmp_path, monkeypatch, caplog):
    """降级决定必须写进日志：用户要能知道数据被放到哪儿了。

    回归锁：曾经降级路径里用了未定义的 ``log``，抛
    ``NameError: name 'log' is not defined``，把「优雅降级」变成「新的崩溃」。
    """
    preferred = tmp_path / "videoRAG"
    deny_write, deny_mkdir = _deny_writes_under(preferred, ".videorag-data-probe-")
    monkeypatch.setattr(Path, "write_bytes", deny_write)
    monkeypatch.setattr(Path, "mkdir", deny_mkdir)

    with caplog.at_level(logging.WARNING, logger="videorag.runtime"):
        path, _ = runtime_env.ensure_usable_data_dir(str(preferred))

    assert any("不可用" in r.message for r in caplog.records), "降级必须留痕"
    # 日志里应能看到实际使用的目录
    assert any(str(path) in r.message for r in caplog.records)


def test_raises_with_actionable_message_when_all_fail(tmp_path, monkeypatch):
    """全都不可用时抛可读错误，含「怎么修」的指引。"""
    preferred = tmp_path / "videoRAG"

    # 让**所有**候选都不可用：统一拦截「探测文件」的写入与建目录
    real_write = Path.write_bytes
    real_mkdir = Path.mkdir

    def deny_write(self, *a, **kw):
        if ".videorag-data-probe-" in self.name:
            raise PermissionError(13, "Access is denied", str(self))
        return real_write(self, *a, **kw)

    def deny_mkdir(self, *a, **kw):
        if ".videorag-data-probe-" in self.name:
            raise PermissionError(13, "Access is denied", str(self))
        return real_mkdir(self, *a, **kw)

    monkeypatch.setattr(Path, "write_bytes", deny_write)
    monkeypatch.setattr(Path, "mkdir", deny_mkdir)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(runtime_env, "bundle_root", lambda: tmp_path / "bundle")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(RuntimeError) as ei:
        runtime_env.ensure_usable_data_dir(str(preferred))
    msg = str(ei.value)
    assert "数据目录" in msg
    assert "--data-dir" in msg
    assert "VIDEORAG_DATA_DIR" in msg


def test_bootstrap_reports_data_source(tmp_path):
    """bootstrap 摘要要带出数据目录来源。"""
    info = runtime_env.bootstrap(str(tmp_path / "boot"))
    assert info["data_source"] == "preferred"
    text = runtime_env.format_summary(info)
    assert "data dir" in text
    assert str(info["data_dir"]) in text


def test_bootstrap_uses_fallback_data_dir(tmp_path, monkeypatch):
    """首选数据目录不可用时，bootstrap 应改用备选目录并更新 DATA_DIR。"""
    preferred = tmp_path / "videoRAG"
    deny_write, deny_mkdir = _deny_writes_under(preferred, ".videorag-data-probe-")
    monkeypatch.setattr(Path, "write_bytes", deny_write)
    monkeypatch.setattr(Path, "mkdir", deny_mkdir)

    info = runtime_env.bootstrap(str(preferred))
    assert Path(info["data_dir"]) != preferred
    # 环境变量要跟着更新，否则 Settings 仍会用旧路径
    assert os.environ["DATA_DIR"] == info["data_dir"]
