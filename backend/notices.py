import asyncio
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field

from .adapters.attendance.fulltime import platform_store as legacy

from .database import Database, json_text, next_event_revision, record_event, utcnow
from .fulltime import MonthId, check_revision, month_record
from .http import database, fail
from .jobs import JobFailure, enqueue
from .parttime import invoke_engine
from .session import Session, require_session


router = APIRouter(prefix="/attendance/fulltime", tags=["attendance notices"])
public = APIRouter(prefix="/attendance", tags=["attendance public"])
log = logging.getLogger(__name__)
RECEIPT_SWEEP_SECONDS = 15 * 60


def record_auto_receipt(connection, notice):
    delivery = connection.execute("SELECT id FROM notification_deliveries WHERE notice_token=? "
                                  "AND delivery_type NOT IN ('test','revision_test') ORDER BY created_at LIMIT 1",
                                  (notice["token"],)).fetchone()
    record_event(connection, "attendance.fulltime.notice_acknowledged.v1", delivery["id"], 1,
                 {"month_id": notice["month_id"], "notice_id": delivery["id"], "ack_source": "auto_24h"})


def auto_acknowledge(db):
    with db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        return legacy.auto_acknowledge_due_notices(connection, on_acknowledged=record_auto_receipt)


async def receipt_worker(db, stopping):
    while not stopping.is_set():
        try:
            await asyncio.to_thread(auto_acknowledge, db)
        except Exception:
            log.exception("全职考勤自动确认扫描失败")
        try:
            await asyncio.wait_for(stopping.wait(), RECEIPT_SWEEP_SECONDS)
        except TimeoutError:
            pass


class Recipient(BaseModel):
    employee_name: str
    recipient_user_id: str
    display_name: str | None = None
    source: str = "manual"


class Recipients(BaseModel):
    items: list[Recipient]


class Exclusions(BaseModel):
    items: list[str]


class Notification(BaseModel):
    version_id: str
    expected_revision: int = Field(ge=0)
    delivery_type: Literal["test", "formal", "revision", "revision_test"]
    employee_names: list[str] = Field(min_length=1, max_length=200)
    test_recipient_user_id: str | None = None


class DayItem(BaseModel):
    day: int = Field(ge=1, le=31)
    requested_status: str = Field(default="", max_length=40)


class Feedback(BaseModel):
    category: Literal["每日状态", "请假/出差/外出", "补卡/迟到/缺卡", "补贴/全勤/餐补", "其他"]
    detail: str = Field(min_length=1, max_length=3000)
    field_key: str | None = None
    day_items: list[DayItem] = Field(default_factory=list, max_length=31)


class Review(BaseModel):
    status: Literal["待玖杉处理", "处理中", "待员工补充", "已处理"]
    review_note: str = Field(default="", max_length=3000)


def version_notice(snapshot, name, display):
    statistics = snapshot["statistics"]
    key = next(column["key"] for column in statistics["columns"] if column["label"] == "姓名" or column["key"] == "name")
    normalized = "".join(name.split())
    rows = [row for row in statistics["rows"] if "".join(str(row.get(key, "")).split()) == normalized]
    if len(rows) != 1:
        fail(422, "EMPLOYEE_MATCH_INVALID", "结果中找不到唯一的员工，请核对考勤姓名")
    row = rows[0]
    attendance = next(item for item in snapshot["attendance"]["rows"] if item["row_number"] == row["row_number"])
    return {"workbook": snapshot["workbook"], "employeeName": display or name, "sourceEmployeeName": name,
            "statistics": {"columns": statistics["columns"], "row": row},
            "attendance": {"columns": snapshot["attendance"]["columns"], "days": attendance["days"]}}


@router.get("/months/{month_id}/recipients")
def recipients(month_id: MonthId, request: Request, session: Session = Depends(require_session)):
    with database(request).connect() as connection:
        month = month_record(connection, month_id)
    return {"items": month["recipient_mappings"], "exclusions": month["notification_exclusions"]}


@router.put("/months/{month_id}/recipients")
def save_recipients(month_id: MonthId, body: Recipients, request: Request, session: Session = Depends(require_session)):
    with database(request).connect() as connection:
        month_record(connection, month_id)
        return legacy.save_recipient_mappings(connection, {"month_id": month_id,
            "mappings": [item.model_dump() for item in body.items], "actor": session.actor})


@router.put("/months/{month_id}/notification-exclusions")
def exclusions(month_id: MonthId, body: Exclusions, request: Request, session: Session = Depends(require_session)):
    with database(request).connect() as connection:
        month_record(connection, month_id)
        return legacy.save_notification_exclusions(connection, {"month_id": month_id,
            "employee_names": body.items, "actor": session.actor})


@router.post("/months/{month_id}/notifications", status_code=202)
def create_notifications(month_id: MonthId, body: Notification, request: Request,
                         session: Session = Depends(require_session), idempotency_key: str = Header(min_length=1, max_length=128)):
    test = body.delivery_type in ("test", "revision_test")
    specifications = body.model_dump()
    with database(request).connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT id,payload FROM jobs WHERE domain='notice' AND actor=? AND resource=? AND idempotency_key=?",
                                      (session.actor, month_id, idempotency_key)).fetchone()
        if existing:
            saved = json.loads(existing["payload"])
            if saved["specifications"] != specifications:
                fail(409, "IDEMPOTENCY_CONFLICT", "该重试标识已用于不同通知")
            return {"job_id": existing["id"], "delivery_ids": saved["deliveries"], "preview_urls": saved["previews"]}
        check_revision(connection, month_id, body.expected_revision)
        month = connection.execute("SELECT * FROM attendance_months WHERE id=?", (month_id,)).fetchone()
        selected = connection.execute("SELECT * FROM result_versions WHERE id=? AND month_id=?",
                                      (body.version_id, month_id)).fetchone()
        if selected is None:
            fail(404, "VERSION_NOT_FOUND", "找不到指定版本")
        if not test and month["published_version_id"] != body.version_id:
            fail(409, "VERSION_NOT_PUBLISHED", "正式通知须绑定已发布版本")
        snapshot = json.loads(selected["snapshot_json"])
        mappings = {row["employee_name"]: row for row in connection.execute(
            "SELECT * FROM employee_recipient_mappings WHERE month_id=?", (month_id,))}
        excluded = {row["employee_name"] for row in connection.execute(
            "SELECT employee_name FROM notification_exclusions WHERE month_id=?", (month_id,))}
        deliveries = []
        previews = []
        for name in dict.fromkeys(body.employee_names):
            if not test and name in excluded:
                continue
            mapping = mappings.get(name)
            recipient = body.test_recipient_user_id if test else mapping["recipient_user_id"] if mapping else None
            if not recipient and os.getenv("HR_ENABLE_DINGTALK_DELIVERY") == "1":
                fail(422, "RECIPIENT_REQUIRED", "存在未配置钉钉接收人的通知")
            display = mapping["display_name"] if mapping else name
            payload = version_notice(snapshot, name, display)
            token = secrets.token_urlsafe(24)
            expires = (datetime.now(timezone.utc) + timedelta(hours=48)).isoformat().replace("+00:00", "Z")
            notice = legacy.save_notice(connection, {"token": token, "month_id": month_id, "run_id": selected["run_id"],
                "version_id": body.version_id, "employee_name": name, "display_name": display,
                "recipient_user_id": recipient, "payload": payload, "delivery_type": body.delivery_type,
                "status": "queued", "expires_at": expires}, commit=False)
            deliveries.append(notice["delivery_id"])
            if test:
                previews.append(f"/attendance-notice/{token}")
        if not deliveries:
            fail(422, "RECIPIENT_REQUIRED", "所选接收人均已排除")
        job = enqueue(connection, "notice", session.actor, month_id,
                      {"deliveries": deliveries, "specifications": specifications, "previews": previews}, idempotency_key)
    return {"job_id": job, "delivery_ids": deliveries, "preview_urls": previews}


def update_delivery(connection, row, status, detail):
    connection.execute("UPDATE notification_deliveries SET status=?,detail_json=? WHERE id=?", (status, json_text(detail), row["id"]))
    connection.execute("UPDATE notices SET status=? WHERE token=? AND acknowledged_at IS NULL", (status, row["notice_token"]))
    event_type = "integration.delivery_status_changed.v1"
    record_event(connection, event_type, row["id"], next_event_revision(connection, event_type, row["id"]), {
        "delivery_id": row["id"], "purpose": row["delivery_type"], "resource_id": row["month_id"], "status": status,
    })


def send_notifications(settings, job, payload):
    db = Database(settings.data_dir / "workbench.sqlite")
    enabled = os.getenv("HR_ENABLE_DINGTALK_DELIVERY") == "1"
    base = os.getenv("HR_PUBLIC_BASE_URL", "").rstrip("/")
    for delivery_id in payload["deliveries"]:
        with db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute("UPDATE jobs SET lease_until=unixepoch()+900 WHERE id=? AND lease=?",
                                       (job["id"], job["lease"]))
            if not lease.rowcount:
                raise JobFailure("通知任务已由另一执行器接管")
            row = connection.execute("SELECT d.*,n.recipient_user_id,n.payload_json FROM notification_deliveries d "
                                     "JOIN notices n ON n.token=d.notice_token WHERE d.id=?", (delivery_id,)).fetchone()
            if row["status"] == "sending":
                update_delivery(connection, row, "unknown", {"reason": "worker_interrupted"})
                continue
            if row["status"] != "queued":
                continue
            if not enabled or not base.startswith("https://"):
                update_delivery(connection, row, "disabled", {})
                continue
            connection.execute("UPDATE notification_deliveries SET status='sending' WHERE id=?", (delivery_id,))
        period = json.loads(row["payload_json"])["workbook"]
        title = f"{period['year']}年{period['month']}月个人考勤结果"
        text = f"[查看个人考勤及反馈]({base}/attendance-notice/{row['notice_token']})"
        try:
            sent = invoke_engine({"operation": "notification-send", "options": {
                "userIds": [row["recipient_user_id"]], "title": title, "text": text,
            }}, timeout=15)
            status = "failed" if sent["invalidUserIds"] else "accepted" if sent["processQueryKey"] else "unknown"
            detail = {"process_query_key": sent["processQueryKey"]}
        except JobFailure:
            status, detail = "unknown", {"reason": "provider_status_unknown"}
        with db.connect() as connection:
            update_delivery(connection, row, status, detail)
    return lambda connection: {"delivery_count": len(payload["deliveries"]), "preview_urls": payload["previews"]}


@router.get("/months/{month_id}/deliveries")
def deliveries(month_id: MonthId, request: Request, session: Session = Depends(require_session)):
    with database(request).connect() as connection:
        rows = connection.execute("SELECT id,version_id,employee_name,delivery_type,status,created_at "
                                  "FROM notification_deliveries WHERE month_id=? ORDER BY created_at DESC", (month_id,)).fetchall()
    return {"items": [dict(row) for row in rows]}


@router.post("/deliveries/{delivery_id}/reconcile")
def reconcile(delivery_id: str, request: Request, session: Session = Depends(require_session)):
    db = database(request)
    with db.connect() as connection:
        row = connection.execute("SELECT * FROM notification_deliveries WHERE id=?", (delivery_id,)).fetchone()
    if row is None:
        fail(404, "DELIVERY_NOT_FOUND", "找不到投递记录")
    detail = json.loads(row["detail_json"] or "{}")
    if not detail.get("process_query_key") or row["status"] != "accepted":
        return {"status": row["status"]}
    try:
        result = invoke_engine({"operation": "notification-status", "options": detail}, timeout=15)
    except JobFailure as error:
        fail(502, "DELIVERY_STATUS_UNAVAILABLE", str(error))
    status = {"SUCCESS": "sent", "FAILED": "failed", "FAIL": "failed"}.get(result["sendStatus"], "accepted")
    if status != "accepted":
        with db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("SELECT status FROM notification_deliveries WHERE id=?", (delivery_id,)).fetchone()
            if current["status"] == "accepted":
                update_delivery(connection, row, status, detail)
            else:
                status = current["status"]
    return {"status": status}


def readable_notice(connection, token):
    result = legacy.get_notice(connection, {"token": token})
    if result is None:
        fail(404, "NOTICE_NOT_FOUND", "链接无效或已过期")
    return result


def safe_feedback(item):
    return {key: item.get(key) for key in ("id", "record_type", "category", "detail", "status", "review_note", "day_items", "created_at")}


def safe_notice(item):
    return {"payload": item["payload"], "acknowledged_at": item["acknowledged_at"], "version_id": item["version_id"],
            "feedbacks": [safe_feedback(feedback) for feedback in item["feedbacks"]]}


@public.get("/notices/{token}")
def get_notice(token: str, request: Request):
    with database(request).connect() as connection:
        return safe_notice(readable_notice(connection, token))


@public.post("/notices/{token}/acknowledgments")
def acknowledge(token: str, request: Request):
    with database(request).connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        old = readable_notice(connection, token)
        item = legacy.acknowledge_notice(connection, {"token": token}, commit=False)
        if old["acknowledged_at"] is None:
            delivery = connection.execute("SELECT id FROM notification_deliveries WHERE notice_token=? ORDER BY created_at LIMIT 1",
                                          (token,)).fetchone()
            if delivery:
                record_event(connection, "attendance.fulltime.notice_acknowledged.v1", delivery["id"], 1,
                             {"month_id": old["month_id"], "notice_id": delivery["id"], "ack_source": "manual"},
                             correlation_id=request.state.request_id)
        return safe_notice(item)


@public.post("/notices/{token}/feedbacks", status_code=201)
def feedback(token: str, body: Feedback, request: Request):
    with database(request).connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        notice = readable_notice(connection, token)
        days = {item["day"]: item["status"] for item in notice["payload"]["attendance"]["days"]}
        if any(item.day not in days for item in body.day_items):
            fail(422, "DAY_INVALID", "反馈日期不在对应月份内")
        day_items = [item.model_dump() | {"current_status": days[item.day]} for item in body.day_items]
        period = notice["payload"]["workbook"]
        field = next((col for col in notice["payload"]["statistics"]["columns"] if col["key"] == body.field_key), {})
        item = legacy.create_feedback(connection, {
            "id": f"FT-FB-{uuid4().hex}", "notice_token": token, "month_id": notice["month_id"], "run_id": notice["run_id"],
            "employee_name": notice["employee_name"], "review_token": secrets.token_urlsafe(24), "record_type": "有问题",
            "period": f"{period['year']}年{period['month']}月", "category": body.category, "detail": body.detail,
            "day_values": [item.day for item in body.day_items], "day_items": day_items, "field_key": field.get("key"),
            "field_label": field.get("label"), "status": "待玖杉处理", "notice_created_at": notice["created_at"],
        }, commit=False)
        record_event(connection, "attendance.fulltime.feedback_updated.v1", item["id"], 1,
                     {"month_id": notice["month_id"], "feedback_id": item["id"], "status": item["status"]})
        return safe_feedback(item)


def review_case(connection, token):
    item = legacy.get_feedback_by_review_token(connection, {"review_token": token})
    if item is None:
        fail(404, "REVIEW_NOT_FOUND", "复核链接无效")
    return item


def update_review(connection, item, body):
    updated = legacy.update_feedback(connection, {"id": item["id"], "patch": body.model_dump() | {"reviewed_at": utcnow()}}, commit=False)
    event_type = "attendance.fulltime.feedback_updated.v1"
    record_event(connection, event_type, item["id"], next_event_revision(connection, event_type, item["id"]),
                 {"month_id": item["month_id"], "feedback_id": item["id"], "status": updated["status"]})
    return safe_feedback(updated)


@public.get("/reviews/{token}")
def review_detail(token: str, request: Request):
    with database(request).connect() as connection:
        return safe_feedback(review_case(connection, token))


@public.patch("/reviews/{token}")
def review_feedback(token: str, body: Review, request: Request):
    with database(request).connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        return update_review(connection, review_case(connection, token), body)


@router.patch("/feedbacks/{feedback_id}")
def admin_feedback(feedback_id: str, body: Review, request: Request, session: Session = Depends(require_session)):
    with database(request).connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        item = legacy.get_feedback_by_id(connection, feedback_id)
        if item is None:
            fail(404, "FEEDBACK_NOT_FOUND", "找不到反馈")
        return update_review(connection, item, body)


class RuleChange(BaseModel):
    change_type: Literal["algorithm", "rule"]
    title: str = Field(min_length=1, max_length=160)
    problem: str = Field(min_length=1, max_length=2000)
    business_confirmation: str = Field(min_length=1, max_length=2000)
    old_behavior: str = ""
    new_behavior: str = Field(min_length=1, max_length=2000)
    validation_result: str = ""


class RuleStatus(BaseModel):
    status: str
    validation_result: str = ""


@router.get("/months/{month_id}/rule-changes")
def rule_changes(month_id: MonthId, request: Request, session: Session = Depends(require_session)):
    with database(request).connect() as connection:
        try:
            return {"items": legacy.list_algorithm_rule_changes(connection, {"month_id": month_id})}
        except ValueError as error:
            fail(404, "MONTH_NOT_FOUND", str(error))


@router.post("/months/{month_id}/rule-changes", status_code=201)
def add_rule_change(month_id: MonthId, body: RuleChange, request: Request, session: Session = Depends(require_session)):
    with database(request).connect() as connection:
        try:
            return legacy.create_algorithm_rule_change(connection, body.model_dump() | {
                "month_id": month_id, "operator_name": session.actor,
            })
        except ValueError as error:
            fail(422, "RULE_CHANGE_INVALID", str(error))


@router.patch("/months/{month_id}/rule-changes/{change_id}")
def change_rule_status(month_id: MonthId, change_id: str, body: RuleStatus, request: Request,
                       session: Session = Depends(require_session)):
    with database(request).connect() as connection:
        try:
            item = legacy.update_algorithm_rule_change(connection, {"month_id": month_id, "change_id": change_id,
                "patch": body.model_dump(), "actor": session.actor})
        except ValueError as error:
            fail(422, "RULE_CHANGE_INVALID", str(error))
        if item is None:
            fail(404, "RULE_CHANGE_NOT_FOUND", "找不到规则变更记录")
        return item
