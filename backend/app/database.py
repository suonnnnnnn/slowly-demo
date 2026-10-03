from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


if settings.database_url.startswith("sqlite:///"):
    database_path = Path(settings.database_url.removeprefix("sqlite:///"))
    database_path.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    # 留出余量。池子太小（原来默认 5+10）在真实使用下很容易被打满：
    # 播放一条本地视频会并发十几个 /content 分片请求，每个都可能在传文件期间
    # 占着一个连接，池子一满后面所有请求都一起超时（素材库打不开就是这么来的）。
    # 真正的修法是流式端点别握着连接（见 session_scope），这里只是加保险。
    pool_size=10,
    max_overflow=20,
    connect_args={"check_same_thread": False, "timeout": 30}
    if settings.database_url.startswith("sqlite")
    else {},
)


if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


# create_all 只会建新表，不会给已有的表补列。开发期的 SQLite 加个轻量迁移，
# 免得每次加字段都要手删数据库（那条已下载的视频就白下了）。
_SQLITE_COLUMNS: dict[str, dict[str, str]] = {
    "tutorial_steps": {
        "breakdown_id": "VARCHAR(36)",
        "text_basis": "VARCHAR(16) NOT NULL DEFAULT 'none'",
        "frame_key": "TEXT",
        "en_title": "TEXT",
        "en_summary": "TEXT",
        "en_question": "TEXT",
        "en_criteria": "TEXT",
        "en_hint": "TEXT",
    },
    "videos": {
        "library_category": "VARCHAR(16)",
        "library_category_basis": "VARCHAR(16)",
    },
    "tutorial_breakdowns": {
        "lang": "VARCHAR(8) NOT NULL DEFAULT 'zh'",
    },
    "step_interactions": {
        "user_id": "VARCHAR(36)",
    },
}


def _add_missing_columns(connection) -> None:
    for table, columns in _SQLITE_COLUMNS.items():
        existing = {
            row[1] for row in connection.execute(text(f"PRAGMA table_info({table})"))
        }
        if not existing:
            continue  # 表刚建出来，列本来就是齐的
        for name, ddl in columns.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def create_tables() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    if settings.database_url.startswith("sqlite"):
        with engine.begin() as connection:
            _add_missing_columns(connection)
            connection.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_video_platform_source_id "
                "ON videos (source_platform, source_video_id) "
                "WHERE source_video_id IS NOT NULL"
            ))
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_step_interactions_user_id "
                "ON step_interactions (user_id)"
            ))


def get_db() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """短生命周期会话，用完立刻关。

    专门给「读完数据就返回、不在会话里写库」的地方用 —— 尤其是返回 FileResponse
    或流式响应的端点。这类端点**不能**用 `Depends(get_db)`：yield 式依赖要等响应
    彻底发完才回收，而传一个大文件期间响应一直没结束，连接就被一直占着。
    浏览器播一条视频会并发十几个分片请求，几个来回就把连接池打满，之后
    任何请求（比如素材库）都跟着超时。这里的写法是读完要用的字段就 close，
    把 FileResponse 放到会话外面返回。
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
