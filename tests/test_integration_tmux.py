import argparse
import contextlib
import io
import os
import shutil
import subprocess
import sys
import time
import unittest
import uuid
from datetime import timedelta
from pathlib import Path
from unittest import mock

import orchestrator
from tests.helpers import TemporaryDatabaseTestCase


@unittest.skipUnless(shutil.which("tmux"), "tmux is required for integration tests")
class TmuxIntegrationTests(TemporaryDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.socket = f"orchestrator-test-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        os.environ["TMUX_ORCHESTRATOR_TMUX_SOCKET"] = self.socket
        os.environ.pop("TMUX", None)
        os.environ.pop("TMUX_PANE", None)
        self.tmux("new-session", "-d", "-s", "test", "-n", "shell")

    def tearDown(self):
        conn = orchestrator.get_db()
        try:
            rows = conn.execute(
                "SELECT id, pid, worker_token FROM schedules WHERE pid IS NOT NULL"
            ).fetchall()
            for row in rows:
                if orchestrator.worker_matches(row):
                    try:
                        os.kill(row["pid"], 15)
                    except ProcessLookupError:
                        pass
        finally:
            conn.close()
        subprocess.run(
            ["tmux", "-L", self.socket, "kill-server"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        orchestrator.reap_workers(wait=True)
        super().tearDown()

    def tmux(self, *args, check=True):
        return subprocess.run(
            ["tmux", "-L", self.socket, *args],
            capture_output=True,
            text=True,
            check=check,
            timeout=10,
        )

    def test_literal_message_and_unique_marker_are_observed(self):
        conn = orchestrator.get_db()
        try:
            body = "literal Enter C-c Unicode ✓"
            result = orchestrator.send_message(conn, "test:0", body, max_attempts=1)
            self.assertTrue(result.verified, result.error)
            message = conn.execute("SELECT * FROM messages").fetchone()
            self.assertEqual("verified", message["status"])
            self.assertEqual(body, message["body"])
            self.assertIn(result.delivery_id, message["delivery_id"])
        finally:
            conn.close()
        pane = self.tmux("capture-pane", "-t", "test:0", "-p", "-J").stdout
        self.assertIn("literal Enter C-c Unicode ✓", pane)
        self.assertIn("literal Enter C-c Unicode ✓ [orchestrator-delivery:", pane)
        self.assertIn(result.delivery_id, pane)

    def test_repeated_recovery_produces_one_worker_and_one_fire(self):
        schedule_id = self.insert_schedule(
            target="test:0",
            due_at=orchestrator.iso(orchestrator.now() + timedelta(seconds=2)),
        )
        self.assertEqual(0, orchestrator.cmd_recover(None))
        self.assertEqual(0, orchestrator.cmd_recover(None))

        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if self.schedule_row(schedule_id)["status"] == "fired":
                break
            time.sleep(0.2)
        self.assertEqual("fired", self.schedule_row(schedule_id)["status"])
        conn = orchestrator.get_db()
        try:
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
        finally:
            conn.close()

    def test_shell_entrypoints_and_default_target(self):
        script_dir = Path(orchestrator.SCRIPT_DIR)
        env = os.environ.copy()
        env["TMUX_PANE"] = self.tmux("display-message", "-p", "-t", "test:0", "#{pane_id}").stdout.strip()
        sent = subprocess.run(
            [str(script_dir / "send-claude-message.sh"), "test:0", "shell wrapper ✓"],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        self.assertEqual(0, sent.returncode, sent.stderr)

        scheduled = subprocess.run(
            [str(script_dir / "schedule_with_note.sh"), "1", "shell wrapper schedule"],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        self.assertEqual(0, scheduled.returncode, scheduled.stderr)
        conn = orchestrator.get_db()
        try:
            row = conn.execute("SELECT * FROM schedules").fetchone()
        finally:
            conn.close()
        self.assertEqual("test:0", row["target"])
        cancelled = subprocess.run(
            [sys.executable, orchestrator.SELF, "cancel", str(row["id"])],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        self.assertEqual(0, cancelled.returncode, cancelled.stderr)
        self.assertEqual("cancelled", self.schedule_row(row["id"])["status"])

    def test_cancel_terminates_identified_detached_worker(self):
        worker_token = uuid.uuid4().hex
        schedule_id = self.insert_schedule(
            due_at=orchestrator.iso(orchestrator.now() + timedelta(minutes=5)),
            worker_token=worker_token,
        )
        pid = orchestrator.spawn_sleeper(schedule_id, 60, worker_token)
        conn = orchestrator.get_db()
        try:
            conn.execute("UPDATE schedules SET pid = ? WHERE id = ?", (pid, schedule_id))
            conn.commit()
        finally:
            conn.close()
        old_row = self.schedule_row(schedule_id)
        self.assertTrue(orchestrator.worker_matches(old_row))
        try:
            self.assertEqual(0, orchestrator.cmd_cancel(argparse.Namespace(id=schedule_id, target=None)))
            deadline = time.monotonic() + 3
            while orchestrator.worker_matches(old_row) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(orchestrator.worker_matches(old_row))
            self.assertEqual("cancelled", self.schedule_row(schedule_id)["status"])
        finally:
            if orchestrator.worker_matches(old_row):
                os.kill(pid, 15)

    def test_missing_target_becomes_failed_without_heartbeat(self):
        schedule_id = self.insert_schedule(target="test:99")
        with mock.patch.object(orchestrator, "MAX_FIRE_ATTEMPTS", 1):
            self.assertEqual(1, orchestrator.cmd_fire(argparse.Namespace(id=schedule_id)))
        self.assertEqual("failed", self.schedule_row(schedule_id)["status"])
        conn = orchestrator.get_db()
        try:
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM heartbeats").fetchone()[0])
            message = conn.execute("SELECT * FROM messages").fetchone()
            self.assertEqual("failed", message["status"])
        finally:
            conn.close()

    def test_hooks_preserve_existing_commands_and_do_not_duplicate(self):
        self.tmux("set-hook", "-g", "session-created", "display-message 'existing hook'")
        self.assertEqual(0, orchestrator.cmd_install_hooks(None))
        self.assertEqual(0, orchestrator.cmd_install_hooks(None))
        for hook in ("session-created", "client-attached"):
            output = self.tmux("show-hooks", "-g", hook).stdout
            self.assertEqual(1, output.count("orchestrator.py"), output)
            if hook == "session-created":
                self.assertIn("existing hook", output)

    def test_snapshot_uses_isolated_socket(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(0, orchestrator.cmd_snapshot(None))
        self.assertIn("Session: test", output.getvalue())
        self.assertIn("Window 0: shell", output.getvalue())

    def test_hook_paths_with_spaces_and_quotes_remain_idempotent(self):
        quoted_path = "/tmp/project with spaces/it's/orchestrator.py"
        with mock.patch.object(orchestrator, "SELF", quoted_path):
            self.assertEqual(0, orchestrator.cmd_install_hooks(None))
            self.assertEqual(0, orchestrator.cmd_install_hooks(None))
        output = self.tmux("show-hooks", "-g", "session-created").stdout
        self.assertEqual(1, output.count("orchestrator.py"), output)
        self.assertIn("project with spaces", output)

    def test_health_and_target_matching_support_spaces_and_colons(self):
        self.tmux("rename-window", "-t", "test:0", "window: QA space")
        self.assertTrue(orchestrator.target_exists("test:window: QA space"))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = orchestrator.cmd_health(None)
        self.assertEqual(0, rc, output.getvalue())
        self.assertIn("window: QA space", output.getvalue())


if __name__ == "__main__":
    unittest.main()
