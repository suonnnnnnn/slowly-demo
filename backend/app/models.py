import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_platform: Mapped[str | None] = mapped_column(String(64))
    source_video_id: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(Text)
    uploader: Mapped[str | None] = mapped_column(Text)
    thumbnail_url: Mapped[str | None] = mapped_column(Text)
    duration_seconds: Mapped[float | None] = mapped_column(Float)

    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)

    storage_provider: Mapped[str | None] = mapped_column(String(32))
    bucket_name: Mapped[str | None] = mapped_column(String(255))
    object_key: Mapped[str | None] = mapped_column(Text)
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    mime_type: Mapped[str | None] = mapped_column(String(128))

    # 素材库分类。model=免费模型按标题与截图步骤判断；rule=标题关键词兜底。
    library_category: Mapped[str | None] = mapped_column(String(16))
    library_category_basis: Mapped[str | None] = mapped_column(String(16))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class User(Base):
    """Anonymous today, ready to be upgraded to a real account later."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="anonymous")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AnonymousSession(Base):
    """Only a SHA-256 digest is stored; the browser owns the bearer token."""

    __tablename__ = "anonymous_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserTutorial(Base):
    """A user's private relationship with a shared video/tutorial."""

    __tablename__ = "user_tutorials"
    __table_args__ = (
        UniqueConstraint("user_id", "video_id", name="uq_user_tutorial_user_video"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    video_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    saved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SearchCache(Base):
    __tablename__ = "search_cache"

    cache_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    query: Mapped[str] = mapped_column(String(100), nullable=False)
    backend: Mapped[str] = mapped_column(String(32), nullable=False, default="youtube")
    results_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)


class TutorialBreakdown(Base):
    """一次「分解」的记录。步骤每次重拆都会换一批，所以把这次用的依据单独存一条。

    这张表的唯一目的是**让「我们到底做了什么」可查**：
    真拆到了几次切点、几段、有没有用模型看画面、模型是谁。
    界面上那句诚实说明就是按它生成的，不允许凭空写。
    """

    __tablename__ = "tutorial_breakdowns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    video_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    # shots = 边界来自真实画面切换；even = 画面几乎没变化，按总时长平均分；mock = 没素材，通用骨架
    method: Mapped[str] = mapped_column(String(16), nullable=False, default="mock")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ready")
    error_message: Mapped[str | None] = mapped_column(Text)
    # 这次分解的卡片文字是用哪种语言生成的（?lang=en 重新分解会直接生成英文）
    lang: Mapped[str] = mapped_column(String(8), nullable=False, default="zh")

    # 真读到的视频信息
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    # 真实画面切点（秒），JSON 数组
    cuts_json: Mapped[str | None] = mapped_column(Text)
    segment_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 其中有多少段的文字是模型看图写的
    captioned_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 存了多少张代表帧
    frame_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model_name: Mapped[str | None] = mapped_column(String(160))
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TutorialStep(Base):
    """一条教程里的一步。带 checkpoint（自检问题 + 合格标准），所以「过了没有」可判定。

    basis 说明这一步的**段边界**是怎么来的：
      shots  —— 真实画面切换切出来的（ffmpeg 画面变化检测）
      even   —— 画面几乎没变化，按总时长平均分
      sample —— 人工写好的示例
      mock   —— 没素材，通用骨架（一个字都没读视频）
      manual —— 用户自己加的

    text_basis 说明**标题/说明/合格标准**是怎么来的：
      model  —— 视觉模型看了这一段的代表帧写的（只说「这一帧里有什么」）
      none   —— 没人写，只有画面，标题是占位

    两个都必须如实显示给用户。把 shots 说成「看懂了视频」是作弊；
    把 model 写的东西说成视频原话也是作弊。
    """

    __tablename__ = "tutorial_steps"
    __table_args__ = (
        UniqueConstraint("video_id", "position", name="uq_tutorial_step_video_position"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    video_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    breakdown_id: Mapped[str | None] = mapped_column(String(36), index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    criteria: Mapped[str] = mapped_column(Text, nullable=False)
    hint: Mapped[str | None] = mapped_column(Text)

    # 英文翻译（?lang=en 第一次访问时惰性翻译并落库；NULL = 还没翻过，诚实退回中文）
    en_title: Mapped[str | None] = mapped_column(Text)
    en_summary: Mapped[str | None] = mapped_column(Text)
    en_question: Mapped[str | None] = mapped_column(Text)
    en_criteria: Mapped[str | None] = mapped_column(Text)
    en_hint: Mapped[str | None] = mapped_column(Text)

    start_seconds: Mapped[float | None] = mapped_column(Float)
    end_seconds: Mapped[float | None] = mapped_column(Float)
    basis: Mapped[str] = mapped_column(String(16), nullable=False, default="mock")
    text_basis: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    # 这一段的代表帧，相对 local_storage_root 的路径
    frame_key: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    @property
    def has_frame(self) -> bool:
        return bool(self.frame_key)


class StepInteraction(Base):
    """每一步的检查 / 提问历史。kind=check 是判定，kind=ask 是问答。"""

    __tablename__ = "step_interactions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    video_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tutorial_steps.id", ondelete="CASCADE"), nullable=False, index=True
    )

    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # check | ask
    user_input: Mapped[str | None] = mapped_column(Text)
    has_image: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    verdict: Mapped[str | None] = mapped_column(String(16))  # pass | retry | unclear
    answer: Mapped[str | None] = mapped_column(Text)  # check=判定理由，ask=回答
    detail: Mapped[str | None] = mapped_column(Text)  # check=到底差在哪
    basis: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    model_name: Mapped[str | None] = mapped_column(String(160))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserStepProgress(Base):
    """Private progress layered over a shared tutorial step."""

    __tablename__ = "user_step_progress"
    __table_args__ = (
        UniqueConstraint("user_id", "step_id", name="uq_user_step_progress_user_step"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    video_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tutorial_steps.id", ondelete="CASCADE"), nullable=False, index=True
    )
    done: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_note: Mapped[str | None] = mapped_column(Text)
    last_verdict: Mapped[str | None] = mapped_column(String(16))
    last_reason: Mapped[str | None] = mapped_column(Text)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
