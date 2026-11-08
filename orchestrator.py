#!/usr/bin/env python3
"""Stateful reliability layer for the Tmux Orchestrator.

Single-file, stdlib-only (sqlite3). The shell scripts send-claude-message.sh
and schedule_with_note.sh are thin shims over this CLI, so the interface
documented in CLAUDE.md keeps working.

State lives in registry/orchestrator.db (SQLite):
  schedules  - persistent check-in schedule journal
  messages   - audit log of messages sent to agents, with delivery verification
  heartbeats - last-seen timestamps per target window

Commands: schedule, fire, list, cancel, recover, send, heartbeat,
          health, doctor, install-hooks, snapshot
"""

import argparse
import os
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REGISTRY_DIR = SCRIPT_DIR / "registry"
DB_PATH = REGISTRY_DIR / "orchestrator.db"
SELF = str(SCRIPT_DIR / "orchestrator.py")

SCHEMA = """
CREATE TABLE IF NOT EXISTS schedules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target TEXT NOT NULL,
  note TEXT NOT NULL,
  created_at TEXT NOT NULL,
  due_at TEXT NOT NULL,
  pid INTEGER,
  status TEXT NOT NULL DEFAULT 'pending'  -- pending|fired|cancelled
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target TEXT NOT NULL,
  body TEXT NOT NULL,
  sent_at TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 1,
  delivered INTEGER              -- 1 verified, 0 failed
);
CREATE TABLE IF NOT EXISTS heartbeats (
  target TEXT PRIMARY KEY,
  last_seen TEXT NOT NULL,
  source TEXT                    -- schedule-fire | message | manual
);
"""

HEARTBEAT_STALE_MINUTES = 45
SHELL_COMMANDS = {"zsh", "bash", "sh", "fish"}


def now() -> datetime:
    return datetime.now()


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def get_db() -> sqlite3.Connection:
    REGISTRY_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# --- tmux helpers -----------------------------------------------------------

def tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def target_exists(target: str) -> bool:
    """Check a target (session:window[.pane], index- or name-based) exists.

    Note: 'tmux display-message -t <bad>' silently falls back to the current
    pane instead of failing, so we validate against the actual lists.
    """
    windows = tmux("list-windows", "-a", "-F",
                   "#{session_name}:#{window_index}\n#{session_name}:#{window_name}")
    panes = tmux("list-panes", "-a", "-F",
                 "#{session_name}:#{window_index}.#{pane_index}\n"
                 "#{session_name}:#{window_name}.#{pane_index}")
    if windows.returncode != 0:
        return False
    valid = set(windows.stdout.split()) | set(panes.stdout.split())
    return target in valid


def kill_pid(pid) -> None:
    if not pid:
        return
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, ValueError):
        pass


def spawn_sleeper(schedule_id: int, seconds: int) -> int:
    """Detach a sleeper process that fires the schedule; returns its PID."""
    proc = subprocess.Popen(
        ["bash", "-c", f"sleep {seconds}; exec {sys.executable} {shlex.quote(SELF)} fire {schedule_id}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc.pid


# --- core operations --------------------------------------------------------

def cmd_schedule(args) -> int:
    if not target_exists(args.target):
        print(f"Error: target window '{args.target}' does not exist.", file=sys.stderr)
        print("Hint: use $(tmux display-message -p '#{session_name}:#{window_index}') for self-scheduling",
              file=sys.stderr)
        return 1

    conn = get_db()
    # Guard against duplicates: cancel previous pending schedules for the same target
    stale = conn.execute(
        "SELECT id, pid FROM schedules WHERE target = ? AND status = 'pending'", (args.target,)
    ).fetchall()
    for row in stale:
        kill_pid(row["pid"])
        conn.execute("UPDATE schedules SET status = 'cancelled', pid = NULL WHERE id = ?", (row["id"],))
    if stale:
        print(f"Cancelled {len(stale)} previous pending schedule(s) for {args.target}")

    due = now() + timedelta(minutes=args.minutes)
    cur = conn.execute(
        "INSERT INTO schedules (target, note, created_at, due_at) VALUES (?, ?, ?, ?)",
        (args.target, args.note, iso(now()), iso(due)),
    )
    schedule_id = cur.lastrowid
    pid = spawn_sleeper(schedule_id, args.minutes * 60)
    conn.execute("UPDATE schedules SET pid = ? WHERE id = ?", (pid, schedule_id))
    conn.commit()

    run_time = due.strftime("%H:%M:%S")
    print(f"Scheduling check in {args.minutes} minutes with note: {args.note}")
    print(f"Scheduled successfully - id={schedule_id}, detached PID: {pid}")
    print(f"SCHEDULED TO RUN AT: {run_time} (in {args.minutes} minutes from {now().strftime('%H:%M:%S')})")
    return 0


def cmd_fire(args) -> int:
    conn = get_db()
    row = conn.execute("SELECT * FROM schedules WHERE id = ?", (args.id,)).fetchone()
    if not row or row["status"] != "pending":
        return 0  # already fired or cancelled - nothing to do

    message = (f"Time for orchestrator check! {row['note']} "
               f"(system state: python3 {SELF} snapshot)")
    delivered = send_message(conn, row["target"], message)
    conn.execute("UPDATE schedules SET status = 'fired', pid = NULL WHERE id = ?", (args.id,))
    upsert_heartbeat(conn, row["target"], "schedule-fire")
    conn.commit()
    if not delivered:
        print(f"Warning: schedule {args.id} fired but delivery to {row['target']} was not verified",
              file=sys.stderr)
    return 0


def cmd_list(args) -> int:
    conn = get_db()
    if args.all:
        rows = conn.execute("SELECT * FROM schedules ORDER BY id DESC LIMIT 50").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM schedules WHERE status = 'pending' ORDER BY due_at"
        ).fetchall()
    if not rows:
        print("No pending schedules." if not args.all else "No schedules on record.")
        return 0
    print(f"{'ID':>4}  {'STATUS':<9}  {'DUE':<19}  {'TARGET':<18}  NOTE")
    for r in rows:
        print(f"{r['id']:>4}  {r['status']:<9}  {r['due_at']:<19}  {r['target']:<18}  {r['note']}")
    return 0


def cmd_cancel(args) -> int:
    conn = get_db()
    if args.target:
        rows = conn.execute(
            "SELECT * FROM schedules WHERE target = ? AND status = 'pending'", (args.target,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM schedules WHERE id = ? AND status = 'pending'", (args.id,)
        ).fetchall()
    if not rows:
        print("Nothing to cancel.")
        return 0
    for r in rows:
        kill_pid(r["pid"])
        conn.execute("UPDATE schedules SET status = 'cancelled', pid = NULL WHERE id = ?", (r["id"],))
        print(f"Cancelled schedule {r['id']} ({r['target']}: {r['note']})")
    conn.commit()
    return 0


def cmd_recover(_args) -> int:
    """Re-arm pending schedules. Idempotent; safe to call often.

    Overdue schedules fire immediately, future ones get a fresh sleeper
    for the remaining time. This is the 'inside tmux only' persistence:
    called from tmux hooks and from the orchestrator startup protocol.
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM schedules WHERE status = 'pending' ORDER BY due_at"
    ).fetchall()
    fired, rearmed = 0, 0
    for r in rows:
        remaining = (datetime.fromisoformat(r["due_at"]) - now()).total_seconds()
        if remaining <= 0:
            fire_args = argparse.Namespace(id=r["id"])
            cmd_fire(fire_args)
            fired += 1
            print(f"Recovered overdue schedule {r['id']} -> fired ({r['target']}: {r['note']})")
        else:
            pid = spawn_sleeper(r["id"], int(remaining) + 1)
            conn.execute("UPDATE schedules SET pid = ? WHERE id = ?", (pid, r["id"]))
            rearmed += 1
    conn.commit()
    print(f"Recover: {fired} overdue fired, {rearmed} re-armed, {len(rows) - fired - rearmed} untouched.")
    return 0


# --- messaging ---------------------------------------------------------------

def upsert_heartbeat(conn, target: str, source: str) -> None:
    conn.execute(
        """INSERT INTO heartbeats (target, last_seen, source) VALUES (?, ?, ?)
           ON CONFLICT(target) DO UPDATE SET last_seen = excluded.last_seen,
                                             source = excluded.source""",
        (target, iso(now()), source),
    )


def normalize(text: str) -> str:
    return " ".join(text.split())


def verify_delivery(target: str, body: str) -> bool:
    """Check the tail of the pane for the message text.

    capture-pane -J joins wrapped lines, so a normalized substring match
    on the first ~40 chars survives pane-width wrapping.
    """
    result = tmux("capture-pane", "-t", target, "-p", "-J", "-S", "-50")
    if result.returncode != 0:
        return False
    needle = normalize(body)[:40]
    return needle in normalize(result.stdout)


def send_message(conn, target: str, body: str, max_attempts: int = 2) -> bool:
    if not target_exists(target):
        conn.execute(
            "INSERT INTO messages (target, body, sent_at, attempts, delivered) VALUES (?, ?, ?, 0, 0)",
            (target, body, iso(now())),
        )
        conn.commit()
        return False

    delivered = False
    attempts = 0
    for attempts in range(1, max_attempts + 1):
        # -l: type the text literally so words like "Enter"/"C-c" are not
        # interpreted as tmux key names
        tmux("send-keys", "-t", target, "-l", body)
        time.sleep(0.5)
        tmux("send-keys", "-t", target, "Enter")
        time.sleep(1)
        delivered = verify_delivery(target, body)
        if delivered:
            break
        print(f"Delivery to {target} not verified (attempt {attempts}/{max_attempts}), retrying...",
              file=sys.stderr)

    conn.execute(
        "INSERT INTO messages (target, body, sent_at, attempts, delivered) VALUES (?, ?, ?, ?, ?)",
        (target, body, iso(now()), attempts, 1 if delivered else 0),
    )
    if delivered:
        upsert_heartbeat(conn, target, "message")
    conn.commit()
    return delivered


def cmd_send(args) -> int:
    conn = get_db()
    delivered = send_message(conn, args.target, args.message)
    if delivered:
        print(f"Message sent to {args.target}: {args.message}")
        return 0
    print(f"Error: message to {args.target} could not be verified as delivered.", file=sys.stderr)
    return 1


def cmd_heartbeat(args) -> int:
    conn = get_db()
    upsert_heartbeat(conn, args.target, args.source)
    conn.commit()
    print(f"Heartbeat recorded for {args.target} ({args.source})")
    return 0


# --- monitoring ---------------------------------------------------------------

def cmd_health(_args) -> int:
    conn = get_db()
    heartbeats = {r["target"]: r for r in conn.execute("SELECT * FROM heartbeats")}
    stale_after = timedelta(minutes=HEARTBEAT_STALE_MINUTES)

    result = tmux("list-panes", "-a", "-F",
                  "#{session_name}:#{window_index}.#{pane_index} #{window_name} "
                  "#{pane_current_command} #{pane_dead} #{window_activity}")
    if result.returncode != 0:
        print("Error: could not list tmux panes (is the tmux server running?)", file=sys.stderr)
        return 1

    print(f"Agent health report - {iso(now())} (stale = no activity for >{HEARTBEAT_STALE_MINUTES} min)")
    print("=" * 70)
    problems = 0
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        target, name, command, dead, activity = line.split(" ", 4)
        status = []
        if dead == "1":
            status.append("DEAD (pane_dead)")
        is_agent_window = name.lower().startswith("claude") or "agent" in name.lower()
        if is_agent_window and command in SHELL_COMMANDS:
            status.append(f"WARN agent window but running '{command}' (claude not started?)")
        idle = now() - datetime.fromtimestamp(int(activity))
        hb = heartbeats.get(target.rsplit(".", 1)[0]) or heartbeats.get(target)
        if idle > stale_after and (not hb or now() - datetime.fromisoformat(hb["last_seen"]) > stale_after):
            status.append(f"STALE (idle {int(idle.total_seconds() // 60)} min)")

        if status:
            problems += 1
            print(f"!! {target} [{name}] cmd={command}")
            for s in status:
                print(f"     {s}")
        else:
            print(f"ok {target} [{name}] cmd={command}")

    overdue = conn.execute(
        "SELECT COUNT(*) AS c FROM schedules WHERE status = 'pending' AND due_at < ?",
        (iso(now()),),
    ).fetchone()["c"]
    print("=" * 70)
    print(f"{problems} problem window(s), {overdue} overdue schedule(s)."
          + (" Run 'orchestrator.py recover'." if overdue else ""))
    return 0 if problems == 0 and overdue == 0 else 1


# --- diagnostics & hooks -------------------------------------------------------

def cmd_doctor(_args) -> int:
    checks = []

    py_ok = sys.version_info >= (3, 9)
    checks.append((py_ok, f"python3 >= 3.9 (found {sys.version.split()[0]})"))

    tmux_path = shutil.which("tmux")
    if tmux_path:
        version = tmux("-V").stdout.strip()
        checks.append((True, f"tmux in PATH ({version})"))
    else:
        checks.append((False, "tmux not found in PATH"))

    for script in ("send-claude-message.sh", "schedule_with_note.sh"):
        path = SCRIPT_DIR / script
        ok = path.exists() and os.access(path, os.X_OK)
        checks.append((ok, f"{script} exists and is executable"))

    try:
        REGISTRY_DIR.mkdir(exist_ok=True)
        probe = REGISTRY_DIR / ".write-probe"
        probe.touch()
        probe.unlink()
        checks.append((True, f"registry/ writable ({REGISTRY_DIR})"))
    except OSError as e:
        checks.append((False, f"registry/ not writable: {e}"))

    try:
        conn = get_db()
        conn.execute("SELECT COUNT(*) FROM schedules")
        checks.append((True, f"SQLite schema OK ({DB_PATH})"))
    except sqlite3.Error as e:
        checks.append((False, f"SQLite error: {e}"))

    hooks_ok = None
    if tmux_path:
        hooks = tmux("show-hooks", "-g").stdout
        hooks_ok = "orchestrator.py recover" in hooks

    all_ok = True
    for ok, label in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
        all_ok = all_ok and ok
    # Hooks are tmux-server state, so they are legitimately missing after a
    # server restart - report as info, not failure (remedy: install-hooks).
    if hooks_ok is not None:
        print(f"{'INFO' if not hooks_ok else 'PASS'}  tmux recover hooks "
              + ("installed" if hooks_ok else "not installed (expected after server restart; run: orchestrator.py install-hooks)"))
    print("Doctor: all checks passed." if all_ok else "Doctor: some checks FAILED.")
    return 0 if all_ok else 1


def cmd_install_hooks(_args) -> int:
    """Register tmux hooks so recover runs on new sessions / client attach.

    Hooks live in the tmux server and are lost when the server stops;
    the orchestrator startup protocol (CLAUDE.md) re-runs install-hooks.
    """
    recover_cmd = f"run-shell '{sys.executable} {SELF} recover'"
    for hook in ("session-created", "client-attached"):
        result = tmux("set-hook", "-g", hook, recover_cmd)
        if result.returncode != 0:
            print(f"Error setting hook {hook}: {result.stderr.strip()}", file=sys.stderr)
            return 1
        print(f"Hook installed: {hook} -> orchestrator.py recover")
    print("Note: hooks are tmux-server state; re-run install-hooks after a server restart.")
    return 0


def cmd_snapshot(_args) -> int:
    sys.path.insert(0, str(SCRIPT_DIR))
    from tmux_utils import TmuxOrchestrator
    print(TmuxOrchestrator().create_monitoring_snapshot())
    return 0


# --- CLI ------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Tmux Orchestrator reliability layer")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("schedule", help="schedule a check-in")
    p.add_argument("minutes", type=int)
    p.add_argument("note")
    p.add_argument("target", nargs="?", default="tmux-orc:0")
    p.set_defaults(func=cmd_schedule)

    p = sub.add_parser("fire", help="fire a pending schedule (called by sleeper)")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_fire)

    p = sub.add_parser("list", help="list schedules")
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("cancel", help="cancel schedule(s)")
    p.add_argument("id", type=int, nargs="?")
    p.add_argument("--target")
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("recover", help="re-arm pending schedules after interruption")
    p.set_defaults(func=cmd_recover)

    p = sub.add_parser("send", help="send a message to a window with delivery verification")
    p.add_argument("target")
    p.add_argument("message")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("heartbeat", help="record a heartbeat for a target")
    p.add_argument("target")
    p.add_argument("source", nargs="?", default="manual")
    p.set_defaults(func=cmd_heartbeat)

    p = sub.add_parser("health", help="agent health report")
    p.set_defaults(func=cmd_health)

    p = sub.add_parser("doctor", help="environment self-check")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("install-hooks", help="register tmux hooks for recover")
    p.set_defaults(func=cmd_install_hooks)

    p = sub.add_parser("snapshot", help="tmux monitoring snapshot (via tmux_utils)")
    p.set_defaults(func=cmd_snapshot)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
