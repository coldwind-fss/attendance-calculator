"""Project workflow nodes from the existing KPI and OKR records."""

from typing import Literal

from pydantic import BaseModel


class WorkflowNode(BaseModel):
    code: str
    label: str
    assignee: Literal["employee", "manager", "bp", "admin"] | None = None
    admin_action: Literal["remind", "send_self_review", "view_archive"] | None = None


def node(code, label, assignee=None, admin_action="remind"):
    return WorkflowNode(code=code, label=label, assignee=assignee, admin_action=admin_action)


ARCHIVED = node("archived", "已归档", admin_action="view_archive")
UNKNOWN = node("unknown", "待核实流程状态", admin_action=None)

OKR_NODES = {
    "invited": node("target_draft", "待员工填写 OKR", "employee"),
    "target_rejected": node("target_draft", "待员工修改 OKR 目标", "employee"),
    "target_submitted": node("target_manager", "待上级确认目标", "manager"),
    "target_manager_approved": node("target_bp", "待 BP 确认目标", "bp"),
    "target_bp_approved": node("target_sign", "待员工确认目标", "employee"),
    "target_confirmed": node("invite_self_review", "待发送自评", "admin", "send_self_review"),
    "self_review_invited": node("self_review", "待员工填写结果", "employee"),
    "self_reviewed": node("manager_score", "待上级评级", "manager"),
    "manager_graded": node("bp_score", "待 BP 核定", "bp"),
    "completed": node("result_sign", "待员工确认结果", "employee"),
    "result_signed_pending_bp": node("result_bp_review", "待 BP 复核归档", "bp"),
    "result_confirmed": ARCHIVED,
}


def kpi_node(records):
    draft = records.get("target-drafts") or {}
    result = records.get("result-data?summary=1") or {}
    signed_target = records.get("kpi-data?summary=1") or {}
    adjustment = draft.get("isTargetAdjustment") and not draft.get("targetSignedAt")
    if result.get("doc"):
        if result.get("sealed") is True and result.get("archiveFile"):
            return ARCHIVED
        return node("result_bp_review", "待 BP 复核归档", "bp", None)
    if "bp-data" in records:
        return node("result_sign", "待员工签署结果", "employee")
    if "mgr-data" in records:
        return node("bp_score", "待 BP 核准", "bp")
    if "data" in records:
        return node("manager_score", "待上级评分", "manager")
    if signed_target.get("doc") and not adjustment:
        return node("self_review", "待员工自评", "employee")
    status = draft.get("status")
    if status == "submitted":
        return node("target_manager", "待直属上级确认目标", "manager")
    if status == "manager_approved":
        return node("target_bp", "待 BP 确认目标", "bp")
    if status == "rejected":
        if draft.get("source") == "admin-entry":
            return node("target_admin_revision", "后台填写的目标已退回", "admin", None)
        return node("target_draft", "待员工修改目标", "employee")
    if status == "invited":
        label = "待员工调整目标" if adjustment else "待员工填写目标"
        return node("target_draft", label, "employee")
    if draft or "kpi-targets" in records:
        label = "待员工签署调整后目标" if adjustment else "待员工签署目标"
        return node("target_sign", label, "employee")
    return UNKNOWN
