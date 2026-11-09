import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field, model_validator

from .config import ROOT
from .database import json_text, record_event, utcnow
from .http import database, fail
from .jobs import JobAccepted, JobFailure, enqueue
from .session import Session, require_session


router = APIRouter(prefix="/hr", tags=["hr"])
Scope = Literal["current", "departed"]
Demand = Literal["新增编制", "离职补缺", "优化替换", "其他"]
FIELDS = set("nickname contractTime certificateNo mobile hourlySalary bankCard bankName notes regularDate birthday age gender maritalStatus ethnicity contractStart contractEnd contractType contractCompany socialSecurity housingFund officeLocation workStartDate education school major householdType idAddress residentialAddress probationFixed probationPerformance probationTotal fixedSalary performanceOkr salaryTotal performanceRatio departureDate departureType departureReason manualActiveRoster".split())
IMPORTED_FIELDS = set("sourceName sourceStatus sourceCenter sourceDepartment sourceGroup sourceTitle sourceHireDate organizationOverride titleOverride".split())


class SyncRequest(BaseModel):
    scope: Scope = "current"


class ProfileRequest(BaseModel):
    fields: dict[str, str]
    expected_revision: int = Field(ge=0)


class ImportedProfile(BaseModel):
    employee_id: str = Field(min_length=1, max_length=200)
    fields: dict[str, str]


class RosterImport(BaseModel):
    records: list[ImportedProfile] = Field(min_length=1, max_length=500)


class StaffingRequest(BaseModel):
    approved_headcount: int = Field(strict=True, ge=0, le=300)
    expected_revision: int = Field(ge=0)


class RecruitmentRequest(BaseModel):
    action: Literal["close", "change_demand_type"]
    demand_type: Demand | None = None
    expected_revision: int = Field(ge=0)

    @model_validator(mode="after")
    def check_demand(self):
        if self.action == "change_demand_type" and self.demand_type is None:
            raise ValueError("请选择需求类型")
        return self


def initialize(db):
    directory = ROOT / "backend" / "adapters" / "hr_schema"
    with db.connect() as connection:
        for migration in sorted(directory.glob("*.sql")):
            connection.executescript(migration.read_text(encoding="utf-8"))
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS hr_snapshots (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS hr_revisions (
                kind TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL, PRIMARY KEY(kind, id)
            );
        """)


def organization(connection, scope="current"):
    row = connection.execute("SELECT payload FROM data_cache WHERE cache_key=?", (scope,)).fetchone()
    if row is None:
        return {"scope": scope, "departments": [], "employees": []}
    return json.loads(row["payload"])


def real_name(value):
    parts = [item.strip() for item in re.split(r"[-—–]", value) if item.strip()]
    return "".join("-".join(parts[1:] if len(parts) > 1 else parts).split()).casefold()


def roster_employees(connection, scope):
    employees = organization(connection, scope)["employees"]
    known = {item["id"] for item in employees}
    canonical_names = {real_name(item["name"]) for item in employees}
    imported = []
    for row in connection.execute("SELECT employee_id,profile_json FROM employee_roster_profiles"):
        fields = json.loads(row["profile_json"])
        name = fields.get("sourceName")
        status = fields.get("sourceStatus", "在职")
        key = row["employee_id"]
        if not name or key in known or (key.startswith("excel:") and real_name(name) in canonical_names):
            continue
        if scope == "departed" and status != "离职":
            continue
        if scope == "current" and (status == "离职" or (status == "在职" and fields.get("manualActiveRoster") != "1")):
            continue
        imported.append({"id": key, "name": name, "nickname": fields.get("nickname", ""),
                         "title": fields.get("sourceTitle", ""), "departmentIds": [], "rosterStatus": status,
                         "hireDate": fields.get("sourceHireDate", ""), "departureDate": fields.get("departureDate", "")})
    return [*employees, *imported]


def revision(connection, kind, key):
    row = connection.execute("SELECT revision FROM hr_revisions WHERE kind=? AND id=?", (kind, key)).fetchone()
    return row["revision"] if row else 0


def update_revision(connection, kind, key, expected):
    if revision(connection, kind, key) != expected:
        fail(409, "HR_RECORD_CHANGED", "记录已更新，请刷新后重试")
    connection.execute("INSERT INTO hr_revisions VALUES (?, ?, ?) "
                       "ON CONFLICT(kind,id) DO UPDATE SET revision=excluded.revision", (kind, key, expected + 1))
    return expected + 1


def save_snapshot(connection, scope, payload):
    from time import time

    snapshot_id = uuid4().hex
    timestamp = utcnow()
    payload = payload | {"scope": scope, "snapshot_id": snapshot_id, "synced_at": timestamp}
    encoded = json_text(payload)
    connection.execute("INSERT INTO hr_snapshots VALUES (?, ?, ?, ?)", (snapshot_id, scope, encoded, timestamp))
    connection.execute("INSERT INTO data_cache VALUES (?, ?, ?) ON CONFLICT(cache_key) "
                       "DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",
                       (scope, encoded, int(time() * 1000)))
    record_event(connection, "hr.organization_snapshot_saved.v1", snapshot_id, 1, {"snapshot_id": snapshot_id})
    return {"snapshot_id": snapshot_id}


def sync_organization(settings, job, payload):
    from .dingtalk import fetch_organization

    scope = payload["scope"]
    fixture = os.getenv("HR_ORGANIZATION_FIXTURE")
    if fixture:
        source = json.loads(Path(fixture).read_text(encoding="utf-8-sig"))
        data = source.get(scope, source)
    else:
        data = fetch_organization(scope)
    if not isinstance(data.get("departments"), list) or not isinstance(data.get("employees"), list):
        raise JobFailure("组织数据缺少部门或员工列表")
    return lambda connection: save_snapshot(connection, scope, data)


@router.get("/organization")
def get_organization(request: Request, scope: Scope = "current", session: Session = Depends(require_session)):
    with database(request).connect() as connection:
        data = organization(connection, scope)
    return {key: value for key, value in data.items() if key != "employees"}


@router.post("/organization-syncs", status_code=202, response_model=JobAccepted)
def start_sync(body: SyncRequest, request: Request, response: Response, session: Session = Depends(require_session)):
    with database(request).connect() as connection:
        job_id = enqueue(connection, "hr", session.actor, body.scope, body.model_dump(), None)
    response.headers["Location"] = f"/api/v1/jobs/{job_id}"
    return JobAccepted(job_id=job_id)


@router.get("/employees")
def employees(request: Request, scope: Scope = "current", department_id: str | None = None,
              q: str = "", cursor: str | None = None, limit: int = Query(50, ge=1, le=100),
              session: Session = Depends(require_session)):
    with database(request).connect() as connection:
        rows = roster_employees(connection, scope)
    filtered = sorted((row for row in rows if (not department_id or department_id in row.get("departmentIds", []))
                       and q.casefold() in (row.get("name", "") + " " + row.get("title", "")).casefold()
                       and (not cursor or row["id"] > cursor)), key=lambda row: row["id"])
    return {"items": filtered[:limit], "next_cursor": filtered[limit - 1]["id"] if len(filtered) > limit else None}


def employee_exists(connection, employee_id):
    return any(employee["id"] == employee_id for scope in ("current", "departed")
               for employee in organization(connection, scope)["employees"])


def profile(connection, employee_id):
    row = connection.execute("SELECT profile_json,updated_at FROM employee_roster_profiles WHERE employee_id=?",
                             (employee_id,)).fetchone()
    if row is None and not employee_exists(connection, employee_id):
        fail(404, "EMPLOYEE_NOT_FOUND", "找不到员工档案")
    return {"employee_id": employee_id, "fields": json.loads(row["profile_json"]) if row else {},
            "updated_at": row["updated_at"] if row else None, "revision": revision(connection, "profile", employee_id)}


@router.get("/employees/{employee_id}/profile")
def get_profile(employee_id: str, request: Request, session: Session = Depends(require_session)):
    with database(request).connect() as connection:
        return profile(connection, employee_id)


@router.patch("/employees/{employee_id}/profile")
def update_profile(employee_id: str, body: ProfileRequest, request: Request, session: Session = Depends(require_session)):
    if body.fields.keys() - FIELDS:
        fail(422, "PROFILE_FIELD_INVALID", "档案中包含不可编辑字段")
    with database(request).connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = profile(connection, employee_id)
        update_revision(connection, "profile", employee_id, body.expected_revision)
        fields = current["fields"] | {key: value.strip()[:500] for key, value in body.fields.items()}
        connection.execute("INSERT INTO employee_roster_profiles VALUES (?, ?, ?) ON CONFLICT(employee_id) "
                           "DO UPDATE SET profile_json=excluded.profile_json,updated_at=excluded.updated_at",
                           (employee_id, json_text(fields), utcnow()))
        return profile(connection, employee_id)


@router.post("/roster-imports", status_code=201)
def import_roster(body: RosterImport, request: Request, session: Session = Depends(require_session)):
    with database(request).connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        now = utcnow()
        for item in body.records:
            if item.fields.keys() - (FIELDS | IMPORTED_FIELDS):
                fail(422, "PROFILE_FIELD_INVALID", "导入档案包含无效字段")
            row = connection.execute("SELECT profile_json FROM employee_roster_profiles WHERE employee_id=?",
                                     (item.employee_id,)).fetchone()
            saved = json.loads(row["profile_json"]) if row else {}
            manual = {key: value for key, value in saved.items() if key in FIELDS and str(value).strip()}
            fields = {key: value.strip()[:500] for key, value in item.fields.items()} | manual
            connection.execute("INSERT INTO employee_roster_profiles VALUES (?, ?, ?) ON CONFLICT(employee_id) "
                               "DO UPDATE SET profile_json=excluded.profile_json,updated_at=excluded.updated_at",
                               (item.employee_id, json_text(fields), now))
            update_revision(connection, "profile", item.employee_id, revision(connection, "profile", item.employee_id))
    return {"imported": len(body.records), "updated_at": now}


def staffing_rows(connection):
    current = organization(connection)
    configured = json.loads(os.getenv("HR_CANONICAL_DEPARTMENTS", "[]"))
    departments = configured or [item | {"sourceIds": [item["id"]]} for item in current["departments"]]
    plans = {row["department_id"]: row for row in connection.execute("SELECT * FROM staffing_plans")}
    for department in departments:
        key = department["id"]
        source_ids = set(department["sourceIds"])
        members = [employee for employee in current["employees"] if employee.get("rosterStatus") != "离职"
                   and source_ids.intersection(employee.get("departmentIds", []))]
        plan = plans.get(key)
        approved = plan["approved_headcount"] if plan else len(members)
        remaining = approved - len(members)
        yield {"department_id": key, "department_name": department["name"], "approved_headcount": approved,
               "current_headcount": len(members), "remaining_headcount": remaining,
               "status": "超编" if remaining < 0 else "满编" if remaining == 0 else "正常",
               "revision": revision(connection, "staffing", key)}


@router.get("/staffing")
def staffing(request: Request, session: Session = Depends(require_session)):
    with database(request).connect() as connection:
        return {"items": list(staffing_rows(connection))}


@router.put("/staffing/{department_id}")
def update_staffing(department_id: str, body: StaffingRequest, request: Request,
                    session: Session = Depends(require_session)):
    with database(request).connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        department = next((row for row in staffing_rows(connection) if row["department_id"] == department_id), None)
        if department is None:
            fail(404, "DEPARTMENT_NOT_FOUND", "找不到对应部门")
        update_revision(connection, "staffing", department_id, body.expected_revision)
        connection.execute("INSERT INTO staffing_plans VALUES (?, ?, ?, ?) ON CONFLICT(department_id) "
                           "DO UPDATE SET approved_headcount=excluded.approved_headcount,updated_at=excluded.updated_at",
                           (department_id, department["department_name"], body.approved_headcount, utcnow()))
        return next(row for row in staffing_rows(connection) if row["department_id"] == department_id)


@router.get("/recruitment/requests")
def recruitment(request: Request, status: str | None = None, department_id: str | None = None,
                demand_type: Demand | None = None, month: str | None = None, cursor: str | None = None,
                limit: int = Query(50, ge=1, le=100), session: Session = Depends(require_session)):
    with database(request).connect() as connection:
        staffing = {row["department_id"]: row for row in staffing_rows(connection)}
        rows = [dict(row) for row in connection.execute("SELECT * FROM recruitment_requests ORDER BY id")]
        filtered = [row for row in rows if (not status or row["status"] == status)
                    and (not department_id or row["department_id"] == department_id)
                    and (not demand_type or row["demand_type"] == demand_type)
                    and (not month or row["created_at"].startswith(month)) and (not cursor or row["id"] > cursor)]
        items = [row | {"revision": revision(connection, "recruitment", row["id"]),
                        "capacity": staffing.get(row["department_id"])} for row in filtered[:limit]]
    return {"items": items, "next_cursor": items[-1]["id"] if len(filtered) > limit else None}


@router.get("/recruitment/{view}")
def recruitment_view(view: Literal["dashboard", "todos"], request: Request,
                     session: Session = Depends(require_session)):
    with database(request).connect() as connection:
        approvals = [dict(row) for row in connection.execute("SELECT * FROM recruitment_approvals WHERE status='PENDING'")]
        warnings = [row for row in staffing_rows(connection) if row["remaining_headcount"] <= 0]
        open_requests = [dict(row) for row in connection.execute("SELECT * FROM recruitment_requests WHERE status IN ('RECRUITING','URGENT')")]
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat().replace("+00:00", "Z")
        stale = [row for row in open_requests if row["created_at"] < cutoff]
        if view == "todos":
            return {"pending_approvals": approvals, "capacity_warnings": [row for row in warnings if row["remaining_headcount"] < 0],
                    "stale_requests": stale}
        snapshots = [dict(row) for row in connection.execute("SELECT * FROM recruitment_monthly_snapshots ORDER BY month DESC")]
    local = datetime.now(timezone(timedelta(hours=8)))
    prior = (local.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    open_count = len(open_requests)
    previous = next((row["open_positions"] for row in snapshots if row["month"] == prior), open_count)
    return {"open_positions": open_count, "open_positions_mom": open_count - previous, "pending_approvals": len(approvals),
            "capacity_warnings": warnings, "monthly_snapshots": snapshots, "stale_positions": len(stale)}


@router.patch("/recruitment/requests/{request_id}")
def update_recruitment(request_id: str, body: RecruitmentRequest, request: Request,
                       session: Session = Depends(require_session)):
    with database(request).connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM recruitment_requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            fail(404, "RECRUITMENT_NOT_FOUND", "找不到招聘需求")
        next_revision = update_revision(connection, "recruitment", request_id, body.expected_revision)
        status = "CLOSED" if body.action == "close" else row["status"]
        demand = body.demand_type if body.action == "change_demand_type" else row["demand_type"]
        updated_at = utcnow()
        connection.execute("UPDATE recruitment_requests SET status=?,demand_type=?,updated_at=? WHERE id=?",
                           (status, demand, updated_at, request_id))
    return dict(row) | {"status": status, "demand_type": demand, "updated_at": updated_at, "revision": next_revision}
