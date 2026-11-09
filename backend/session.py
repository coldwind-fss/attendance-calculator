import hmac
import secrets
import time
from hashlib import sha256
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from .http import database, fail, problem_responses


router = APIRouter(prefix="/session", tags=["session"])
COOKIE = "hr_session"
SESSION_SECONDS = 8 * 60 * 60
LOGIN_WINDOW = 300
LOGIN_FAILURES = 5


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1024)


class Session(BaseModel):
    actor: str
    role: Literal["admin", "performance"] = "admin"
    csrf_token: str


def token_digest(token):
    return sha256(token.encode()).hexdigest()


def read_session(request: Request, same_origin_form: bool):
    token = request.cookies.get(COOKIE, "")
    with database(request).connect() as connection:
        row = connection.execute(
            "SELECT actor, csrf FROM sessions WHERE token = ? AND expires_at > ?",
            (token_digest(token), int(time.time())),
        ).fetchone()
    if row is None:
        fail(401, "SESSION_REQUIRED", "请先登录人事管理系统")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        supplied = request.headers.get("X-CSRF-Token", "")
        origin = f"{request.url.scheme}://{request.url.netloc}"
        if not hmac.compare_digest(supplied.encode(), row["csrf"].encode()) and not (
            same_origin_form and request.headers.get("Origin") == origin
        ):
            fail(403, "CSRF_INVALID", "请求校验失败，请重新登录")
    settings = request.app.state.settings
    if row["actor"] == settings.admin_username:
        return Session(actor=row["actor"], csrf_token=row["csrf"])
    if settings.performance_compat and row["actor"] in settings.performance_accounts:
        return Session(actor=row["actor"], role="performance", csrf_token=row["csrf"])
    fail(401, "SESSION_REQUIRED", "账户会话已失效，请重新登录")


def require_session(request: Request):
    session = read_session(request, False)
    if session.role != "admin":
        fail(403, "MANAGEMENT_FORBIDDEN", "该账户仅可访问绩效管理")
    return session


@router.post("", response_model=Session, responses=problem_responses(401, 429, 503))
def login(credentials: Credentials, request: Request, response: Response):
    settings = request.app.state.settings
    if not settings.admin_username or not settings.admin_password:
        fail(503, "SESSION_NOT_CONFIGURED", "管理员登录尚未配置")
    admin = hmac.compare_digest(credentials.username.encode(), settings.admin_username.encode())
    role = "admin" if admin else "performance"
    expected = settings.admin_password if admin else (
        settings.performance_accounts.get(credentials.username, "") if settings.performance_compat else ""
    )
    valid = bool(expected) and hmac.compare_digest(credentials.password.encode(), expected.encode())
    now = int(time.time())
    address = request.client.host if request.client else "local"
    with database(request).connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM login_attempts WHERE window_start <= ?", (now - LOGIN_WINDOW,))
        attempts = connection.execute("SELECT attempts FROM login_attempts WHERE address=?", (address,)).fetchone()
        if attempts and attempts["attempts"] >= LOGIN_FAILURES:
            fail(429, "LOGIN_RATE_LIMITED", "登录失败次数过多，请稍后重试")
        if valid:
            token = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(32)
            connection.execute("DELETE FROM login_attempts WHERE address=?", (address,))
            connection.execute("DELETE FROM sessions WHERE expires_at <= ? OR token = ?", (
                now, token_digest(request.cookies.get(COOKIE, "")),
            ))
            connection.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (
                token_digest(token), credentials.username, csrf, now + SESSION_SECONDS,
            ))
        else:
            connection.execute("INSERT INTO login_attempts VALUES (?,1,?) ON CONFLICT(address) "
                               "DO UPDATE SET attempts=attempts+1", (address, now))
    if not valid:
        fail(401, "CREDENTIALS_INVALID", "账号或密码错误")
    response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
                        secure=settings.secure_cookie, samesite="lax")
    response.headers["Cache-Control"] = "no-store"
    return Session(actor=credentials.username, role=role, csrf_token=csrf)


@router.get("", response_model=Session, responses=problem_responses(401))
def get_session(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return read_session(request, False)


@router.delete("", status_code=204, responses=problem_responses(401, 403))
def logout(request: Request):
    read_session(request, False)
    with database(request).connect() as connection:
        connection.execute("DELETE FROM sessions WHERE token = ?", (token_digest(request.cookies[COOKIE]),))
    response = Response(status_code=204)
    response.delete_cookie(COOKIE, secure=request.app.state.settings.secure_cookie, httponly=True)
    return response
