from __future__ import annotations

import json
import sqlite3
import threading
from typing import Optional

from backend.config import DB_FILE, ensure_directories
from backend.models import DownloadItem, DownloadStatus


class DownloadDB:
    def __init__(self) -> None:
        ensure_directories()
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(DB_FILE), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS downloads (
                    id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            self._conn.commit()

    def save(self, item: DownloadItem) -> None:
        # Don't bloat DB with full speed_history
        copy_item = item.model_copy(update={"speed_history": item.speed_history[-30:]})
        payload = copy_item.model_dump_json()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO downloads (id, created_at, payload)
                VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET payload = excluded.payload
                """,
                (item.id, item.created_at, payload),
            )
            self._conn.commit()

    def delete(self, download_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM downloads WHERE id = ?", (download_id,))
            self._conn.commit()

    def load_all(self) -> list[DownloadItem]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM downloads ORDER BY created_at DESC"
            ).fetchall()
        items: list[DownloadItem] = []
        for (payload,) in rows:
            try:
                item = DownloadItem.model_validate_json(payload)
                # Any download that was mid-flight when app exited cleanly resets to PAUSED
                if item.status in (
                    DownloadStatus.DOWNLOADING,
                    DownloadStatus.PROBING,
                    DownloadStatus.MERGING,
                    DownloadStatus.QUEUED,
                ):
                    item.status = DownloadStatus.PAUSED
                    item.status_text = "Paused (Ready to Resume)"
                    item.speed = 0.0
                    item.eta = None
                    item.active_connections = 0
                    for seg in item.segments:
                        if seg.status == "active":
                            seg.status = "paused"
                            seg.speed = 0.0
                items.append(item)
            except Exception:
                continue
        return items

    def clear_completed(self) -> list[str]:
        all_items = self.load_all()
        removed: list[str] = []
        with self._lock:
            for item in all_items:
                if item.status in (DownloadStatus.COMPLETED, DownloadStatus.CANCELLED):
                    self._conn.execute("DELETE FROM downloads WHERE id = ?", (item.id,))
                    removed.append(item.id)
            self._conn.commit()
        return removed

    def close(self) -> None:
        with self._lock:
            self._conn.close()
