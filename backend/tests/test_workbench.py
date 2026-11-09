import io
import json
import tempfile
import time
import unittest
from base64 import urlsafe_b64encode
from hmac import digest
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote
from zipfile import ZipFile

from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from backend import fulltime
from backend.config import Settings
from backend.database import Database
from backend.main import create_app
from backend.migrate import migrate


def workbook(*rows):
    book = Workbook()
    for row in rows:
        book.active.append(row)
    output = io.BytesIO()
    book.save(output)
    return output.getvalue()


class ParttimeSliceTest(unittest.TestCase):
    def test_login_to_versioned_workbook(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(Path(directory), "fixture-admin", "fixture-password", False)
            with TestClient(create_app(settings)) as client:
                self.assertEqual(client.get("/api/v1/attendance/parttime/months").status_code, 401)
                login = client.post("/api/v1/session", json={"username": "fixture-admin", "password": "fixture-password"})
                self.assertEqual(login.status_code, 200, login.text)
                event_types = client.get("/api/v1/event-schema").json()
                payload = event_types["payload_schemas"]["attendance.parttime.analysis_completed.v1"]
                self.assertEqual(set(payload["required"]), {"month_id", "analysis_id", "workbook_id"})
                client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
                schedule = workbook(
                    [None] * 5 + [datetime(2026, 8, day) for day in (1, 2, 3)],
                    ["官旗店", None, "09:00-11:00", None, "主播", "测试甲"],
                )
                backend = workbook(["开播时间", "开播时长（分钟）"], ["2026-08-01 09:00:00", 120])
                source_ids = {}
                for source_type, content in (("schedule", schedule), ("backend", backend)):
                    response = client.post("/api/v1/attendance/parttime/source-files", data={"source_type": source_type},
                                           files={"file": (f"{source_type}.xlsx", content)})
                    self.assertEqual(response.status_code, 201, response.text)
                    source_ids[f"{source_type}_file_id"] = response.json()["file_id"]
                inspection = client.post("/api/v1/attendance/parttime/schedule-inspections", json={
                    "file_id": source_ids["schedule_file_id"],
                })
                self.assertEqual(inspection.json(), {"sheets": ["Sheet"]})
                route = "/api/v1/attendance/parttime/months/2026-08/analyses"
                headers = {"Idempotency-Key": "fixture-analysis"}
                response = client.post(route, json=source_ids, headers=headers)
                self.assertEqual(response.status_code, 202, response.text)
                self.assertEqual(client.post(route, json=source_ids, headers=headers).json(), response.json())
                deadline = time.monotonic() + 15
                while True:
                    job = client.get(response.headers["Location"]).json()
                    if job["status"] in ("succeeded", "failed") or time.monotonic() >= deadline:
                        break
                    time.sleep(0.05)
                self.assertEqual(job["status"], "succeeded", job)
                analysis_id = job["result"]["analysis_id"]
                base = f"/api/v1/attendance/parttime/analyses/{analysis_id}"
                result = client.get(base).json()["result"]
                row = next(row for row in result["schedule"] if row["person"] == "测试甲")
                self.assertEqual((row["planned_hours"], row["backend_hours"], row["final_hours"]), (2, 2, 2))
                self.assertEqual(len(client.get(route).json()["items"]), 1)
                export = client.get(f"{base}/workbook")
                self.assertEqual(export.status_code, 200)
                book = load_workbook(io.BytesIO(export.content), read_only=True)
                self.assertIn("标准化排班", book.sheetnames)
                book.close()


class FulltimeSliceTest(unittest.TestCase):
    def test_due_receipts_are_updated_without_opening_the_month(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(Path(directory), "fixture-admin", "fixture-password", False)
            app = create_app(settings)
            db = app.state.database
            db.initialize()
            fulltime.initialize(db)
            created_at = "2026-01-01T00:00:00.000Z"
            with db.connect() as connection:
                connection.execute("INSERT INTO attendance_months (id,year,month,created_at,updated_at) VALUES (?,?,?,?,?)",
                                   ("FTM-202601", 2026, 1, created_at, created_at))
                for token, delivery_type, status in (("due", "formal", "sent"), ("test", "test", "sent"),
                                                      ("accepted", "formal", "accepted")):
                    fulltime.legacy.save_notice(connection, {
                        "token": token, "month_id": "FTM-202601", "employee_name": "样例员工", "payload": {},
                        "created_at": created_at, "delivery_type": delivery_type, "status": status,
                    }, commit=False)
            with TestClient(app):
                deadline = time.monotonic() + 5
                while True:
                    with db.connect() as connection:
                        statuses = {row["token"]: row["status"] for row in connection.execute("SELECT token,status FROM notices")}
                    if statuses["due"] == "acknowledged" or time.monotonic() >= deadline:
                        break
                    time.sleep(0.05)
                self.assertEqual(statuses, {"due": "acknowledged", "test": "sent", "accepted": "accepted"})
                with db.connect() as connection:
                    events = connection.execute("SELECT payload FROM outbox WHERE event_type=?",
                                                ("attendance.fulltime.notice_acknowledged.v1",)).fetchall()
                self.assertEqual([json.loads(event["payload"])["ack_source"] for event in events], ["auto_24h"])

    @patch.dict("os.environ", {"HR_ENABLE_DINGTALK_DELIVERY": "0", "HR_ENABLE_PERFORMANCE_EXTERNAL": "0",
                               "HR_PERFORMANCE_LINK_SECRET": "fixture-only-link-signing-key-for-workbench"})
    def test_calculate_publish_and_review_in_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(Path(directory), "fixture-admin", "fixture-password", False, performance_compat=True)
            with TestClient(create_app(settings)) as client:
                login = client.post("/api/v1/session", json={"username": "fixture-admin", "password": "fixture-password"})
                self.assertEqual(login.status_code, 200, login.text)
                client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
                self.assertEqual(client.get("/api/v1/performance/months").json(), {"items": []})

                book = Workbook()
                sheet = book.active
                sheet.title = "全职"
                sheet["C3"], sheet["D3"], sheet["C6"], sheet["C8"], sheet["D8"], sheet["G8"] = (
                    2026, 8, "姓名", "测试乙", "2026-01-01", 21.75,
                )
                for day in range(1, 32):
                    sheet.cell(6, day + 31, day)
                sheet.cell(6, 63, "备注")
                monthly = book.create_sheet("月度数据")
                monthly.cell(5, 1, "测试乙")
                monthly.cell(5, 44, "正常")
                book.create_sheet("原始数据")
                output = io.BytesIO()
                book.save(output)
                source = client.post("/api/v1/attendance/fulltime/source-files", data={"source_type": "workbook"},
                                     files={"file": ("fixture.xlsx", output.getvalue())})
                self.assertEqual(source.status_code, 201, source.text)
                route = "/api/v1/attendance/fulltime/months/FTM-202608"
                accepted = client.post(f"{route}/runs", headers={"Idempotency-Key": "calculate"}, json={
                    "run_type": "workbook", "workbook_id": source.json()["file_id"],
                })
                self.assertEqual(accepted.status_code, 202, accepted.text)
                self.wait_for_job(client, accepted.json()["job_id"])
                result = client.get(route).json()
                monthly_rows = (["月度汇总 2026-08-01"], [], [], [], ["测试乙"] + [None] * 42 + ["正常"])
                source_ids = {}
                for source_type, content in (("monthly", workbook(*monthly_rows)), ("raw", workbook(["原始记录"]))):
                    upload = client.post("/api/v1/attendance/fulltime/source-files", data={"source_type": source_type},
                                         files={"file": (f"{source_type}.xlsx", content)})
                    self.assertEqual(upload.status_code, 201, upload.text)
                    source_ids[f"{source_type}_file_id"] = upload.json()["file_id"]
                merge = client.post(f"{route}/runs", headers={"Idempotency-Key": "merge"}, json={
                    "run_type": "source_merge", "expected_revision": result["revision"], **source_ids,
                })
                self.assertEqual(merge.status_code, 202, merge.text)
                self.wait_for_job(client, merge.json()["job_id"])
                result = client.get(route).json()
                version_id = result["current_version_id"]
                publication = client.post(f"{route}/publications", headers={"Idempotency-Key": "publish"},
                                          json={"version_id": version_id, "expected_revision": result["revision"]})
                self.assertEqual(publication.status_code, 201, publication.text)
                notice = client.post(f"{route}/notifications", headers={"Idempotency-Key": "preview"}, json={
                    "version_id": version_id, "expected_revision": publication.json()["revision"],
                    "delivery_type": "test", "employee_names": ["测试乙"],
                })
                self.assertEqual(notice.status_code, 202, notice.text)
                repeat = client.post(f"{route}/notifications", headers={"Idempotency-Key": "preview"}, json={
                    "version_id": version_id, "expected_revision": publication.json()["revision"],
                    "delivery_type": "test", "employee_names": ["测试乙"],
                })
                self.assertEqual(repeat.json(), notice.json())
                self.wait_for_job(client, notice.json()["job_id"])
                token = notice.json()["preview_urls"][0].rsplit("/", 1)[-1]
                public = f"/api/public/v1/attendance/notices/{token}"
                acknowledged = client.post(f"{public}/acknowledgments")
                self.assertTrue(acknowledged.json()["acknowledged_at"])
                feedback = client.post(f"{public}/feedbacks", json={"category": "其他", "detail": "合成样例反馈"})
                self.assertEqual(feedback.status_code, 201, feedback.text)
                case = client.get(route).json()["feedbacks"][0]
                reviewed = client.patch(f"/api/public/v1/attendance/reviews/{case['review_token']}", json={"status": "已处理"})
                self.assertEqual(reviewed.json()["status"], "已处理")
                rebuilt = client.post(f"{route}/runs", headers={"Idempotency-Key": "recalculate"}, json={
                    "run_type": "recalculate", "expected_revision": publication.json()["revision"],
                })
                self.assertEqual(rebuilt.status_code, 202, rebuilt.text)
                self.wait_for_job(client, rebuilt.json()["job_id"])
                result = client.get(route).json()
                self.assertNotEqual(result["current_version_id"], version_id)
                self.assertEqual(client.get(public).json()["version_id"], result["published_version_id"])
                self.assertEqual(client.get(f"/api/v1/attendance/fulltime/versions/{version_id}/workbook").status_code, 200)

            path = Path(directory)
            target = path / "migration-candidate"
            performance_zip = path / "performance-fixture.zip"
            key = "example|2026年9月"
            approval_key = "2026年9月|测试部门"
            identity = {"empId": "example", "month": "2026年9月"}
            employee = {"name": "测试员工", "realName": "测试员工", "directMgr": "测试经理", "hrbp": "测试BP", "dept": "测试部门"}
            records = {
                "data/okr_workflows.json": {key: identity | {"status": "invited", "emp": employee}},
                "data/kpi_targets.json": {key: identity | {"emp": employee,
                    "kpis": [{"seq": 1, "indicator": "测试KPI", "weight": 100, "rule": "示例"}]},
                    "unsigned|2026年9月": {"empId": "unsigned", "month": identity["month"],
                        "emp": employee | {"name": "待签员工", "realName": "待签员工"},
                        "kpis": [{"seq": 1, "indicator": "待签目标", "weight": 100, "rule": "示例"}]}},
                "data/kpi_confirm_data.json": {key: identity | {"name": employee["name"], "doc": "<p>合成目标签署</p>"}},
                "data/oa_approval_data.json": {approval_key: {"status": "failed", "lastError": {"message": "样例失败"},
                    "snapshot": {"month": "2026年9月", "department": "测试部门", "employees": []}}},
            }
            with ZipFile(performance_zip, "w") as archive:
                manifest = []
                for entry, contents in records.items():
                    content = json.dumps(contents, ensure_ascii=False).encode("utf-8")
                    archive.writestr(entry, content)
                    manifest.append({"path": entry, "size": len(content), "sha256": sha256(content).hexdigest()})
                archive.writestr("manifest.json", json.dumps({"files": manifest}))
            report = migrate(target, fulltime_db=path / "workbench.sqlite", fulltime_workbooks=path / "fulltime" / "workbooks",
                             performance_zip=performance_zip)
            self.assertEqual(report["fulltime"]["tables"]["result_versions"], 2)
            settings = Settings(target, "fixture-admin", "fixture-password", False, performance_compat=True)
            with TestClient(create_app(settings)) as restored:
                restored.post("/api/v1/session", json={"username": "fixture-admin", "password": "fixture-password"})
                self.assertEqual(restored.get(route).json()["published_version_id"], version_id)
                self.assertEqual(restored.get(f"/api/v1/attendance/fulltime/versions/{version_id}/workbook").status_code, 200)
                okr = restored.get("/api/v1/performance/months/2026-09/assessments?assessment_type=okr").json()["items"]
                self.assertEqual([(item["employee_id"], item["status"]) for item in okr], [("example", "invited")])
                self.assertEqual(okr[0]["workflow"]["admin_action"], "remind")
                session = restored.get("/api/v1/session").json()
                csrf = {"X-CSRF-Token": session["csrf_token"]}
                signature = restored.post("/api/v1/performance/signature-verifications", headers=csrf,
                    json={"employee_id": "example", "month": "2026-09", "document_type": "kpi"})
                self.assertEqual(signature.status_code, 409, signature.text)
                approval_list = "/api/v1/performance/oa-approvals"
                approval = restored.get(approval_list).json()["items"][0]
                self.assertEqual((approval["approval_id"], approval["status"], approval["can_retry"]), (approval_key, "failed", True))
                retry = restored.post(f"{approval_list}/{quote(approval_key)}/retries", headers=csrf)
                self.assertEqual(retry.json()["approval"]["status"], "disabled")
                premature = restored.post("/api/v1/performance/months/2026-09/reminders",
                    headers={"X-CSRF-Token": session["csrf_token"]}, json={
                        "employee_id": "example", "assessment_type": "okr", "action": "send_self_review",
                    })
                self.assertEqual(premature.status_code, 409, premature.text)
                def public_page(path):
                    token = digest(b"fixture-only-link-signing-key-for-workbench", path.encode(), "sha256").hex()
                    return restored.get(f"/hrms/performance{path}?token={token}")

                encoded = urlsafe_b64encode("unsigned|2026年9月".encode()).decode().rstrip("=")
                signing = public_page(f"/kpi-confirm-page/{encoded}")
                self.assertEqual(signing.status_code, 200, signing.text)
                self.assertIn("获取钉钉验证码", signing.text)

                def action_token(action):
                    signed_path = f"/workflow-action/{action}/example/{quote('2026年9月')}"
                    return digest(b"fixture-only-link-signing-key-for-workbench", signed_path.encode(), "sha256").hex()

                action = "okr-target-submit"
                payload = identity | {"action": action, "actionToken": action_token(action),
                           "objectives": [{"title": "验证计算", "weight": 100,
                                           "keyResults": [{"result": "旧新结果一致", "weight": 60}]}]}
                invalid = restored.post("/hrms/performance/okr-action", json=payload)
                self.assertEqual(invalid.status_code, 400, invalid.text)
                payload["objectives"][0]["keyResults"][0]["weight"] = 100
                submitted = restored.post("/hrms/performance/okr-action", json=payload)
                self.assertEqual(submitted.json()["status"], "target_submitted", submitted.text)
                detail = restored.get("/api/v1/performance/months/2026-09/assessments?assessment_type=okr").json()["items"][0]
                self.assertEqual(detail["workflow"]["assignee"], "manager")
                self_score = restored.post("/hrms/performance/submit", json=identity | {"actionToken": action_token("self"),
                    "selfScore": 0, "details": [{"seq": 1, "selfScore": 110, "completion": "完成"}]})
                self.assertEqual(self_score.status_code, 200, self_score.text)
                manager_page = public_page(f"/mgr-page/example/{quote(identity['month'])}")
                self.assertEqual(manager_page.status_code, 200, manager_page.text)
                self.assertIn("提交评分", manager_page.text)
                manager_score = restored.post("/hrms/performance/submit-mgr", headers={"Origin": "http://testserver"},
                    json=identity | {"actionToken": action_token("manager"), "mgrScore": 0, "mgrScores": [{"seq": 1, "score": 111}]})
                self.assertEqual(manager_score.status_code, 200, manager_score.text)
                self.assertIn("BP 核准", public_page(f"/bp-page/example/{quote(identity['month'])}").text)
                bp_score = restored.post("/hrms/performance/submit-bp", headers={"Origin": "http://testserver"},
                    json=identity | {"actionToken": action_token("bp"), "bpScore": 0, "bpScores": [{"seq": 1, "score": 100}]})
                self.assertEqual(bp_score.status_code, 200, bp_score.text)
                result = restored.get("/hrms/performance/bp-data").json()[key]
                self.assertEqual((result["selfScore"], result["mgrScore"], result["bpScore"], result["grade"]), (110, 111, 100, "B"))
                self.assertIn("获取钉钉验证码", public_page(f"/result-page/example/{quote(identity['month'])}").text)

    def wait_for_job(self, client, job_id):
        deadline = time.monotonic() + 30
        while True:
            job = client.get(f"/api/v1/jobs/{job_id}").json()
            if job["status"] in ("succeeded", "failed") or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        self.assertEqual(job["status"], "succeeded", job)


class HrSliceTest(unittest.TestCase):
    def test_sync_profile_and_staffing_with_local_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            fixture = path / "organization.json"
            fixture.write_text(json.dumps({
                "departments": [{"id": "d1", "name": "样例部门", "parentId": "1", "managerNames": []}],
                "employees": [{"id": "e1", "name": "测试丙", "departmentIds": ["d1"], "title": "样例岗位", "rosterStatus": "在职"}],
            }), encoding="utf-8")
            with patch.dict("os.environ", {"HR_ORGANIZATION_FIXTURE": str(fixture), "HR_CANONICAL_DEPARTMENTS": "[]"}):
                settings = Settings(path / "instance", "fixture-admin", "fixture-password", False)
                with TestClient(create_app(settings)) as client:
                    login = client.post("/api/v1/session", json={"username": "fixture-admin", "password": "fixture-password"})
                    client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
                    sync = client.post("/api/v1/hr/organization-syncs", json={"scope": "current"})
                    self.assertEqual(sync.status_code, 202, sync.text)
                    deadline = time.monotonic() + 10
                    while True:
                        job = client.get(sync.headers["Location"]).json()
                        if job["status"] in ("succeeded", "failed") or time.monotonic() >= deadline:
                            break
                        time.sleep(0.1)
                    self.assertEqual(job["status"], "succeeded", job)
                    roster = client.get("/api/v1/hr/employees").json()["items"]
                    self.assertEqual([item["id"] for item in roster], ["e1"])
                    profile = client.patch("/api/v1/hr/employees/e1/profile", json={
                        "fields": {"nickname": "样例昵称"}, "expected_revision": 0,
                    })
                    self.assertEqual(profile.json()["fields"]["nickname"], "样例昵称")
                    response = client.put("/api/v1/hr/staffing/d1", json={"approved_headcount": 0, "expected_revision": 0})
                    self.assertEqual(response.json()["remaining_headcount"], -1)
                    with Database(settings.data_dir / "workbench.sqlite").connect() as connection:
                        connection.execute("INSERT INTO recruitment_requests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (
                            "req-1", "d1", "样例部门", "样例岗位", "新增编制", "RECRUITING", 1,
                            "2026-08-01T00:00:00.000Z", "2026-08-01T00:00:00.000Z",
                        ))
                        connection.execute("INSERT INTO recruitment_approvals VALUES (?, ?, ?, ?)", (
                            "approval-1", "req-1", "PENDING", "2026-08-01T00:00:00.000Z",
                        ))
                    recruitment = "/api/v1/hr/recruitment"
                    filters = {"department_id": "d1", "status": "RECRUITING", "month": "2026-08"}
                    self.assertEqual([row["id"] for row in client.get(f"{recruitment}/requests", params=filters).json()["items"]], ["req-1"])
                    self.assertEqual(client.get(f"{recruitment}/dashboard").json()["open_positions"], 1)
                    self.assertEqual(client.get(f"{recruitment}/todos").json()["pending_approvals"][0]["request_id"], "req-1")
                    closed = client.patch(f"{recruitment}/requests/req-1", json={"action": "close", "expected_revision": 0})
                    self.assertEqual(closed.json()["status"], "CLOSED")
                    self.assertEqual(client.get(f"{recruitment}/requests", params=filters).json()["items"], [])
