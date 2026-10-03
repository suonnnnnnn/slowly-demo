import hashlib
import re
import secrets
from datetime import timedelta

from fastapi import Request, Response
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import SessionLocal
from app.models import AnonymousSession, User, utcnow


COOKIE_NAME = "slowly_session"
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _is_secure(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
    return request.url.scheme == "https" or forwarded == "https"


def current_user_id(request: Request, response: Response) -> str:
    """Resolve an anonymous browser to one durable user without storing raw tokens."""
    token = request.cookies.get(COOKIE_NAME)
    if not token or not TOKEN_RE.fullmatch(token):
        token = _new_token()

    digest = token_hash(token)
    now = utcnow()
    session = SessionLocal()
    try:
        anonymous = session.get(AnonymousSession, digest)
        # SQLite may deserialize timestamps without tzinfo. Both values are UTC.
        expired = anonymous is not None and anonymous.expires_at.replace(tzinfo=now.tzinfo) <= now
        if expired:
            session.delete(anonymous)
            session.commit()
            anonymous = None
        if anonymous is None:
            user = User()
            session.add(user)
            session.flush()
            anonymous = AnonymousSession(
                token_hash=digest,
                user_id=user.id,
                expires_at=now + timedelta(days=settings.anonymous_session_days),
            )
            session.add(anonymous)
            try:
                session.commit()
            except IntegrityError:
                # Two first-page API calls may race with the same freshly issued cookie.
                session.rollback()
                anonymous = session.get(AnonymousSession, digest)
                if anonymous is None:
                    raise
        user_id = anonymous.user_id
    finally:
        session.close()

    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.anonymous_session_days * 24 * 60 * 60,
        httponly=True,
        secure=_is_secure(request),
        samesite="lax",
        path="/",
    )
    return user_id
