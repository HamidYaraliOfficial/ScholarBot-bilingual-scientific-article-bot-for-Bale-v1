"""SQLAlchemy models. All timestamps are naive UTC."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (JSON, BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    bale_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(128))
    first_name: Mapped[Optional[str]] = mapped_column(String(128))
    language: Mapped[Optional[str]] = mapped_column(String(8))  # None until the user picks one
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    quota_daily: Mapped[Optional[int]] = mapped_column(Integer)  # None = global default
    state: Mapped[Optional[str]] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ArticleFile(Base):
    __tablename__ = "article_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    path: Mapped[str] = mapped_column(String(512))  # relative to storage/pdfs
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    page_count: Mapped[Optional[int]] = mapped_column(Integer)
    validation_status: Mapped[str] = mapped_column(String(16), default="valid")
    validation_note: Mapped[Optional[str]] = mapped_column(String(255))
    source: Mapped[Optional[str]] = mapped_column(String(64))
    license: Mapped[Optional[str]] = mapped_column(String(128))
    is_permanent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Article(Base):
    __tablename__ = "articles"
    id: Mapped[int] = mapped_column(primary_key=True)
    doi: Mapped[Optional[str]] = mapped_column(String(255), unique=True)
    title_hash: Mapped[Optional[str]] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(Text, default="")
    original_title: Mapped[Optional[str]] = mapped_column(Text)
    authors: Mapped[list] = mapped_column(JSON, default=list)
    year: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    journal: Mapped[Optional[str]] = mapped_column(String(512))
    volume: Mapped[Optional[str]] = mapped_column(String(64))
    issue: Mapped[Optional[str]] = mapped_column(String(64))
    pages: Mapped[Optional[str]] = mapped_column(String(64))
    issn: Mapped[Optional[str]] = mapped_column(String(64))
    publisher: Mapped[Optional[str]] = mapped_column(String(255))
    abstract: Mapped[Optional[str]] = mapped_column(Text)
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    work_type: Mapped[Optional[str]] = mapped_column(String(64))
    access_status: Mapped[str] = mapped_column(String(16), default="unknown")  # open|closed|unknown
    license: Mapped[Optional[str]] = mapped_column(String(128))
    landing_url: Mapped[Optional[str]] = mapped_column(String(1024))
    doi_url: Mapped[Optional[str]] = mapped_column(String(300))
    pdf_url: Mapped[Optional[str]] = mapped_column(String(1024))
    source: Mapped[Optional[str]] = mapped_column(String(64))
    metadata_sources: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="processing", index=True)
    file_id: Mapped[Optional[int]] = mapped_column(ForeignKey("article_files.id", ondelete="SET NULL"))
    last_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    last_requested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    file: Mapped[Optional[ArticleFile]] = relationship(lazy="joined")
    last_user: Mapped[Optional[User]] = relationship(lazy="joined")
    saved: Mapped[Optional["SavedArticle"]] = relationship(back_populates="article", lazy="joined", uselist=False,
                                                        cascade="all, delete-orphan")


class SavedArticle(Base):
    """Permanent library entry (admin curated)."""
    __tablename__ = "saved_articles"
    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id", ondelete="CASCADE"), unique=True)
    saved_by: Mapped[Optional[str]] = mapped_column(String(64))
    note: Mapped[Optional[str]] = mapped_column(Text)
    saved_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    article: Mapped[Article] = relationship(back_populates="saved")


class Bookmark(Base):
    """A user's own "⭐ saved articles" list inside the bot."""
    __tablename__ = "bookmarks"
    __table_args__ = (UniqueConstraint("user_id", "article_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DownloadJob(Base):
    __tablename__ = "download_jobs"
    __table_args__ = (Index("ix_jobs_user_created", "user_id", "created_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    chat_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    lang: Mapped[Optional[str]] = mapped_column(String(8))
    query_text: Mapped[str] = mapped_column(Text)
    query_type: Mapped[str] = mapped_column(String(16))  # doi|title|author|url|arxiv|pmid
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    article_id: Mapped[Optional[int]] = mapped_column(ForeignKey("articles.id", ondelete="SET NULL"))
    error_code: Mapped[Optional[str]] = mapped_column(String(32))
    error_detail: Mapped[Optional[str]] = mapped_column(Text)
    candidates: Mapped[Optional[list]] = mapped_column(JSON)
    source: Mapped[Optional[str]] = mapped_column(String(64))
    status_message_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    user: Mapped[Optional[User]] = relationship(lazy="joined")
    article: Mapped[Optional[Article]] = relationship(lazy="joined")


class Delivery(Base):
    __tablename__ = "deliveries"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("download_jobs.id", ondelete="SET NULL"))
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    article_id: Mapped[Optional[int]] = mapped_column(ForeignKey("articles.id", ondelete="SET NULL"), index=True)
    file_id: Mapped[Optional[int]] = mapped_column(ForeignKey("article_files.id", ondelete="SET NULL"))
    bale_message_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default="sent")
    error: Mapped[Optional[str]] = mapped_column(Text)
    size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Admin(Base):
    __tablename__ = "admins"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="admin")  # superadmin|admin|viewer
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    language: Mapped[str] = mapped_column(String(8), default="fa")
    theme: Mapped[str] = mapped_column(String(8), default="light")
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    admin_username: Mapped[Optional[str]] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[Optional[str]] = mapped_column(String(255))
    detail: Mapped[Optional[str]] = mapped_column(Text)
    ip: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ProviderConfig(Base):
    __tablename__ = "provider_configs"
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    last_status: Mapped[str] = mapped_column(String(16), default="unknown")  # ok|error|unknown
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_latency_ms: Mapped[Optional[int]] = mapped_column(Integer)
    total_requests: Mapped[int] = mapped_column(Integer, default=0)
    total_errors: Mapped[int] = mapped_column(Integer, default=0)
    total_hits: Mapped[int] = mapped_column(Integer, default=0)  # times it supplied the PDF


class ErrorEvent(Base):
    __tablename__ = "error_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)  # bot|provider|delivery|system
    source: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
