import json
import tempfile
import unittest
from pathlib import Path

from backend.config import Settings
from backend.database import Database, record_event
from backend.jobs import claim, enqueue, execute


class JobRecoveryTest(unittest.TestCase):
    def test_expired_worker_lease_replays_job_and_commits_one_result(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(Path(directory))
            db = Database(settings.data_dir / "workbench.sqlite")
            db.initialize()
            with db.connect() as connection:
                job_id = enqueue(connection, "hr", "admin", "current", {"scope": "current"}, "example-sync")
            self.assertEqual(claim(db)["id"], job_id)
            with db.connect() as connection:
                connection.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (job_id,))

            def handle(_settings, job, _payload):
                def save(connection):
                    record_event(connection, "hr.organization_snapshot_saved.v1", "fixture", 1,
                                 {"snapshot_id": "fixture"}, causation_id=job["id"])
                    return {"snapshot_id": "fixture"}
                return save

            handlers = {"hr": handle}
            self.assertTrue(execute(db, settings, handlers))
            self.assertFalse(execute(db, settings, handlers))
            with db.connect() as connection:
                job = connection.execute("SELECT status,result FROM jobs WHERE id=?", (job_id,)).fetchone()
                self.assertEqual((job["status"], json.loads(job["result"])), ("succeeded", {"snapshot_id": "fixture"}))
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1)
