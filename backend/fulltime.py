import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, Path as PathParameter, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator

from .adapters.attendance.fulltime import platform_store as legacy

from .config import LEGACY_FULLTIME
from .database import Database, json_text, record_event, utcnow
from .files import XLSX, get_source
from .http import database, fail
from .fulltime_template import write_blank_template
from .jobs import JobAccepted, JobFailure, enqueue
from .parttime import invoke_engine
from .session import Session, require_session


router = APIRouter(prefix="/attendance/fulltime", tags=["fulltime attendance"])
MonthId = Annotated[str, PathParameter(pattern=r"^FTM-\d{4}(0[1-9]|1[0-2])$")]
ALGORITHM_VERSION = "2026-09-august-regression"


class RunRequest(BaseModel):
    run_type: Literal["workbook", "source_merge", "recalculate"]
    workbook_id: str | None = None
    monthly_file_id: str | None = None
    raw_file_id: str | None = None
    expected_revision: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def check_files(self):
        if self.run_type == "workbook" and not self.workbook_id:
            raise ValueError("请选择整合工作簿")
        if self.run_type == "source_merge" and not (self.monthly_file_id and self.raw_file_id):
            raise ValueError("请选择月度汇总与原始数据")
        return self


class Correction(BaseModel):
    employee_name: str = Field(min_length=1, max_length=100)
    target_type: Literal["day", "remark", "statistic"]
    day: int | None = Field(default=None, ge=1, le=31)
    field_key: str | None = None
    new_value: str | int | float
    reason: str = Field(min_length=1, max_length=300)
    feedback_id: str | None = None
    expected_revision: int = Field(ge=0)

    @model_validator(mode="after")
    def check_target(self):
        if self.target_type == "day" and self.day is None:
            raise ValueError("日级修订需要日期")
        if self.target_type == "statistic" and not self.field_key:
            raise ValueError("统计修订需要字段")
        return self


class Revision(BaseModel):
    expected_revision: int = Field(ge=0)


class Publish(Revision):
    version_id: str


class Employee(BaseModel, extra="allow"):
    name: str = Field(min_length=1, max_length=100)
    entry_date: date
    attendance_type: str = Field(min_length=1, max_length=30)
    scheduled_days: str | float | None = None


class Roster(BaseModel):
    items: list[Employee]


def initialize(db):
    with db.connect() as connection:
        legacy.migrate(connection)


def current_revision(connection, month_id):
    row = connection.execute("SELECT revision FROM fulltime_revisions WHERE month_id=?", (month_id,)).fetchone()
    return row["revision"] if row else 0


def advance_revision(connection, month_id):
    revision = current_revision(connection, month_id) + 1
    connection.execute("INSERT INTO fulltime_revisions VALUES (?, ?) "
                       "ON CONFLICT(month_id) DO UPDATE SET revision=excluded.revision", (month_id, revision))
    return revision


def check_revision(connection, month_id, expected):
    if current_revision(connection, month_id) != expected:
        fail(409, "MONTH_CHANGED", "月份已被更新，请刷新后重试")


def month_record(connection, month_id):
    result = legacy.get_month(connection, {"month_id": month_id}, refresh_receipts=False)
    if result is None:
        fail(404, "MONTH_NOT_FOUND", "没有该考勤月份")
    result["revision"] = current_revision(connection, month_id)
    return result


def roster_items(connection):
    return [json.loads(row["data"]) for row in connection.execute("SELECT data FROM fulltime_roster ORDER BY position")]


def run_python(script, *args):
    process = subprocess.run([sys.executable, str(LEGACY_FULLTIME / script), *map(str, args)],
                             env=os.environ | {"PYTHONIOENCODING": "utf-8"}, capture_output=True, timeout=180)
    if process.returncode:
        raise JobFailure("全职计算失败，请检查源表、月份和员工数据")
    return process.stdout.decode("utf-8")


def run_fulltime(settings, job, payload):
    month_id = job["resource"]
    year, month = int(month_id[4:8]), int(month_id[8:10])
    db = Database(settings.data_dir / "workbench.sqlite")
    directory = settings.data_dir / "fulltime" / "workbooks"
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / f"{job['id']}-{job['lease']}.xlsx"
    run_type = payload["run_type"]
    inputs = payload["inputs"]
    with db.connect() as connection:
        if current_revision(connection, month_id) != payload["expected_revision"]:
            raise JobFailure("月份已被更新，请刷新后重新提交计算")
        roster = roster_items(connection)
        previous = month_record(connection, month_id) if run_type == "recalculate" else None

    with TemporaryDirectory(dir=settings.data_dir) as temporary:
        temp = Path(temporary)
        employees = temp / "employees.json"
        employees.write_text(json_text(roster), encoding="utf-8")
        if previous:
            corrections = temp / "corrections.json"
            selected = previous["corrections"]
            base_path = previous["run"]["workbook_path"]
            options = ["--employees-json-file", employees]
            if correction_id := payload.get("fast_correction_id"):
                selected = [item for item in selected if item["id"] == correction_id]
                base_path = previous["version"]["workbook_path"]
                options = ["--fast-statistic-only"]
            corrections.write_text(json_text(selected), encoding="utf-8")
            run_python("apply_corrections.py", "--input", base_path, "--output", output,
                       "--corrections-json-file", corrections, *options)
        else:
            source = inputs["workbook"]["path"] if run_type == "workbook" else temp / "merged.xlsx"
            if run_type == "source_merge":
                template = settings.fulltime_template
                if template is None:
                    template = temp / "template.xlsx"
                    write_blank_template(template)
                elif not template.is_file():
                    raise JobFailure("HR_FULLTIME_TEMPLATE 指定的全职模板不存在")
                run_python("build_fulltime_source.py", "--template", template,
                           "--monthly", inputs["monthly"]["path"], "--raw", inputs["raw"]["path"],
                           "--output", source, "--month", month, "--year", year,
                           "--employees-json-file", employees)
            run_python("run_attendance.py", source, "--output", output, "--month", month)
        snapshot = invoke_engine({"operation": "fulltime-preview", "source": {"path": str(output)}})

    if (snapshot["workbook"]["year"], snapshot["workbook"]["month"]) != (year, month):
        output.unlink(missing_ok=True)
        raise JobFailure("工作簿月份与核算月份不一致")

    def save(connection):
        if current_revision(connection, month_id) != payload["expected_revision"]:
            output.unlink(missing_ok=True)
            raise JobFailure("计算期间月份已更新，结果未覆盖当前版本")
        if previous:
            version = legacy.save_version(connection, {
                "month_id": month_id, "run_id": previous["current_run_id"], "snapshot": snapshot,
                "workbook_path": str(output), "label": "人工复核草稿", "actor": job["actor"],
            }, commit=False)
            result = {"month_id": month_id, "run_id": previous["current_run_id"], "version_id": version["id"]}
        else:
            result = legacy.persist_analysis(connection, {
                "analysis_id": job["id"], "preview": snapshot, "workbook_path": str(output),
                "input_hashes": {key: item["sha256"] for key, item in inputs.items()},
                "algorithm_version": ALGORITHM_VERSION, "actor": job["actor"],
            }, commit=False)
        revision = advance_revision(connection, month_id)
        record_event(connection, "attendance.fulltime.version_created.v1", month_id, revision,
                     {**result, "algorithm_version": ALGORITHM_VERSION}, causation_id=job["id"])
        return result

    return save


@router.get("/months")
def months(request: Request, session: Session = Depends(require_session)):
    with database(request).connect() as connection:
        return {"items": legacy.list_months(connection, {})}


def public_version(version):
    return {key: value for key, value in version.items() if key != "workbook_path"} if version else None


@router.get("/months/{month_id}")
def month_detail(month_id: MonthId, request: Request, session: Session = Depends(require_session)):
    with database(request).connect() as connection:
        month = month_record(connection, month_id)
    month["run"] = public_version(month["run"])
    month["version"] = public_version(month["version"])
    month["versions"] = [public_version(version) for version in month["versions"]]
    return month


@router.get("/months/{month_id}/versions")
def versions(month_id: MonthId, request: Request, session: Session = Depends(require_session)):
    with database(request).connect() as connection:
        month = month_record(connection, month_id)
    return {"items": [public_version(version) for version in month["versions"]]}


@router.get("/roster", response_model=Roster)
def get_roster(request: Request, session: Session = Depends(require_session)):
    with database(request).connect() as connection:
        return {"items": roster_items(connection)}


@router.patch("/roster", response_model=Roster)
def save_roster(body: Roster, request: Request, session: Session = Depends(require_session)):
    with database(request).connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        records = {item["name"]: item for item in roster_items(connection)}
        for item in body.items:
            name = item.name.strip()
            if not name or not item.attendance_type.strip():
                fail(422, "ROSTER_INVALID", "新增人员必须填写姓名、入职日期和出勤类型")
            records[name] = item.model_dump(mode="json", exclude_none=True) | {"name": name}
        ordered = sorted(records.values(), key=lambda item: (item["entry_date"], item["name"]))
        connection.execute("DELETE FROM fulltime_roster")
        connection.executemany("INSERT INTO fulltime_roster VALUES (?, ?)",
                               ((index, json_text(item)) for index, item in enumerate(ordered)))
    return {"items": ordered}


@router.post("/months/{month_id}/runs", status_code=202, response_model=JobAccepted)
def create_run(month_id: MonthId, body: RunRequest, request: Request, response: Response,
               session: Session = Depends(require_session),
               idempotency_key: str = Header(min_length=1, max_length=128)):
    with database(request).connect() as connection:
        inputs = {}
        file_ids = {"workbook": body.workbook_id, "monthly": body.monthly_file_id, "raw": body.raw_file_id}
        for kind, file_id in file_ids.items():
            if file_id:
                inputs[kind] = get_source(connection, request.app.state.settings.data_dir,
                                          session.actor, "fulltime", file_id, kind)
        if body.run_type == "recalculate":
            month_record(connection, month_id)
        payload = {"run_type": body.run_type, "inputs": inputs, "expected_revision": body.expected_revision}
        job_id = enqueue(connection, "fulltime", session.actor, month_id, payload, idempotency_key)
    response.headers["Location"] = f"/api/v1/jobs/{job_id}"
    return JobAccepted(job_id=job_id)


@router.post("/months/{month_id}/corrections", status_code=201)
def create_correction(month_id: MonthId, body: Correction, request: Request,
                      session: Session = Depends(require_session)):
    with database(request).connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        check_revision(connection, month_id, body.expected_revision)
        row = connection.execute("SELECT v.snapshot_json FROM attendance_months m JOIN result_versions v "
                                 "ON v.id=m.current_version_id WHERE m.id=?", (month_id,)).fetchone()
        if row is None:
            fail(404, "VERSION_NOT_FOUND", "请先生成该月份的计算版本")
        snapshot = json.loads(row["snapshot_json"])
        employee = "".join(body.employee_name.split())
        matches = [item for item in snapshot["attendance"]["rows"] if "".join(item["name"].split()) == employee]
        if len(matches) != 1:
            fail(422, "CORRECTION_INVALID", "未找到唯一的员工考勤记录")
        target = matches[0]
        if body.target_type == "day":
            item = next((item for item in target["days"] if item["day"] == body.day), None)
            if item is None:
                fail(422, "CORRECTION_INVALID", "日期不在考勤表内")
            original, value = item["status"], str(body.new_value).strip()[:60]
        elif body.target_type == "remark":
            original, value = target["remark"], str(body.new_value).strip()[:500]
        else:
            stats = next(item for item in snapshot["statistics"]["rows"] if item["row_number"] == target["row_number"])
            if body.field_key not in {column["key"] for column in snapshot["statistics"]["columns"]}:
                fail(422, "CORRECTION_INVALID", "统计字段不存在")
            original, value = stats[body.field_key], body.new_value
            if isinstance(value, str):
                value = value.strip()[:80]
        if value == "" or str(value) == str(original):
            fail(422, "CORRECTION_INVALID", "请填写与当前结果不同的修订值")
        if body.feedback_id:
            feedback = legacy.get_feedback_by_id(connection, body.feedback_id)
            if not feedback or feedback["month_id"] != month_id or feedback["employee_name"] != target["name"]:
                fail(422, "CORRECTION_INVALID", "反馈与修订对象不匹配")
        try:
            item = legacy.add_correction(connection, body.model_dump(exclude={"expected_revision"}) | {
                "month_id": month_id, "operator_name": session.actor, "employee_name": target["name"],
                "original_value": original, "new_value": value,
            }, commit=False)
        except ValueError as error:
            fail(422, "CORRECTION_INVALID", str(error))
        if body.feedback_id:
            legacy.update_feedback(connection, {"id": body.feedback_id, "patch": {"status": "处理中",
                "review_note": f"已由{session.actor}在月度工作台登记人工修订", "reviewed_at": utcnow()}}, commit=False)
        revision = advance_revision(connection, month_id)
        record_event(connection, "attendance.fulltime.correction_recorded.v1", month_id, revision,
                     {"month_id": month_id, "correction_id": item["id"], "operation": "create"},
                     correlation_id=request.state.request_id)
        payload = {"run_type": "recalculate", "inputs": {}, "expected_revision": revision}
        previous_job = connection.execute("SELECT status FROM jobs WHERE domain='fulltime' AND resource=? "
                                          "ORDER BY created_at DESC,rowid DESC LIMIT 1", (month_id,)).fetchone()
        if body.target_type == "statistic" and (previous_job is None or previous_job["status"] == "succeeded"):
            payload["fast_correction_id"] = item["id"]
        job_id = enqueue(connection, "fulltime", session.actor, month_id, payload, None)
    return {"correction": item, "revision": revision, "job_id": job_id}


@router.post("/corrections/{correction_id}/reversals")
def reverse_correction(correction_id: str, body: Revision, request: Request,
                       session: Session = Depends(require_session)):
    with database(request).connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT month_id,status FROM corrections WHERE id=?", (correction_id,)).fetchone()
        if row is None:
            fail(404, "CORRECTION_NOT_FOUND", "找不到修订记录")
        month_id = row["month_id"]
        if row["status"] == "reversed":
            return {"revision": current_revision(connection, month_id)}
        check_revision(connection, month_id, body.expected_revision)
        legacy.delete_correction(connection, {"month_id": month_id, "correction_id": correction_id,
                                              "actor": session.actor}, commit=False)
        revision = advance_revision(connection, month_id)
        record_event(connection, "attendance.fulltime.correction_recorded.v1", month_id, revision,
                     {"month_id": month_id, "correction_id": correction_id, "operation": "reverse"})
        job_id = enqueue(connection, "fulltime", session.actor, month_id,
                         {"run_type": "recalculate", "inputs": {}, "expected_revision": revision}, None)
    return {"revision": revision, "job_id": job_id}


def publication(row):
    return {"publication_id": row["id"], "month_id": row["month_id"], "current_version_id": row["version_id"],
            "published_version_id": row["version_id"], "revision": row["revision"]}


@router.post("/months/{month_id}/publications", status_code=201)
def publish(month_id: MonthId, body: Publish, request: Request, response: Response,
            session: Session = Depends(require_session), idempotency_key: str = Header(min_length=1, max_length=128)):
    with database(request).connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM fulltime_publications WHERE month_id=? AND actor=? AND idempotency_key=?",
                                 (month_id, session.actor, idempotency_key)).fetchone()
        if row:
            if (row["version_id"], row["expected_revision"]) != (body.version_id, body.expected_revision):
                fail(409, "IDEMPOTENCY_CONFLICT", "该重试标识已用于不同请求")
            result = publication(row)
        else:
            check_revision(connection, month_id, body.expected_revision)
            try:
                legacy.publish_version(connection, {"month_id": month_id, "version_id": body.version_id,
                                                    "actor": session.actor}, commit=False)
            except ValueError as error:
                fail(404, "VERSION_NOT_FOUND", str(error))
            revision = advance_revision(connection, month_id)
            row = {"id": uuid4().hex, "month_id": month_id, "version_id": body.version_id, "revision": revision}
            connection.execute("INSERT INTO fulltime_publications VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
                row["id"], month_id, body.version_id, session.actor, idempotency_key, body.expected_revision,
                revision, utcnow(),
            ))
            result = publication(row)
            record_event(connection, "attendance.fulltime.version_published.v1", month_id, revision,
                         {"month_id": month_id, "version_id": body.version_id}, correlation_id=request.state.request_id)
    response.headers["Location"] = f"/api/v1/attendance/fulltime/publications/{result['publication_id']}"
    return result


@router.get("/publications/{publication_id}")
def read_publication(publication_id: str, request: Request, session: Session = Depends(require_session)):
    with database(request).connect() as connection:
        row = connection.execute("SELECT * FROM fulltime_publications WHERE id=?", (publication_id,)).fetchone()
    if row is None:
        fail(404, "PUBLICATION_NOT_FOUND", "找不到发布记录")
    return publication(row)


@router.get("/versions/{version_id}/workbook", response_class=FileResponse)
def workbook(version_id: str, request: Request, session: Session = Depends(require_session)):
    with database(request).connect() as connection:
        row = connection.execute("SELECT * FROM result_versions WHERE id=?", (version_id,)).fetchone()
    if row is None or not Path(row["workbook_path"]).is_file():
        fail(404, "WORKBOOK_NOT_FOUND", "找不到对应版本的工作簿")
    return FileResponse(row["workbook_path"], media_type=XLSX,
                        filename=f"全职考勤-{row['month_id'][4:]}-版本{row['version_number']}.xlsx")
