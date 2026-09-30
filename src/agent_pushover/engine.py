"""Durable local queue and short-lived sender; hooks do no network I/O."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Callable

from .config import load_config, load_credentials, private_dir
from .events import Event, classify, render
from .pushover import DeliveryError, send


def log(home: Path, status: str, **fields) -> None:
    """Only callers' diagnostic codes/counts belong here, never raw event data."""
    try:
        private_dir(home)
        with lock(home / "log.lock"):
            path = home / "events.log"
            if path.exists() and path.stat().st_size > 1_000_000:
                os.replace(path, home / "events.log.1")
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a") as stream:
                stream.write(json.dumps({"time": time.time(), "status": status, **fields}) + "\n")
    except Exception:
        pass  # Notification failures must never become coding-agent failures.


@contextlib.contextmanager
def lock(path: Path, blocking: bool = True):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield fd
    finally:
        os.close(fd)


class Store:
    def __init__(self, home: Path):
        private_dir(home)
        self.db = sqlite3.connect(home / "state.sqlite3", timeout=2)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, generation TEXT NOT NULL, state TEXT NOT NULL, touched REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sent (key TEXT PRIMARY KEY, at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, key TEXT UNIQUE NOT NULL, session TEXT NOT NULL,
                trigger TEXT NOT NULL, message TEXT NOT NULL, created REAL NOT NULL,
                due REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                generation TEXT, priority INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS jobs_due ON jobs(due);
        """)
        (home / "state.sqlite3").chmod(0o600)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    def session(self, key: str, now: float) -> sqlite3.Row:
        self.db.execute("INSERT OR IGNORE INTO sessions VALUES (?, ?, 'active', ?)",
                        (key, uuid.uuid4().hex, now))
        return self.db.execute("SELECT * FROM sessions WHERE id=?", (key,)).fetchone()

    def enqueue(self, event: Event, message: dict, config: dict, now: float,
                *, due: float | None = None, generation: str | None = None,
                dedupe_generation: str = "") -> bool:
        if not config["triggers"][event.trigger]["enabled"]:
            return False
        key = event.dedupe_key(dedupe_generation)
        recent = self.db.execute("SELECT at FROM sent WHERE key=?", (key,)).fetchone()
        if recent and now - recent["at"] < config["triggers"][event.trigger]["dedupe_seconds"]:
            return False
        if self.db.execute("SELECT 1 FROM jobs WHERE key=?", (key,)).fetchone():
            return False
        count = self.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        if count >= config["delivery"]["max_pending"]:
            # Preserve high-priority alerts over normal reminders during a storm.
            victim = self.db.execute("SELECT id, priority FROM jobs ORDER BY priority, created LIMIT 1").fetchone()
            if victim["priority"] > message["priority"]:
                return False
            self.db.execute("DELETE FROM jobs WHERE id=?", (victim["id"],))
        self.db.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                        (uuid.uuid4().hex, key, event.session_key, event.trigger,
                         json.dumps(message), now, now if due is None else due,
                         generation, message["priority"]))
        return True


def launch_worker(home: Path) -> None:
    """Coordinate sender startup with its empty-queue check, avoiding lost wakeups."""
    entry = Path(__file__).resolve().parents[1] / "entry.py"
    args = [sys.executable, "-I", str(entry), "--home", str(home), "drain", "--wait"]
    with lock(home / "coordinator.lock"):
        try:
            with lock(home / "worker.lock", blocking=False):
                # The child takes this lock after we release it. Close inherited descriptors.
                subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
        except BlockingIOError:
            return  # Existing worker will see the already-committed job.


def handle(home: Path, platform: str, data: dict, *, spawn: bool = True,
           now: float | None = None, environ: dict | None = None) -> int:
    """Normalize and enqueue. Exceptions are handled by the fail-open hook entrypoint."""
    if (home / "disabled").exists():
        return 0
    env = os.environ if environ is None else environ
    now = time.time() if now is None else now
    config = load_config(home)
    event = classify(platform, data, env.get("AGENT_PUSHOVER_RUN_ID", ""))
    identity = event or classify(platform, {**data, "hook_event_name": "IdleReminder"},
                                 env.get("AGENT_PUSHOVER_RUN_ID", ""))
    if identity is None:
        return 0
    name = data.get("hook_event_name") or ("Stop" if data.get("type") == "agent-turn-complete" else "")
    try:
        secrets = tuple(load_credentials(home).values())
    except ValueError:
        secrets = ()
    added = 0
    with Store(home) as store:
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            row = store.session(identity.session_key, now)
            generation = row["generation"]
            if name in ("SessionStart", "UserPromptSubmit", "PreToolUse", "Input", "SessionEnd"):
                generation = uuid.uuid4().hex
                state = "closed" if name == "SessionEnd" else "active"
                store.db.execute("UPDATE sessions SET generation=?, state=?, touched=? WHERE id=?",
                                 (generation, state, now, identity.session_key))
                # Cancel derived reminders immediately, including pending retries.
                store.db.execute("DELETE FROM jobs WHERE session=? AND generation IS NOT NULL",
                                 (identity.session_key,))
            if name in ("StopFailure", "RuntimeError"):
                store.db.execute("UPDATE sessions SET state='failed', touched=? WHERE id=?",
                                 (now, identity.session_key))
                store.db.execute("DELETE FROM jobs WHERE session=? AND generation IS NOT NULL",
                                 (identity.session_key,))
            # In codex exec --json mode use turn.completed, which arrives after Stop hooks.
            skip_stop = platform == "codex" and env.get("AGENT_PUSHOVER_STREAM") == "json" and name == "Stop" \
                and not data.get("_stream_event")
            if name == "Stop" and not identity.agent and not skip_stop:
                store.db.execute("UPDATE sessions SET state='stopped', touched=? WHERE id=?",
                                 (now, identity.session_key))
                delay = config["codex"]["idle_after_stop_seconds"]
                if platform == "codex" and delay > 0 and env.get("AGENT_PUSHOVER_STREAM") != "json":
                    reminder = replace(identity, trigger="idle", reason="idle", source="IdleReminder", detail="",
                                       summary="No new input was observed after the turn stopped. Check the session.")
                    added += store.enqueue(reminder, render(reminder, config, secrets), config, now,
                                           due=now + delay, generation=generation)
            if event and not skip_stop:
                added += store.enqueue(event, render(event, config, secrets), config, now,
                                       dedupe_generation=generation)
    if added:
        log(home, "queued", platform=platform, trigger=event.trigger if event else "idle", count=added)
        if spawn:
            launch_worker(home)
    return added


def safe_handle(home: Path, platform: str, data: dict, **kwargs) -> int:
    try:
        return handle(home, platform, data, **kwargs)
    except Exception as exc:
        log(home, "handler_failed", error_type=type(exc).__name__)
        return 0


def recent_failure(home: Path, platform: str, run_id: str, seconds: int = 60) -> bool:
    """Avoid a second generic process-exit error after a more informative stream error."""
    dummy = classify(platform, {"hook_event_name": "RuntimeError", "session_id": run_id})
    if dummy is None:
        return False
    try:
        with Store(home) as store:
            row = store.db.execute("SELECT 1 FROM sessions WHERE id=? AND state='failed' AND touched>?",
                                   (dummy.session_key, time.time() - seconds)).fetchone()
            return bool(row)
    except Exception:
        return False


def _clean(store: Store, config: dict, now: float) -> None:
    store.db.execute("DELETE FROM jobs WHERE created < ?", (now - config["delivery"]["max_age_seconds"],))
    store.db.execute("DELETE FROM jobs WHERE generation IS NOT NULL AND NOT EXISTS "
                     "(SELECT 1 FROM sessions s WHERE s.id=jobs.session "
                     "AND s.generation=jobs.generation AND s.state='stopped')")
    for trigger, settings in config["triggers"].items():
        if not settings["enabled"]:
            store.db.execute("DELETE FROM jobs WHERE trigger=?", (trigger,))
    store.db.execute("DELETE FROM sent WHERE at < ?", (now - 86400,))
    store.db.execute("DELETE FROM sessions WHERE touched < ?", (now - 30 * 86400,))


def drain(home: Path, *, wait: bool = False, sender: Callable = send,
          clock: Callable = time.time, sleep: Callable = time.sleep) -> int:
    """One serialized sender per home. Exits once the queue (including timers) is empty."""
    private_dir(home)
    worker_fd = os.open(home / "worker.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(worker_fd, fcntl.LOCK_EX)
    try:
        with Store(home) as store:
            while True:
                config = load_config(home)
                now = clock()
                # Worker lock is released while still holding the coordinator when exiting.
                with lock(home / "coordinator.lock"):
                    with store.db:
                        if (home / "disabled").exists():
                            store.db.execute("DELETE FROM jobs")
                        _clean(store, config, now)
                    row = store.db.execute("SELECT * FROM jobs ORDER BY due, priority DESC LIMIT 1").fetchone()
                    if row is None or (row["due"] > now and not wait):
                        fcntl.flock(worker_fd, fcntl.LOCK_UN)
                        return 0
                if row["due"] > now:
                    sleep(min(0.5, row["due"] - now))
                    continue
                # Re-read validity immediately before sending; concurrent input may have canceled it.
                if not store.db.execute("SELECT 1 FROM jobs WHERE id=?", (row["id"],)).fetchone():
                    continue
                try:
                    credentials = load_credentials(home)
                    message = json.loads(row["message"])
                    message["priority"] = config["triggers"][row["trigger"]]["priority"]
                    sender(credentials, message, config)
                except Exception as exc:
                    retry = isinstance(exc, DeliveryError) and exc.retryable
                    attempts = row["attempts"] + 1
                    if retry and attempts < config["delivery"]["max_attempts"]:
                        delay = (2, 10, 30, 60)[min(attempts - 1, 3)]
                        with store.db:
                            store.db.execute("UPDATE jobs SET attempts=?, due=? WHERE id=?",
                                             (attempts, clock() + delay, row["id"]))
                        log(home, "delivery_retry", trigger=row["trigger"], attempt=attempts,
                            code=exc.code)
                    else:
                        with store.db:
                            store.db.execute("DELETE FROM jobs WHERE id=?", (row["id"],))
                        log(home, "delivery_failed", trigger=row["trigger"],
                            code=exc.code if isinstance(exc, DeliveryError) else type(exc).__name__)
                else:
                    with store.db:
                        store.db.execute("INSERT OR REPLACE INTO sent VALUES (?, ?)", (row["key"], clock()))
                        store.db.execute("DELETE FROM jobs WHERE id=?", (row["id"],))
                    log(home, "delivered", trigger=row["trigger"], priority=message["priority"])
    finally:
        os.close(worker_fd)
