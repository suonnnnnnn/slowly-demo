from contextlib import asynccontextmanager

import asyncio
import faulthandler
import json
import logging
import threading
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import FileResponse, Response
from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import breakdown as breakdown_module
from app import translate as translate_module
from app import video_analysis
from app.breakdown import build_steps, mock_note
from app.coach import ask_step, check_step
from app.config import settings
from app.database import SessionLocal, create_tables, get_db, session_scope
from app.i18n import msg, normalize
from app.local_queue import executor, submit
from app.library import CATEGORY_LABELS, classify_tutorials, display_tutorial_title, keyword_category
from app.models import StepInteraction, TutorialBreakdown, TutorialStep, Video, utcnow
from app.schemas import (
    PlaybackRead,
    LibraryItem,
    SavedTutorial,
    SearchResponse,
    StepAskRequest,
    StepAskResponse,
    StepCheckRequest,
    StepCheckResponse,
    StepRead,
    StepUpdate,
    StepsResponse,
    VideoCreate,
    VideoRead,
)
from app.search import SearchBusy, SearchTimeout, SearchUpstreamError, search_youtube
from app.security import UnsafeURLError, validate_source_url
from app.storage import (
    delete_file,
    delete_frames_for,
    frame_key as frame_storage_key,
    resolve_object,
    save_frame,
)
from app.tasks import download_video

logger = logging.getLogger("cookclip.breakdown")

# 这个后端偶尔会「整只卡死」：端口还在监听，但所有请求（连 /api/health）都超时。
# 之前两次都是靠重启混过去的，不知道卡在哪。这个看门狗专门抓现行：
# 每 10 秒往事件循环里塞一个回调，5 秒还没跑，就把全部线程的调用栈写进 watchdog.log。
# 下次再卡，日志里就是「卡在哪一行」的证据；如果连日志都没写，说明整个进程被外部冻结了。
_WATCHDOG_LOG = Path(__file__).resolve().parent.parent / "watchdog.log"


def _start_loop_watchdog() -> None:
    loop = asyncio.get_running_loop()

    def watch() -> None:
        while True:
            fired = threading.Event()
            loop.call_soon_threadsafe(fired.set)
            if not fired.wait(5.0):
                try:
                    with open(_WATCHDOG_LOG, "a", encoding="utf-8") as handle:
                        handle.write(f"\n===== event loop stuck @ {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
                        faulthandler.dump_traceback(file=handle)
                except OSError:
                    pass
            time.sleep(10)

    threading.Thread(target=watch, name="loop-watchdog", daemon=True).start()


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_tables()
    _start_loop_watchdog()
    session = SessionLocal()
    interrupted_ids: list[str] = []
    try:
        interrupted = session.scalars(
            select(Video).where(
                Video.status.in_(
                    ["pending", "inspecting", "downloading", "processing", "uploading"]
                )
            )
        ).all()
        for video in interrupted:
            video.status = "pending"
            video.progress = 0
            interrupted_ids.append(video.id)
        session.commit()
    finally:
        session.close()
    for video_id in interrupted_ids:
        submit(download_video, video_id)
    try:
        yield
    finally:
        executor.shutdown(wait=False, cancel_futures=False)


app = FastAPI(title=settings.app_name, lifespan=lifespan)


def _submit_download(video: Video, db: Session) -> None:
    try:
        submit(download_video, video.id)
    except Exception as exc:
        video.status = "failed"
        video.error_message = "本地下载队列暂不可用"
        db.commit()
        raise HTTPException(status_code=503, detail="本地下载队列暂不可用") from exc


def _reuse_video(video: Video, payload: VideoCreate, db: Session) -> Video:
    if video.status != "failed":
        return video
    video.source_url = str(payload.url)
    video.source_platform = payload.source_platform or video.source_platform
    video.source_video_id = payload.source_video_id or video.source_video_id
    video.title = payload.title or video.title
    video.uploader = payload.uploader or video.uploader
    video.thumbnail_url = str(payload.thumbnail_url) if payload.thumbnail_url else video.thumbnail_url
    video.duration_seconds = (
        payload.duration_seconds if payload.duration_seconds is not None else video.duration_seconds
    )
    video.status = "pending"
    video.progress = 0
    video.error_message = None
    db.commit()
    db.refresh(video)
    _submit_download(video, db)
    return video


@app.get("/api/health")
def health(db: Session = Depends(get_db)):
    db.execute(select(1))
    return {"status": "ok"}


@app.post("/api/videos", response_model=VideoRead, status_code=status.HTTP_202_ACCEPTED)
def create_video(payload: VideoCreate, db: Session = Depends(get_db)):
    source_url = str(payload.url)
    try:
        validate_source_url(source_url, settings.allowed_domains)
    except UnsafeURLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    existing = None
    if payload.source_platform and payload.source_video_id:
        existing = db.scalar(
            select(Video).where(
                func.lower(Video.source_platform) == payload.source_platform,
                Video.source_video_id == payload.source_video_id,
            )
        )
    if existing is None:
        existing = db.scalar(select(Video).where(Video.source_url == source_url))
    if existing is not None:
        return _reuse_video(existing, payload, db)

    video = Video(
        source_url=source_url,
        source_platform=payload.source_platform,
        source_video_id=payload.source_video_id,
        title=payload.title,
        uploader=payload.uploader,
        thumbnail_url=str(payload.thumbnail_url) if payload.thumbnail_url else None,
        duration_seconds=payload.duration_seconds,
        status="pending",
        progress=0,
    )
    db.add(video)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if payload.source_platform and payload.source_video_id:
            existing = db.scalar(
                select(Video).where(
                    func.lower(Video.source_platform) == payload.source_platform,
                    Video.source_video_id == payload.source_video_id,
                )
            )
        if existing is not None:
            return _reuse_video(existing, payload, db)
        raise
    db.refresh(video)

    _submit_download(video, db)
    return video


@app.get("/api/search", response_model=SearchResponse)
def search_videos(
    q: str = Query(min_length=1, max_length=50),
    db: Session = Depends(get_db),
):
    query = q.strip()
    if not query:
        raise HTTPException(status_code=400, detail="请输入要搜索的菜名")
    try:
        items, cached = search_youtube(db, query)
    except SearchTimeout as exc:
        raise HTTPException(
            status_code=504,
            detail=f"检索超时：{exc}",
        ) from exc
    except SearchBusy as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except SearchUpstreamError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    candidate_ids = [item["platform_video_id"] for item in items]
    saved_ids: set[str] = set()
    if candidate_ids:
        saved_ids = set(
            db.scalars(
                select(Video.source_video_id).where(
                    func.lower(Video.source_platform) == "youtube",
                    Video.source_video_id.in_(candidate_ids),
                    Video.status != "failed",
                )
            ).all()
        )
    for item in items:
        item["already_saved"] = item["platform_video_id"] in saved_ids
    return SearchResponse(query=query, cached=cached, count=len(items), items=items)


@app.get("/api/videos", response_model=list[VideoRead])
def list_videos(saved: bool | None = Query(default=None), db: Session = Depends(get_db)):
    """任务列表。带 saved=1 就只看存下来的教程。"""
    query = select(Video).order_by(Video.created_at.desc()).limit(50)
    if saved is not None:
        query = query.where(Video.saved_at.is_not(None) if saved else Video.saved_at.is_(None))
    return db.scalars(query).all()


@app.get("/api/saved-tutorials", response_model=list[SavedTutorial])
def list_saved_tutorials(db: Session = Depends(get_db)):
    """「存着慢慢做」列表：按存下时间倒序，带上做到第几步了。

    只看存下的，不掺别的——这个页面存在的意义就是「我打算回来做的那几个」。
    """
    videos = db.scalars(
        select(Video)
        .where(Video.saved_at.is_not(None))
        .order_by(Video.saved_at.desc())
        .limit(50)
    ).all()
    if not videos:
        return []

    counts: dict[str, tuple[int, int]] = {}
    rows = db.execute(
        select(
            TutorialStep.video_id,
            func.count(TutorialStep.id),
            func.sum(case((TutorialStep.done.is_(True), 1), else_=0)),
        )
        .where(TutorialStep.video_id.in_([video.id for video in videos]))
        .group_by(TutorialStep.video_id)
    ).all()
    for video_id, total, done in rows:
        counts[video_id] = (int(total or 0), int(done or 0))

    return [
        SavedTutorial(
            id=video.id,
            title=video.title,
            uploader=video.uploader,
            duration_seconds=video.duration_seconds,
            thumbnail_url=video.thumbnail_url,
            status=video.status,
            saved_at=video.saved_at,
            step_total=counts.get(video.id, (0, 0))[0],
            step_done=counts.get(video.id, (0, 0))[1],
        )
        for video in videos
    ]


@app.get("/api/library", response_model=list[LibraryItem])
def list_library(db: Session = Depends(get_db)):
    """已经完成真实分解的素材；最新分解失败或只有通用骨架时不展示。"""
    videos = db.scalars(
        select(Video)
        .where(Video.status == "ready")
        .order_by(Video.updated_at.desc())
        .limit(100)
    ).all()
    if not videos:
        return []

    video_ids = [video.id for video in videos]
    latest_by_video: dict[str, TutorialBreakdown] = {}
    for record in db.scalars(
        select(TutorialBreakdown)
        .where(TutorialBreakdown.video_id.in_(video_ids))
        .order_by(TutorialBreakdown.created_at.desc())
    ).all():
        latest_by_video.setdefault(record.video_id, record)

    counts = {
        video_id: (int(total or 0), int(done or 0))
        for video_id, total, done in db.execute(
            select(
                TutorialStep.video_id,
                func.count(TutorialStep.id),
                func.sum(case((TutorialStep.done.is_(True), 1), else_=0)),
            )
            .where(TutorialStep.video_id.in_(video_ids))
            .group_by(TutorialStep.video_id)
        ).all()
    }

    unclassified = []
    for video in videos:
        record = latest_by_video.get(video.id)
        total, _ = counts.get(video.id, (0, 0))
        if (
            not video.library_category
            and record is not None
            and record.status == "ready"
            and record.method in {"shots", "even"}
            and total
        ):
            step_titles = [step.title for step in _ordered_steps(video.id, db)]
            unclassified.append({"id": video.id, "title": video.title, "step_titles": step_titles})
    if unclassified:
        classified = classify_tutorials(unclassified)
        for video in videos:
            if video.id in classified:
                video.library_category, video.library_category_basis = classified[video.id]
        db.commit()

    items: list[LibraryItem] = []
    for video in videos:
        record = latest_by_video.get(video.id)
        total, done = counts.get(video.id, (0, 0))
        if (
            record is None
            or record.status != "ready"
            or record.method not in {"shots", "even"}
            or total == 0
        ):
            continue
        category = video.library_category if video.library_category in CATEGORY_LABELS else None
        category_basis = video.library_category_basis if video.library_category_basis in {"model", "rule"} else None
        if category is None or category_basis is None:
            category, category_basis = keyword_category(video.title)
        category_label = CATEGORY_LABELS[category]
        items.append(
            LibraryItem(
                id=video.id,
                title=video.title or "未命名教程",
                display_title=display_tutorial_title(video.title),
                uploader=video.uploader,
                duration_seconds=video.duration_seconds,
                category=category,
                category_label=category_label,
                icon=category,
                category_basis=category_basis,
                step_total=total,
                step_done=done,
                breakdown_basis=record.method,
                updated_at=video.updated_at,
            )
        )
    return items[:50]


@app.post("/api/videos/{video_id}/save", response_model=VideoRead)
def toggle_save_video(video_id: str, db: Session = Depends(get_db)):
    """「存下来，慢慢做」：再点一次就是取消。进度和步骤本来就都在库里，
    这个标记的意思是「我以后还想做它」，方便之后按「存下的教程」列出来。"""
    video = _load_video(video_id, db)
    video.saved_at = None if video.saved_at else utcnow()
    db.commit()
    db.refresh(video)
    return video


@app.get("/api/videos/{video_id}", response_model=VideoRead)
def get_video(video_id: str, db: Session = Depends(get_db)):
    video = db.get(Video, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return video


@app.get("/api/videos/{video_id}/playback", response_model=PlaybackRead)
def get_playback(video_id: str, db: Session = Depends(get_db)):
    video = db.get(Video, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if video.status != "ready" or not video.object_key:
        raise HTTPException(status_code=409, detail="视频尚未准备完成")
    return PlaybackRead(url=f"/api/videos/{video.id}/content")


@app.get("/api/videos/{video_id}/content")
def get_video_content(video_id: str):
    # 故意不用 Depends(get_db)：FileResponse 传大文件期间响应一直没结束，
    # 而 yield 式依赖要等响应发完才回收连接。播放时并发十几个分片请求，
    # 会把连接池占满、拖垮其它接口（素材库打不开就是这个原因）。
    # 这里读完要用的字段就立刻关会话。
    with session_scope() as db:
        video = db.get(Video, video_id)
        if video is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        if video.status != "ready" or not video.object_key:
            raise HTTPException(status_code=409, detail="视频尚未准备完成")
        object_key, mime_type = video.object_key, video.mime_type
    try:
        path = resolve_object(object_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="视频文件不存在")
    return FileResponse(
        path,
        media_type=mime_type,
        filename=path.name,
        content_disposition_type="inline",
    )


@app.delete("/api/videos/{video_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_video(video_id: str, db: Session = Depends(get_db)):
    video = db.get(Video, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if video.status not in {"ready", "failed"}:
        raise HTTPException(status_code=409, detail="视频仍在处理中，请完成后再删除")

    if video.object_key:
        try:
            delete_file(video.object_key)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=500, detail="本地视频文件删除失败") from exc

    # 步骤和它们的代表帧跟着视频一起走，别在盘上留孤儿文件
    for step in _ordered_steps(video_id, db):
        db.delete(step)
    for interaction in db.scalars(
        select(StepInteraction).where(StepInteraction.video_id == video_id)
    ).all():
        db.delete(interaction)
    for record in db.scalars(
        select(TutorialBreakdown).where(TutorialBreakdown.video_id == video_id)
    ).all():
        db.delete(record)
    delete_frames_for(video_id)

    db.delete(video)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------- 步骤：真分解（ffmpeg 切点 + 视觉模型看图） + 检查 ----------

MAX_IMAGE_CHARS = 8_000_000

# 同一条视频的分解串行跑，避免背后两个请求同时解码同一份文件
_analysis_locks: dict[str, threading.Lock] = {}
_analysis_locks_guard = threading.Lock()
# 正在排队/正在跑的分解。放在内存里，是为了让 GET /steps 能立刻回答「还在拆」
_running_analyses: set[str] = set()


def _analysis_lock(video_id: str) -> threading.Lock:
    with _analysis_locks_guard:
        lock = _analysis_locks.get(video_id)
        if lock is None:
            lock = threading.Lock()
            _analysis_locks[video_id] = lock
        return lock


def _is_analyzing(video_id: str) -> bool:
    with _analysis_locks_guard:
        return video_id in _running_analyses


def _load_video(video_id: str, db: Session, lang: str = "zh") -> Video:
    video = db.get(Video, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail=msg("error.video_not_found", lang))
    return video


def _ordered_steps(video_id: str, db: Session) -> list[TutorialStep]:
    return list(
        db.scalars(
            select(TutorialStep)
            .where(TutorialStep.video_id == video_id)
            .order_by(TutorialStep.position)
        ).all()
    )


def _latest_breakdown(video_id: str, db: Session) -> TutorialBreakdown | None:
    return db.scalars(
        select(TutorialBreakdown)
        .where(TutorialBreakdown.video_id == video_id)
        .order_by(TutorialBreakdown.created_at.desc())
        .limit(1)
    ).first()


def _local_video_path(video: Video) -> Path | None:
    """本地那份视频文件在哪。没有就说明还没下载完，或者文件被删了。"""
    if not video.object_key:
        return None
    try:
        path = resolve_object(video.object_key)
    except ValueError:
        return None
    return path if path.is_file() else None


def _load_step(video_id: str, step_id: str, db: Session, lang: str = "zh") -> TutorialStep:
    step = db.get(TutorialStep, step_id)
    if step is None or step.video_id != video_id:
        raise HTTPException(status_code=404, detail=msg("error.step_not_found", lang))
    return step


# ---------- 真分解 ----------


def build_breakdown(video_id: str, *, force: bool = False, lang: str = "zh") -> str:
    """给一条视频做一次分解，结果落库。幂等：已经拆过就直接返回。

    自己开 session，因为要能在后台线程里跑。返回 "ready" / "failed" / "missing"。
    lang 决定卡片文字的生成语言（英文请求直接生成英文，省掉二次翻译）；
    落库列永远存生成原文，record.lang 记下它是什么语言。
    """
    session = SessionLocal()
    try:
        with _analysis_lock(video_id):
            video = session.get(Video, video_id)
            if video is None:
                return "missing"

            cached = _latest_breakdown(video_id, session)
            source = _local_video_path(video)

            if cached is not None and cached.status == "ready" and not force:
                # 已经拆过就复用。唯一的例外：上次因为没素材只给了骨架，现在素材到了。
                if not (cached.method == "mock" and source is not None):
                    return "ready"

            result: breakdown_module.Breakdown | None = None
            failure: str | None = None
            generation_lang = normalize(lang)
            if source is not None:
                try:
                    result = breakdown_module.analyze(source, lang=generation_lang)
                except (video_analysis.AnalysisError, OSError) as exc:
                    failure = str(exc)
                except Exception as exc:  # 别让一个奇怪的视频把整个页面打死
                    logger.exception("分解 %s 失败", video_id)
                    failure = f"分析这段视频时出错了：{exc}"
            else:
                failure = "这条视频还没下载到本地，所以只能先给你一套通用骨架。"

            record = TutorialBreakdown(
                video_id=video_id,
                method=result.method if result else breakdown_module.MOCK_BASIS,
                status="ready" if result else "failed",
                error_message=failure,
                lang=generation_lang,
                duration_seconds=result.duration if result else video.duration_seconds,
                width=result.width if result else None,
                height=result.height if result else None,
                cuts_json=json.dumps(result.cuts) if result else None,
                segment_count=len(result.segments) if result else 0,
                captioned_count=result.captioned if result else 0,
                frame_count=0,
                model_name=result.model_name if result else None,
                note="",
            )
            session.add(record)
            session.flush()  # 拿到 record.id，代表帧要按它分目录

            # 旧的步骤和它们的判定历史一起清掉：位置都换了，留着只会对不上
            for step in _ordered_steps(video_id, session):
                session.delete(step)
            for interaction in session.scalars(
                select(StepInteraction).where(StepInteraction.video_id == video_id)
            ).all():
                session.delete(interaction)
            session.flush()
            # 代表帧也整目录重来
            delete_frames_for(video_id)

            if result is not None:
                for draft, segment in zip(result.drafts(), result.segments):
                    key = frame_storage_key(video_id, record.id, draft["position"])
                    try:
                        save_frame(segment.frame, key)
                    except OSError as exc:
                        logger.warning("代表帧写盘失败 %s：%s", key, exc)
                        key = None
                    session.add(
                        TutorialStep(video_id=video_id, breakdown_id=record.id, frame_key=key, **draft)
                    )
                record.frame_count = sum(
                    1 for step in _ordered_steps(video_id, session) if step.frame_key
                )
                record.note = breakdown_module.real_note(result)
                classification = classify_tutorials([
                    {
                        "id": video.id,
                        "title": video.title,
                        "step_titles": [segment.title for segment in result.segments],
                    }
                ])[video.id]
                video.library_category, video.library_category_basis = classification
            else:
                for draft in build_steps(video):
                    session.add(
                        TutorialStep(video_id=video_id, breakdown_id=record.id, **draft)
                    )
                record.note = f"这次没能真拆：{failure}{mock_note(video)}"

            session.commit()
            return record.status
    finally:
        session.close()


def _run_breakdown_job(video_id: str, force: bool = False, lang: str = "zh") -> None:
    try:
        build_breakdown(video_id, force=force, lang=lang)
    except Exception:  # 后台任务不能把异常抛到线程池外面
        logger.exception("后台分解 %s 崩了", video_id)
    finally:
        with _analysis_locks_guard:
            _running_analyses.discard(video_id)


def _start_analysis(video_id: str, *, force: bool = False, lang: str = "zh") -> None:
    """起一个后台分解。已经在跑就不重复起（force 也只是等它跑完，不再排一个）。"""
    with _analysis_locks_guard:
        if video_id in _running_analyses:
            return
        _running_analyses.add(video_id)
    try:
        submit(_run_breakdown_job, video_id, force, normalize(lang))
    except Exception:
        with _analysis_locks_guard:
            _running_analyses.discard(video_id)
        raise HTTPException(status_code=503, detail=msg("error.queue_unavailable", lang))


def _needs_analysis(video: Video, record: TutorialBreakdown | None) -> bool:
    """现在该不该动手拆这一条。"""
    if record is None:
        return True
    if record.status == "running":
        return True
    if record.status == "failed":
        # 拆失败过就别自动重试了，免得每次刷新都白烧一遍 ffmpeg；交给用户点「重新分解」
        return False
    # 上次因为没素材只给了骨架，现在文件到了 → 值得真拆一次
    return record.method == breakdown_module.MOCK_BASIS and _local_video_path(video) is not None


_EN_STEP_FIELDS = (
    ("en_title", "title"),
    ("en_summary", "summary"),
    ("en_question", "question"),
    ("en_criteria", "criteria"),
    ("en_hint", "hint"),
)


def _step_is_translated(step: TutorialStep) -> bool:
    return bool((step.en_title or "").strip())


def _ensure_english_steps(steps: list[TutorialStep], db: Session) -> bool:
    """英文模式下确保这批步骤有英文文案；缺就现翻一次并落库。

    返回 True = 全都有英文（可能刚翻好）；False = 模型不可用/没答上来，
    调用方诚实退回中文（_steps_response 会给出如实提示）。
    """
    missing = [step for step in steps if not _step_is_translated(step)]
    if not missing:
        return True
    if not settings.model_key:
        return False

    result = translate_module.translate_steps(
        [
            {
                "title": step.title,
                "summary": step.summary,
                "question": step.question,
                "criteria": step.criteria,
                "hint": step.hint,
            }
            for step in missing
        ]
    )
    if result is None:
        return False

    for index, step in enumerate(missing):
        translated = result.get(index)
        if not translated:
            continue  # 个别步骤没翻过：那几步保持中文（has_en 判断会兜住）
        if translated.get("title"):
            step.en_title = translated["title"]
        if translated.get("summary"):
            step.en_summary = translated["summary"]
        if translated.get("question"):
            step.en_question = translated["question"]
        if translated.get("criteria"):
            step.en_criteria = translated["criteria"]
        step.en_hint = translated.get("hint")  # None 合法（原本就没提示）
    db.commit()
    return all(_step_is_translated(step) for step in steps)


def _step_read(step: TutorialStep, lang: str, record_lang: str = "zh") -> StepRead:
    """按请求语言出文案。

    落库列存的是「生成原文」（record.lang 记它是哪种语言）：
    - 请求语言 == 生成语言 → 直接用原文，一个字都不动；
    - 请求 en、原文 zh → en_* 列翻好了就覆盖，没翻好原样中文（不假装）。
    """
    if normalize(lang) == "en" and record_lang == "zh" and _step_is_translated(step):
        data = StepRead.model_validate(step).model_dump()
        for en_field, zh_field in _EN_STEP_FIELDS:
            value = getattr(step, en_field)
            if value is not None:
                data[zh_field] = value
        return StepRead(**data)
    return StepRead.model_validate(step)


def _steps_response(
    video: Video,
    steps: list[TutorialStep],
    record: TutorialBreakdown | None,
    *,
    analyzing: bool,
    waiting: bool,
    lang: str = "zh",
) -> StepsResponse:
    lang = normalize(lang)
    record_lang = record.lang if record else "zh"
    note = record.note if record else ""
    if lang == "en" and note:
        # 诚实说明按同一份落库数据重建英文版，不是模型现翻的
        note = breakdown_module.real_note_en(record)
    if analyzing:
        if steps:
            # 页面上还挂着上一次的结果。骨架的话直说它马上会被换掉；
            # 真拆过的话就什么都别挂（否则会把上一次那段长说明又翻出来）
            note = (
                msg("note.reanalyzing_skeleton", lang)
                if all(step.basis == breakdown_module.MOCK_BASIS for step in steps)
                else ""
            )
        else:
            note = msg("note.analyzing", lang)
    elif waiting and not steps:
        note = msg("note.waiting", lang)

    # 翻译没跟上的两种情况都如实说，别让用户猜：
    # - 英文请求、原文是中文、还没翻好 → 先看中文
    # - 中文请求、但这版步骤是英文生成的 → 先看英文（重新分解一次并切回中文可拿到中文）
    if steps:
        if lang == "en" and record_lang == "zh" and any(not _step_is_translated(step) for step in steps) and not note:
            note = msg("note.translation_unavailable", lang)
        elif lang == "zh" and record_lang == "en":
            note = (note + msg("note.steps_english_only", lang)).strip()

    return StepsResponse(
        video_id=video.id,
        title=video.title,
        total=len(steps),
        done_count=sum(1 for step in steps if step.done),
        # 只有整份步骤都是骨架时才算 mock。混着的时候一律按真的说，界面上每步自己会标。
        mock=bool(steps) and all(step.basis == breakdown_module.MOCK_BASIS for step in steps),
        vision_ready=settings.model_vision_enabled and bool(settings.model_key),
        analyzing=analyzing,
        waiting=waiting,
        basis=record.method if record else None,
        frame_count=sum(1 for step in steps if step.frame_key),
        note=note,
        steps=[_step_read(step, lang, record_lang) for step in steps],
    )


def _validate_image(image_data_url: str | None, lang: str = "zh") -> str | None:
    if image_data_url is None:
        return None
    value = image_data_url.strip()
    if not value:
        return None
    if not value.startswith("data:image/"):
        raise HTTPException(status_code=400, detail=msg("error.photo_format", lang))
    if len(value) > MAX_IMAGE_CHARS:
        raise HTTPException(status_code=413, detail=msg("error.photo_too_large", lang))
    return value


@app.get("/api/videos/{video_id}/steps", response_model=StepsResponse)
def get_video_steps(
    video_id: str,
    lang: str = Query(default="zh"),
    db: Session = Depends(get_db),
):
    """拿到这条视频的步骤。

    第一次访问（或素材刚到位）会自动起一个后台分解，这时返回 analyzing=true，
    前端轮询等它变成 false 即可——不要在这里同步跑，不然页面会卡住半分钟。
    """
    video = _load_video(video_id, db, lang)
    source = _local_video_path(video)
    ready = video.status == "ready" and source is not None
    record = _latest_breakdown(video_id, db)

    if not ready:
        # 还没下载完：不给假步骤，就说清楚在等什么
        return _steps_response(video, [], record, analyzing=False, waiting=True, lang=lang)

    if _needs_analysis(video, record):
        _start_analysis(video_id, lang=lang)

    db.expire_all()
    video = _load_video(video_id, db, lang)
    steps = _ordered_steps(video_id, db)
    record = _latest_breakdown(video_id, db)
    if normalize(lang) == "en" and steps and (record is None or record.lang == "zh"):
        # 英文请求 + 中文生成的步骤：现翻一次并落库；英文生成的直接用原文，不用翻
        _ensure_english_steps(steps, db)
    return _steps_response(
        video,
        steps,
        record,
        analyzing=_is_analyzing(video_id) or _needs_analysis(video, record),
        waiting=False,
        lang=lang,
    )


@app.post("/api/videos/{video_id}/steps/regenerate", response_model=StepsResponse)
def regenerate_video_steps(
    video_id: str,
    force: bool = Query(default=False),
    lang: str = Query(default="zh"),
    db: Session = Depends(get_db),
):
    """重新拆一次。

    界面上的按钮是**一次点击**就生效的：它会带上 force=1，因为按钮旁边就写着
    「重新拆一遍会换掉全部步骤」。这里保留 force 这道闸，是给直接打接口的场景兜底——
    有进度又没带 force 的话，先告诉调用方会丢什么，别让它悄悄清掉。
    """
    video = _load_video(video_id, db, lang)
    if _local_video_path(video) is None:
        raise HTTPException(status_code=409, detail=msg("error.cannot_regen_not_local", lang))

    steps = _ordered_steps(video_id, db)
    done_count = sum(1 for step in steps if step.done)
    if done_count and not force:
        raise HTTPException(
            status_code=409,
            detail=msg("error.regen_would_reset", lang).format(done=done_count),
        )

    # 后台跑，别把请求挂在这里等半分钟；前端拿到 analyzing=true 会自己轮询
    _start_analysis(video_id, force=True, lang=lang)
    db.expire_all()
    video = _load_video(video_id, db, lang)
    steps = _ordered_steps(video_id, db)
    record = _latest_breakdown(video_id, db)
    if normalize(lang) == "en" and steps and (record is None or record.lang == "zh"):
        _ensure_english_steps(steps, db)
    return _steps_response(video, steps, record, analyzing=True, waiting=False, lang=lang)


@app.get("/api/videos/{video_id}/steps/{step_id}/frame")
def get_step_frame(
    video_id: str,
    step_id: str,
    lang: str = Query(default="zh"),
):
    """这一步的代表画面。没有就如实 404，不要拿别的图糊弄。"""
    # 同样是流式响应：步骤页会一次性拉很多张帧，读完后立刻关会话，
    # 别把连接留到图片传完（原因见 get_video_content）。
    with session_scope() as db:
        _load_video(video_id, db, lang)
        step = _load_step(video_id, step_id, db, lang)
        frame_key = step.frame_key
    if not frame_key:
        raise HTTPException(status_code=404, detail=msg("error.no_frame", lang))
    try:
        path = resolve_object(frame_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=msg("error.frame_path_bad", lang)) from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail=msg("error.frame_missing", lang))
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@app.patch("/api/videos/{video_id}/steps/{step_id}", response_model=StepRead)
def update_step(
    video_id: str,
    step_id: str,
    payload: StepUpdate,
    lang: str = Query(default="zh"),
    db: Session = Depends(get_db),
):
    """用户自己勾「我做到了」/ 撤销，或者留一句备注。"""
    _load_video(video_id, db, lang)
    step = _load_step(video_id, step_id, db, lang)

    if payload.done is not None and payload.done != step.done:
        step.done = payload.done
        step.done_at = utcnow() if payload.done else None
        if not payload.done:
            # 撤销时把上一次判定一起清掉，免得出现「没做到但判定是通过」
            step.last_verdict = None
            step.last_reason = None
            step.last_checked_at = None
    if payload.user_note is not None:
        step.user_note = payload.user_note.strip() or None

    db.commit()
    db.refresh(step)
    return _step_read(step, lang)


@app.post("/api/videos/{video_id}/steps/{step_id}/check", response_model=StepCheckResponse)
def check_video_step(
    video_id: str,
    step_id: str,
    payload: StepCheckRequest,
    lang: str = Query(default="zh"),
    db: Session = Depends(get_db),
):
    """「让小慢帮我看看」：按这一步的合格标准判定过没过。判定通过就自动记成做到了。"""
    video = _load_video(video_id, db, lang)
    step = _load_step(video_id, step_id, db, lang)
    image = _validate_image(payload.image_data_url, lang)

    outcome = check_step(step, payload.report.strip(), image, lang=lang)

    step.last_verdict = outcome.verdict
    step.last_reason = outcome.reason
    step.last_checked_at = utcnow()
    if outcome.verdict == "pass":
        step.done = True
        step.done_at = utcnow()

    db.add(
        StepInteraction(
            video_id=video.id,
            step_id=step.id,
            kind="check",
            user_input=payload.report.strip(),
            has_image=bool(image),
            verdict=outcome.verdict,
            answer=outcome.reason,
            detail=outcome.detail or None,
            basis=outcome.basis,
            model_name=outcome.model_name,
        )
    )
    db.commit()
    db.refresh(step)

    return StepCheckResponse(
        verdict=outcome.verdict,
        reason=outcome.reason,
        detail=outcome.detail,
        basis=outcome.basis,
        note=outcome.note,
        model_name=outcome.model_name,
        step=StepRead.model_validate(step),
    )


@app.post("/api/videos/{video_id}/steps/{step_id}/ask", response_model=StepAskResponse)
def ask_video_step(
    video_id: str,
    step_id: str,
    payload: StepAskRequest,
    lang: str = Query(default="zh"),
    db: Session = Depends(get_db),
):
    """卡住了问一句。"""
    video = _load_video(video_id, db, lang)
    step = _load_step(video_id, step_id, db, lang)

    outcome = ask_step(step, payload.question.strip(), lang=lang)

    db.add(
        StepInteraction(
            video_id=video.id,
            step_id=step.id,
            kind="ask",
            user_input=payload.question.strip(),
            basis=outcome["basis"],
            answer=outcome["answer"],
            model_name=outcome["model_name"],
        )
    )
    db.commit()
    return StepAskResponse(**outcome)
