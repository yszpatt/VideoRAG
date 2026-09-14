"""已下载媒体归档（app/core/media_archive.py）单元测试。"""

from pathlib import Path

from app.core.media_archive import (
    archive_media,
    build_archive_path,
    ensure_archive_dir,
    is_in_temp_media_dir,
    sanitize_filename,
)


# ============ 文件名清洗 ============


def test_sanitize_filename_replaces_illegal_chars():
    assert sanitize_filename('a<b>c:d"e/f\\g|h?i*j') == "a_b_c_d_e_f_g_h_i_j"


def test_sanitize_filename_strips_control_chars_and_newlines():
    assert sanitize_filename("前\n段\t后") == "前 段 后"


def test_sanitize_filename_folds_whitespace_and_strips_ends():
    assert sanitize_filename("  hello \n\t world  ") == "hello world"


def test_sanitize_filename_truncates_and_drops_trailing_dots():
    assert len(sanitize_filename("x" * 200)) == 80
    assert sanitize_filename("abc....") == "abc"
    # 截断处恰好是点号时需再次清理（Windows 尾随点号不合法）→ 80 字符截为 79
    assert sanitize_filename("a" * 79 + "...." + "b" * 10) == "a" * 79


def test_sanitize_filename_empty_and_chinese():
    assert sanitize_filename("") == ""
    assert sanitize_filename(None) == ""
    assert sanitize_filename("中文标题·测试（第 1 期）") == "中文标题·测试（第 1 期）"


# ============ 目标路径计算 ============


def test_build_archive_path_uses_title_id_and_source_ext(tmp_path):
    p = build_archive_path(tmp_path, "vid1", "我的标题", "/x/audio.mp3")
    assert p.parent == tmp_path
    assert p.name == "我的标题-vid1.mp3"


def test_build_archive_path_falls_back_to_id_when_title_empty(tmp_path):
    assert build_archive_path(tmp_path, "vid1", None, "v.mp4").name == "vid1.mp4"
    assert build_archive_path(tmp_path, "vid1", "   ", "v.mp4").name == "vid1.mp4"


def test_build_archive_path_keeps_extensions_apart(tmp_path):
    mp3 = build_archive_path(tmp_path, "v", "T", "a.mp3")
    mp4 = build_archive_path(tmp_path, "v", "T", "a.mp4")
    assert mp3.name == "T-v.mp3"
    assert mp4.name == "T-v.mp4"


def test_build_archive_path_falls_back_to_bin_without_ext(tmp_path):
    assert build_archive_path(tmp_path, "v", "T", "noext").name == "T-v.bin"


# ============ 临时目录冲突判定 ============


def test_is_in_temp_media_dir():
    root = "/data"
    assert is_in_temp_media_dir(Path("/data/downloads"), root) is True
    assert is_in_temp_media_dir(Path("/data/frames/vid1"), root) is True
    assert is_in_temp_media_dir(Path("/data/transcripts"), root) is True
    # 数据根目录本身与正式目录不在清扫范围
    assert is_in_temp_media_dir(Path("/data"), root) is False
    assert is_in_temp_media_dir(Path("/data/notes"), root) is False
    assert is_in_temp_media_dir(Path("/media"), root) is False
    # data_dir 未提供时不判定
    assert is_in_temp_media_dir(Path("/data/downloads"), None) is False


# ============ 归档主流程 ============


def test_archive_media_disabled_when_dir_empty(tmp_path):
    src = tmp_path / "audio.mp3"
    src.write_bytes(b"data")
    assert archive_media(str(src), "", "vid1", "T") is None
    assert archive_media(str(src), None, "vid1", "T") is None
    # 默认关闭：源文件不受影响
    assert src.read_bytes() == b"data"


def test_archive_media_copies_and_keeps_source(tmp_path):
    src = tmp_path / "audio.mp3"
    src.write_bytes(b"hello-audio")
    save_dir = tmp_path / "archive"

    out = archive_media(str(src), str(save_dir), "vid1", "我的标题", data_dir=str(tmp_path))

    assert out is not None
    archived = Path(out)
    assert archived.name == "我的标题-vid1.mp3"
    assert archived.read_bytes() == b"hello-audio"
    # 复制而非移动：源临时文件仍由调用方（finally）清理
    assert src.exists()


def test_archive_media_skips_existing_without_overwrite(tmp_path):
    src = tmp_path / "audio.mp3"
    src.write_bytes(b"new")
    save_dir = tmp_path / "archive"
    save_dir.mkdir()
    existing = save_dir / "T-vid1.mp3"
    existing.write_bytes(b"old")

    assert archive_media(str(src), str(save_dir), "vid1", "T") is None
    assert existing.read_bytes() == b"old"  # 已存在则跳过，不覆盖


def test_archive_media_isolates_missing_source(tmp_path):
    assert archive_media(str(tmp_path / "nope.mp3"), str(tmp_path / "a"), "v", "T") is None


def test_archive_media_isolates_file_blocked_dir(tmp_path):
    """目录不可写/路径被文件占用 → 只告警返回 None，不抛异常。"""
    src = tmp_path / "audio.mp3"
    src.write_bytes(b"x")
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")  # 同名文件占位 → mkdir 抛 NotADirectoryError
    assert archive_media(str(src), str(blocker / "sub"), "v", "T") is None


def test_archive_media_rejects_temp_media_dirs(tmp_path):
    """归档目录落在启动清扫目录内 → 拒绝归档（否则下次启动被删）。"""
    src = tmp_path / "audio.mp3"
    src.write_bytes(b"x")
    downloads = tmp_path / "downloads"

    assert archive_media(str(src), str(downloads), "vid1", "T", data_dir=str(tmp_path)) is None
    assert not (downloads / "T-vid1.mp3").exists()


# ============ 启动校验 ============


def test_ensure_archive_dir_creates_and_probes(tmp_path):
    target = tmp_path / "archive"
    assert ensure_archive_dir(str(target), str(tmp_path)) is True
    assert target.is_dir()
    assert list(target.iterdir()) == []  # 写探针文件已清理


def test_ensure_archive_dir_disabled_or_conflicting(tmp_path):
    assert ensure_archive_dir("", str(tmp_path)) is False
    assert ensure_archive_dir(None, str(tmp_path)) is False
    # 命中临时清扫目录 → 拒绝
    assert ensure_archive_dir(str(tmp_path / "downloads"), str(tmp_path)) is False


def test_ensure_archive_dir_unavailable_path(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    assert ensure_archive_dir(str(blocker / "sub"), str(tmp_path)) is False
