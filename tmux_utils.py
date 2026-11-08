#!/usr/bin/env python3
"""Read-only tmux inspection utilities for the orchestrator.

Sending messages is handled exclusively by send-claude-message.sh;
this module only captures state (status / snapshot).

Usage:
    python3 tmux_utils.py status     # JSON of all sessions and windows
    python3 tmux_utils.py snapshot   # compact text snapshot for Claude analysis
"""

import subprocess
import json
import os
import sys
from typing import List, Dict, Tuple
from dataclasses import dataclass
from datetime import datetime


_FIELD_SEPARATOR = "\x1f"
_TMUX_TIMEOUT_SECONDS = 5


def _run_tmux(*args: str) -> subprocess.CompletedProcess:
    command = ["tmux"]
    socket = os.environ.get("TMUX_ORCHESTRATOR_TMUX_SOCKET")
    if socket:
        command.extend(["-L", socket])
    command.extend(args)
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=True,
        timeout=_TMUX_TIMEOUT_SECONDS,
    )


def _parse_tmux_fields(line: str, expected_count: int) -> List[str]:
    """Split one tmux response line and reject malformed responses."""
    fields = line.split(_FIELD_SEPARATOR)
    if len(fields) != expected_count:
        raise ValueError(
            f"Expected {expected_count} fields from tmux, got {len(fields)}: {line!r}"
        )
    return fields


def _is_no_sessions_error(error: subprocess.CalledProcessError) -> bool:
    """Identify tmux's normal response when no server or sessions exist."""
    output = f"{error.stdout or ''}\n{error.stderr or ''}".lower()
    return "no server running" in output or "no sessions" in output


@dataclass
class TmuxWindow:
    session_name: str
    window_index: int
    window_name: str
    active: bool

@dataclass
class TmuxSession:
    name: str
    windows: List[TmuxWindow]
    attached: bool

class TmuxOrchestrator:
    def __init__(self):
        self.max_lines_capture = 1000

    def get_tmux_sessions(self) -> List[TmuxSession]:
        """Get all tmux sessions and their windows"""
        try:
            sessions_format = _FIELD_SEPARATOR.join((
                "#{session_name}", "#{session_attached}"
            ))
            sessions_result = _run_tmux("list-sessions", "-F", sessions_format)

            sessions = []
            for line in sessions_result.stdout.splitlines():
                if not line:
                    continue
                session_name, attached = _parse_tmux_fields(line, 2)

                # Get windows for this session
                windows_format = _FIELD_SEPARATOR.join((
                    "#{window_index}", "#{window_name}", "#{window_active}"
                ))
                windows_result = _run_tmux("list-windows", "-t", session_name, "-F", windows_format)

                windows = []
                for window_line in windows_result.stdout.splitlines():
                    if not window_line:
                        continue
                    window_index, window_name, window_active = _parse_tmux_fields(window_line, 3)
                    windows.append(TmuxWindow(
                        session_name=session_name,
                        window_index=int(window_index),
                        window_name=window_name,
                        active=window_active == '1'
                    ))

                sessions.append(TmuxSession(
                    name=session_name,
                    windows=windows,
                    attached=int(attached) > 0
                ))

            return sessions
        except subprocess.CalledProcessError as e:
            if _is_no_sessions_error(e):
                return []
            print(f"Error getting tmux sessions: {e}")
            raise

    def capture_window_content(self, session_name: str, window_index: int, num_lines: int = 50) -> str:
        """Safely capture the last N lines from a tmux window"""
        if num_lines > self.max_lines_capture:
            num_lines = self.max_lines_capture

        try:
            result = _run_tmux(
                "capture-pane", "-t", f"{session_name}:{window_index}", "-p", "-S", f"-{num_lines}"
            )
            return result.stdout
        except subprocess.CalledProcessError as e:
            return f"Error capturing window content: {e}"

    def get_window_info(self, session_name: str, window_index: int) -> Dict:
        """Get detailed information about a specific window"""
        try:
            window_format = _FIELD_SEPARATOR.join((
                "#{window_name}", "#{window_active}", "#{window_panes}", "#{window_layout}"
            ))
            result = _run_tmux(
                "display-message", "-t", f"{session_name}:{window_index}", "-p", window_format
            )

            if result.stdout.strip():
                parts = _parse_tmux_fields(result.stdout.splitlines()[0], 4)
                return {
                    "name": parts[0],
                    "active": parts[1] == '1',
                    "panes": int(parts[2]),
                    "layout": parts[3],
                    "content": self.capture_window_content(session_name, window_index)
                }
        except subprocess.CalledProcessError as e:
            return {"error": f"Could not get window info: {e}"}

    def get_all_windows_status(self) -> Dict:
        """Get status of all windows across all sessions"""
        sessions = self.get_tmux_sessions()
        status = {
            "timestamp": datetime.now().isoformat(),
            "sessions": []
        }

        for session in sessions:
            session_data = {
                "name": session.name,
                "attached": session.attached,
                "windows": []
            }

            for window in session.windows:
                window_info = self.get_window_info(session.name, window.window_index)
                window_data = {
                    "index": window.window_index,
                    "name": window.window_name,
                    "active": window.active,
                    "info": window_info
                }
                session_data["windows"].append(window_data)

            status["sessions"].append(session_data)

        return status

    def find_window_by_name(self, window_name: str) -> List[Tuple[str, int]]:
        """Find windows by name across all sessions"""
        sessions = self.get_tmux_sessions()
        matches = []

        for session in sessions:
            for window in session.windows:
                if window_name.lower() in window.window_name.lower():
                    matches.append((session.name, window.window_index))

        return matches

    def create_monitoring_snapshot(self) -> str:
        """Create a comprehensive snapshot for Claude analysis"""
        status = self.get_all_windows_status()

        # Format for Claude consumption
        snapshot = f"Tmux Monitoring Snapshot - {status['timestamp']}\n"
        snapshot += "=" * 50 + "\n\n"

        for session in status['sessions']:
            snapshot += f"Session: {session['name']} ({'ATTACHED' if session['attached'] else 'DETACHED'})\n"
            snapshot += "-" * 30 + "\n"

            for window in session['windows']:
                snapshot += f"  Window {window['index']}: {window['name']}"
                if window['active']:
                    snapshot += " (ACTIVE)"
                snapshot += "\n"

                if 'content' in window['info']:
                    # Get last 10 lines for overview
                    content_lines = window['info']['content'].split('\n')
                    recent_lines = content_lines[-10:] if len(content_lines) > 10 else content_lines
                    snapshot += "    Recent output:\n"
                    for line in recent_lines:
                        if line.strip():
                            snapshot += f"    | {line}\n"
                snapshot += "\n"

        return snapshot

if __name__ == "__main__":
    orchestrator = TmuxOrchestrator()
    command = sys.argv[1] if len(sys.argv) > 1 else "status"

    if command == "status":
        print(json.dumps(orchestrator.get_all_windows_status(), indent=2))
    elif command == "snapshot":
        print(orchestrator.create_monitoring_snapshot())
    else:
        print(f"Unknown command: {command}")
        print("Usage: tmux_utils.py [status|snapshot]")
        sys.exit(1)
