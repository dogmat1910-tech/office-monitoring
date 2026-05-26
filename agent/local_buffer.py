"""
Локальный SQLite-буфер для агента.

При отвале сети все исходящие запросы складываем сюда, при восстановлении —
sender в отдельном потоке последовательно отправляет всё на сервер.

Поддерживаемые типы payload'ов:
- json: dict для POST с Content-Type application/json
- multipart: dict для POST с multipart (form fields + один файл)

База: ~/.office-monitoring/buffer.db
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger("agent")

BUFFER_DIR = Path(os.environ.get("OM_LOG_DIR", str(Path.home() / ".office-monitoring")))
BUFFER_DIR.mkdir(parents=True, exist_ok=True)
BLOBS_DIR = BUFFER_DIR / "blobs"
BLOBS_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = BUFFER_DIR / "buffer.db"

# Максимальный размер буфера (ограничение, чтобы не съесть диск)
MAX_ITEMS = int(os.environ.get("OM_BUFFER_MAX_ITEMS", "5000"))
MAX_BLOB_BYTES = int(os.environ.get("OM_BUFFER_MAX_BLOB_BYTES", str(500 * 1024 * 1024)))  # 500 MB


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at REAL NOT NULL,
            method TEXT NOT NULL,
            url TEXT NOT NULL,
            kind TEXT NOT NULL,        -- 'json' | 'multipart'
            data_json TEXT,            -- form fields для multipart, или JSON-payload
            blob_path TEXT,            -- путь к файлу для multipart
            blob_filename TEXT,
            blob_content_type TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_outbox_created ON outbox(created_at);
    """)
    conn.commit()


class LocalBuffer:
    """Потокобезопасная очередь outbound-запросов с дисковым backing'ом."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        with self._lock:
            self._conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, isolation_level=None)
            self._conn.execute("PRAGMA journal_mode=WAL")
            _ensure_schema(self._conn)
        self._cleanup_orphan_blobs()

    def _cleanup_orphan_blobs(self) -> None:
        """Удаляем файлы blobs/ для которых нет записи в БД."""
        with self._lock:
            rows = self._conn.execute("SELECT blob_path FROM outbox WHERE blob_path IS NOT NULL").fetchall()
            referenced = {r[0] for r in rows}
        for f in BLOBS_DIR.iterdir():
            if str(f) not in referenced:
                try:
                    f.unlink()
                except Exception:
                    pass

    def _used_blob_bytes(self) -> int:
        return sum(f.stat().st_size for f in BLOBS_DIR.iterdir() if f.is_file())

    def _count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def enqueue_json(self, method: str, url: str, payload: dict) -> bool:
        if self._count() >= MAX_ITEMS:
            log.warning("buffer full (%d items) — drop %s %s", MAX_ITEMS, method, url)
            return False
        with self._lock:
            self._conn.execute(
                "INSERT INTO outbox(created_at, method, url, kind, data_json) VALUES (?,?,?,?,?)",
                (time.time(), method, url, "json", json.dumps(payload, ensure_ascii=False)),
            )
        return True

    def enqueue_multipart(self, url: str, form: dict, file_bytes: bytes,
                         filename: str, content_type: str) -> bool:
        if self._count() >= MAX_ITEMS:
            log.warning("buffer full — drop %s", url)
            return False
        if self._used_blob_bytes() + len(file_bytes) > MAX_BLOB_BYTES:
            log.warning("buffer blobs > %d bytes — drop %s", MAX_BLOB_BYTES, url)
            return False
        # сохраняем blob отдельным файлом, не в БД (не раздуваем БД)
        blob_filename = f"blob_{int(time.time() * 1000)}_{filename}"
        blob_path = BLOBS_DIR / blob_filename
        try:
            blob_path.write_bytes(file_bytes)
        except Exception as e:
            log.warning("buffer: blob save failed: %s", e)
            return False
        with self._lock:
            self._conn.execute(
                "INSERT INTO outbox(created_at, method, url, kind, data_json, blob_path, blob_filename, blob_content_type) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (time.time(), "POST", url, "multipart", json.dumps(form, ensure_ascii=False),
                 str(blob_path), filename, content_type),
            )
        return True

    def peek(self, limit: int = 10) -> list[dict]:
        """Возвращает старейшие N записей. Для drain в порядке очереди."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, method, url, kind, data_json, blob_path, blob_filename, blob_content_type, attempts "
                "FROM outbox ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        result = []
        for r in rows:
            id_, method, url, kind, data_json, blob_path, blob_filename, blob_ct, attempts = r
            item = {
                "id": id_, "method": method, "url": url, "kind": kind,
                "data": json.loads(data_json) if data_json else None,
                "blob_path": blob_path, "blob_filename": blob_filename,
                "blob_content_type": blob_ct, "attempts": attempts,
            }
            result.append(item)
        return result

    def delete(self, id_: int) -> None:
        with self._lock:
            row = self._conn.execute("SELECT blob_path FROM outbox WHERE id = ?", (id_,)).fetchone()
            if row and row[0]:
                try:
                    Path(row[0]).unlink(missing_ok=True)
                except Exception:
                    pass
            self._conn.execute("DELETE FROM outbox WHERE id = ?", (id_,))

    def mark_error(self, id_: int, err: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE outbox SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                (err[:500], id_),
            )

    def size(self) -> int:
        return self._count()
