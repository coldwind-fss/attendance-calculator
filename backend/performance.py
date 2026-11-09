import asyncio
import os
import re
import secrets
import sys
from base64 import urlsafe_b64encode
from contextlib import asynccontextmanager
from typing import Literal
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .config import ROOT
from .http import fail
from .performance_workflow import OKR_NODES, UNKNOWN, WorkflowNode, kpi_node
from .session import COOKIE, Session, read_session


router = APIRouter(prefix="/performance", tags=["performance"])
compat = APIRouter(prefix="/hrms/performance", include_in_schema=False)
PUBLIC_PAGES = (
    "target-draft-page", "target-manager-page", "target-bp-page", "selfeval-page", "mgr-page", "bp-page",
    "result-page", "result-bp-review-page", "kpi-page", "kpi-page-b64", "kpi-confirm-page", "okr-fill",
    "okr-weekly-fill", "okr-target-manager", "okr-target-bp", "okr-target-confirm", "okr-self-review",
    "okr-manager-grade", "okr-bp-grade", "okr-result-confirm", "okr-result-bp",
)
PUBLIC_ACTIONS = frozenset((
    "submit-target-draft", "adjust-target-review", "review-target-manager", "review-target-draft", "submit",
    "submit-mgr", "submit-bp", "sign-otp-status", "request-sign-otp", "verify-sign-otp", "submit-kpi",
    "submit-result", "review-result-bp", "okr-sign-otp", "okr-sign-verify", "okr-action",
))
ADMIN_PAGES = ("admin-target-manager-page", "admin-target-bp-page", "admin-manager-page", "admin-bp-page", "okr-admin-open", "data")
ADMIN_READS = frozenset((
    "", "preview.html", "healthz", "data", "mgr-data", "bp-data", "kpi-data", "kpi-targets", "target-drafts", "result-data",
    "employee-roster", "monthly-events", "workflow-resets", "performance-exclusions", "queue", "delivery-status",
    "pending-count", "signature-audit", "verify-signature-integrity", "archive-view", "okr-admin", "okr-data", "okr-archive-view",
    "oa-approval-status", "workflow-events", "ui-version", "ping", "employee-overrides",
))
ADMIN_WRITES = frozenset((
    "invite-target-draft", "request-target-adjustment", "withdraw-target-invitation", "workflow-reminder",
    "generate-kpi-pages", "employee-overrides", "performance-exclusions", "workflow-resets", "monthly-events",
    "reject-signature", "okr-invite", "okr-remind", "okr-send-self-review", "oa-approval-retry", "send-bot-msg", "send-bot-batch",
))


@asynccontextmanager
async def runtime(settings):
    if not settings.performance_compat:
        yield None
        return
    key = secrets.token_hex(32)
    compatibility_directory = ROOT / "backend" / "adapters" / "performance"
    public_origin = os.getenv("HR_PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    environment = os.environ | {
        "HR_BRIDGE_KEY": key,
        "HR_BRIDGE_PYTHON": sys.executable,
        "DATA_DIR": str(settings.data_dir / "performance"),
        "PERFORMANCE_MODULE_DIR": os.getenv("HR_PERFORMANCE_TEMPLATE_DIR", str(compatibility_directory)),
        "PUBLIC_SERVER_URL": f"{public_origin}/hrms/performance",
        "ADMIN_PASSWORD": secrets.token_urlsafe(32),
        "LINK_SIGNING_SECRET": os.getenv("HR_PERFORMANCE_LINK_SECRET", "") or secrets.token_hex(32),
        "DISABLE_EXTERNAL_NOTIFICATIONS": "1",
    }
    if os.getenv("HR_ENABLE_PERFORMANCE_EXTERNAL") == "1" and public_origin.startswith("https://"):
        environment["DISABLE_EXTERNAL_NOTIFICATIONS"] = "0"
    else:
        environment["DINGTALK_OA_ENABLED"] = "false"
    # Preserve issued legacy links when a signing key is provisioned. A local
    # compatibility preview can use the same key across application restarts.
    key_file = settings.data_dir / "performance-link-key"
    if not os.getenv("HR_PERFORMANCE_LINK_SECRET"):
        try:
            with key_file.open("x", encoding="ascii") as file:
                file.write(environment["LINK_SIGNING_SECRET"])
            key_file.chmod(0o600)
        except FileExistsError:
            environment["LINK_SIGNING_SECRET"] = key_file.read_text(encoding="ascii")
    child = await asyncio.create_subprocess_exec("node", str(ROOT / "backend" / "performance_runtime.cjs"),
                                                 env=environment, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.DEVNULL)
    try:
        line = await asyncio.wait_for(child.stdout.readline(), timeout=15)
        if not line.strip().isdigit():
            raise RuntimeError("绩效兼容服务启动失败")
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{int(line)}", trust_env=False, timeout=30,
                                     headers={"X-HR-Bridge-Key": key}) as client:
            yield client
    finally:
        child.stdin.close()
        try:
            await asyncio.wait_for(child.wait(), timeout=5)
        except TimeoutError:
            child.terminate()
            await child.wait()


def client_for(request):
    client = request.app.state.performance
    if client is None:
        fail(503, "PERFORMANCE_NOT_CONFIGURED", "绩效兼容服务尚未启用")
    return client


def bridge_headers(session):
    headers = {"X-HR-Bridge-Scope": session.role}
    if session.role == "performance":
        headers["X-HR-Bridge-Actor"] = urlsafe_b64encode(session.actor.encode()).decode()
    return headers


def require_performance(request: Request):
    session = read_session(request, False)
    request.state.performance_session = session
    return session


async def call(request, path, method="GET", data=None):
    client = client_for(request)
    try:
        result = await client.request(method, path, json=data, headers=bridge_headers(request.state.performance_session))
    except httpx.RequestError:
        fail(502, "PERFORMANCE_UNAVAILABLE", "绩效兼容服务暂不可用")
    if result.status_code >= 400:
        fail(result.status_code, "PERFORMANCE_ACTION_FAILED", "绩效请求未完成，请检查当前状态或资料配置")
    return result.json()


class Action(BaseModel):
    action: Literal["invite_target", "adjust_target", "withdraw_target", "remind", "reject_signature", "reset",
                    "invite_okr", "remind_okr", "send_okr_self_review", "retry_oa"]
    fields: dict


class Initiate(BaseModel):
    employee_id: str = Field(min_length=1, max_length=100)
    assessment_type: Literal["kpi", "okr"]


class Reminder(Initiate):
    action: Literal["remind", "send_self_review"] = "remind"


class SignatureVerification(BaseModel):
    employee_id: str = Field(min_length=1, max_length=100)
    month: str
    document_type: Literal["kpi", "result"]


class ApprovalRow(BaseModel):
    approval_id: str
    month: str
    department: str
    status: str
    instance_id: str = ""
    last_error: str = ""
    can_retry: bool


class ApprovalPage(BaseModel):
    channel: dict
    approvals: dict
    items: list[ApprovalRow]


ACTIONS = {
    "invite_target": "invite-target-draft", "adjust_target": "request-target-adjustment",
    "withdraw_target": "withdraw-target-invitation", "remind": "workflow-reminder", "reject_signature": "reject-signature",
    "reset": "workflow-resets", "invite_okr": "okr-invite", "remind_okr": "okr-remind",
    "send_okr_self_review": "okr-send-self-review", "retry_oa": "oa-approval-retry",
}


class AssessmentRow(BaseModel):
    employee_id: str
    month: str
    assessment_type: Literal["kpi", "okr"]
    status: str
    employee_name: str = ""
    department: str = ""
    result: str | int | float | None = None
    workflow: WorkflowNode


class AssessmentPage(BaseModel):
    items: list[AssessmentRow]


def normalize_month(raw):
    match = re.fullmatch(r"(\d{4})年(\d{1,2})月", str(raw or ""))
    return f"{match[1]}-{int(match[2]):02}" if match else str(raw or "")


def legacy_month(month):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        fail(422, "MONTH_INVALID", "请选择考核月份")
    year, number = month.split("-")
    return f"{year}年{int(number)}月"


async def get_roster(request, month):
    payload = await call(request, "/employee-roster?" + urlencode({"assessmentMonth": legacy_month(month)}))
    return [employee for employee in payload["employees"] if normalize_month(employee["assessmentStartMonth"]) <= month
            and (not employee["assessmentInactiveFromMonth"] or month < normalize_month(employee["assessmentInactiveFromMonth"]))]


async def assessment_rows(request, kind):
    paths = ["okr-data"] if kind == "okr" else ["target-drafts", "kpi-targets", "kpi-data?summary=1", "data", "mgr-data", "bp-data", "result-data?summary=1"]
    responses = await asyncio.gather(*(call(request, f"/{path}") for path in paths))
    merged = {}
    sources = {}
    for path, data in zip(paths, responses):
        for key, item in data.items():
            if not isinstance(item, dict):
                continue
            emp = item.get("emp") or {}
            employee_id = str(item.get("empId") or emp.get("empId") or key.split("|")[0])
            month = normalize_month(item.get("month") or key.partition("|")[2])
            if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
                continue
            reference = (employee_id, month)
            old = merged.get(reference)
            records = sources.setdefault(reference, {})
            records[path] = item
            status = {
                "target-drafts": item.get("status", "待确认目标"), "kpi-targets": "待签署目标", "kpi-data?summary=1": "目标已确认",
                "data": "员工已自评", "mgr-data": "上级已评分", "bp-data": "BP 已核定",
                "result-data?summary=1": "已归档" if item.get("sealed") else "待 BP 复核",
                "okr-data": item.get("status", "待处理"),
            }[path]
            result = item.get("bpGrade", item.get("bpScore"))
            workflow = OKR_NODES.get(status, UNKNOWN) if kind == "okr" else kpi_node(records)
            merged[reference] = AssessmentRow(employee_id=employee_id, month=month, assessment_type=kind, status=status,
                employee_name=str(emp.get("name") or item.get("name") or (old.employee_name if old else "")),
                department=str(emp.get("dept") or item.get("dept") or (old.department if old else "")),
                result=result if result is not None else (old.result if old else None), workflow=workflow)
    return sorted(merged.values(), key=lambda row: (row.month, row.employee_id), reverse=True)


@router.get("/months")
async def months(request: Request, session: Session = Depends(require_performance)):
    kpi, okr = await asyncio.gather(assessment_rows(request, "kpi"), assessment_rows(request, "okr"))
    return {"items": sorted({row.month for row in kpi + okr}, reverse=True)}


@router.get("/months/{month}/assessments", response_model=AssessmentPage)
async def assessments(month: str, request: Request, assessment_type: Literal["kpi", "okr"] = "kpi",
                      employee_id: str | None = None, session: Session = Depends(require_performance)):
    items = await assessment_rows(request, assessment_type)
    return {"items": [item for item in items if item.month == month and (not employee_id or item.employee_id == employee_id)]}


@router.get("/months/{month}/roster")
async def roster(month: str, request: Request, session: Session = Depends(require_performance)):
    people = await get_roster(request, month)
    return {"items": [{"employee_id": item["assessmentId"], "employee_name": item["nick"], "department": item["department"]}
                      for item in people]}


@router.post("/months/{month}/invitations")
async def invite(month: str, body: Initiate, request: Request, session: Session = Depends(require_performance)):
    data = {"empId": body.employee_id, "month": legacy_month(month)}
    if body.assessment_type == "okr":
        return await call(request, "/okr-invite", "POST", data)
    employee = next((item for item in await get_roster(request, month) if item["assessmentId"] == body.employee_id), None)
    if employee is None:
        fail(404, "EMPLOYEE_NOT_FOUND", "该员工不在当月考核名册内")
    data["emp"] = {"name": employee["nick"], "realName": employee["realName"], "dept": employee["department"],
                   "position": employee["title"], "directMgr": employee.get("directMgr", ""), "hrbp": employee.get("hrbp", "")}
    return await call(request, "/invite-target-draft", "POST", data)


@router.post("/months/{month}/reminders")
async def remind(month: str, body: Reminder, request: Request, session: Session = Depends(require_performance)):
    if body.action == "send_self_review" and body.assessment_type != "okr":
        fail(422, "PERFORMANCE_ACTION_INVALID", "该操作仅用于 OKR")
    if body.assessment_type == "kpi":
        route = "/workflow-reminder"
    else:
        route = "/okr-remind" if body.action == "remind" else "/okr-send-self-review"
    period = legacy_month(month)
    if body.assessment_type == "okr":
        records = await call(request, "/okr-data")
        record = records.get(f"{body.employee_id}|{period}")
        if not record:
            fail(404, "ASSESSMENT_NOT_FOUND", "找不到可办理的 OKR")
        if OKR_NODES.get(record.get("status"), UNKNOWN).admin_action != body.action:
            fail(409, "PERFORMANCE_STAGE_CONFLICT", "当前 OKR 节点不支持该操作，请刷新后重试")
    return await call(request, route, "POST", {"empId": body.employee_id, "month": period})


@router.post("/actions")
async def perform(body: Action, request: Request, session: Session = Depends(require_performance)):
    return await call(request, f"/{ACTIONS[body.action]}", "POST", body.fields)


@router.get("/oa-approvals", response_model=ApprovalPage)
async def approvals(request: Request, session: Session = Depends(require_performance)):
    response = await call(request, "/oa-approval-status")
    items = []
    for key, record in sorted(response["approvals"].items(), reverse=True):
        period, _, group = key.partition("|")
        items.append(ApprovalRow(
            approval_id=key, month=normalize_month(record.get("month") or period),
            department=record.get("department") or group, status=record["status"],
            instance_id=record.get("instanceId") or "", last_error=(record.get("lastError") or {}).get("message", ""),
            can_retry=bool(record.get("snapshot")) and record["status"] not in ("submitted", "submitting"),
        ))
    return response | {"items": items}


@router.post("/oa-approvals/{approval_id:path}/retries")
async def retry_approval(approval_id: str, request: Request, session: Session = Depends(require_performance)):
    period, _, group = approval_id.partition("|")
    if not group or not re.fullmatch(r"\d{4}年(?:0?[1-9]|1[0-2])月", period):
        fail(422, "APPROVAL_INVALID", "请选择有效的部门审批记录")
    return await call(request, "/oa-approval-retry", "POST", {"month": period, "department": group})


@router.get("/signature-audits")
async def audits(request: Request, session: Session = Depends(require_performance)):
    return await call(request, "/signature-audit")


@router.post("/signature-verifications")
async def verify_signatures(body: SignatureVerification, request: Request, session: Session = Depends(require_performance)):
    query = urlencode({"empId": body.employee_id, "month": legacy_month(body.month), "type": body.document_type})
    return await call(request, f"/verify-signature-integrity?{query}")


def allow_compat(method, path):
    parts = path.split("/")
    if method == "GET" and len(parts) > 1 and parts[0] in PUBLIC_PAGES:
        return "public"
    if method == "POST" and path in PUBLIC_ACTIONS:
        return "public"
    if method == "GET" and (path in ADMIN_READS or (len(parts) > 1 and parts[0] in ADMIN_PAGES)):
        return "admin"
    if method == "POST" and path in ADMIN_WRITES:
        return "admin"
    fail(404, "PERFORMANCE_ROUTE_NOT_FOUND", "找不到绩效入口")


@compat.api_route("", methods=["GET"])
@compat.api_route("/{path:path}", methods=["GET", "POST"])
async def legacy_route(request: Request, path: str = ""):
    scope = allow_compat(request.method, path)
    if any(part in (".", "..") for part in path.split("/")):
        fail(404, "PERFORMANCE_ROUTE_NOT_FOUND", "找不到绩效入口")
    scope_headers = bridge_headers(read_session(request, True)) if scope == "admin" else {"X-HR-Bridge-Scope": "public"}
    client = client_for(request)
    raw_path = request.scope["raw_path"].decode("ascii").removeprefix("/hrms/performance") or "/"
    query = request.url.query
    target = raw_path + (f"?{query}" if query else "")
    headers = scope_headers | {"Content-Type": request.headers.get("Content-Type", "application/json")}
    if request.method == "POST" and scope == "public":
        origin = request.headers.get("Origin")
        if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
            fail(403, "ORIGIN_INVALID", "请求来源无效")
        if path in ("submit-mgr", "submit-bp") and request.cookies.get(COOKIE):
            try:
                principal = read_session(request, True)
            except HTTPException as error:
                if error.status_code != 401:
                    raise
            else:
                headers.update(bridge_headers(principal))
    try:
        upstream = await client.send(client.build_request(request.method, target, content=await request.body(), headers=headers),
                                     stream=True)
    except httpx.RequestError:
        fail(502, "PERFORMANCE_UNAVAILABLE", "绩效兼容服务暂不可用")
    forward = {name: value for name, value in upstream.headers.items() if name in ("content-type", "content-disposition", "location")}
    forward.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Frame-Options": "SAMEORIGIN"})
    if forward.get("location", "").startswith("/") and not forward["location"].startswith("/hrms/performance/"):
        forward["location"] = "/hrms/performance" + forward["location"]
    if "text/event-stream" in forward.get("content-type", ""):
        async def stream():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()
        return StreamingResponse(stream(), status_code=upstream.status_code, headers=forward)
    try:
        content = await upstream.aread()
        return Response(content, status_code=upstream.status_code, headers=forward)
    finally:
        await upstream.aclose()
