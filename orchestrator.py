#!/usr/bin/env python3
"""Stateful reliability layer for the Tmux Orchestrator.

Single-file, stdlib-only (sqlite3). The shell scripts send-claude-message.sh
and schedule_with_note.sh are thin shims over this CLI, so the interface
documented in CLAUDE.md keeps working.

State lives in registry/orchestrator.db (SQLite) unless TMUX_ORCHESTRATOR_DB
is set. Tmux commands can be isolated with TMUX_ORCHESTRATOR_TMUX_SOCKET.
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
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_REGISTRY_DIR = SCRIPT_DIR / "registry"
DEFAULT_DB_PATH = DEFAULT_REGISTRY_DIR / "orchestrator.db"
SELF = str(SCRIPT_DIR / "orchestrator.py")

SCHEMA = """
CREATE TABLE IF NOT EXISTS schedules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target TEXT NOT NULL,
  note TEXT NOT NULL,
  created_at TEXT NOT NULL,
  due_at TEXT NOT NULL,
  pid INTEGER,
  status TEXT NOT NULL DEFAULT 'pending',
  worker_token TEXT,
  claim_token TEXT,
  claimed_at TEXT,
  fire_attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target TEXT NOT NULL,
  body TEXT NOT NULL,
  sent_at TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 1,
  delivered INTEGER,
  delivery_id TEXT,
  status TEXT,
  error TEXT
);
CREATE TABLE IF NOT EXISTS heartbeats (
  target TEXT PRIMARY KEY,
  last_seen TEXT NOT NULL,
  source TEXT
);
"""

SCHEDULE_COLUMNS = {
    "worker_token": "TEXT",
    "claim_token": "TEXT",
    "claimed_at": "TEXT",
    "fire_attempts": "INTEGER NOT NULL DEFAULT 0",
    "last_error": "TEXT",
}
MESSAGE_COLUMNS = {
    "delivery_id": "TEXT",
    "status": "TEXT",
    "error": "TEXT",
}
SCHEMA_VERSION = 1
HEARTBEAT_STALE_MINUTES = 45
SHELL_COMMANDS = {"zsh", "bash", "sh", "fish"}
TMUX_TIMEOUT_SECONDS = 5
VERIFY_TIMEOUT_SECONDS = 2.0
VERIFY_INTERVAL_SECONDS = 0.1
CLAIM_LEASE_SECONDS = 300
MAX_FIRE_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = {1: 30, 2: 120}
FIELD_SEPARATOR = "\x1f"
DETACHED_WORKERS = {}


def reap_workers(wait: bool = False) -> None:
    """Retain detached Popen handles and reap them without waiting in production."""
    for pid, process in list(DETACHED_WORKERS.items()):
        if wait:
            process.wait()
        else:
            process.poll()
        if process.returncode is not None:
            DETACHED_WORKERS.pop(pid, None)


@dataclass
class DeliveryResult:
    delivery_id: str
    status: str
    attempts: int
    error: str = ""
    retryable: bool = False

    @property
    def verified(self) -> bool:
        return self.status == "verified"


def db_path() -> Path:
    return Path(os.environ.get("TMUX_ORCHESTRATOR_DB", str(DEFAULT_DB_PATH))).expanduser().resolve()


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(timezone.utc)


def _legacy_utc(value: str) -> str:
    """Interpret pre-migration naive timestamps as local time exactly once."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        return iso(parsed)
    return iso(parsed.astimezone())


def run_migrations(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise sqlite3.DatabaseError(
                f"Database schema {version} is newer than supported schema {SCHEMA_VERSION}"
            )
        if version == 0:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            for table, columns in (("schedules", SCHEDULE_COLUMNS), ("messages", MESSAGE_COLUMNS)):
                existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                for name, definition in columns.items():
                    if name not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

            for table, timestamp_columns, key in (
                ("schedules", ("created_at", "due_at"), "id"),
                ("messages", ("sent_at",), "id"),
                ("heartbeats", ("last_seen",), "target"),
            ):
                rows = conn.execute(f"SELECT {key}, * FROM {table}").fetchall()
                for row in rows:
                    for column in timestamp_columns:
                        value = row[column]
                        if value and datetime.fromisoformat(value).tzinfo is None:
                            conn.execute(
                                f"UPDATE {table} SET {column} = ? WHERE {key} = ?",
                                (_legacy_utc(value), row[key]),
                            )
            conn.execute("PRAGMA user_version = 1")

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_schedules_status_due ON schedules(status, due_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_schedules_target_status ON schedules(target, status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_status_sent ON messages(status, sent_at)"
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def get_db() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    run_migrations(conn)
    return conn


# --- tmux helpers -----------------------------------------------------------


def tmux(*args: str) -> subprocess.CompletedProcess:
    command = ["tmux"]
    socket = os.environ.get("TMUX_ORCHESTRATOR_TMUX_SOCKET")
    if socket:
        command.extend(["-L", socket])
    command.extend(args)
    try:
        return subprocess.run(
            command, capture_output=True, text=True, timeout=TMUX_TIMEOUT_SECONDS
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return subprocess.CompletedProcess(command, 1, "", str(exc))


def target_exists(target: str) -> bool:
    """Check a target without tmux's fallback to the current pane."""
    windows = tmux(
        "list-windows",
        "-a",
        "-F",
        "#{session_name}:#{window_index}\n#{session_name}:#{window_name}",
    )
    panes = tmux(
        "list-panes",
        "-a",
        "-F",
        "#{session_name}:#{window_index}.#{pane_index}\n"
        "#{session_name}:#{window_name}.#{pane_index}",
    )
    if windows.returncode != 0 or panes.returncode != 0:
        return False
    valid = {line for output in (windows.stdout, panes.stdout) for line in output.splitlines() if line}
    return target in valid


def resolve_target(value: Optional[str]) -> Optional[str]:
    if value is not None:
        target = value.strip()
        return target or None
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return None
    result = tmux("display-message", "-p", "-t", pane, "#{session_name}:#{window_index}")
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def process_args(pid: int) -> str:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="],
            capture_output=True,
            text=True,
            timeout=TMUX_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def worker_matches(row: sqlite3.Row) -> bool:
    pid, token = row["pid"], row["worker_token"]
    if not pid or not token:
        return False
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, PermissionError, ValueError):
        return False
    args = process_args(int(pid))
    return token in args and SELF in args and " fire " in f" {args} "


def stop_worker(row: sqlite3.Row) -> None:
    """Stop only a positively identified tokenized worker; never legacy PIDs."""
    if not worker_matches(row):
        return
    try:
        os.kill(int(row["pid"]), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, ValueError):
        pass


def spawn_sleeper(schedule_id: int, seconds: int, worker_token: str) -> int:
    """Detach a token-identified sleeper and retain its diagnostics."""
    reap_workers()
    seconds = max(0, int(seconds))
    log_dir = db_path().parent / "workers"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"schedule-{schedule_id}-{worker_token}.log"
    with log_path.open("a", encoding="utf-8") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                SELF,
                "fire",
                str(schedule_id),
                "--worker-token",
                worker_token,
                "--delay",
                str(seconds),
            ],
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    DETACHED_WORKERS[proc.pid] = proc
    return proc.pid


# --- core operations --------------------------------------------------------


def cmd_schedule(args) -> int:
    target = resolve_target(args.target)
    if not target:
        print(
            "Error: target is required outside tmux (or when the current window cannot be resolved).",
            file=sys.stderr,
        )
        print("Example: ./schedule_with_note.sh 15 \"Review progress\" \"ai-chat:2\"", file=sys.stderr)
        return 1
    if not args.note.strip():
        print("Error: schedule note must not be empty.", file=sys.stderr)
        return 1
    if not target_exists(target):
        print(f"Error: target window '{target}' does not exist.", file=sys.stderr)
        print("Hint: use $(tmux display-message -p '#{session_name}:#{window_index}') for self-scheduling", file=sys.stderr)
        return 1

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        stale = conn.execute(
            "SELECT * FROM schedules WHERE target = ? AND status = 'pending'", (target,)
        ).fetchall()
        for row in stale:
            conn.execute(
                "UPDATE schedules SET status = 'cancelled', pid = NULL, "
                "worker_token = NULL, claim_token = NULL, claimed_at = NULL WHERE id = ?",
                (row["id"],),
            )
        if stale:
            print(f"Cancelled {len(stale)} previous pending schedule(s) for {target}")

        created = now()
        due = created + timedelta(minutes=args.minutes)
        worker_token = uuid.uuid4().hex
        cur = conn.execute(
            "INSERT INTO schedules "
            "(target, note, created_at, due_at, worker_token) VALUES (?, ?, ?, ?, ?)",
            (target, args.note, iso(created), iso(due), worker_token),
        )
        schedule_id = cur.lastrowid
        pid = spawn_sleeper(schedule_id, args.minutes * 60, worker_token)
        conn.execute("UPDATE schedules SET pid = ? WHERE id = ?", (pid, schedule_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    for row in stale:
        stop_worker(row)

    print(f"Scheduling check in {args.minutes} minutes with note: {args.note}")
    print(f"Scheduled successfully - id={schedule_id}, detached PID: {pid}")
    print(f"SCHEDULED TO RUN AT: {iso(due)} (in {args.minutes} minutes)")
    return 0


def claim_schedule(schedule_id: int, worker_token: Optional[str] = None) -> Optional[dict]:
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
        if not row or row["status"] != "pending" or parse_timestamp(row["due_at"]) > now():
            conn.rollback()
            return None
        if worker_token is not None and row["worker_token"] != worker_token:
            conn.rollback()
            return None

        claim_token = uuid.uuid4().hex
        claimed_at = now()
        cur = conn.execute(
            "UPDATE schedules SET status = 'firing', claim_token = ?, claimed_at = ?, "
            "fire_attempts = fire_attempts + 1, last_error = NULL "
            "WHERE id = ? AND status = 'pending'",
            (claim_token, iso(claimed_at), schedule_id),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
        claimed = dict(row)
        claimed.update(
            status="firing",
            claim_token=claim_token,
            claimed_at=iso(claimed_at),
            fire_attempts=row["fire_attempts"] + 1,
        )
        return claimed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def finalize_schedule(
    schedule_id: int, claim_token: str, verified: bool, error: str = "", retryable: bool = False
) -> str:
    """Finalize only the active claim; a concurrent cancellation wins."""
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM schedules WHERE id = ? AND status = 'firing' AND claim_token = ?",
            (schedule_id, claim_token),
        ).fetchone()
        if not row:
            current = conn.execute("SELECT status FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
            conn.rollback()
            return current["status"] if current else "missing"

        if verified:
            status = "fired"
            conn.execute(
                "UPDATE schedules SET status = 'fired', pid = NULL, worker_token = NULL, "
                "claim_token = NULL, claimed_at = NULL, last_error = NULL WHERE id = ?",
                (schedule_id,),
            )
        else:
            attempts = row["fire_attempts"]
            if attempts >= MAX_FIRE_ATTEMPTS or not retryable:
                status = "failed"
                conn.execute(
                    "UPDATE schedules SET status = 'failed', pid = NULL, worker_token = NULL, "
                    "claim_token = NULL, claimed_at = NULL, last_error = ? WHERE id = ?",
                    (error or "Delivery failed", schedule_id),
                )
            else:
                status = "pending"
                due = now() + timedelta(seconds=RETRY_BACKOFF_SECONDS[attempts])
                worker_token = uuid.uuid4().hex
                conn.execute(
                    "UPDATE schedules SET status = 'pending', due_at = ?, worker_token = ?, "
                    "pid = NULL, claim_token = NULL, claimed_at = NULL, last_error = ? WHERE id = ?",
                    (iso(due), worker_token, error or "Delivery failed", schedule_id),
                )
                try:
                    pid = spawn_sleeper(schedule_id, RETRY_BACKOFF_SECONDS[attempts], worker_token)
                except OSError as exc:
                    status = "failed"
                    conn.execute(
                        "UPDATE schedules SET status = 'failed', worker_token = NULL, last_error = ? WHERE id = ?",
                        (f"{error}; could not spawn retry worker: {exc}", schedule_id),
                    )
                else:
                    conn.execute("UPDATE schedules SET pid = ? WHERE id = ?", (pid, schedule_id))
        conn.commit()
        return status
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cmd_fire(args) -> int:
    delay = max(0, getattr(args, "delay", 0))
    if delay:
        time.sleep(delay)
    claim = claim_schedule(args.id, getattr(args, "worker_token", None))
    if not claim:
        return 0

    message = f"Time for orchestrator check! {claim['note']} (system state: python3 {SELF} snapshot)"
    conn = get_db()
    try:
        result = send_message(conn, claim["target"], message, max_attempts=1)
        if result.verified:
            upsert_heartbeat(conn, claim["target"], "schedule-fire")
            conn.commit()
    finally:
        conn.close()

    status = finalize_schedule(
        args.id, claim["claim_token"], result.verified, result.error, result.retryable
    )
    if not result.verified and status not in ("cancelled", "missing"):
        print(
            f"Warning: schedule {args.id} delivery was not verified; status={status}: {result.error}",
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_list(args) -> int:
    conn = get_db()
    try:
        if args.all:
            rows = conn.execute("SELECT * FROM schedules ORDER BY id DESC LIMIT 50").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM schedules WHERE status IN ('pending', 'firing') ORDER BY due_at"
            ).fetchall()
        if not rows:
            print("No active schedules." if not args.all else "No schedules on record.")
            return 0
        print(f"{'ID':>4}  {'STATUS':<9}  {'DUE':<25}  {'ATTEMPTS':>8}  {'TARGET':<18}  NOTE / ERROR")
        for row in rows:
            detail = row["note"]
            if row["last_error"]:
                detail += f" | error: {row['last_error']}"
            print(
                f"{row['id']:>4}  {row['status']:<9}  {row['due_at']:<25}  "
                f"{row['fire_attempts']:>8}  {row['target']:<18}  {detail}"
            )
        return 0
    finally:
        conn.close()


def cmd_cancel(args) -> int:
    if (args.id is None) == (args.target is None):
        print("Error: provide exactly one schedule ID or --target.", file=sys.stderr)
        return 2
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if args.target is not None:
            if not args.target.strip():
                print("Error: target must not be empty.", file=sys.stderr)
                conn.rollback()
                return 2
            rows = conn.execute(
                "SELECT * FROM schedules WHERE target = ? AND status IN ('pending', 'firing', 'failed')",
                (args.target,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM schedules WHERE id = ? AND status IN ('pending', 'firing', 'failed')",
                (args.id,),
            ).fetchall()
        if not rows:
            conn.rollback()
            print("Nothing to cancel.")
            return 0
        for row in rows:
            conn.execute(
                "UPDATE schedules SET status = 'cancelled', pid = NULL, worker_token = NULL, "
                "claim_token = NULL, claimed_at = NULL WHERE id = ?",
                (row["id"],),
            )
            action = "Acknowledged" if row["status"] == "failed" else "Cancelled"
            print(f"{action} schedule {row['id']} ({row['target']}: {row['note']})")
        conn.commit()
        for row in rows:
            if row["status"] == "pending":
                stop_worker(row)
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def recover_stale_claims() -> int:
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cutoff = now() - timedelta(seconds=CLAIM_LEASE_SECONDS)
        rows = conn.execute("SELECT * FROM schedules WHERE status = 'firing'").fetchall()
        recovered = 0
        for row in rows:
            if row["claimed_at"] and parse_timestamp(row["claimed_at"]) > cutoff:
                continue
            status = "failed" if row["fire_attempts"] >= MAX_FIRE_ATTEMPTS else "pending"
            error = (
                "Delivery claim expired after the final attempt"
                if status == "failed"
                else "Recovered an expired delivery claim"
            )
            conn.execute(
                "UPDATE schedules SET status = ?, pid = NULL, worker_token = NULL, "
                "claim_token = NULL, claimed_at = NULL, last_error = ? WHERE id = ? AND claim_token = ?",
                (status, error, row["id"], row["claim_token"]),
            )
            recovered += 1
        conn.commit()
        return recovered
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def rearm_schedule(row: sqlite3.Row) -> str:
    """Re-arm one pending row while serializing worker check and spawn."""
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT * FROM schedules WHERE id = ? AND status = 'pending'", (row["id"],)
        ).fetchone()
        if not current:
            conn.rollback()
            return "unchanged"
        if parse_timestamp(current["due_at"]) <= now():
            conn.rollback()
            return "due"
        if worker_matches(current):
            conn.commit()
            return "active"

        worker_token = uuid.uuid4().hex
        remaining = max(0, int((parse_timestamp(current["due_at"]) - now()).total_seconds()) + 1)
        pid = spawn_sleeper(current["id"], remaining, worker_token)
        conn.execute(
            "UPDATE schedules SET worker_token = ?, pid = ? WHERE id = ? AND status = 'pending'",
            (worker_token, pid, current["id"]),
        )
        conn.commit()
        return "rearmed"
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cmd_recover(_args) -> int:
    stale = recover_stale_claims()
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM schedules WHERE status = 'pending' ORDER BY due_at").fetchall()
    finally:
        conn.close()

    processed, rearmed, active, failures = 0, 0, 0, 0
    for row in rows:
        action = rearm_schedule(row)
        if action == "active":
            active += 1
        elif action == "rearmed":
            rearmed += 1
        elif action == "due":
            processed += 1
            rc = cmd_fire(argparse.Namespace(id=row["id"], worker_token=row["worker_token"]))
            if rc != 0:
                failures += 1
            print(f"Recovered overdue schedule {row['id']} ({row['target']}: {row['note']})")
    print(
        f"Recover: {processed} overdue processed, {rearmed} re-armed, "
        f"{active} already active, {stale} stale claims recovered."
    )
    return 1 if failures else 0


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


def verify_delivery(target: str, marker: str) -> Tuple[bool, str]:
    """Confirm this exact attempt's marker, not that Claude acknowledged it."""
    deadline = time.monotonic() + VERIFY_TIMEOUT_SECONDS
    last_error = "verification marker was not observed"
    while True:
        result = tmux("capture-pane", "-t", target, "-p", "-J", "-S", "-200")
        if result.returncode == 0:
            if normalize(marker) in normalize(result.stdout):
                return True, ""
        else:
            last_error = result.stderr.strip() or "capture-pane failed"
        if time.monotonic() >= deadline:
            break
        time.sleep(VERIFY_INTERVAL_SECONDS)
    return False, last_error


def send_message(conn, target: str, body: str, max_attempts: int = 2) -> DeliveryResult:
    delivery_id = uuid.uuid4().hex
    if not target.strip() or not body.strip() or not target_exists(target):
        error = "target or message is empty" if not target.strip() or not body.strip() else "target does not exist"
        result = DeliveryResult(delivery_id, "failed", 0, error, retryable=True)
        conn.execute(
            "INSERT INTO messages "
            "(target, body, sent_at, attempts, delivered, delivery_id, status, error) "
            "VALUES (?, ?, ?, 0, 0, ?, 'failed', ?)",
            (target, body, iso(now()), delivery_id, error),
        )
        conn.commit()
        return result

    status, error, attempts, retryable = "unverified", "", 0, False
    for attempts in range(1, max_attempts + 1):
        marker = f"[orchestrator-delivery: {delivery_id}-{attempts}]"
        payload = f"{body} {marker}"
        typed = tmux("send-keys", "-t", target, "-l", payload)
        if typed.returncode != 0:
            status = "failed"
            retryable = True
            error = typed.stderr.strip() or "literal send-keys failed"
        else:
            time.sleep(0.1)
            submitted = tmux("send-keys", "-t", target, "Enter")
            if submitted.returncode != 0:
                status = "failed"
                retryable = False
                error = submitted.stderr.strip() or "Enter send-keys failed"
                break
            verified, error = verify_delivery(target, marker)
            if verified:
                status = "verified"
                retryable = False
                error = ""
                break
            # The prompt may already be executing. Never resubmit an uncertain input.
            status = "unverified"
            retryable = False
            break
        if attempts < max_attempts and retryable:
            print(
                f"Delivery to {target} not verified (attempt {attempts}/{max_attempts}), retrying...",
                file=sys.stderr,
            )
        else:
            break

    result = DeliveryResult(delivery_id, status, attempts, error, retryable)
    conn.execute(
        "INSERT INTO messages "
        "(target, body, sent_at, attempts, delivered, delivery_id, status, error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (target, body, iso(now()), attempts, 1 if result.verified else 0, delivery_id, status, error),
    )
    if result.verified:
        upsert_heartbeat(conn, target, "message")
    conn.commit()
    return result


def cmd_send(args) -> int:
    conn = get_db()
    try:
        result = send_message(conn, args.target, args.message)
    finally:
        conn.close()
    if result.verified:
        print(f"Message input observed in pane {args.target}: {args.message}")
        return 0
    print(
        f"Error: message to {args.target} was {result.status}: {result.error}",
        file=sys.stderr,
    )
    return 1


def cmd_heartbeat(args) -> int:
    if not args.target.strip():
        print("Error: target must not be empty.", file=sys.stderr)
        return 1
    conn = get_db()
    try:
        upsert_heartbeat(conn, args.target, args.source)
        conn.commit()
    finally:
        conn.close()
    print(f"Delivery activity recorded for {args.target} ({args.source})")
    return 0


# --- monitoring ---------------------------------------------------------------


def cmd_health(_args) -> int:
    conn = get_db()
    try:
        heartbeats = {row["target"]: row for row in conn.execute("SELECT * FROM heartbeats")}
        stale_after = timedelta(minutes=HEARTBEAT_STALE_MINUTES)
        result = tmux(
            "list-panes",
            "-a",
            "-F",
            FIELD_SEPARATOR.join(
                (
                    "#{session_name}:#{window_index}.#{pane_index}",
                    "#{window_name}",
                    "#{pane_current_command}",
                    "#{pane_dead}",
                    "#{window_activity}",
                )
            ),
        )
        if result.returncode != 0:
            print(f"Error: could not list tmux panes: {result.stderr.strip()}", file=sys.stderr)
            return 1

        print(f"Window health report - {iso(now())} (stale = no activity for >{HEARTBEAT_STALE_MINUTES} min)")
        print("=" * 70)
        problems = 0
        for line in result.stdout.splitlines():
            if not line:
                continue
            try:
                target, name, command, dead, activity = line.split(FIELD_SEPARATOR, 4)
                idle = now() - datetime.fromtimestamp(int(activity), timezone.utc)
            except (ValueError, OverflowError, OSError) as exc:
                problems += 1
                print(f"!! unparseable tmux row: {exc}")
                continue
            status = []
            if dead == "1":
                status.append("DEAD (pane_dead)")
            is_agent_window = name.lower().startswith("claude") or "agent" in name.lower()
            if is_agent_window and command in SHELL_COMMANDS:
                status.append(f"WARN agent window but running '{command}' (claude not started?)")
            heartbeat_candidates = [
                heartbeat
                for heartbeat in (
                    heartbeats.get(target.rsplit(".", 1)[0]),
                    heartbeats.get(target),
                )
                if heartbeat
            ]
            hb = max(
                heartbeat_candidates,
                key=lambda heartbeat: parse_timestamp(heartbeat["last_seen"]),
                default=None,
            )
            if idle > stale_after and (not hb or now() - parse_timestamp(hb["last_seen"]) > stale_after):
                status.append(f"STALE (idle {int(idle.total_seconds() // 60)} min)")

            if status:
                problems += 1
                print(f"!! {target} [{name}] cmd={command}")
                for item in status:
                    print(f"     {item}")
            else:
                print(f"ok {target} [{name}] cmd={command}")

        overdue = conn.execute(
            "SELECT COUNT(*) AS c FROM schedules WHERE "
            "(status = 'pending' AND due_at < ?) OR "
            "(status = 'firing' AND (claimed_at IS NULL OR claimed_at < ?))",
            (iso(now()), iso(now() - timedelta(seconds=CLAIM_LEASE_SECONDS))),
        ).fetchone()["c"]
        failed = conn.execute(
            "SELECT COUNT(*) AS c FROM schedules WHERE status = 'failed'"
        ).fetchone()["c"]
        print("=" * 70)
        print(f"{problems} problem window(s), {overdue} overdue schedule(s), {failed} failed schedule(s).")
        if overdue:
            print("Run 'orchestrator.py recover'.")
        return 0 if problems == 0 and overdue == 0 and failed == 0 else 1
    finally:
        conn.close()


# --- diagnostics & hooks -------------------------------------------------------


def cmd_doctor(_args) -> int:
    checks = []
    py_ok = sys.version_info >= (3, 9)
    checks.append((py_ok, f"python3 >= 3.9 (found {sys.version.split()[0]})"))

    tmux_path = shutil.which("tmux")
    if tmux_path:
        version_result = tmux("-V")
        checks.append((version_result.returncode == 0, f"tmux in PATH ({version_result.stdout.strip() or version_result.stderr.strip()})"))
    else:
        checks.append((False, "tmux not found in PATH"))

    for script in ("send-claude-message.sh", "schedule_with_note.sh"):
        path = SCRIPT_DIR / script
        ok = path.exists() and os.access(path, os.X_OK)
        checks.append((ok, f"{script} exists and is executable"))

    try:
        path = db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        probe = path.parent / ".write-probe"
        probe.touch()
        probe.unlink()
        checks.append((True, f"database directory writable ({path.parent})"))
    except OSError as exc:
        checks.append((False, f"database directory not writable: {exc}"))

    try:
        conn = get_db()
        try:
            conn.execute("SELECT COUNT(*) FROM schedules")
            checks.append((True, f"SQLite schema OK ({db_path()})"))
        finally:
            conn.close()
    except sqlite3.Error as exc:
        checks.append((False, f"SQLite error: {exc}"))

    hooks_ok = None
    if tmux_path:
        hooks_result = tmux("show-hooks", "-g")
        hooks_ok = hooks_result.returncode == 0 and "orchestrator.py" in hooks_result.stdout and " recover" in hooks_result.stdout

    all_ok = True
    for ok, label in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
        all_ok = all_ok and ok
    if hooks_ok is not None:
        print(
            f"{'PASS' if hooks_ok else 'INFO'}  tmux recover hooks "
            + ("installed" if hooks_ok else "not installed (expected after server restart; run: orchestrator.py install-hooks)")
        )
    print("Doctor: all checks passed." if all_ok else "Doctor: some checks FAILED.")
    return 0 if all_ok else 1


def cmd_install_hooks(_args) -> int:
    """Append recovery hooks without replacing existing user configuration."""
    recover_script = shlex.join([sys.executable, SELF, "recover"])
    recover_command = "run-shell " + shlex.quote(recover_script)
    for hook in ("session-created", "client-attached"):
        existing = tmux("show-hooks", "-g", hook)
        if existing.returncode != 0:
            print(f"Error reading hook {hook}: {existing.stderr.strip()}", file=sys.stderr)
            return 1
        installed = False
        for line in existing.stdout.splitlines():
            try:
                parts = shlex.split(line)
            except ValueError:
                continue
            if len(parts) >= 2 and parts[-2:] == ["run-shell", recover_script]:
                installed = True
                break
        if installed:
            print(f"Hook already installed: {hook}")
            continue
        result = tmux("set-hook", "-g", "-a", hook, recover_command)
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


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description="Tmux Orchestrator reliability layer")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("schedule", help="schedule a check-in")
    p.add_argument("minutes", type=positive_int)
    p.add_argument("note")
    p.add_argument("target", nargs="?")
    p.set_defaults(func=cmd_schedule)

    p = sub.add_parser("fire", help="fire a pending schedule (called by sleeper)")
    p.add_argument("id", type=positive_int)
    p.add_argument("--worker-token")
    p.add_argument("--delay", type=int, default=0, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_fire)

    p = sub.add_parser("list", help="list schedules")
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("cancel", help="cancel schedule(s)")
    p.add_argument("id", type=positive_int, nargs="?")
    p.add_argument("--target")
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("recover", help="re-arm pending schedules after interruption")
    p.set_defaults(func=cmd_recover)

    p = sub.add_parser("send", help="send a message and verify its input marker")
    p.add_argument("target")
    p.add_argument("message")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("heartbeat", help="record outbound delivery activity")
    p.add_argument("target")
    p.add_argument("source", nargs="?", default="manual")
    p.set_defaults(func=cmd_heartbeat)

    p = sub.add_parser("health", help="window and schedule health report")
    p.set_defaults(func=cmd_health)

    p = sub.add_parser("doctor", help="environment self-check")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("install-hooks", help="append recovery hooks idempotently")
    p.set_defaults(func=cmd_install_hooks)

    p = sub.add_parser("snapshot", help="tmux monitoring snapshot (via tmux_utils)")
    p.set_defaults(func=cmd_snapshot)

    args = parser.parse_args()
    try:
        return args.func(args)
    except (OSError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
