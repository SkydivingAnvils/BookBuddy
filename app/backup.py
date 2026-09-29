"""Scheduled SQLite backups.

Copies the live database with SQLite's online backup API (safe while the app
is writing, WAL included) into BACKUP_DIR, keeping the newest BACKUP_KEEP files.
"""
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime
from typing import Optional

from .database import DATABASE_URL

logger = logging.getLogger(__name__)

BACKUP_NAME_RE = re.compile(r"^bookbuddy-\d{8}-\d{6}\.db$")


def _db_path() -> Optional[str]:
    if not DATABASE_URL.startswith("sqlite:///"):
        return None
    path = DATABASE_URL[len("sqlite:///"):]
    return None if path in ("", ":memory:") else path


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        logger.warning("%s is not an integer; using %s", name, default)
        return default


def backup_dir() -> Optional[str]:
    if os.getenv("BACKUP_DIR"):
        return os.getenv("BACKUP_DIR")
    db = _db_path()
    return os.path.join(os.path.dirname(os.path.abspath(db)), "backups") if db else None


def keep_count() -> int:
    return max(1, _int_env("BACKUP_KEEP", 14))


def interval_hours() -> int:
    return _int_env("BACKUP_INTERVAL_HOURS", 24)


def list_backups() -> list:
    directory = backup_dir()
    if not directory or not os.path.isdir(directory):
        return []
    backups = []
    for name in os.listdir(directory):
        if BACKUP_NAME_RE.match(name):
            stat = os.stat(os.path.join(directory, name))
            backups.append({
                "name": name,
                "size": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            })
    # Names embed the timestamp, so they sort chronologically
    return sorted(backups, key=lambda b: b["name"], reverse=True)


def backup_path(name: str) -> Optional[str]:
    """Resolve a backup file name, rejecting anything that isn't one of ours."""
    directory = backup_dir()
    if not directory or not BACKUP_NAME_RE.match(name):
        return None
    path = os.path.join(directory, name)
    return path if os.path.isfile(path) else None


def run_backup() -> dict:
    db = _db_path()
    if not db:
        raise RuntimeError("Backups are only supported for file-based SQLite databases.")
    directory = backup_dir()
    os.makedirs(directory, exist_ok=True)

    name = f"bookbuddy-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    dest_path = os.path.join(directory, name)
    src = sqlite3.connect(db)
    dest = sqlite3.connect(dest_path)
    try:
        src.backup(dest)
    finally:
        dest.close()
        src.close()
    logger.info("Database backed up to %s", dest_path)

    for old in list_backups()[keep_count():]:
        os.remove(os.path.join(directory, old["name"]))
        logger.info("Pruned old backup %s", old["name"])

    return next(b for b in list_backups() if b["name"] == name)


def _hours_since_last_backup() -> Optional[float]:
    backups = list_backups()
    if not backups:
        return None
    last = datetime.fromisoformat(backups[0]["created_at"])
    return (datetime.now() - last).total_seconds() / 3600


class BackupScheduler:
    """Background thread that backs up every interval_hours(); 0 disables it."""

    def __init__(self):
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        hours = interval_hours()
        if hours <= 0 or not _db_path():
            logger.info("Scheduled backups disabled")
            return
        self._thread = threading.Thread(target=self._run, args=(hours,), daemon=True, name="backup")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self, hours: int):
        while not self._stop.is_set():
            since = _hours_since_last_backup()
            if since is None or since >= hours:
                try:
                    run_backup()
                except Exception as e:
                    logger.error("Scheduled backup failed: %s", e)
                wait = hours * 3600
            else:
                wait = (hours - since) * 3600
            # Wake at least hourly so a clock change or a deleted backup is noticed
            self._stop.wait(min(wait, 3600))
