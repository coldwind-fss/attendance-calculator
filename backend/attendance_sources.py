from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .database import utcnow
from .files import XLSX
from .http import database, fail
from .jobs import JobFailure
from .parttime import invoke_engine
from .session import Session, require_session


router = APIRouter(prefix="/attendance", tags=["attendance sources"])


class ParttimeQuery(BaseModel):
    source: Literal["ai-status", "ai-preview", "ai-backend-preview"]
    month: str | None = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    limit: int = Field(default=30, ge=1, le=200)


class FulltimeQuery(BaseModel):
    month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    dept_id: int | None = None
    include_children: bool = True
    limit: int = Field(default=30, ge=1, le=200)


@router.post("/parttime/source-queries")
def parttime_sources(body: ParttimeQuery, session: Session = Depends(require_session)):
    try:
        return invoke_engine({"operation": body.source, "options": {"month": body.month, "limit": body.limit}})
    except JobFailure as error:
        fail(502, "DINGTALK_SOURCE_UNAVAILABLE", str(error))


@router.post("/fulltime/source-queries", status_code=201)
def fulltime_sources(body: FulltimeQuery, request: Request, session: Session = Depends(require_session)):
    query_id = uuid4().hex
    directory = request.app.state.settings.data_dir / "dingtalk-sources"
    directory.mkdir(exist_ok=True)
    output = directory / f"{query_id}.xlsx"
    try:
        result = invoke_engine({"operation": "attendance-source", "output": str(output), "options": {
            "month": body.month, "deptId": body.dept_id, "includeChildren": body.include_children, "limit": body.limit,
        }})
    except JobFailure as error:
        fail(502, "DINGTALK_SOURCE_UNAVAILABLE", str(error))
    with database(request).connect() as connection:
        connection.execute("INSERT INTO attendance_source_queries VALUES (?, ?, ?, ?)",
                           (query_id, body.month, session.actor, utcnow()))
    return result | {"source_id": query_id}


@router.get("/fulltime/source-queries/{source_id}/workbook", response_class=FileResponse)
def source_workbook(source_id: str, request: Request, session: Session = Depends(require_session)):
    with database(request).connect() as connection:
        row = connection.execute("SELECT * FROM attendance_source_queries WHERE id=? AND actor=?",
                                 (source_id, session.actor)).fetchone()
    if row is None:
        fail(404, "SOURCE_NOT_FOUND", "找不到钉钉来源工作簿")
    path = request.app.state.settings.data_dir / "dingtalk-sources" / f"{row['id']}.xlsx"
    return FileResponse(path, media_type=XLSX, filename=f"钉钉考勤源数据-{row['month']}.xlsx")
