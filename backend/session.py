import hmac
import secrets
import time
from hashlib import sha256

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from .http import database, fail


router = APIRouter(prefix="/session", tags=["demo session"])
COOKIE = "attendance_demo"
SESSION_SECONDS = 8 * 60 * 60


class Session(BaseModel):
    actor: str
    csrf_token: str


def token_digest(token):
    return sha256(token.encode()).hexdigest()


def find_session(connection, token, now):
    if token:
        return connection.execute(
            "SELECT actor, csrf FROM sessions WHERE token=? AND expires_at>?",
            (token_digest(token), now),
        ).fetchone()


def read_session(request: Request, same_origin_form: bool):
    with database(request).connect() as connection:
        row = find_session(connection, request.cookies.get(COOKIE), int(time.time()))
    if row is None:
        fail(401, "SESSION_REQUIRED", "请刷新页面重新进入演示")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        supplied = request.headers.get("X-CSRF-Token", "")
        origin = f"{request.url.scheme}://{request.url.netloc}"
        if not hmac.compare_digest(supplied.encode(), row["csrf"].encode()) and not (
            same_origin_form and request.headers.get("Origin") == origin
        ):
            fail(403, "CSRF_INVALID", "请求校验失败，请刷新页面后重试")
    return Session(actor=row["actor"], csrf_token=row["csrf"])


def require_session(request: Request):
    return read_session(request, False)


@router.post("", response_model=Session, summary="打开演示工作台")
def open_demo(request: Request, response: Response):
    now = int(time.time())
    with database(request).connect() as connection:
        row = find_session(connection, request.cookies.get(COOKIE), now)
        if row is not None:
            return Session(actor=row["actor"], csrf_token=row["csrf"])
        token = secrets.token_urlsafe(32)
        session = Session(actor=f"demo-{secrets.token_hex(12)}", csrf_token=secrets.token_urlsafe(32))
        connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        connection.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (
            token_digest(token), session.actor, session.csrf_token, now + SESSION_SECONDS,
        ))
    response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
                        secure=request.app.state.settings.secure_cookie, samesite="lax")
    return session
