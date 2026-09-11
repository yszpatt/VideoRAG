import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Index, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Video(TimestampMixin, Base):
    __tablename__ = "videos"

    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    platform: Mapped[str]
    url: Mapped[str]
    title: Mapped[str | None] = mapped_column(default=None)
    author: Mapped[str | None] = mapped_column(default=None)
    duration_sec: Mapped[float | None] = mapped_column(Float, default=None)
    # pending|fetching|transcribing|noting|embedding|done|failed
    status: Mapped[str] = mapped_column(default="pending")
    note_path: Mapped[str | None] = mapped_column(default=None)
    error: Mapped[str | None] = mapped_column(default=None)

    # E1 元数据（v2；老库列由 app/db.py _ensure_columns 补齐）
    description: Mapped[str | None] = mapped_column(Text, default=None)
    cover_url: Mapped[str | None] = mapped_column(default=None)
    upload_date: Mapped[str | None] = mapped_column(default=None)  # "YYYYMMDD"
    view_count: Mapped[int | None] = mapped_column(default=None)
    like_count: Mapped[int | None] = mapped_column(default=None)
    tags: Mapped[list | None] = mapped_column(JSON, default=None)
    comment_count: Mapped[int | None] = mapped_column(default=None)
    meta_source: Mapped[str | None] = mapped_column(default=None)  # ytdlp|none

    tasks: Mapped[list["Task"]] = relationship(back_populates="video", lazy="selectin")
    segments: Mapped[list["Segment"]] = relationship(back_populates="video", lazy="selectin")
    chunks: Mapped[list["Chunk"]] = relationship(back_populates="video", lazy="selectin")
    note: Mapped["Note | None"] = relationship(back_populates="video", lazy="selectin")
    comments: Mapped[list["Comment"]] = relationship(back_populates="video")


class Task(TimestampMixin, Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"))
    # fetch|transcribe|note|embed
    type: Mapped[str]
    status: Mapped[str] = mapped_column(default="pending")  # pending|running|done|failed
    progress: Mapped[float] = mapped_column(Float, default=0)
    error: Mapped[str | None] = mapped_column(default=None)
    payload: Mapped[dict | None] = mapped_column(JSON, default=None)

    video: Mapped[Video] = relationship(back_populates="tasks", lazy="selectin")


class Segment(TimestampMixin, Base):
    __tablename__ = "segments"

    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"))
    start_sec: Mapped[float] = mapped_column(Float)
    end_sec: Mapped[float] = mapped_column(Float)
    text: Mapped[str] = mapped_column(Text)
    speaker: Mapped[str | None] = mapped_column(default=None)  # 预留 diarization
    # E3：来源（speech=语音|subtitle=字幕|ocr=画面文字|vlm=画面描述），老库由 db._ensure_columns 补齐
    source: Mapped[str] = mapped_column(default="speech")

    video: Mapped[Video] = relationship(back_populates="segments", lazy="selectin")


class Chunk(TimestampMixin, Base):
    __tablename__ = "chunks"

    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"))
    content: Mapped[str] = mapped_column(Text)
    start_sec: Mapped[float] = mapped_column(Float)
    end_sec: Mapped[float] = mapped_column(Float)
    # content=口播切片（带时间戳）| meta=视频级简介/标题分片（start/end 为 0）
    kind: Mapped[str] = mapped_column(default="content")
    meta: Mapped[dict | None] = mapped_column(JSON, default=None)
    lancedb_id: Mapped[str | None] = mapped_column(default=None)

    video: Mapped[Video] = relationship(back_populates="chunks", lazy="selectin")


class Note(TimestampMixin, Base):
    __tablename__ = "notes"

    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), unique=True)
    summary: Mapped[str | None] = mapped_column(Text, default=None)
    chapters: Mapped[list | None] = mapped_column(JSON, default=None)
    key_points: Mapped[list | None] = mapped_column(JSON, default=None)
    quotes: Mapped[list | None] = mapped_column(JSON, default=None)
    glossary: Mapped[list | None] = mapped_column(JSON, default=None)
    markdown: Mapped[str | None] = mapped_column(Text, default=None)

    video: Mapped[Video] = relationship(back_populates="note", lazy="selectin")


class Comment(TimestampMixin, Base):
    """E1 热评（v2）：抓取上限 100 条，详情展示 top5、笔记注入 top5。"""

    __tablename__ = "comments"

    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), index=True)
    author: Mapped[str | None] = mapped_column(default=None)
    text: Mapped[str] = mapped_column(Text)
    like_count: Mapped[int] = mapped_column(default=0)
    published_at: Mapped[str | None] = mapped_column(default=None)  # ISO8601

    video: Mapped[Video] = relationship(back_populates="comments", lazy="selectin")


class QueryHistory(TimestampMixin, Base):
    """E5 历史提问/检索（v2）：跨设备持久化、去重计数、环形淘汰。

    query_key = kind + "\\x00" + query[:500]，作为去重唯一键（对超长 query
    只取前 500 字符入唯一约束）。
    """

    __tablename__ = "query_history"
    __table_args__ = (
        Index("ix_query_history_kind_used", "kind", "last_used_at"),
        Index("uq_query_history_kind_query", "kind", "query_key", unique=True),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column()  # ask | search
    query: Mapped[str] = mapped_column(Text)
    query_key: Mapped[str] = mapped_column(default="")  # kind\x00query[:500]
    top_k: Mapped[int | None] = mapped_column(default=None)
    # ask：答案前 2000 字 + citations 原样（带 url）
    answer: Mapped[str | None] = mapped_column(Text, default=None)
    citations_json: Mapped[list | None] = mapped_column(JSON, default=None)
    # search：命中条数
    hits_count: Mapped[int | None] = mapped_column(default=None)
    hit_count: Mapped[int] = mapped_column(default=1)  # 相同问题执行次数
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
