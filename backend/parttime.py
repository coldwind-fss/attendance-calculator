import json
import os
import subprocess
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator

from .config import LEGACY_ATTENDANCE, ROOT
from .database import json_text, record_event, utcnow
from .files import XLSX, get_source
from .http import database, fail, problem_responses
from .jobs import JobAccepted, JobFailure, enqueue
from .parttime_hours import match_hours
from .parttime_payroll import calculate_settlement
from .parttime_xlsx import infer_store, read_sessions, sheet_names
from .session import Session, require_session


router = APIRouter(prefix="/attendance/parttime", tags=["parttime attendance"])
Month = Annotated[str, Path(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
Store = Literal["official", "sports", "tmall"]


class Inspection(BaseModel):
    file_id: str
    source_type: Literal["schedule", "tmall_schedule"] = "schedule"


class SheetList(BaseModel):
    sheets: list[str]


class AnalysisRequest(BaseModel):
    schedule_file_id: str
    schedule_sheet: str | None = None
    tmall_schedule_file_id: str | None = None
    tmall_schedule_sheet: str | None = None
    backend_file_id: str | None = None
    backend_file_ids: list[str] = Field(default_factory=list, max_length=12)
    backend_stores: list[Store | None] = Field(default_factory=list, max_length=12)
    operation_file_id: str | None = None
    operation_source: Literal["upload", "dingtalk_ai_table"] = "upload"
    calculation_mode: Literal["payroll_protection", "backend_allocation"] = "payroll_protection"
    warning_threshold_hours: float = Field(default=0.25, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def check_backend_files(self):
        if bool(self.backend_file_id) == bool(self.backend_file_ids):
            raise ValueError("请填写一组后台源文件")
        if self.backend_stores and len(self.backend_stores) != (len(self.backend_file_ids) or 1):
            raise ValueError("后台文件与店铺数量不一致")
        return self


class AnalysisReference(BaseModel):
    analysis_id: str
    month_id: str
    created_at: str


class AnalysisPage(BaseModel):
    items: list[AnalysisReference]
    next_cursor: str | None = None


class AnalysisDetail(AnalysisReference):
    result: dict


def invoke_engine(arguments, *, timeout=240):
    if not (LEGACY_ATTENDANCE / "node_modules" / "exceljs").exists():
        raise JobFailure("兼职计算依赖未安装，请在 backend/adapters/attendance 执行 npm ci")
    try:
        process = subprocess.run(
            ["node", str(ROOT / "backend" / "parttime_engine.mjs")], input=json_text(arguments),
            text=True, encoding="utf-8", capture_output=True, timeout=timeout,
            env=os.environ | {"TZ": "Asia/Shanghai"},
        )
    except subprocess.TimeoutExpired:
        raise JobFailure("计算或外部查询超时") from None
    if not process.stdout:
        raise JobFailure("无法启动兼职计算引擎")
    response = json.loads(process.stdout)
    if process.returncode:
        raise JobFailure(response["error"])
    return response["result"]


def run_analysis(settings, job, payload):
    analysis_id = job["id"]
    month = job["resource"]
    directory = settings.data_dir / "parttime"
    directory.mkdir(exist_ok=True)
    output = directory / f"{analysis_id}-{job['lease']}.xlsx"
    stores = payload["options"]["backend_stores"]
    sessions = []
    for index, file in enumerate(payload["source"]["backend"]):
        store = stores[index] if stores else None
        sessions.extend(read_sessions(file["path"], store or infer_store(file["filename"])))
    if not sessions:
        raise JobFailure("未从后台数据中识别到直播场次")
    if any(not row["date"].startswith(f"{month}-") for row in sessions):
        raise JobFailure("工作簿含有其他月份的数据，请检查所选工作表")
    rows = invoke_engine({"operation": "parse-analysis", "month": month, "backend": sessions, **payload})
    rows["schedule"] = match_hours(rows["schedule"], rows["backend"], rows["operations"],
                                   mode=payload["options"]["calculation_mode"],
                                   warning_hours=payload["options"]["warning_threshold_hours"])
    rows["calculated"] = calculate_settlement(rows["schedule"], rows.pop("policy"),
                                              attendance=rows["calculation"].get("attendanceOutput"),
                                              settlement=rows["calculation"].get("settlementOutput"))
    result = invoke_engine({"operation": "render-analysis", "output": str(output), "rows": rows})

    def save(connection):
        output.replace(directory / f"{analysis_id}.xlsx")
        connection.execute("INSERT INTO parttime_analyses VALUES (?, ?, ?, ?, ?, ?)", (
            analysis_id, month, job["actor"], json_text(payload), json_text(result), utcnow(),
        ))
        record_event(connection, "attendance.parttime.analysis_completed.v1", analysis_id, 1,
                     {"month_id": month, "analysis_id": analysis_id, "workbook_id": analysis_id}, causation_id=job["id"])
        return {"analysis_id": analysis_id, "month_id": month}

    return save


@router.get("/stores")
def stores(session: Session = Depends(require_session)):
    return {"items": [{"id": "official", "name": "官旗"}, {"id": "sports", "name": "运动"},
                      {"id": "tmall", "name": "天猫"}]}


@router.post("/schedule-inspections", response_model=SheetList)
def inspect_schedule(body: Inspection, request: Request, session: Session = Depends(require_session)):
    with database(request).connect() as connection:
        source = get_source(connection, request.app.state.settings.data_dir, session.actor, "parttime",
                            body.file_id, body.source_type)
    try:
        return SheetList(sheets=sheet_names(source["path"]))
    except JobFailure as error:
        fail(422, "WORKBOOK_INVALID", str(error))


@router.post("/months/{month_id}/analyses", status_code=202, response_model=JobAccepted, responses={
    202: {"headers": {"Location": {"description": "任务状态地址", "schema": {"type": "string"}}}},
    **problem_responses(401, 403, 404, 409),
})
def create_analysis(month_id: Month, body: AnalysisRequest, request: Request, response: Response,
                    session: Session = Depends(require_session),
                    idempotency_key: str = Header(min_length=1, max_length=128)):
    with database(request).connect() as connection:
        def source(file_id, source_type):
            return get_source(connection, request.app.state.settings.data_dir, session.actor,
                              "parttime", file_id, source_type)

        payload = {"source": {
            "schedule": source(body.schedule_file_id, "schedule"),
            "tmall": source(body.tmall_schedule_file_id, "tmall_schedule") if body.tmall_schedule_file_id else None,
            "backend": [source(file_id, "backend") for file_id in (body.backend_file_ids or [body.backend_file_id])],
            "operation": source(body.operation_file_id, "operation") if body.operation_file_id else None,
        }, "options": body.model_dump()}
        job_id = enqueue(connection, "parttime", session.actor, month_id, payload, idempotency_key)
    response.headers["Location"] = f"/api/v1/jobs/{job_id}"
    return JobAccepted(job_id=job_id)


@router.get("/months")
def months(request: Request, session: Session = Depends(require_session)):
    with database(request).connect() as connection:
        rows = connection.execute("SELECT DISTINCT month FROM parttime_analyses WHERE actor=? ORDER BY month DESC",
                                  (session.actor,)).fetchall()
    return {"items": [{"month_id": row["month"]} for row in rows]}


def reference(row):
    return AnalysisReference(analysis_id=row["id"], month_id=row["month"], created_at=row["created_at"])


@router.get("/months/{month_id}/analyses", response_model=AnalysisPage)
def analyses(month_id: Month, request: Request, session: Session = Depends(require_session),
             limit: int = Query(50, ge=1, le=100), cursor: str | None = None):
    with database(request).connect() as connection:
        position = ""
        parameters = [session.actor, month_id]
        if cursor:
            row = connection.execute("SELECT created_at FROM parttime_analyses WHERE id=? AND month=? AND actor=?",
                                     (cursor, month_id, session.actor)).fetchone()
            if row is None:
                fail(422, "CURSOR_INVALID", "分页位置无效")
            position = " AND (created_at,id) < (?,?)"
            parameters.extend([row["created_at"], cursor])
        rows = connection.execute("SELECT id, month, created_at FROM parttime_analyses WHERE actor=? AND month=?"
                                  + position + " ORDER BY created_at DESC, id DESC LIMIT ?",
                                  (*parameters, limit + 1)).fetchall()
    return AnalysisPage(items=[reference(row) for row in rows[:limit]],
                        next_cursor=rows[limit - 1]["id"] if len(rows) > limit else None)


def get_analysis(request, actor, analysis_id):
    with database(request).connect() as connection:
        row = connection.execute("SELECT * FROM parttime_analyses WHERE id=? AND actor=?",
                                 (analysis_id, actor)).fetchone()
    if row is None:
        fail(404, "ANALYSIS_NOT_FOUND", "找不到兼职核算记录")
    return row


@router.get("/analyses/{analysis_id}", response_model=AnalysisDetail, responses=problem_responses(401, 403, 404))
def analysis(analysis_id: str, request: Request, session: Session = Depends(require_session)):
    row = get_analysis(request, session.actor, analysis_id)
    return AnalysisDetail(**reference(row).model_dump(), result=json.loads(row["result"]))


@router.get("/analyses/{analysis_id}/workbook", response_class=FileResponse, responses={
    200: {"content": {XLSX: {"schema": {"type": "string", "format": "binary"}}}},
    **problem_responses(401, 403, 404),
})
def workbook(analysis_id: str, request: Request, session: Session = Depends(require_session)):
    row = get_analysis(request, session.actor, analysis_id)
    path = request.app.state.settings.data_dir / "parttime" / f"{row['id']}.xlsx"
    if not path.is_file():
        fail(404, "WORKBOOK_NOT_FOUND", "找不到该版本的工作簿")
    return FileResponse(path, media_type=XLSX, filename=f"兼职考勤-{row['month']}-{analysis_id[:8]}.xlsx")
