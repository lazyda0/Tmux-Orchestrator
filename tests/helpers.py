import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import orchestrator


@contextmanager
def database():
    conn = orchestrator.get_db()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


class TemporaryDatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_file = Path(self.temp_dir.name) / "orchestrator.db"
        self.env = mock.patch.dict(
            os.environ,
            {
                "TMUX_ORCHESTRATOR_DB": str(self.db_file),
                "TMUX_ORCHESTRATOR_TMUX_SOCKET": "",
            },
            clear=False,
        )
        self.env.start()
        os.environ.pop("TMUX_PANE", None)

    def tearDown(self):
        self.env.stop()
        self.temp_dir.cleanup()

    def insert_schedule(self, **overrides):
        values = {
            "target": "test:0",
            "note": "check progress",
            "created_at": orchestrator.iso(orchestrator.now()),
            "due_at": orchestrator.iso(orchestrator.now() - orchestrator.timedelta(seconds=1)),
            "pid": None,
            "status": "pending",
            "worker_token": None,
            "claim_token": None,
            "claimed_at": None,
            "fire_attempts": 0,
            "last_error": None,
        }
        values.update(overrides)
        conn = orchestrator.get_db()
        try:
            columns = ", ".join(values)
            placeholders = ", ".join("?" for _ in values)
            cursor = conn.execute(
                f"INSERT INTO schedules ({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def schedule_row(self, schedule_id):
        conn = orchestrator.get_db()
        try:
            return conn.execute(
                "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
        finally:
            conn.close()
