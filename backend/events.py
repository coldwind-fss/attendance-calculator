from typing import Literal

from pydantic import BaseModel, Field


class EventEnvelope(BaseModel):
    event_id: str
    event_type: str
    occurred_at: str
    aggregate_type: Literal["hr.organization_snapshot", "attendance.parttime.analysis", "attendance.fulltime.month",
                            "attendance.fulltime.notice", "attendance.fulltime.feedback", "integration.delivery"]
    aggregate_id: str
    aggregate_revision: int = Field(ge=1)
    correlation_id: str | None = None
    causation_id: str | None = None
    payload: dict


class OrganizationSnapshotSaved(BaseModel):
    snapshot_id: str


class ParttimeAnalysisCompleted(BaseModel):
    month_id: str
    analysis_id: str
    workbook_id: str


class FulltimeVersionPublished(BaseModel):
    month_id: str
    version_id: str


class FulltimeVersionCreated(FulltimeVersionPublished):
    run_id: str
    algorithm_version: str


class FulltimeCorrectionRecorded(BaseModel):
    month_id: str
    correction_id: str
    operation: Literal["create", "reverse"]


class FulltimeNoticeAcknowledged(BaseModel):
    month_id: str | None
    notice_id: str
    ack_source: Literal["manual", "auto_24h"]


class FulltimeFeedbackUpdated(BaseModel):
    month_id: str | None
    feedback_id: str
    status: str


class DeliveryStatusChanged(BaseModel):
    delivery_id: str
    purpose: str
    resource_id: str | None
    status: str


EVENT_CONTRACTS = {
    "hr.organization_snapshot_saved.v1": ("hr.organization_snapshot", OrganizationSnapshotSaved),
    "attendance.parttime.analysis_completed.v1": ("attendance.parttime.analysis", ParttimeAnalysisCompleted),
    "attendance.fulltime.version_created.v1": ("attendance.fulltime.month", FulltimeVersionCreated),
    "attendance.fulltime.correction_recorded.v1": ("attendance.fulltime.month", FulltimeCorrectionRecorded),
    "attendance.fulltime.version_published.v1": ("attendance.fulltime.month", FulltimeVersionPublished),
    "attendance.fulltime.notice_acknowledged.v1": ("attendance.fulltime.notice", FulltimeNoticeAcknowledged),
    "attendance.fulltime.feedback_updated.v1": ("attendance.fulltime.feedback", FulltimeFeedbackUpdated),
    "integration.delivery_status_changed.v1": ("integration.delivery", DeliveryStatusChanged),
}
