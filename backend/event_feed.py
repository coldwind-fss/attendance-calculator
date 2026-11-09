import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Request
from fastapi.responses import StreamingResponse

from .http import database
from .session import Session, require_session


router = APIRouter(prefix="/events", tags=["events"])


def latest_cursor(db):
    with db.connect() as connection:
        return connection.execute("SELECT COALESCE(MAX(rowid), 0) FROM outbox").fetchone()[0]


def month_events(db, month_id, cursor):
    with db.connect() as connection:
        return connection.execute("""
            SELECT rowid AS cursor, id, event_type, aggregate_revision
            FROM outbox
            WHERE rowid > ? AND event_type LIKE 'attendance.fulltime.%'
              AND json_extract(payload, '$.month_id') = ?
            ORDER BY rowid LIMIT 100
        """, (cursor, month_id)).fetchall()


@router.get("/attendance/fulltime/{month_id}", response_class=StreamingResponse,
            responses={200: {"content": {"text/event-stream": {}}}})
def fulltime_changes(month_id: Annotated[str, Path(pattern=r"^FTM-\d{4}(0[1-9]|1[0-2])$")],
                     request: Request, session: Session = Depends(require_session),
                     last_event_id: Annotated[int | None, Header(ge=0)] = None):
    db = database(request)

    async def stream():
        newest = await asyncio.to_thread(latest_cursor, db)
        cursor = last_event_id if last_event_id is not None and last_event_id <= newest else newest
        yield f"id: {cursor}\nevent: ready\ndata: {{}}\n\n"
        idle = 0
        while not await request.is_disconnected():
            rows = await asyncio.to_thread(month_events, db, month_id, cursor)
            for row in rows:
                cursor = row["cursor"]
                data = json.dumps({"event_id": row["id"], "event_type": row["event_type"],
                                   "revision": row["aggregate_revision"]}, separators=(",", ":"))
                yield f"id: {cursor}\nevent: changed\ndata: {data}\n\n"
            if rows:
                idle = 0
            else:
                idle += 1
                if idle == 15:
                    yield ": keep-alive\n\n"
                    idle = 0
            if len(rows) < 100:
                await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-store", "X-Accel-Buffering": "no",
    })
