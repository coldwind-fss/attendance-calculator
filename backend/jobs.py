import asyncio
import json
import logging
import time
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from .database import json_text, utcnow
from .http import database, fail, problem_responses
from .session import Session, require_session


router = APIRouter(prefix="/jobs", tags=["jobs"])
log = logging.getLogger(__name__)
LEASE_SECONDS = 900


class JobAccepted(BaseModel):
    job_id: str


class JobStatus(JobAccepted):
    status: Literal["queued", "running", "succeeded", "failed"]
    result: dict | None = None
    error: str | None = None


def enqueue(connection, domain, actor, resource, payload, key):
    value = json_text(payload)
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    if key:
        existing = connection.execute(
            "SELECT id, payload FROM jobs WHERE domain=? AND actor=? AND resource=? AND idempotency_key=?",
            (domain, actor, resource, key),
        ).fetchone()
        if existing:
            if existing["payload"] != value:
                fail(409, "IDEMPOTENCY_CONFLICT", "该重试标识已用于不同请求")
            return existing["id"]
    job_id = uuid4().hex
    connection.execute(
        "INSERT INTO jobs (id, domain, actor, resource, idempotency_key, payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (job_id, domain, actor, resource, key, value, utcnow()),
    )
    return job_id


def claim(db):
    now = int(time.time())
    lease = uuid4().hex
    with db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM jobs WHERE status='queued' OR (status='running' AND lease_until < ?) "
            "ORDER BY created_at, id LIMIT 1", (now,),
        ).fetchone()
        if row:
            connection.execute("UPDATE jobs SET status='running', lease=?, lease_until=? WHERE id=?",
                               (lease, now + LEASE_SECONDS, row["id"]))
            return dict(row) | {"lease": lease}


class JobFailure(Exception):
    """An actionable, user-visible failure with no sensitive input attached."""


def execute(db, settings, handlers):
    job = claim(db)
    if job is None:
        return False
    try:
        prepare_result = handlers[job["domain"]](settings, job, json.loads(job["payload"]))
        with db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute("SELECT lease FROM jobs WHERE id=?", (job["id"],)).fetchone()["lease"]
            if lease == job["lease"]:
                result = prepare_result(connection)
                connection.execute("UPDATE jobs SET status='succeeded', result=?, lease=NULL WHERE id=?",
                                   (json_text(result), job["id"]))
    except Exception as error:
        if not isinstance(error, JobFailure):
            log.error("Job %s (%s) failed: %s", job["id"], job["domain"], type(error).__name__)
        message = str(error) if isinstance(error, JobFailure) else "任务执行失败，请检查运行配置或重新提交"
        with db.connect() as connection:
            connection.execute("UPDATE jobs SET status='failed', error=?, lease=NULL WHERE id=? AND lease=?",
                               (message, job["id"], job["lease"]))
    return True


async def worker(db, settings, handlers, stopping):
    while not stopping.is_set():
        if not await asyncio.to_thread(execute, db, settings, handlers):
            try:
                await asyncio.wait_for(stopping.wait(), 0.25)
            except TimeoutError:
                pass


@router.get("/{job_id}", response_model=JobStatus, responses=problem_responses(401, 403, 404))
def get_job(job_id: str, request: Request, session: Session = Depends(require_session)):
    with database(request).connect() as connection:
        job = connection.execute("SELECT * FROM jobs WHERE id=? AND actor=?", (job_id, session.actor)).fetchone()
    if job is None:
        fail(404, "JOB_NOT_FOUND", "任务不存在")
    return JobStatus(job_id=job_id, status=job["status"],
                     result=json.loads(job["result"]) if job["result"] else None, error=job["error"])
