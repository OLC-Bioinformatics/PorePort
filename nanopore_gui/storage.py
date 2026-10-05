from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


FILE_STATUSES = ("pending", "uploading", "uploaded", "error")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class QueueStore:
    """Thread-safe durable state for runs, uploads, and cached reports."""

    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()

        with self.lock:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA busy_timeout=5000")
            self.connection.execute("PRAGMA foreign_keys=ON")
            self._create_schema()
            self._migrate_schema()
            self.connection.commit()

    def _create_schema(self) -> None:
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id INTEGER PRIMARY KEY,
                run_name TEXT NOT NULL,
                local_path TEXT NOT NULL,
                started_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                active INTEGER NOT NULL DEFAULT 1,
                workflow_state TEXT,
                last_status_at TEXT
            )
        """)
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS files (
                run_id INTEGER NOT NULL,
                relative_path TEXT NOT NULL,
                local_path TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                server_file_id INTEGER,
                error TEXT,
                server_blob_name TEXT,
                server_container TEXT,
                discovered_at TEXT,
                upload_started_at TEXT,
                uploaded_at TEXT,
                last_attempt_at TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (run_id, relative_path)
            )
        """)
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                run_id INTEGER NOT NULL,
                iteration INTEGER NOT NULL,
                manifest_path TEXT,
                downloaded_at TEXT,
                report_directory TEXT,
                PRIMARY KEY (run_id, iteration)
            )
        """)

    def _columns(self, table: str) -> set[str]:
        return {
            row[1]
            for row in self.connection.execute(
                "PRAGMA table_info({0})".format(table)
            )
        }

    def _add_column(self, table: str, definition: str) -> None:
        column = definition.split()[0]
        if column not in self._columns(table):
            self.connection.execute(
                "ALTER TABLE {0} ADD COLUMN {1}".format(table, definition)
            )

    def _migrate_schema(self) -> None:
        self._add_column(
            "runs", "metadata_json TEXT NOT NULL DEFAULT '{}'"
        )
        self._add_column("runs", "workflow_state TEXT")
        self._add_column("runs", "last_status_at TEXT")
        self._add_column("files", "server_blob_name TEXT")
        self._add_column("files", "server_container TEXT")
        self._add_column("files", "discovered_at TEXT")
        self._add_column("files", "upload_started_at TEXT")
        self._add_column("files", "uploaded_at TEXT")
        self._add_column("files", "last_attempt_at TEXT")
        self._add_column(
            "files", "attempt_count INTEGER NOT NULL DEFAULT 0"
        )

    def add_pending(
        self,
        run_id: int,
        relative_path: str,
        local_path: str,
        size_bytes: int,
    ) -> bool:
        """Insert a new queue item or refresh a retryable changed item.

        Returns True when the caller should submit the item to the uploader.
        Completed files and files already uploading are left unchanged.
        """
        now = _utc_now()
        with self.lock:
            row = self.connection.execute(
                "SELECT status, local_path, size_bytes FROM files "
                "WHERE run_id = ? AND relative_path = ?",
                (run_id, relative_path),
            ).fetchone()

            if row is None:
                self.connection.execute(
                    "INSERT INTO files("
                    "run_id, relative_path, local_path, size_bytes, "
                    "status, discovered_at"
                    ") VALUES (?, ?, ?, ?, 'pending', ?)",
                    (
                        run_id,
                        relative_path,
                        local_path,
                        size_bytes,
                        now,
                    ),
                )
                self.connection.commit()
                return True

            if row["status"] not in ("pending", "error"):
                return False

            changed = (
                row["local_path"] != local_path
                or row["size_bytes"] != size_bytes
            )
            if changed or row["status"] == "error":
                self.connection.execute(
                    "UPDATE files SET local_path = ?, size_bytes = ?, "
                    "status = 'pending', error = NULL, discovered_at = ? "
                    "WHERE run_id = ? AND relative_path = ?",
                    (
                        local_path,
                        size_bytes,
                        now,
                        run_id,
                        relative_path,
                    ),
                )
                self.connection.commit()
            return True

    def save_run(
        self,
        run_id: int,
        run_name: str,
        local_path: str,
        started_at: str,
        metadata: dict | None = None,
    ) -> None:
        with self.lock:
            self.connection.execute(
                "INSERT OR REPLACE INTO runs("
                "run_id, run_name, local_path, started_at, metadata_json, "
                "active, workflow_state, last_status_at"
                ") VALUES (?, ?, ?, ?, ?, 1, "
                "COALESCE((SELECT workflow_state FROM runs WHERE run_id = ?), "
                "NULL), "
                "COALESCE((SELECT last_status_at FROM runs WHERE run_id = ?), "
                "NULL))",
                (
                    run_id,
                    run_name,
                    local_path,
                    started_at,
                    json.dumps(metadata or {}),
                    run_id,
                    run_id,
                ),
            )
            self.connection.commit()

    def update_run_status(self, run_id: int, workflow_state: str) -> None:
        """Persist the latest server workflow state.

        Runs stay locally active while processing or stopping so the GUI can
        resume monitoring after a restart. Terminal runs are made inactive.
        """
        active = 0 if workflow_state in ("complete", "error") else 1
        with self.lock:
            self.connection.execute(
                "UPDATE runs SET workflow_state = ?, last_status_at = ?, "
                "active = ? WHERE run_id = ?",
                (workflow_state, _utc_now(), active, run_id),
            )
            self.connection.commit()

    def active_run(self):
        with self.lock:
            return self.connection.execute(
                "SELECT * FROM runs WHERE active = 1 "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()

    def run(self, run_id: int):
        """Return a locally saved run, including completed runs, by ID."""
        with self.lock:
            return self.connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()

    def list_runs(self):
        """Return locally saved runs; caller must verify server access."""
        with self.lock:
            return self.connection.execute(
                "SELECT * FROM runs ORDER BY started_at DESC"
            ).fetchall()

    def set_runs_active(self, changes: dict[int, bool]) -> None:
        """Atomically change local startup eligibility without deleting history."""
        if not changes:
            return
        with self.lock:
            try:
                for run_id, active in changes.items():
                    result = self.connection.execute(
                        "UPDATE runs SET active = ? WHERE run_id = ?",
                        (int(bool(active)), int(run_id)),
                    )
                    if result.rowcount != 1:
                        raise ValueError("Unknown locally saved run: {0}".format(run_id))
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise

    def close_run(self, run_id: int) -> None:
        with self.lock:
            self.connection.execute(
                "UPDATE runs SET active = 0 WHERE run_id = ?", (run_id,)
            )
            self.connection.commit()

    def pending(self, run_id: int):
        with self.lock:
            return self.connection.execute(
                "SELECT * FROM files WHERE run_id = ? "
                "AND status IN ('pending', 'uploading', 'error') "
                "ORDER BY relative_path",
                (run_id,),
            ).fetchall()

    def all(self, run_id: int):
        with self.lock:
            return self.connection.execute(
                "SELECT * FROM files WHERE run_id = ? ORDER BY relative_path",
                (run_id,),
            ).fetchall()

    def recover_uploading(self, run_id: int) -> int:
        with self.lock:
            result = self.connection.execute(
                "UPDATE files SET status = 'pending', "
                "upload_started_at = NULL "
                "WHERE run_id = ? AND status = 'uploading'",
                (run_id,),
            )
            self.connection.commit()
            return result.rowcount

    def mark(
        self,
        run_id: int,
        relative_path: str,
        status: str,
        file_id=None,
        error=None,
        blob_name=None,
        container=None,
    ) -> None:
        if status not in FILE_STATUSES:
            raise ValueError("Unsupported queue status: {0}".format(status))

        now = _utc_now()
        upload_started_at = now if status == "uploading" else None
        uploaded_at = now if status == "uploaded" else None
        attempt_increment = 1 if status == "uploading" else 0

        with self.lock:
            self.connection.execute(
                "UPDATE files SET status = ?, "
                "server_file_id = COALESCE(?, server_file_id), "
                "error = ?, "
                "server_blob_name = COALESCE(?, server_blob_name), "
                "server_container = COALESCE(?, server_container), "
                "upload_started_at = COALESCE(?, upload_started_at), "
                "uploaded_at = COALESCE(?, uploaded_at), "
                "last_attempt_at = CASE WHEN ? = 1 THEN ? "
                "ELSE last_attempt_at END, "
                "attempt_count = attempt_count + ? "
                "WHERE run_id = ? AND relative_path = ?",
                (
                    status,
                    file_id,
                    error,
                    blob_name,
                    container,
                    upload_started_at,
                    uploaded_at,
                    attempt_increment,
                    now,
                    attempt_increment,
                    run_id,
                    relative_path,
                ),
            )
            self.connection.commit()

    def counts(self, run_id: int) -> tuple[int, int, int]:
        with self.lock:
            row = self.connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(size_bytes), 0), "
                "COALESCE(SUM(CASE WHEN status = 'uploaded' "
                "THEN size_bytes ELSE 0 END), 0) "
                "FROM files WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return tuple(row)

    def save_report(
        self,
        run_id: int,
        iteration: int,
        report_directory: str,
        manifest_path: str | None = None,
    ) -> None:
        with self.lock:
            self.connection.execute(
                "INSERT OR REPLACE INTO reports("
                "run_id, iteration, manifest_path, downloaded_at, "
                "report_directory"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    run_id,
                    iteration,
                    manifest_path,
                    _utc_now(),
                    report_directory,
                ),
            )
            self.connection.commit()

    def report(self, run_id: int, iteration: int):
        with self.lock:
            return self.connection.execute(
                "SELECT * FROM reports WHERE run_id = ? AND iteration = ?",
                (run_id, iteration),
            ).fetchone()

    def reports(self, run_id: int):
        with self.lock:
            return self.connection.execute(
                "SELECT * FROM reports WHERE run_id = ? "
                "ORDER BY iteration",
                (run_id,),
            ).fetchall()

    def close(self) -> None:
        with self.lock:
            self.connection.close()
