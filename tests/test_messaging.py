"""Tests for orchestrator messaging and delivery verification."""

import argparse
import io
import itertools
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import orchestrator


TARGET = "ai chat:project room.2"
BODY = "Review the current implementation"
DELIVERY_ID = "test-delivery"
MARKER = f"[orchestrator-delivery: {DELIVERY_ID}-1]"


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["tmux"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class MessagingTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        db_path = Path(self.temp_dir.name) / "orchestrator.db"
        env = mock.patch.dict(
            os.environ,
            {
                "TMUX_ORCHESTRATOR_DB": str(db_path),
                "TMUX_ORCHESTRATOR_TMUX_SOCKET": "messaging-tests",
            },
            clear=True,
        )
        env.start()
        self.addCleanup(env.stop)

        self.conn = orchestrator.get_db()
        self.addCleanup(self.conn.close)

        self.tmux = self.patch("tmux")
        self.target_exists = self.patch("target_exists", return_value=True)
        self.sleep = self.patch("time.sleep")
        # A failed verification sees one capture, then advances beyond its deadline.
        self.monotonic = self.patch(
            "time.monotonic",
            side_effect=itertools.count(0.0, orchestrator.VERIFY_TIMEOUT_SECONDS + 1.0),
        )
        self.patch(
            "uuid.uuid4", return_value=SimpleNamespace(hex=DELIVERY_ID)
        )

    def patch(self, name, **kwargs):
        patched = mock.patch(f"orchestrator.{name}", **kwargs)
        started = patched.start()
        self.addCleanup(patched.stop)
        return started

    def message_row(self):
        rows = self.conn.execute("SELECT * FROM messages").fetchall()
        self.assertEqual(len(rows), 1)
        return rows[0]

    def assert_no_heartbeat(self):
        count = self.conn.execute("SELECT COUNT(*) FROM heartbeats").fetchone()[0]
        self.assertEqual(count, 0)

    def test_stale_identical_body_without_current_marker_is_unverified(self):
        stale_pane = (
            f"{BODY}\n[orchestrator-delivery: previous-delivery-1]\n"
            "old prompt output"
        )
        self.tmux.side_effect = [
            completed(),
            completed(),
            completed(stdout=stale_pane),
        ]

        result = orchestrator.send_message(self.conn, TARGET, BODY, max_attempts=2)

        self.assertEqual(result.status, "unverified")
        self.assertEqual(result.attempts, 1)
        self.assertFalse(result.retryable)
        self.assertEqual(self.tmux.call_count, 3)
        self.assertEqual(result.error, "verification marker was not observed")
        self.assertNotIn(MARKER, stale_pane)
        row = self.message_row()
        self.assertEqual(row["status"], "unverified")
        self.assertEqual(row["delivered"], 0)
        self.assert_no_heartbeat()

    def test_successful_literal_enter_and_marker_is_verified_with_heartbeat(self):
        payload = f"{BODY} {MARKER}"
        self.tmux.side_effect = [
            completed(),
            completed(),
            completed(stdout=f"prompt\n{payload}\n"),
        ]

        result = orchestrator.send_message(self.conn, TARGET, BODY)

        self.assertTrue(result.verified)
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.error, "")
        self.assertEqual(
            self.tmux.call_args_list,
            [
                mock.call("send-keys", "-t", TARGET, "-l", payload),
                mock.call("send-keys", "-t", TARGET, "Enter"),
                mock.call("capture-pane", "-t", TARGET, "-p", "-J", "-S", "-200"),
            ],
        )
        row = self.message_row()
        self.assertEqual(row["status"], "verified")
        self.assertEqual(row["delivered"], 1)
        self.assertEqual(row["attempts"], 1)
        heartbeat = self.conn.execute(
            "SELECT target, source FROM heartbeats"
        ).fetchone()
        self.assertEqual(heartbeat["target"], TARGET)
        self.assertEqual(heartbeat["source"], "message")

    def test_missing_target_fails_with_zero_attempts_and_no_heartbeat(self):
        self.target_exists.return_value = False

        result = orchestrator.send_message(self.conn, TARGET, BODY)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.attempts, 0)
        self.assertEqual(result.error, "target does not exist")
        self.tmux.assert_not_called()
        row = self.message_row()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["delivered"], 0)
        self.assertEqual(row["error"], "target does not exist")
        self.assert_no_heartbeat()

    def test_literal_send_keys_nonzero_is_failed(self):
        self.tmux.return_value = completed(
            returncode=1, stderr="literal send failed"
        )

        result = orchestrator.send_message(self.conn, TARGET, BODY, max_attempts=1)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.error, "literal send failed")
        self.assertEqual(
            self.tmux.call_args_list,
            [mock.call("send-keys", "-t", TARGET, "-l", f"{BODY} {MARKER}")],
        )
        row = self.message_row()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["delivered"], 0)
        self.assert_no_heartbeat()

    def test_enter_nonzero_is_failed_without_retry(self):
        self.tmux.side_effect = [
            completed(),
            completed(returncode=1, stderr="Enter send failed"),
        ]

        result = orchestrator.send_message(self.conn, TARGET, BODY, max_attempts=2)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.error, "Enter send failed")
        self.assertEqual(
            self.tmux.call_args_list,
            [
                mock.call("send-keys", "-t", TARGET, "-l", f"{BODY} {MARKER}"),
                mock.call("send-keys", "-t", TARGET, "Enter"),
            ],
        )
        row = self.message_row()
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["status"], "failed")
        self.assert_no_heartbeat()

    def test_capture_failure_is_unverified_without_heartbeat(self):
        self.tmux.side_effect = [
            completed(),
            completed(),
            completed(returncode=1, stderr="capture failed"),
        ]

        result = orchestrator.send_message(self.conn, TARGET, BODY, max_attempts=2)

        self.assertEqual(result.status, "unverified")
        self.assertEqual(result.attempts, 1)
        self.assertFalse(result.retryable)
        self.assertEqual(self.tmux.call_count, 3)
        self.assertEqual(result.error, "capture failed")
        row = self.message_row()
        self.assertEqual(row["status"], "unverified")
        self.assertEqual(row["delivered"], 0)
        self.assertEqual(row["error"], "capture failed")
        self.assert_no_heartbeat()

    def test_literal_enter_c_c_and_unicode_payload_is_preserved(self):
        body = "Type Enter, then C-c; Unicode: café 日本語 🚀"
        payload = f"{body} {MARKER}"
        self.tmux.side_effect = [
            completed(),
            completed(),
            completed(stdout=payload),
        ]

        result = orchestrator.send_message(self.conn, TARGET, body, max_attempts=1)

        self.assertTrue(result.verified)
        self.assertEqual(
            self.tmux.call_args_list[:2],
            [
                mock.call("send-keys", "-t", TARGET, "-l", payload),
                mock.call("send-keys", "-t", TARGET, "Enter"),
            ],
        )
        self.assertNotIn(
            mock.call("send-keys", "-t", TARGET, "C-c"),
            self.tmux.call_args_list,
        )
        row = self.message_row()
        self.assertEqual(row["body"], body)
        self.assertEqual(row["delivery_id"], DELIVERY_ID)
        self.assertIn(MARKER, self.tmux.call_args_list[0].args[-1])

    def test_each_pre_input_retry_uses_a_unique_marker(self):
        second_marker = f"[orchestrator-delivery: {DELIVERY_ID}-2]"
        self.tmux.side_effect = [
            completed(returncode=1, stderr="tmux unavailable"),
            completed(),
            completed(),
            completed(stdout=f"{BODY} {second_marker}"),
        ]

        result = orchestrator.send_message(self.conn, TARGET, BODY, max_attempts=2)

        self.assertTrue(result.verified)
        self.assertEqual(result.attempts, 2)
        first_payload = self.tmux.call_args_list[0].args[-1]
        second_payload = self.tmux.call_args_list[1].args[-1]
        self.assertEqual(first_payload, f"{BODY} {MARKER}")
        self.assertEqual(second_payload, f"{BODY} {second_marker}")
        self.assertNotEqual(first_payload, second_payload)
        self.assertNotIn("\n", first_payload)
        row = self.message_row()
        self.assertEqual(row["attempts"], 2)
        self.assertEqual(row["status"], "verified")

    def test_cmd_send_returns_zero_for_verified_delivery(self):
        args = argparse.Namespace(target=TARGET, message=BODY)
        self.tmux.side_effect = [
            completed(),
            completed(),
            completed(stdout=f"{BODY} {MARKER}"),
        ]
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            return_code = orchestrator.cmd_send(args)

        self.assertEqual(return_code, 0)
        self.assertIn("Message input observed", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(self.message_row()["status"], "verified")

    def test_cmd_send_returns_one_for_failed_delivery(self):
        args = argparse.Namespace(target=TARGET, message=BODY)
        self.target_exists.return_value = False
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            return_code = orchestrator.cmd_send(args)

        self.assertEqual(return_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("failed", stderr.getvalue())
        self.assertIn("target does not exist", stderr.getvalue())
        self.assertEqual(self.message_row()["status"], "failed")
        self.assert_no_heartbeat()


class TargetExistsTestCase(unittest.TestCase):
    @mock.patch.object(orchestrator, "tmux")
    def test_target_matching_uses_complete_lines_including_spaces(self, tmux):
        def list_tmux_targets(*args):
            if args[0] == "list-windows":
                return completed(
                    stdout="ai chat:1\nai chat:project room\n"
                )
            if args[0] == "list-panes":
                return completed(
                    stdout="ai chat:1.0\nai chat:project room.2\n"
                )
            self.fail(f"unexpected tmux command: {args}")

        tmux.side_effect = list_tmux_targets
        cases = [
            ("ai chat:project room", True),
            ("ai chat:project room.2", True),
            ("ai chat:project", False),
            ("ai chat:project room.2.extra", False),
        ]

        for target, expected in cases:
            with self.subTest(target=target):
                self.assertIs(orchestrator.target_exists(target), expected)


if __name__ == "__main__":
    unittest.main()
