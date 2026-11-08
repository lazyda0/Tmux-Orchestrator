import subprocess
import os
import unittest
from unittest.mock import call, patch

from tmux_utils import TmuxOrchestrator, TmuxSession, TmuxWindow


def completed(stdout=""):
    return subprocess.CompletedProcess(
        args=["tmux"], returncode=0, stdout=stdout, stderr=""
    )


class TmuxParsingTests(unittest.TestCase):
    def setUp(self):
        self.orchestrator = TmuxOrchestrator()

    @patch("tmux_utils.subprocess.run")
    def test_names_with_spaces_and_colons_are_preserved(self, mock_run):
        mock_run.side_effect = [
            completed("team: alpha west\x1f1\n"),
            completed(
                "0\x1feditor: main branch\x1f1\n"
                "2\x1fbuild logs: worker one\x1f0\n"
            ),
        ]

        sessions = self.orchestrator.get_tmux_sessions()

        self.assertEqual(sessions, [
            TmuxSession(
                name="team: alpha west",
                attached=True,
                windows=[
                    TmuxWindow("team: alpha west", 0, "editor: main branch", True),
                    TmuxWindow("team: alpha west", 2, "build logs: worker one", False),
                ],
            )
        ])
        self.assertEqual(mock_run.call_args_list[0], call(
            ["tmux", "list-sessions", "-F", "#{session_name}\x1f#{session_attached}"],
            capture_output=True, text=True, check=True, timeout=5,
        ))
        self.assertEqual(mock_run.call_args_list[1], call(
            [
                "tmux", "list-windows", "-t", "team: alpha west", "-F",
                "#{window_index}\x1f#{window_name}\x1f#{window_active}",
            ],
            capture_output=True, text=True, check=True, timeout=5,
        ))

    @patch("tmux_utils.subprocess.run")
    def test_socket_override_is_applied_to_every_command(self, mock_run):
        mock_run.side_effect = [completed("team\x1f2\n"), completed("")]
        with patch.dict(os.environ, {"TMUX_ORCHESTRATOR_TMUX_SOCKET": "isolated"}):
            sessions = self.orchestrator.get_tmux_sessions()
        self.assertTrue(sessions[0].attached)
        self.assertEqual(
            mock_run.call_args_list[0].args[0],
            ["tmux", "-L", "isolated", "list-sessions", "-F", "#{session_name}\x1f#{session_attached}"],
        )
        self.assertEqual(
            mock_run.call_args_list[1].args[0][:3], ["tmux", "-L", "isolated"]
        )

    @patch("tmux_utils.subprocess.run")
    def test_empty_session_output_is_an_empty_list(self, mock_run):
        mock_run.return_value = completed("")

        self.assertEqual(self.orchestrator.get_tmux_sessions(), [])
        self.assertEqual(mock_run.call_count, 1)

    @patch("tmux_utils.subprocess.run")
    def test_no_server_running_is_an_empty_list(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(
            1, ["tmux", "list-sessions"], stderr="no server running on /tmp/tmux-default"
        )

        self.assertEqual(self.orchestrator.get_tmux_sessions(), [])

    @patch("tmux_utils.subprocess.run")
    def test_other_tmux_failures_are_not_empty_success(self, mock_run):
        error = subprocess.CalledProcessError(
            1, ["tmux", "list-sessions"], stderr="permission denied"
        )
        mock_run.side_effect = error

        with patch("builtins.print"):
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                self.orchestrator.get_tmux_sessions()
        self.assertIs(raised.exception, error)

    @patch("tmux_utils.subprocess.run")
    def test_session_records_require_exact_field_count(self, mock_run):
        mock_run.return_value = completed("team\x1f1\x1funexpected\n")

        with self.assertRaisesRegex(ValueError, "Expected 2 fields"):
            self.orchestrator.get_tmux_sessions()

    @patch("tmux_utils.subprocess.run")
    def test_window_info_preserves_colons_and_requires_four_fields(self, mock_run):
        mock_run.side_effect = [
            completed("editor: main\x1f1\x1f2\x1flayout: even-horizontal\n"),
            completed("window content\n"),
        ]

        info = self.orchestrator.get_window_info("team: alpha", 3)

        self.assertEqual(info, {
            "name": "editor: main",
            "active": True,
            "panes": 2,
            "layout": "layout: even-horizontal",
            "content": "window content\n",
        })
        self.assertEqual(mock_run.call_args_list[0], call(
            [
                "tmux", "display-message", "-t", "team: alpha:3", "-p",
                "#{window_name}\x1f#{window_active}\x1f#{window_panes}\x1f#{window_layout}",
            ],
            capture_output=True, text=True, check=True, timeout=5,
        ))

    @patch("tmux_utils.subprocess.run")
    def test_window_info_rejects_wrong_field_count(self, mock_run):
        mock_run.return_value = completed("editor\x1f1\x1f2\n")

        with self.assertRaisesRegex(ValueError, "Expected 4 fields"):
            self.orchestrator.get_window_info("team", 0)


class CaptureTests(unittest.TestCase):
    @patch("tmux_utils.subprocess.run")
    def test_capture_line_count_is_capped(self, mock_run):
        mock_run.return_value = completed("captured output\n")
        orchestrator = TmuxOrchestrator()

        content = orchestrator.capture_window_content("team: alpha", 4, num_lines=5000)

        self.assertEqual(content, "captured output\n")
        self.assertEqual(mock_run.call_args, call(
            ["tmux", "capture-pane", "-t", "team: alpha:4", "-p", "-S", "-1000"],
            capture_output=True, text=True, check=True, timeout=5,
        ))


class SnapshotTests(unittest.TestCase):
    @patch("tmux_utils.subprocess.run")
    def test_snapshot_contains_correct_names_and_recent_content(self, mock_run):
        content = "".join(f"line {number}\n" for number in range(1, 13))
        mock_run.side_effect = [
            completed("team: alpha west\x1f0\n"),
            completed("7\x1feditor: main branch\x1f1\n"),
            completed("editor: main branch\x1f1\x1f2\x1flayout: main-vertical\n"),
            completed(content),
        ]

        snapshot = TmuxOrchestrator().create_monitoring_snapshot()

        self.assertTrue(snapshot.startswith("Tmux Monitoring Snapshot - "))
        self.assertIn("Session: team: alpha west (DETACHED)\n", snapshot)
        self.assertIn("  Window 7: editor: main branch (ACTIVE)\n", snapshot)
        self.assertIn("    | line 4\n", snapshot)
        self.assertIn("    | line 12\n", snapshot)
        self.assertNotIn("    | line 3\n", snapshot)


if __name__ == "__main__":
    unittest.main()
