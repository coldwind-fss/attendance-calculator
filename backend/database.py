import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .events import EVENT_CONTRACTS, EventEnvelope


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY, actor TEXT NOT NULL, csrf TEXT NOT NULL, expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS source_files (
    id TEXT PRIMARY KEY, domain TEXT NOT NULL, source_type TEXT NOT NULL,
    filename TEXT NOT NULL, digest TEXT NOT NULL, size INTEGER NOT NULL,
    actor TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, domain TEXT NOT NULL, actor TEXT NOT NULL, resource TEXT NOT NULL,
    idempotency_key TEXT, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
    lease TEXT, lease_until INTEGER, result TEXT, error TEXT, created_at TEXT NOT NULL,
    UNIQUE(domain, actor, resource, idempotency_key)
);
CREATE TABLE IF NOT EXISTS outbox (
    id TEXT PRIMARY KEY, event_type TEXT NOT NULL, aggregate_id TEXT NOT NULL,
    aggregate_revision INTEGER NOT NULL, payload TEXT NOT NULL, occurred_at TEXT NOT NULL, envelope TEXT
);
CREATE INDEX IF NOT EXISTS outbox_aggregate ON outbox(event_type, aggregate_id, aggregate_revision);
CREATE TABLE IF NOT EXISTS parttime_analyses (
    id TEXT PRIMARY KEY, month TEXT NOT NULL, actor TEXT NOT NULL,
    payload TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS parttime_analyses_month ON parttime_analyses(month, created_at, id);
CREATE TABLE IF NOT EXISTS fulltime_revisions (
    month_id TEXT PRIMARY KEY, revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS fulltime_publications (
    id TEXT PRIMARY KEY, month_id TEXT NOT NULL, version_id TEXT NOT NULL,
    actor TEXT NOT NULL, idempotency_key TEXT NOT NULL, expected_revision INTEGER NOT NULL,
    revision INTEGER NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(month_id, actor, idempotency_key)
);
CREATE TABLE IF NOT EXISTS fulltime_roster (
    position INTEGER PRIMARY KEY, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attendance_source_queries (
    id TEXT PRIMARY KEY, month TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def record_event(connection, event_type, aggregate_id, revision, payload, *, correlation_id=None, causation_id=None):
    aggregate_type, payload_model = EVENT_CONTRACTS[event_type]
    payload_model.model_validate(payload)
    event = EventEnvelope(event_id=uuid4().hex, event_type=event_type, occurred_at=utcnow(),
                          aggregate_type=aggregate_type, aggregate_id=aggregate_id,
                          aggregate_revision=revision, payload=payload, correlation_id=correlation_id,
                          causation_id=causation_id)
    connection.execute(
        "INSERT INTO outbox (id,event_type,aggregate_id,aggregate_revision,payload,occurred_at,envelope) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (event.event_id, event_type, aggregate_id, revision, json_text(payload), event.occurred_at, event.model_dump_json()),
    )


def next_event_revision(connection, event_type, aggregate_id):
    return connection.execute("SELECT COALESCE(MAX(aggregate_revision),0)+1 FROM outbox WHERE event_type=? AND aggregate_id=?",
                              (event_type, aggregate_id)).fetchone()[0]


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)
            if "envelope" not in {row[1] for row in connection.execute("PRAGMA table_info(outbox)")}:
                connection.execute("ALTER TABLE outbox ADD COLUMN envelope TEXT")
