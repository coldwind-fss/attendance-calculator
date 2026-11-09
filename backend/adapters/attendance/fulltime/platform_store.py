import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCHEMA_VERSION = 7


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso_datetime(value):
    """Parse stored UTC timestamps on both modern Python and Python 3.6."""
    text = str(value or "").strip()
    if not text:
        return None
    from_isoformat = getattr(datetime, "fromisoformat", None)
    if callable(from_isoformat):
        return from_isoformat(text.replace("Z", "+00:00"))
    if text.endswith("Z"):
        text = text[:-1]
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError("invalid ISO datetime")


def json_dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_load(value, fallback=None):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


def connect():
    db_path = Path(os.environ["FULLTIME_DB_PATH"]).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    migrate(conn)
    os.chmod(db_path, 0o600)
    return conn


def migrate(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS platform_meta (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS attendance_months (
          id TEXT PRIMARY KEY,
          year INTEGER NOT NULL,
          month INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'draft',
          current_run_id TEXT,
          current_version_id TEXT,
          published_version_id TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(year, month)
        );

        CREATE TABLE IF NOT EXISTS calculation_runs (
          id TEXT PRIMARY KEY,
          analysis_id TEXT NOT NULL UNIQUE,
          month_id TEXT NOT NULL REFERENCES attendance_months(id),
          filename TEXT NOT NULL,
          source_filename TEXT,
          source_meta_json TEXT,
          preview_json TEXT NOT NULL,
          workbook_path TEXT NOT NULL,
          input_hashes_json TEXT,
          algorithm_version TEXT,
          created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_calculation_runs_month
          ON calculation_runs(month_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS result_versions (
          id TEXT PRIMARY KEY,
          month_id TEXT NOT NULL REFERENCES attendance_months(id),
          run_id TEXT NOT NULL REFERENCES calculation_runs(id),
          version_number INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'draft',
          label TEXT,
          snapshot_json TEXT NOT NULL,
          workbook_path TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          published_at TEXT,
          UNIQUE(month_id, version_number)
        );

        CREATE TABLE IF NOT EXISTS corrections (
          id TEXT PRIMARY KEY,
          month_id TEXT NOT NULL REFERENCES attendance_months(id),
          run_id TEXT NOT NULL REFERENCES calculation_runs(id),
          employee_name TEXT NOT NULL,
          target_type TEXT NOT NULL,
          day INTEGER,
          field_key TEXT,
          original_value_json TEXT,
          new_value_json TEXT NOT NULL,
          reason TEXT NOT NULL,
          operator_name TEXT,
          feedback_id TEXT,
          status TEXT NOT NULL DEFAULT 'active',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_corrections_month_employee
          ON corrections(month_id, employee_name, status);

        CREATE TABLE IF NOT EXISTS notices (
          token TEXT PRIMARY KEY,
          month_id TEXT REFERENCES attendance_months(id),
          run_id TEXT REFERENCES calculation_runs(id),
          version_id TEXT REFERENCES result_versions(id),
          employee_name TEXT NOT NULL,
          display_name TEXT,
          recipient_user_id TEXT,
          payload_json TEXT NOT NULL,
          summary_json TEXT,
          status TEXT NOT NULL DEFAULT 'sent',
          created_at TEXT NOT NULL,
          expires_at TEXT,
          acknowledged_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_notices_month_employee
          ON notices(month_id, employee_name, created_at DESC);

        CREATE TABLE IF NOT EXISTS feedback_cases (
          id TEXT PRIMARY KEY,
          notice_token TEXT REFERENCES notices(token),
          month_id TEXT REFERENCES attendance_months(id),
          run_id TEXT REFERENCES calculation_runs(id),
          review_token TEXT UNIQUE,
          record_type TEXT NOT NULL,
          parent_feedback_id TEXT,
          employee_name TEXT NOT NULL,
          period TEXT,
          category TEXT,
          field_key TEXT,
          field_label TEXT,
          day_values_json TEXT,
          day_items_json TEXT,
          days TEXT,
          detail TEXT,
          status TEXT,
          statistics_snapshot_json TEXT,
          notice_created_at TEXT,
          reviewer_notified_at TEXT,
          reviewer_notify_error TEXT,
          review_note TEXT,
          reviewed_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_feedback_month_employee
          ON feedback_cases(month_id, employee_name, status);
        CREATE INDEX IF NOT EXISTS idx_feedback_notice
          ON feedback_cases(notice_token, created_at);

        CREATE TABLE IF NOT EXISTS notification_deliveries (
          id TEXT PRIMARY KEY,
          notice_token TEXT REFERENCES notices(token),
          month_id TEXT REFERENCES attendance_months(id),
          version_id TEXT REFERENCES result_versions(id),
          employee_name TEXT NOT NULL,
          delivery_type TEXT NOT NULL,
          status TEXT NOT NULL,
          detail_json TEXT,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS employee_recipient_mappings (
          month_id TEXT NOT NULL REFERENCES attendance_months(id),
          employee_name TEXT NOT NULL,
          recipient_user_id TEXT NOT NULL,
          display_name TEXT,
          source TEXT,
          updated_at TEXT NOT NULL,
          PRIMARY KEY(month_id, employee_name)
        );

        CREATE TABLE IF NOT EXISTS notification_exclusions (
          month_id TEXT NOT NULL REFERENCES attendance_months(id),
          employee_name TEXT NOT NULL,
          reason TEXT,
          updated_at TEXT NOT NULL,
          PRIMARY KEY(month_id, employee_name)
        );

        CREATE TABLE IF NOT EXISTS audit_logs (
          id TEXT PRIMARY KEY,
          month_id TEXT,
          entity_type TEXT NOT NULL,
          entity_id TEXT,
          action TEXT NOT NULL,
          actor TEXT,
          detail_json TEXT,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS algorithm_rule_changes (
          id TEXT PRIMARY KEY,
          month_id TEXT NOT NULL REFERENCES attendance_months(id),
          change_type TEXT NOT NULL,
          title TEXT NOT NULL,
          problem TEXT NOT NULL,
          business_confirmation TEXT NOT NULL,
          old_behavior TEXT,
          new_behavior TEXT NOT NULL,
          impact_scope TEXT,
          changed_files_json TEXT,
          source_refs_json TEXT,
          validation_result TEXT,
          algorithm_version_before TEXT,
          algorithm_version_after TEXT,
          status TEXT NOT NULL DEFAULT 'confirmed',
          operator_name TEXT,
          confirmed_by TEXT,
          confirmed_at TEXT,
          implemented_at TEXT,
          verified_at TEXT,
          published_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_algorithm_rule_changes_month
          ON algorithm_rule_changes(month_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_algorithm_rule_changes_status
          ON algorithm_rule_changes(status, updated_at DESC);
        """
    )
    ensure_column(conn, "feedback_cases", "field_key", "TEXT")
    ensure_column(conn, "feedback_cases", "field_label", "TEXT")
    ensure_column(conn, "notices", "ack_source", "TEXT")
    conn.execute(
        "UPDATE feedback_cases SET status='已阅，无问题' "
        "WHERE record_type='已阅' AND status='已阅，暂未发现问题'"
    )
    conn.execute(
        "INSERT INTO platform_meta(key,value) VALUES('schema_version',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def ensure_column(conn, table, column, definition):
    columns = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def row_to_dict(row):
    return dict(row) if row else None


def month_for_preview(conn, preview):
    workbook = preview.get("workbook") or {}
    year = int(workbook.get("year") or datetime.now().year)
    month = int(workbook.get("month") or datetime.now().month)
    existing = conn.execute(
        "SELECT * FROM attendance_months WHERE year=? AND month=?", (year, month)
    ).fetchone()
    if existing:
        return row_to_dict(existing)
    timestamp = now_iso()
    month_id = f"FTM-{year:04d}{month:02d}"
    conn.execute(
        "INSERT INTO attendance_months(id,year,month,status,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?)",
        (month_id, year, month, "draft", timestamp, timestamp),
    )
    return row_to_dict(
        conn.execute("SELECT * FROM attendance_months WHERE id=?", (month_id,)).fetchone()
    )


def persist_analysis(conn, payload, *, commit=True):
    preview = payload["preview"]
    month = month_for_preview(conn, preview)
    run_id = payload.get("run_id") or f"FTR-{uuid.uuid4().hex}"
    timestamp = now_iso()
    conn.execute(
        """
        INSERT INTO calculation_runs(
          id,analysis_id,month_id,filename,source_filename,source_meta_json,
          preview_json,workbook_path,input_hashes_json,algorithm_version,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(analysis_id) DO UPDATE SET
          filename=excluded.filename,
          source_filename=excluded.source_filename,
          source_meta_json=excluded.source_meta_json,
          preview_json=excluded.preview_json,
          workbook_path=excluded.workbook_path,
          input_hashes_json=excluded.input_hashes_json,
          algorithm_version=excluded.algorithm_version
        """,
        (
            run_id,
            payload["analysis_id"],
            month["id"],
            payload.get("filename") or "全职考勤结果.xlsx",
            payload.get("source_filename") or "",
            json_dump(payload.get("source_meta") or {}),
            json_dump(preview),
            payload["workbook_path"],
            json_dump(payload.get("input_hashes") or {}),
            payload.get("algorithm_version") or "",
            timestamp,
        ),
    )
    draft = conn.execute(
        "SELECT * FROM result_versions WHERE month_id=? AND status='draft' "
        "ORDER BY version_number DESC LIMIT 1",
        (month["id"],),
    ).fetchone()
    if draft:
        version_id = draft["id"]
        conn.execute(
            """
            UPDATE result_versions SET run_id=?,snapshot_json=?,workbook_path=?,
              label=?,updated_at=? WHERE id=?
            """,
            (
                run_id,
                json_dump(preview),
                payload["workbook_path"],
                payload.get("version_label") or "计算草稿",
                timestamp,
                version_id,
            ),
        )
    else:
        version_number = (
            conn.execute(
                "SELECT COALESCE(MAX(version_number),0)+1 FROM result_versions WHERE month_id=?",
                (month["id"],),
            ).fetchone()[0]
        )
        version_id = f"FTV-{uuid.uuid4().hex}"
        conn.execute(
            """
            INSERT INTO result_versions(
              id,month_id,run_id,version_number,status,label,snapshot_json,
              workbook_path,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                version_id,
                month["id"],
                run_id,
                version_number,
                "draft",
                payload.get("version_label") or "计算草稿",
                json_dump(preview),
                payload["workbook_path"],
                timestamp,
                timestamp,
            ),
        )
    conn.execute(
        """
        UPDATE attendance_months SET current_run_id=?,current_version_id=?,
          status='draft',updated_at=? WHERE id=?
        """,
        (run_id, version_id, timestamp, month["id"]),
    )
    conn.execute(
        """
        UPDATE feedback_cases SET month_id=?,run_id=COALESCE(run_id,?)
        WHERE month_id IS NULL AND period=?
        """,
        (
            month["id"],
            run_id,
            f"{month['year']}年{month['month']}月",
        ),
    )
    add_audit(
        conn,
        month["id"],
        "calculation_run",
        run_id,
        "persist",
        payload.get("actor") or "system",
        {"analysis_id": payload["analysis_id"], "version_id": version_id},
    )
    if commit:
        conn.commit()
    return {
        "month_id": month["id"],
        "run_id": run_id,
        "version_id": version_id,
        "year": month["year"],
        "month": month["month"],
    }


def get_run(conn, payload):
    key = payload.get("analysis_id")
    row = conn.execute(
        "SELECT * FROM calculation_runs WHERE analysis_id=?", (key,)
    ).fetchone()
    if not row:
        return None
    result = row_to_dict(row)
    result["preview"] = json_load(result.pop("preview_json"), {})
    result["source_meta"] = json_load(result.pop("source_meta_json"), {})
    result["input_hashes"] = json_load(result.pop("input_hashes_json"), {})
    return result


def list_months(conn, _payload):
    rows = conn.execute(
        """
        SELECT m.*,
          (SELECT COUNT(*) FROM calculation_runs r WHERE r.month_id=m.id) run_count,
          (SELECT COUNT(*) FROM feedback_cases f WHERE f.month_id=m.id AND f.record_type!='已阅') feedback_count,
          (SELECT COUNT(*) FROM feedback_cases f WHERE f.month_id=m.id AND f.record_type!='已阅'
             AND COALESCE(f.status,'')!='已处理') pending_feedback_count,
          (SELECT COUNT(*) FROM corrections c WHERE c.month_id=m.id AND c.status='active') correction_count
          ,(SELECT COUNT(*) FROM algorithm_rule_changes arc WHERE arc.month_id=m.id) algorithm_rule_change_count
          ,(SELECT COUNT(*) FROM algorithm_rule_changes arc WHERE arc.month_id=m.id
             AND arc.status NOT IN ('verified','published','cancelled')) pending_algorithm_rule_change_count
        FROM attendance_months m ORDER BY m.year DESC,m.month DESC
        """
    ).fetchall()
    return [row_to_dict(row) for row in rows]


def version_payload(row):
    if not row:
        return None
    result = row_to_dict(row)
    result["snapshot"] = json_load(result.pop("snapshot_json"), {})
    return result


def feedback_payload(row):
    if not row:
        return None
    result = row_to_dict(row)
    result["day_values"] = json_load(result.pop("day_values_json"), [])
    result["day_items"] = json_load(result.pop("day_items_json"), [])
    result["statistics_snapshot"] = json_load(
        result.pop("statistics_snapshot_json"), {}
    )
    return result


def correction_payload(row):
    result = row_to_dict(row)
    result["original_value"] = json_load(result.pop("original_value_json"), None)
    result["new_value"] = json_load(result.pop("new_value_json"), None)
    return result


def auto_acknowledge_due_notices(conn, month_id=None, *, on_acknowledged=None):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat().replace(
        "+00:00", "Z"
    )
    params = [cutoff]
    month_filter = ""
    if month_id:
        month_filter = " AND n.month_id=?"
        params.append(month_id)
    rows = conn.execute(
        """
        SELECT n.token,n.month_id,n.employee_name,n.created_at
        FROM notices n
        WHERE n.status='sent' AND n.acknowledged_at IS NULL
          AND n.created_at<=?
          AND EXISTS(
            SELECT 1 FROM notification_deliveries d
            WHERE d.notice_token=n.token
              AND d.delivery_type NOT IN ('test','revision_test')
          )
          AND NOT EXISTS(
            SELECT 1 FROM feedback_cases f
            WHERE f.notice_token=n.token AND f.record_type!='已阅'
          )
        """
        + month_filter,
        tuple(params),
    ).fetchall()
    if not rows:
        return {"acknowledged": 0}
    timestamp = now_iso()
    acknowledged = 0
    for row in rows:
        update = conn.execute(
            "UPDATE notices SET acknowledged_at=?,ack_source='auto_24h',"
            "status='acknowledged' WHERE token=? AND acknowledged_at IS NULL",
            (timestamp, row["token"]),
        )
        if not update.rowcount:
            continue
        add_audit(
            conn,
            row["month_id"],
            "notice",
            row["token"],
            "auto_acknowledge_24h",
            "system",
            {"employee_name": row["employee_name"], "notified_at": row["created_at"]},
        )
        if on_acknowledged:
            on_acknowledged(conn, row)
        acknowledged += 1
    conn.commit()
    return {"acknowledged": acknowledged, "acknowledged_at": timestamp}


def notification_receipt_payload(conn, month_id, version):
    snapshot = (version or {}).get("snapshot") or {}
    statistics = snapshot.get("statistics") or {}
    columns = statistics.get("columns") or []
    name_key = next(
        (column.get("key") for column in columns if column.get("key") == "name"),
        "name",
    )
    excluded = {
        row["employee_name"]
        for row in conn.execute(
            "SELECT employee_name FROM notification_exclusions WHERE month_id=?",
            (month_id,),
        ).fetchall()
    }
    employees = []
    for row in statistics.get("rows") or []:
        employee_name = str(row.get(name_key) or "").strip()
        if employee_name and employee_name not in excluded and employee_name not in employees:
            employees.append(employee_name)

    notice_rows = conn.execute(
        """
        SELECT n.employee_name,n.recipient_user_id,n.status,n.created_at,n.acknowledged_at,n.ack_source,
               n.version_id,d.id AS delivery_id,d.delivery_type,d.detail_json
        FROM notices n
        JOIN notification_deliveries d ON d.notice_token=n.token
        WHERE n.month_id=?
          AND d.delivery_type NOT IN ('test','revision_test')
          AND n.created_at=(
            SELECT MAX(n2.created_at)
            FROM notices n2
            JOIN notification_deliveries d2 ON d2.notice_token=n2.token
            WHERE n2.month_id=n.month_id AND n2.employee_name=n.employee_name
              AND d2.delivery_type NOT IN ('test','revision_test')
          )
        ORDER BY n.employee_name
        """,
        (month_id,),
    ).fetchall()
    notices = {}
    for row in notice_rows:
        notice = row_to_dict(row)
        notice["delivery_detail"] = json_load(notice.pop("detail_json", None), {})
        notices[row["employee_name"]] = notice

    feedback_rows = conn.execute(
        """
        SELECT f.employee_name,f.status,f.created_at
        FROM feedback_cases f
        JOIN notification_deliveries d ON d.notice_token=f.notice_token
        WHERE f.month_id=? AND f.record_type!='已阅'
          AND d.delivery_type NOT IN ('test','revision_test')
        ORDER BY f.created_at DESC
        """,
        (month_id,),
    ).fetchall()
    feedbacks = {}
    for row in feedback_rows:
        info = feedbacks.setdefault(
            row["employee_name"],
            {"count": 0, "latest_status": row["status"] or "待玖杉处理", "latest_at": row["created_at"]},
        )
        info["count"] += 1

    rows = []
    counts = {
        "eligible": len(employees),
        "notified": 0,
        "acknowledged": 0,
        "auto_acknowledged": 0,
        "problem": 0,
        "pending": 0,
        "failed": 0,
        "not_sent": 0,
    }
    for employee_name in employees:
        notice = notices.get(employee_name)
        feedback = feedbacks.get(employee_name)
        if not notice:
            receipt_status = "not_sent"
            status_label = "未发送"
            counts["not_sent"] += 1
        else:
            if notice["status"] != "failed":
                counts["notified"] += 1
            if feedback:
                receipt_status = "problem"
                status_label = "有问题（{}）".format(feedback["latest_status"])
                counts["problem"] += 1
            elif notice["status"] == "failed":
                receipt_status = "failed"
                status_label = "发送失败"
                counts["failed"] += 1
            elif notice["acknowledged_at"]:
                if notice["ack_source"] == "auto_24h":
                    receipt_status = "auto_acknowledged"
                    status_label = "已阅，无问题（24小时自动）"
                    counts["auto_acknowledged"] += 1
                else:
                    receipt_status = "acknowledged"
                    status_label = "已阅，无问题"
                counts["acknowledged"] += 1
            else:
                receipt_status = "pending"
                status_label = "待确认"
                counts["pending"] += 1
        rows.append(
            {
                "employee_name": employee_name,
                "receipt_status": receipt_status,
                "status_label": status_label,
                "notified_at": notice.get("created_at") if notice else None,
                "acknowledged_at": notice.get("acknowledged_at") if notice else None,
                "ack_source": notice.get("ack_source") if notice else None,
                "recipient_user_id": notice.get("recipient_user_id") if notice else None,
                "version_id": notice.get("version_id") if notice else None,
                "delivery_id": notice.get("delivery_id") if notice else None,
                "delivery_detail": notice.get("delivery_detail") if notice else {},
                "delivery_type": notice.get("delivery_type") if notice else None,
                "feedback_count": feedback.get("count", 0) if feedback else 0,
                "feedback_status": feedback.get("latest_status") if feedback else None,
                "feedback_at": feedback.get("latest_at") if feedback else None,
            }
        )
    priority = {"problem": 0, "failed": 1, "pending": 2, "not_sent": 3,
                "auto_acknowledged": 4, "acknowledged": 5}
    rows.sort(key=lambda item: (priority.get(item["receipt_status"], 9), item["employee_name"]))
    counts["acknowledged_total"] = counts["acknowledged"]
    return {"summary": counts, "rows": rows}


def workflow_timeline_payload(conn, month, versions):
    month_id = month["id"]
    version_by_id = {item["id"]: item for item in versions}
    correction_by_id = {
        row["id"]: correction_payload(row)
        for row in conn.execute(
            "SELECT * FROM corrections WHERE month_id=?", (month_id,)
        ).fetchall()
    }
    events = []
    audit_rows = conn.execute(
        """
        SELECT id,entity_type,entity_id,action,actor,detail_json,created_at
        FROM audit_logs
        WHERE month_id=?
          AND ((entity_type='correction' AND action IN ('create','reverse'))
            OR (entity_type='result_version' AND action IN ('rebuild','publish')))
        ORDER BY created_at DESC LIMIT 120
        """,
        (month_id,),
    ).fetchall()
    for row in audit_rows:
        detail = json_load(row["detail_json"], {})
        if row["entity_type"] == "correction":
            correction = correction_by_id.get(row["entity_id"], {})
            source = detail if row["action"] == "create" else correction
            events.append(
                {
                    "id": row["id"],
                    "type": "correction_created" if row["action"] == "create" else "correction_reversed",
                    "title": "人工修订" if row["action"] == "create" else "撤销修订",
                    "created_at": row["created_at"],
                    "actor": row["actor"] or "HR",
                    "employee_name": source.get("employee_name") or correction.get("employee_name") or "",
                    "target_type": source.get("target_type") or correction.get("target_type") or "",
                    "day": source.get("day") if source.get("day") is not None else correction.get("day"),
                    "field_key": source.get("field_key") or correction.get("field_key") or "",
                    "original_value": source.get("original_value", correction.get("original_value")),
                    "new_value": source.get("new_value", correction.get("new_value")),
                    "reason": source.get("reason") or correction.get("reason") or "",
                }
            )
        else:
            version = version_by_id.get(row["entity_id"], {})
            number = version.get("version_number")
            published = row["action"] == "publish"
            events.append(
                {
                    "id": row["id"],
                    "type": "version_published" if published else "draft_generated",
                    "title": ("版本{} 已发布" if published else "修订草稿已生成").format(number or ""),
                    "created_at": row["created_at"],
                    "actor": row["actor"] or "system",
                    "version_id": row["entity_id"],
                    "version_number": number,
                }
            )

    delivery_rows = conn.execute(
        """
        SELECT id,employee_name,version_id,status,created_at
        FROM notification_deliveries
        WHERE month_id=? AND delivery_type='single_revision'
        ORDER BY created_at DESC LIMIT 80
        """,
        (month_id,),
    ).fetchall()
    for row in delivery_rows:
        version = version_by_id.get(row["version_id"], {})
        events.append(
            {
                "id": row["id"],
                "type": "revision_sent" if row["status"] == "sent" else "revision_failed",
                "title": "修订通知已发送" if row["status"] == "sent" else "修订通知发送失败",
                "created_at": row["created_at"],
                "actor": "system",
                "employee_name": row["employee_name"],
                "version_id": row["version_id"],
                "version_number": version.get("version_number"),
            }
        )
    events.sort(key=lambda item: item["created_at"], reverse=True)
    events = events[:120]
    last_published_at = max(
        (item.get("published_at") or "" for item in versions), default=""
    )
    current_version = version_by_id.get(month["current_version_id"], {})
    can_send_revision = (
        month["status"] == "published"
        and month["published_version_id"] == month["current_version_id"]
    )
    pending_change_count = 0
    if not can_send_revision:
        pending_change_count = conn.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE month_id=? AND "
            # Python 3.6 / Windows 的时间精度可能让“发布”和紧随其后的
            # 人工修订落在同一时间戳；边界值也必须算作发布后的新增修订。
            "entity_type='correction' AND action='create' AND created_at>=?",
            (month_id, last_published_at),
        ).fetchone()[0]
    return {
        "state": "published" if can_send_revision else "draft",
        "state_label": "已发布，可发送修订通知" if can_send_revision else "有未发布修改，请先发布版本",
        "can_send_revision": can_send_revision,
        "current_version_number": current_version.get("version_number"),
        "active_correction_count": len(
            [item for item in correction_by_id.values() if item.get("status") == "active"]
        ),
        "pending_change_count": pending_change_count,
        "revision_sent_count": len(
            [item for item in delivery_rows if item["status"] == "sent"]
        ),
        "last_published_at": last_published_at or None,
        "events": events,
    }


def get_month(conn, payload, *, refresh_receipts=True):
    month_id = payload["month_id"]
    if refresh_receipts:
        auto_acknowledge_due_notices(conn, month_id)
    month = conn.execute(
        "SELECT * FROM attendance_months WHERE id=?", (month_id,)
    ).fetchone()
    if not month:
        return None
    result = row_to_dict(month)
    run = conn.execute(
        "SELECT * FROM calculation_runs WHERE id=?", (month["current_run_id"],)
    ).fetchone()
    version = conn.execute(
        "SELECT * FROM result_versions WHERE id=?", (month["current_version_id"],)
    ).fetchone()
    corrections = conn.execute(
        "SELECT * FROM corrections WHERE month_id=? AND status='active' ORDER BY created_at",
        (month_id,),
    ).fetchall()
    algorithm_rule_changes = conn.execute(
        "SELECT * FROM algorithm_rule_changes WHERE month_id=? ORDER BY created_at DESC, id DESC",
        (month_id,),
    ).fetchall()
    feedbacks = conn.execute(
        "SELECT * FROM feedback_cases WHERE month_id=? AND record_type!='已阅' ORDER BY created_at DESC",
        (month_id,),
    ).fetchall()
    versions = conn.execute(
        "SELECT id,month_id,run_id,version_number,status,label,workbook_path,"
        "created_at,updated_at,published_at FROM result_versions "
        "WHERE month_id=? ORDER BY version_number DESC",
        (month_id,),
    ).fetchall()
    result["run"] = get_run(conn, {"analysis_id": run["analysis_id"]}) if run else None
    result["version"] = version_payload(version)
    result["corrections"] = [correction_payload(row) for row in corrections]
    result["algorithm_rule_changes"] = [
        algorithm_rule_change_payload(row) for row in algorithm_rule_changes
    ]
    result["feedbacks"] = [feedback_payload(row) for row in feedbacks]
    result["versions"] = [row_to_dict(row) for row in versions]
    delivery_rows = conn.execute(
        """
        SELECT status,delivery_type,COUNT(*) AS count
        FROM notification_deliveries WHERE month_id=?
        GROUP BY status,delivery_type
        ORDER BY delivery_type,status
        """,
        (month_id,),
    ).fetchall()
    notice_status_rows = conn.execute(
        "SELECT status,COUNT(*) AS count FROM notices WHERE month_id=? GROUP BY status",
        (month_id,),
    ).fetchall()
    notice_counts = {row["status"]: int(row["count"]) for row in notice_status_rows}
    result["notification_summary"] = {
        "total": sum(notice_counts.values()),
        "sent": notice_counts.get("sent", 0),
        "acknowledged": notice_counts.get("acknowledged", 0),
        "failed": notice_counts.get("failed", 0),
        "by_type": [row_to_dict(row) for row in delivery_rows],
    }
    result["notified_employees"] = [
        row_to_dict(row)
        for row in conn.execute(
            """
            SELECT n.employee_name,n.status,n.created_at,n.acknowledged_at,n.version_id
            FROM notices n
            WHERE n.month_id=?
            AND n.created_at=(
              SELECT MAX(n2.created_at) FROM notices n2
              WHERE n2.month_id=n.month_id AND n2.employee_name=n.employee_name
            )
            ORDER BY n.employee_name
            """,
            (month_id,),
        ).fetchall()
    ]
    result["recipient_mappings"] = [
        row_to_dict(row)
        for row in conn.execute(
            "SELECT employee_name,recipient_user_id,display_name,source,updated_at "
            "FROM employee_recipient_mappings WHERE month_id=? ORDER BY employee_name",
            (month_id,),
        ).fetchall()
    ]
    result["notification_exclusions"] = [
        row_to_dict(row)
        for row in conn.execute(
            "SELECT employee_name,reason,updated_at FROM notification_exclusions "
            "WHERE month_id=? ORDER BY employee_name",
            (month_id,),
        ).fetchall()
    ]
    receipts = notification_receipt_payload(conn, month_id, result["version"])
    result["notification_receipt_summary"] = receipts["summary"]
    result["notification_receipts"] = receipts["rows"]
    result["workflow_progress"] = workflow_timeline_payload(
        conn, month, result["versions"]
    )
    return result


def save_recipient_mappings(conn, payload):
    month_id = payload["month_id"]
    timestamp = now_iso()
    records = []
    for item in payload.get("mappings") or []:
        employee_name = str(item.get("employee_name") or "").strip()
        recipient_user_id = str(item.get("recipient_user_id") or "").strip()
        if not employee_name or not recipient_user_id:
            continue
        conn.execute(
            """
            INSERT INTO employee_recipient_mappings(
              month_id,employee_name,recipient_user_id,display_name,source,updated_at
            ) VALUES(?,?,?,?,?,?)
            ON CONFLICT(month_id,employee_name) DO UPDATE SET
              recipient_user_id=excluded.recipient_user_id,
              display_name=excluded.display_name,source=excluded.source,updated_at=excluded.updated_at
            """,
            (
                month_id,
                employee_name,
                recipient_user_id,
                str(item.get("display_name") or employee_name).strip(),
                str(item.get("source") or "manual").strip(),
                timestamp,
            ),
        )
        records.append(employee_name)
    add_audit(conn, month_id, "recipient_mapping", month_id, "save", payload.get("actor") or "HR", {"count": len(records)})
    conn.commit()
    return {"saved": len(records)}


def save_notification_exclusions(conn, payload):
    month_id = payload["month_id"]
    timestamp = now_iso()
    names = sorted({
        str(item).strip() for item in (payload.get("employee_names") or [])
        if str(item).strip()
    })
    conn.execute("DELETE FROM notification_exclusions WHERE month_id=?", (month_id,))
    conn.executemany(
        "INSERT INTO notification_exclusions(month_id,employee_name,reason,updated_at) "
        "VALUES(?,?,?,?)",
        [
            (month_id, name, str(payload.get("reason") or "本月不发送通知").strip(), timestamp)
            for name in names
        ],
    )
    add_audit(
        conn, month_id, "notification_exclusion", month_id, "replace",
        payload.get("actor") or "HR", {"count": len(names), "employees": names},
    )
    conn.commit()
    return {"saved": len(names), "employees": names}


def save_version(conn, payload, *, commit=True):
    timestamp = now_iso()
    month_id = payload["month_id"]
    run_id = payload["run_id"]
    draft = conn.execute(
        "SELECT * FROM result_versions WHERE month_id=? AND status='draft' "
        "ORDER BY version_number DESC LIMIT 1",
        (month_id,),
    ).fetchone()
    if draft:
        version_id = draft["id"]
        conn.execute(
            """
            UPDATE result_versions SET run_id=?,snapshot_json=?,workbook_path=?,
              label=?,updated_at=? WHERE id=?
            """,
            (
                run_id,
                json_dump(payload["snapshot"]),
                payload["workbook_path"],
                payload.get("label") or draft["label"],
                timestamp,
                version_id,
            ),
        )
    else:
        number = conn.execute(
            "SELECT COALESCE(MAX(version_number),0)+1 FROM result_versions WHERE month_id=?",
            (month_id,),
        ).fetchone()[0]
        version_id = f"FTV-{uuid.uuid4().hex}"
        conn.execute(
            """
            INSERT INTO result_versions(
              id,month_id,run_id,version_number,status,label,snapshot_json,
              workbook_path,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                version_id,
                month_id,
                run_id,
                number,
                "draft",
                payload.get("label") or f"修订草稿 {number}",
                json_dump(payload["snapshot"]),
                payload["workbook_path"],
                timestamp,
                timestamp,
            ),
        )
    conn.execute(
        "UPDATE attendance_months SET current_version_id=?,status='draft',updated_at=? WHERE id=?",
        (version_id, timestamp, month_id),
    )
    add_audit(
        conn,
        month_id,
        "result_version",
        version_id,
        "rebuild",
        payload.get("actor") or "system",
        {"workbook_path": payload["workbook_path"]},
    )
    if commit:
        conn.commit()
    return version_payload(
        conn.execute("SELECT * FROM result_versions WHERE id=?", (version_id,)).fetchone()
    )


def publish_version(conn, payload, *, commit=True):
    month_id = payload["month_id"]
    version_id = payload.get("version_id")
    if not version_id:
        version_id = conn.execute(
            "SELECT current_version_id FROM attendance_months WHERE id=?", (month_id,)
        ).fetchone()[0]
    row = conn.execute(
        "SELECT * FROM result_versions WHERE id=? AND month_id=?",
        (version_id, month_id),
    ).fetchone()
    if not row:
        raise ValueError("待发布版本不存在")
    timestamp = now_iso()
    conn.execute(
        "UPDATE result_versions SET status='published',published_at=?,updated_at=? WHERE id=?",
        (timestamp, timestamp, version_id),
    )
    conn.execute(
        """
        UPDATE attendance_months SET status='published',published_version_id=?,
          current_version_id=?,updated_at=? WHERE id=?
        """,
        (version_id, version_id, timestamp, month_id),
    )
    add_audit(
        conn,
        month_id,
        "result_version",
        version_id,
        "publish",
        payload.get("actor") or "HR",
        {},
    )
    if commit:
        conn.commit()
    return version_payload(
        conn.execute("SELECT * FROM result_versions WHERE id=?", (version_id,)).fetchone()
    )


def add_correction(conn, payload, *, commit=True):
    month = conn.execute(
        "SELECT * FROM attendance_months WHERE id=?", (payload["month_id"],)
    ).fetchone()
    if not month or not month["current_run_id"]:
        raise ValueError("月度批次不存在或尚未完成计算")
    correction_id = payload.get("id") or f"FTC-{uuid.uuid4().hex}"
    timestamp = now_iso()
    conn.execute(
        """
        INSERT INTO corrections(
          id,month_id,run_id,employee_name,target_type,day,field_key,
          original_value_json,new_value_json,reason,operator_name,feedback_id,
          status,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            correction_id,
            payload["month_id"],
            month["current_run_id"],
            payload["employee_name"],
            payload["target_type"],
            payload.get("day"),
            payload.get("field_key"),
            json_dump(payload.get("original_value")),
            json_dump(payload.get("new_value")),
            payload["reason"],
            payload.get("operator_name") or "HR",
            payload.get("feedback_id") or "",
            "active",
            timestamp,
            timestamp,
        ),
    )
    add_audit(
        conn,
        payload["month_id"],
        "correction",
        correction_id,
        "create",
        payload.get("operator_name") or "HR",
        payload,
    )
    if commit:
        conn.commit()
    return correction_payload(
        conn.execute("SELECT * FROM corrections WHERE id=?", (correction_id,)).fetchone()
    )


def delete_correction(conn, payload, *, commit=True):
    row = conn.execute(
        "SELECT * FROM corrections WHERE id=? AND month_id=?",
        (payload["correction_id"], payload["month_id"]),
    ).fetchone()
    if not row:
        return False
    timestamp = now_iso()
    conn.execute(
        "UPDATE corrections SET status='reversed',updated_at=? WHERE id=?",
        (timestamp, row["id"]),
    )
    add_audit(
        conn,
        payload["month_id"],
        "correction",
        row["id"],
        "reverse",
        payload.get("actor") or "HR",
        {},
    )
    if commit:
        conn.commit()
    return True


def save_notice(conn, payload, *, commit=True):
    timestamp = payload.get("created_at") or now_iso()
    conn.execute(
        """
        INSERT INTO notices(
          token,month_id,run_id,version_id,employee_name,display_name,
          recipient_user_id,payload_json,summary_json,status,created_at,expires_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(token) DO UPDATE SET
          payload_json=excluded.payload_json,summary_json=excluded.summary_json,
          status=excluded.status,expires_at=excluded.expires_at
        """,
        (
            payload["token"],
            payload.get("month_id"),
            payload.get("run_id"),
            payload.get("version_id"),
            payload["employee_name"],
            payload.get("display_name") or payload["employee_name"],
            payload.get("recipient_user_id") or "",
            json_dump(payload["payload"]),
            json_dump(payload.get("summary") or {}),
            payload.get("status") or "sent",
            timestamp,
            payload.get("expires_at"),
        ),
    )
    delivery_id = f"FTD-{uuid.uuid4().hex}"
    conn.execute(
        """
        INSERT INTO notification_deliveries(
          id,notice_token,month_id,version_id,employee_name,delivery_type,status,
          detail_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            delivery_id,
            payload["token"],
            payload.get("month_id"),
            payload.get("version_id"),
            payload["employee_name"],
            payload.get("delivery_type") or "test",
            payload.get("status") or "sent",
            json_dump(payload.get("delivery_detail") or {}),
            timestamp,
        ),
    )
    if commit:
        conn.commit()
    return {"token": payload["token"], "delivery_id": delivery_id}


def get_notice(conn, payload):
    row = conn.execute("SELECT * FROM notices WHERE token=?", (payload["token"],)).fetchone()
    if not row:
        return None
    if row["expires_at"]:
        try:
            expires = parse_iso_datetime(row["expires_at"])
            if expires < datetime.now(timezone.utc):
                return None
        except ValueError:
            pass
    result = row_to_dict(row)
    result["payload"] = json_load(result.pop("payload_json"), {})
    result["summary"] = json_load(result.pop("summary_json"), {})
    result["feedbacks"] = [
        feedback_payload(item)
        for item in conn.execute(
            "SELECT * FROM feedback_cases WHERE notice_token=? ORDER BY created_at",
            (payload["token"],),
        ).fetchall()
    ]
    return result


def acknowledge_notice(conn, payload, *, commit=True):
    row = conn.execute("SELECT * FROM notices WHERE token=?", (payload["token"],)).fetchone()
    if not row:
        return None
    if not row["acknowledged_at"]:
        timestamp = now_iso()
        notice_payload = json_load(row["payload_json"], {})
        workbook = notice_payload.get("workbook") or {}
        feedback_id = (
            f"FT-ACK-{timestamp[:10].replace('-', '')}-{uuid.uuid4().hex[:10].upper()}"
        )
        conn.execute(
            "UPDATE notices SET acknowledged_at=?,ack_source='manual',"
            "status='acknowledged' WHERE token=?",
            (timestamp, payload["token"]),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO feedback_cases(
              id,notice_token,month_id,run_id,record_type,employee_name,period,
              status,notice_created_at,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                feedback_id,
                payload["token"],
                row["month_id"],
                row["run_id"],
                "已阅",
                row["employee_name"],
                f"{workbook.get('year','')}年{workbook.get('month','')}月",
                "已阅，无问题",
                row["created_at"],
                timestamp,
                timestamp,
            ),
        )
        if commit:
            conn.commit()
    return get_notice(conn, payload)


def create_feedback(conn, payload, *, commit=True):
    timestamp = payload.get("created_at") or now_iso()
    conn.execute(
        """
        INSERT INTO feedback_cases(
          id,notice_token,month_id,run_id,review_token,record_type,
          parent_feedback_id,employee_name,period,category,field_key,field_label,
          day_values_json,day_items_json,days,detail,status,statistics_snapshot_json,
          notice_created_at,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            payload["id"],
            payload.get("notice_token"),
            payload.get("month_id"),
            payload.get("run_id"),
            payload.get("review_token"),
            payload["record_type"],
            payload.get("parent_feedback_id") or "",
            payload["employee_name"],
            payload.get("period") or "",
            payload.get("category") or "",
            payload.get("field_key") or "",
            payload.get("field_label") or "",
            json_dump(payload.get("day_values") or []),
            json_dump(payload.get("day_items") or []),
            payload.get("days") or "",
            payload.get("detail") or "",
            payload.get("status") or "",
            json_dump(payload.get("statistics_snapshot") or {}),
            payload.get("notice_created_at") or "",
            timestamp,
            timestamp,
        ),
    )
    add_audit(
        conn,
        payload.get("month_id"),
        "feedback",
        payload["id"],
        "create",
        payload["employee_name"],
        {"notice_token": payload.get("notice_token"), "days": payload.get("day_values")},
    )
    if commit:
        conn.commit()
    return get_feedback_by_id(conn, payload["id"])


def get_feedback_by_id(conn, feedback_id):
    return feedback_payload(
        conn.execute("SELECT * FROM feedback_cases WHERE id=?", (feedback_id,)).fetchone()
    )


def update_feedback(conn, payload, *, commit=True):
    allowed = {
        "status",
        "review_note",
        "reviewed_at",
        "reviewer_notified_at",
        "reviewer_notify_error",
    }
    patch = {key: value for key, value in (payload.get("patch") or {}).items() if key in allowed}
    if not patch:
        return get_feedback_by_id(conn, payload["id"])
    patch["updated_at"] = now_iso()
    fields = ",".join(f"{key}=?" for key in patch)
    conn.execute(
        f"UPDATE feedback_cases SET {fields} WHERE id=?",
        (*patch.values(), payload["id"]),
    )
    if commit:
        conn.commit()
    return get_feedback_by_id(conn, payload["id"])


def get_feedback_by_review_token(conn, payload):
    return feedback_payload(
        conn.execute(
            "SELECT * FROM feedback_cases WHERE review_token=?",
            (payload["review_token"],),
        ).fetchone()
    )


def update_feedback_by_review_token(conn, payload):
    row = conn.execute(
        "SELECT id FROM feedback_cases WHERE review_token=?",
        (payload["review_token"],),
    ).fetchone()
    if not row:
        return None
    return update_feedback(
        conn, {"id": row["id"], "patch": payload.get("patch") or {}}
    )


def algorithm_rule_change_payload(row):
    if not row:
        return None
    result = row_to_dict(row)
    result["changed_files"] = json_load(result.pop("changed_files_json"), [])
    result["source_refs"] = json_load(result.pop("source_refs_json"), [])
    return result


ALGORITHM_RULE_CHANGE_STATUSES = {
    "draft",
    "confirmed",
    "implemented",
    "verified",
    "published",
    "cancelled",
}


def list_algorithm_rule_changes(conn, payload):
    month_id = str(payload.get("month_id") or "").strip()
    if not conn.execute(
        "SELECT 1 FROM attendance_months WHERE id=?", (month_id,)
    ).fetchone():
        raise ValueError("未找到该月度考勤批次")
    status = str(payload.get("status") or "").strip()
    params = [month_id]
    where = "month_id=?"
    if status:
        if status not in ALGORITHM_RULE_CHANGE_STATUSES:
            raise ValueError("无效的算法/规则变更状态")
        where += " AND status=?"
        params.append(status)
    rows = conn.execute(
        "SELECT * FROM algorithm_rule_changes WHERE " + where +
        " ORDER BY created_at DESC, id DESC LIMIT 500",
        tuple(params),
    ).fetchall()
    return [algorithm_rule_change_payload(row) for row in rows]


def create_algorithm_rule_change(conn, payload):
    month_id = str(payload.get("month_id") or "").strip()
    if not conn.execute(
        "SELECT 1 FROM attendance_months WHERE id=?", (month_id,)
    ).fetchone():
        raise ValueError("未找到该月度考勤批次")
    change_type = str(payload.get("change_type") or "").strip()
    if change_type not in ("algorithm", "rule"):
        raise ValueError("change_type 必须是 algorithm 或 rule")
    title = str(payload.get("title") or "").strip()
    problem = str(payload.get("problem") or "").strip()
    business_confirmation = str(payload.get("business_confirmation") or "").strip()
    old_behavior = str(payload.get("old_behavior") or "").strip()
    new_behavior = str(payload.get("new_behavior") or "").strip()
    if not title or not problem or not business_confirmation or not new_behavior:
        raise ValueError("变更标题、问题说明、业务确认结论和变更后规则不能为空")
    status = str(payload.get("status") or "confirmed").strip()
    if status not in ALGORITHM_RULE_CHANGE_STATUSES:
        raise ValueError("无效的算法/规则变更状态")
    change_id = str(payload.get("id") or "").strip() or f"FTRC-{uuid.uuid4().hex}"
    timestamp = now_iso()
    operator_name = str(payload.get("operator_name") or "HR").strip()[:40]
    confirmed_by = str(payload.get("confirmed_by") or "").strip()[:80]
    confirmed_at = payload.get("confirmed_at") or (
        timestamp if status in ("confirmed", "implemented", "verified", "published") else None
    )
    implemented_at = payload.get("implemented_at") or (
        timestamp if status in ("implemented", "verified", "published") else None
    )
    verified_at = payload.get("verified_at") or (
        timestamp if status in ("verified", "published") else None
    )
    published_at = payload.get("published_at") or (
        timestamp if status == "published" else None
    )
    conn.execute(
        """
        INSERT INTO algorithm_rule_changes(
          id,month_id,change_type,title,problem,business_confirmation,
          old_behavior,new_behavior,impact_scope,changed_files_json,source_refs_json,
          validation_result,algorithm_version_before,algorithm_version_after,status,
          operator_name,confirmed_by,confirmed_at,implemented_at,verified_at,published_at,
          created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            change_id,
            month_id,
            change_type,
            title[:160],
            problem[:2000],
            business_confirmation[:2000],
            old_behavior[:2000],
            new_behavior[:2000],
            str(payload.get("impact_scope") or "").strip()[:1000],
            json_dump(payload.get("changed_files") or []),
            json_dump(payload.get("source_refs") or []),
            str(payload.get("validation_result") or "").strip()[:3000],
            str(payload.get("algorithm_version_before") or "").strip()[:120],
            str(payload.get("algorithm_version_after") or "").strip()[:120],
            status,
            operator_name,
            confirmed_by,
            confirmed_at,
            implemented_at,
            verified_at,
            published_at,
            timestamp,
            timestamp,
        ),
    )
    add_audit(
        conn,
        month_id,
        "algorithm_rule_change",
        change_id,
        "create",
        operator_name,
        algorithm_rule_change_payload(
            conn.execute(
                "SELECT * FROM algorithm_rule_changes WHERE id=?", (change_id,)
            ).fetchone()
        ),
    )
    conn.commit()
    return algorithm_rule_change_payload(
        conn.execute(
            "SELECT * FROM algorithm_rule_changes WHERE id=?", (change_id,)
        ).fetchone()
    )


def update_algorithm_rule_change(conn, payload):
    month_id = str(payload.get("month_id") or "").strip()
    change_id = str(payload.get("change_id") or "").strip()
    row = conn.execute(
        "SELECT * FROM algorithm_rule_changes WHERE id=? AND month_id=?",
        (change_id, month_id),
    ).fetchone()
    if not row:
        return None
    patch = payload.get("patch") or {}
    allowed = {
        "change_type",
        "title",
        "problem",
        "business_confirmation",
        "old_behavior",
        "new_behavior",
        "impact_scope",
        "changed_files",
        "source_refs",
        "validation_result",
        "algorithm_version_before",
        "algorithm_version_after",
        "status",
        "operator_name",
        "confirmed_by",
    }
    updates = {}
    for key, value in patch.items():
        if key not in allowed:
            continue
        if key == "change_type":
            value = str(value or "").strip()
            if value not in ("algorithm", "rule"):
                raise ValueError("change_type 必须是 algorithm 或 rule")
        elif key == "status":
            value = str(value or "").strip()
            if value not in ALGORITHM_RULE_CHANGE_STATUSES:
                raise ValueError("无效的算法/规则变更状态")
        elif key in ("changed_files", "source_refs"):
            value = value if isinstance(value, list) else []
            key = key + "_json"
            value = json_dump(value)
        else:
            value = str(value or "").strip()
            limits = {
                "title": 160,
                "problem": 2000,
                "business_confirmation": 2000,
                "old_behavior": 2000,
                "new_behavior": 2000,
                "impact_scope": 1000,
                "validation_result": 3000,
                "algorithm_version_before": 120,
                "algorithm_version_after": 120,
                "operator_name": 40,
                "confirmed_by": 80,
            }
            value = value[:limits.get(key, 2000)]
        updates[key] = value
    if "title" in updates and not updates["title"]:
        raise ValueError("变更标题不能为空")
    if "problem" in updates and not updates["problem"]:
        raise ValueError("问题说明不能为空")
    if "business_confirmation" in updates and not updates["business_confirmation"]:
        raise ValueError("业务确认结论不能为空")
    if "new_behavior" in updates and not updates["new_behavior"]:
        raise ValueError("变更后规则不能为空")
    if "status" in updates:
        status = updates["status"]
        timestamp = now_iso()
        if status in ("confirmed", "implemented", "verified", "published") and not row["confirmed_at"]:
            updates["confirmed_at"] = timestamp
        if status in ("implemented", "verified", "published") and not row["implemented_at"]:
            updates["implemented_at"] = timestamp
        if status in ("verified", "published") and not row["verified_at"]:
            updates["verified_at"] = timestamp
        if status == "published" and not row["published_at"]:
            updates["published_at"] = timestamp
    if not updates:
        return algorithm_rule_change_payload(row)
    updates["updated_at"] = now_iso()
    fields = ",".join(f"{key}=?" for key in updates)
    conn.execute(
        f"UPDATE algorithm_rule_changes SET {fields} WHERE id=? AND month_id=?",
        (*updates.values(), change_id, month_id),
    )
    actor = str(payload.get("actor") or updates.get("operator_name") or row["operator_name"] or "HR")
    after = conn.execute(
        "SELECT * FROM algorithm_rule_changes WHERE id=?", (change_id,)
    ).fetchone()
    add_audit(
        conn,
        month_id,
        "algorithm_rule_change",
        change_id,
        "update",
        actor,
        {"patch": updates, "record": algorithm_rule_change_payload(after)},
    )
    conn.commit()
    return algorithm_rule_change_payload(after)


def import_legacy_feedback(conn, payload):
    imported = 0
    for record in payload.get("records") or []:
        if not record.get("id") or conn.execute(
            "SELECT 1 FROM feedback_cases WHERE id=?", (record["id"],)
        ).fetchone():
            continue
        normalized = dict(record)
        normalized.setdefault("record_type", "新增异常")
        normalized.setdefault("employee_name", "未知员工")
        normalized.setdefault("created_at", now_iso())
        if normalized.get("status") == "待久山处理":
            normalized["status"] = "待玖杉处理"
        create_feedback(conn, normalized)
        patch = {
            key: record.get(key)
            for key in (
                "reviewer_notified_at",
                "reviewer_notify_error",
                "review_note",
                "reviewed_at",
            )
            if record.get(key) is not None
        }
        if patch:
            update_feedback(conn, {"id": record["id"], "patch": patch})
        imported += 1
    return {"imported": imported}


def add_audit(conn, month_id, entity_type, entity_id, action, actor, detail):
    conn.execute(
        """
        INSERT INTO audit_logs(
          id,month_id,entity_type,entity_id,action,actor,detail_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            f"FTA-{uuid.uuid4().hex}",
            month_id,
            entity_type,
            entity_id,
            action,
            actor,
            json_dump(detail or {}),
            now_iso(),
        ),
    )


ACTIONS = {
    "init": lambda conn, payload: {"schema_version": SCHEMA_VERSION},
    "persist_analysis": persist_analysis,
    "get_run": get_run,
    "list_months": list_months,
    "get_month": get_month,
    "save_version": save_version,
    "publish_version": publish_version,
    "add_correction": add_correction,
    "delete_correction": delete_correction,
    "save_notice": save_notice,
    "save_recipient_mappings": save_recipient_mappings,
    "save_notification_exclusions": save_notification_exclusions,
    "get_notice": get_notice,
    "acknowledge_notice": acknowledge_notice,
    "auto_acknowledge_notices": auto_acknowledge_due_notices,
    "create_feedback": create_feedback,
    "update_feedback": update_feedback,
    "get_feedback_by_review_token": get_feedback_by_review_token,
    "update_feedback_by_review_token": update_feedback_by_review_token,
    "list_algorithm_rule_changes": list_algorithm_rule_changes,
    "create_algorithm_rule_change": create_algorithm_rule_change,
    "update_algorithm_rule_change": update_algorithm_rule_change,
    "import_legacy_feedback": import_legacy_feedback,
}


def main():
    request = json.load(sys.stdin)
    action = request.get("action")
    if action not in ACTIONS:
        raise ValueError(f"不支持的数据库操作：{action}")
    with connect() as conn:
        result = ACTIONS[action](conn, request.get("payload") or {})
    print(json_dump({"ok": True, "result": result}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json_dump({"ok": False, "error": str(exc)}))
        sys.exit(1)
