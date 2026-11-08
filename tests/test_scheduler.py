import argparse
import contextlib
import io
import os
import sqlite3
import subprocess
import threading
import unittest
from datetime import timedelta
from unittest import mock

import orchestrator
from tests.helpers import TemporaryDatabaseTestCase, database


class SchedulerTests(TemporaryDatabaseTestCase):
    def test_concurrent_fire_has_one_claimant_and_one_send(self):
        schedule_id = self.insert_schedule(worker_token="worker")
        sends = []
        lock = threading.Lock()

        def successful_send(conn, target, body, max_attempts=1):
            with lock:
                sends.append((target, body))
            return orchestrator.DeliveryResult("delivery", "verified", 1)

        with mock.patch.object(orchestrator, "send_message", side_effect=successful_send):
            threads = [
                threading.Thread(
                    target=orchestrator.cmd_fire,
                    args=(argparse.Namespace(id=schedule_id, worker_token="worker"),),
                )
                for _ in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(1, len(sends))
        row = self.schedule_row(schedule_id)
        self.assertEqual("fired", row["status"])
        self.assertEqual(1, row["fire_attempts"])

    def test_wrong_or_duplicate_worker_cannot_claim(self):
        schedule_id = self.insert_schedule(worker_token="current")
        self.assertIsNone(orchestrator.claim_schedule(schedule_id, "stale"))
        first = orchestrator.claim_schedule(schedule_id, "current")
        self.assertIsNotNone(first)
        self.assertIsNone(orchestrator.claim_schedule(schedule_id, None))

        cleared_id = self.insert_schedule(worker_token=None)
        self.assertIsNone(orchestrator.claim_schedule(cleared_id, "expired-worker"))
        self.assertEqual("pending", self.schedule_row(cleared_id)["status"])

    def test_failed_delivery_retries_without_heartbeat_then_fails(self):
        schedule_id = self.insert_schedule()
        failure = orchestrator.DeliveryResult("delivery", "failed", 1, "target missing", retryable=True)
        with mock.patch.object(orchestrator, "send_message", return_value=failure), mock.patch.object(
            orchestrator, "spawn_sleeper", side_effect=[101, 102]
        ) as spawn:
            for expected_attempt in (1, 2, 3):
                with database() as conn:
                    conn.execute(
                        "UPDATE schedules SET due_at = ? WHERE id = ?",
                        (orchestrator.iso(orchestrator.now() - timedelta(seconds=1)), schedule_id),
                    )
                rc = orchestrator.cmd_fire(argparse.Namespace(id=schedule_id, worker_token=None))
                self.assertEqual(1, rc)
                row = self.schedule_row(schedule_id)
                self.assertEqual(expected_attempt, row["fire_attempts"])
                self.assertIn("target missing", row["last_error"])
                if expected_attempt < 3:
                    self.assertEqual("pending", row["status"])
                else:
                    self.assertEqual("failed", row["status"])
            self.assertEqual(2, spawn.call_count)

        with database() as conn:
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM heartbeats").fetchone()[0])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            orchestrator.cmd_list(argparse.Namespace(all=True))
        self.assertIn("target missing", output.getvalue())
        self.assertIn("failed", output.getvalue())

        with mock.patch.object(
            orchestrator, "tmux", return_value=subprocess.CompletedProcess([], 0, "", "")
        ):
            health_output = io.StringIO()
            with contextlib.redirect_stdout(health_output):
                self.assertEqual(1, orchestrator.cmd_health(None))
        self.assertIn("1 failed schedule(s)", health_output.getvalue())

    def test_successful_fire_records_delivery_activity(self):
        schedule_id = self.insert_schedule()
        success = orchestrator.DeliveryResult("delivery", "verified", 1)
        with mock.patch.object(orchestrator, "send_message", return_value=success):
            self.assertEqual(0, orchestrator.cmd_fire(argparse.Namespace(id=schedule_id)))
        self.assertEqual("fired", self.schedule_row(schedule_id)["status"])
        with database() as conn:
            heartbeat = conn.execute("SELECT * FROM heartbeats").fetchone()
            self.assertEqual("schedule-fire", heartbeat["source"])

    def test_cancellation_during_send_is_not_resurrected(self):
        schedule_id = self.insert_schedule()

        def cancel_during_send(conn, target, body, max_attempts=1):
            with database() as other:
                other.execute(
                    "UPDATE schedules SET status = 'cancelled', claim_token = NULL WHERE id = ?",
                    (schedule_id,),
                )
            return orchestrator.DeliveryResult("delivery", "verified", 1)

        with mock.patch.object(orchestrator, "send_message", side_effect=cancel_during_send):
            orchestrator.cmd_fire(argparse.Namespace(id=schedule_id))
        self.assertEqual("cancelled", self.schedule_row(schedule_id)["status"])

    def test_stale_claim_recovers_but_live_claim_is_kept(self):
        stale_id = self.insert_schedule(
            status="firing",
            claim_token="stale",
            claimed_at=orchestrator.iso(
                orchestrator.now() - timedelta(seconds=orchestrator.CLAIM_LEASE_SECONDS + 1)
            ),
        )
        live_id = self.insert_schedule(
            status="firing", claim_token="live", claimed_at=orchestrator.iso(orchestrator.now())
        )
        self.assertEqual(1, orchestrator.recover_stale_claims())
        self.assertEqual("pending", self.schedule_row(stale_id)["status"])
        self.assertEqual("firing", self.schedule_row(live_id)["status"])

    def test_repeated_recovery_reuses_matching_worker(self):
        schedule_id = self.insert_schedule(
            due_at=orchestrator.iso(orchestrator.now() + timedelta(minutes=10))
        )
        with mock.patch.object(
            orchestrator, "worker_matches", side_effect=lambda row: bool(row["pid"])
        ), mock.patch.object(orchestrator, "spawn_sleeper", return_value=4321) as spawn:
            self.assertEqual(0, orchestrator.cmd_recover(None))
            self.assertEqual(0, orchestrator.cmd_recover(None))
        self.assertEqual(1, spawn.call_count)
        row = self.schedule_row(schedule_id)
        self.assertEqual(4321, row["pid"])
        self.assertIsNotNone(row["worker_token"])

    def test_worker_identity_checks_pid_token_and_command(self):
        schedule_id = self.insert_schedule(pid=123, worker_token="expected")
        row = self.schedule_row(schedule_id)
        with mock.patch.object(orchestrator.os, "kill"), mock.patch.object(
            orchestrator, "process_args", return_value=f"python {orchestrator.SELF} fire {schedule_id} --worker-token expected"
        ):
            self.assertTrue(orchestrator.worker_matches(row))
        with mock.patch.object(orchestrator.os, "kill"), mock.patch.object(
            orchestrator, "process_args", return_value="python unrelated.py --worker-token expected"
        ):
            self.assertFalse(orchestrator.worker_matches(row))
        with mock.patch.object(orchestrator.os, "kill", side_effect=ProcessLookupError):
            self.assertFalse(orchestrator.worker_matches(row))

    def test_schedule_defaults_to_current_window_and_replaces_pending(self):
        display = subprocess.CompletedProcess([], 0, "custom-session:7\n", "")
        with mock.patch.dict(os.environ, {"TMUX_PANE": "%9"}), mock.patch.object(
            orchestrator, "tmux", return_value=display
        ), mock.patch.object(orchestrator, "target_exists", return_value=True), mock.patch.object(
            orchestrator, "spawn_sleeper", side_effect=[100, 101]
        ):
            args = argparse.Namespace(minutes=5, note="first", target=None)
            self.assertEqual(0, orchestrator.cmd_schedule(args))
            args.note = "second"
            self.assertEqual(0, orchestrator.cmd_schedule(args))
        with database() as conn:
            rows = conn.execute("SELECT * FROM schedules ORDER BY id").fetchall()
        self.assertEqual(["cancelled", "pending"], [row["status"] for row in rows])
        self.assertEqual({"custom-session:7"}, {row["target"] for row in rows})

    def test_schedule_outside_tmux_requires_target(self):
        args = argparse.Namespace(minutes=5, note="test", target=None)
        self.assertEqual(1, orchestrator.cmd_schedule(args))

    def test_spawn_failure_rolls_back_schedule_replacement(self):
        old_id = self.insert_schedule(target="test:0")
        args = argparse.Namespace(minutes=5, note="replacement", target="test:0")
        with mock.patch.object(orchestrator, "target_exists", return_value=True), mock.patch.object(
            orchestrator, "spawn_sleeper", side_effect=OSError("cannot spawn")
        ):
            with self.assertRaises(OSError):
                orchestrator.cmd_schedule(args)
        self.assertEqual("pending", self.schedule_row(old_id)["status"])
        with database() as conn:
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM schedules").fetchone()[0])

    def test_cancel_requires_exactly_one_selector(self):
        self.assertEqual(2, orchestrator.cmd_cancel(argparse.Namespace(id=None, target=None)))
        self.assertEqual(2, orchestrator.cmd_cancel(argparse.Namespace(id=1, target="test:0")))
        self.assertEqual(2, orchestrator.cmd_cancel(argparse.Namespace(id=None, target="")))

    def test_positive_minutes_and_ids_are_enforced(self):
        for value in ("0", "-1"):
            with self.assertRaises(argparse.ArgumentTypeError):
                orchestrator.positive_int(value)
        self.assertEqual(2, orchestrator.positive_int("2"))

    def test_retry_spawn_failure_becomes_visible_failed(self):
        schedule_id = self.insert_schedule()
        failure = orchestrator.DeliveryResult(
            "delivery", "failed", 1, "tmux unavailable", retryable=True
        )
        with mock.patch.object(orchestrator, "send_message", return_value=failure), mock.patch.object(
            orchestrator, "spawn_sleeper", side_effect=OSError("process limit")
        ):
            self.assertEqual(1, orchestrator.cmd_fire(argparse.Namespace(id=schedule_id)))
        row = self.schedule_row(schedule_id)
        self.assertEqual("failed", row["status"])
        self.assertIn("process limit", row["last_error"])

    def test_unverified_submission_is_not_automatically_resent(self):
        schedule_id = self.insert_schedule()
        uncertain = orchestrator.DeliveryResult("delivery", "unverified", 1, "capture failed")
        with mock.patch.object(orchestrator, "send_message", return_value=uncertain), mock.patch.object(
            orchestrator, "spawn_sleeper"
        ) as spawn:
            self.assertEqual(1, orchestrator.cmd_fire(argparse.Namespace(id=schedule_id)))
        self.assertEqual("failed", self.schedule_row(schedule_id)["status"])
        spawn.assert_not_called()

    def test_expired_final_claim_becomes_failed_without_fourth_round(self):
        schedule_id = self.insert_schedule(
            status="firing",
            claim_token="expired",
            claimed_at=orchestrator.iso(
                orchestrator.now() - timedelta(seconds=orchestrator.CLAIM_LEASE_SECONDS + 1)
            ),
            fire_attempts=orchestrator.MAX_FIRE_ATTEMPTS,
        )
        self.assertEqual(1, orchestrator.recover_stale_claims())
        self.assertEqual("failed", self.schedule_row(schedule_id)["status"])
        self.assertIsNone(orchestrator.claim_schedule(schedule_id))
        self.assertEqual(
            orchestrator.MAX_FIRE_ATTEMPTS, self.schedule_row(schedule_id)["fire_attempts"]
        )

    def test_failed_schedule_can_be_acknowledged(self):
        schedule_id = self.insert_schedule(status="failed", last_error="target missing")
        self.assertEqual(0, orchestrator.cmd_cancel(argparse.Namespace(id=schedule_id, target=None)))
        self.assertEqual("cancelled", self.schedule_row(schedule_id)["status"])
        with mock.patch.object(
            orchestrator, "tmux", return_value=subprocess.CompletedProcess([], 0, "", "")
        ):
            self.assertEqual(0, orchestrator.cmd_health(None))

    def test_cancel_and_replace_stop_only_identified_workers(self):
        cancelled_id = self.insert_schedule(pid=123, worker_token="cancel-me")
        old_row = self.schedule_row(cancelled_id)
        with mock.patch.object(orchestrator, "stop_worker") as stop:
            self.assertEqual(0, orchestrator.cmd_cancel(argparse.Namespace(id=cancelled_id, target=None)))
        stop.assert_called_once()
        self.assertEqual(old_row["worker_token"], stop.call_args.args[0]["worker_token"])

        replaced_id = self.insert_schedule(
            target="test:0", pid=456, worker_token="replace-me",
            due_at=orchestrator.iso(orchestrator.now() + timedelta(minutes=5)),
        )
        args = argparse.Namespace(minutes=5, note="replacement", target="test:0")
        with mock.patch.object(orchestrator, "target_exists", return_value=True), mock.patch.object(
            orchestrator, "spawn_sleeper", return_value=999
        ), mock.patch.object(orchestrator, "stop_worker") as stop:
            self.assertEqual(0, orchestrator.cmd_schedule(args))
        stop.assert_called_once()
        self.assertEqual("replace-me", stop.call_args.args[0]["worker_token"])

    def test_cancel_does_not_kill_in_flight_delivery(self):
        schedule_id = self.insert_schedule(
            status="firing", claim_token="active", pid=123, worker_token="in-flight"
        )
        with mock.patch.object(orchestrator, "stop_worker") as stop:
            self.assertEqual(0, orchestrator.cmd_cancel(argparse.Namespace(id=schedule_id, target=None)))
        stop.assert_not_called()
        self.assertEqual("cancelled", self.schedule_row(schedule_id)["status"])

    def test_newest_window_or_pane_heartbeat_wins(self):
        target = "test:0.0"
        old_activity = int((orchestrator.now() - timedelta(hours=2)).timestamp())
        tmux_row = orchestrator.FIELD_SEPARATOR.join(
            (target, "shell", "zsh", "0", str(old_activity))
        )
        with database() as conn:
            conn.execute(
                "INSERT INTO heartbeats VALUES (?, ?, ?)",
                ("test:0", orchestrator.iso(orchestrator.now() - timedelta(hours=1)), "message"),
            )
            conn.execute(
                "INSERT INTO heartbeats VALUES (?, ?, ?)",
                (target, orchestrator.iso(orchestrator.now()), "message"),
            )
        with mock.patch.object(
            orchestrator, "tmux", return_value=subprocess.CompletedProcess([], 0, tmux_row + "\n", "")
        ):
            self.assertEqual(0, orchestrator.cmd_health(None))

    def test_health_flags_only_expired_firing_claims(self):
        schedule_id = self.insert_schedule(
            status="firing", claim_token="live", claimed_at=orchestrator.iso(orchestrator.now())
        )
        empty_tmux = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(orchestrator, "tmux", return_value=empty_tmux):
            self.assertEqual(0, orchestrator.cmd_health(None))
            with database() as conn:
                conn.execute(
                    "UPDATE schedules SET claimed_at = ? WHERE id = ?",
                    (
                        orchestrator.iso(
                            orchestrator.now()
                            - timedelta(seconds=orchestrator.CLAIM_LEASE_SECONDS + 1)
                        ),
                        schedule_id,
                    ),
                )
            self.assertEqual(1, orchestrator.cmd_health(None))

    def test_legacy_database_migrates_without_losing_pending_schedule(self):
        legacy_path = self.db_file
        conn = sqlite3.connect(legacy_path)
        conn.executescript(
            """
            CREATE TABLE schedules (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              target TEXT NOT NULL,
              note TEXT NOT NULL,
              created_at TEXT NOT NULL,
              due_at TEXT NOT NULL,
              pid INTEGER,
              status TEXT NOT NULL DEFAULT 'pending'
            );
            CREATE TABLE messages (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              target TEXT NOT NULL,
              body TEXT NOT NULL,
              sent_at TEXT NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 1,
              delivered INTEGER
            );
            CREATE TABLE heartbeats (
              target TEXT PRIMARY KEY,
              last_seen TEXT NOT NULL,
              source TEXT
            );
            INSERT INTO schedules(target, note, created_at, due_at)
            VALUES ('legacy:0', 'keep me', '2026-01-01T12:00:00', '2026-01-01T13:00:00');
            """
        )
        conn.commit()
        conn.close()

        migrated = orchestrator.get_db()
        try:
            row = migrated.execute("SELECT * FROM schedules").fetchone()
            self.assertEqual("pending", row["status"])
            self.assertEqual("keep me", row["note"])
            self.assertEqual(0, row["fire_attempts"])
            self.assertIsNotNone(orchestrator.parse_timestamp(row["due_at"]).utcoffset())
            self.assertEqual(1, migrated.execute("PRAGMA user_version").fetchone()[0])
        finally:
            migrated.close()


if __name__ == "__main__":
    unittest.main()
