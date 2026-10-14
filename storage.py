"""SQLite storage for pipensx-catalog.

Manages persistent state across runs, supporting resume, incremental updates,
raw HTML/filelist cache, and catalog entries.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable


def now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


class Storage:
    """SQLite storage manager for RuTracker catalog pipeline."""

    def __init__(self, db_path: str | Path = "catalog.db"):
        self.db_path = str(db_path)
        self._init_db()

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self) -> None:
        """Create tables and indexes if they do not exist."""
        conn = self.get_connection()
        try:
            with conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS topics (
                        topic_id INTEGER PRIMARY KEY,
                        url TEXT NOT NULL,
                        title TEXT,
                        forum_id INTEGER,
                        last_post_time TEXT,
                        status TEXT DEFAULT 'pending',
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS topic_details (
                        topic_id INTEGER PRIMARY KEY,
                        info_hash TEXT,
                        registered_at TEXT,
                        size_bytes INTEGER,
                        raw_html TEXT,
                        filelist_json TEXT,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY(topic_id) REFERENCES topics(topic_id) ON DELETE CASCADE
                    );

                    CREATE TABLE IF NOT EXISTS catalog_entries (
                        topic_id INTEGER PRIMARY KEY,
                        title_id TEXT,
                        entry_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY(topic_id) REFERENCES topics(topic_id) ON DELETE CASCADE
                    );

                    CREATE INDEX IF NOT EXISTS idx_topics_forum_id ON topics(forum_id);
                    CREATE INDEX IF NOT EXISTS idx_topics_status ON topics(status);
                    CREATE INDEX IF NOT EXISTS idx_topic_details_info_hash ON topic_details(info_hash);
                    CREATE INDEX IF NOT EXISTS idx_catalog_entries_title_id ON catalog_entries(title_id);
                """)
        finally:
            conn.close()

    def upsert_topic(
        self,
        topic_id: int,
        url: str,
        title: str | None = None,
        forum_id: int | None = None,
        last_post_time: str | None = None,
        status: str = "pending",
    ) -> None:
        now = now_utc_iso()
        conn = self.get_connection()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO topics (topic_id, url, title, forum_id, last_post_time, status, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(topic_id) DO UPDATE SET
                        url = excluded.url,
                        title = COALESCE(excluded.title, topics.title),
                        forum_id = COALESCE(excluded.forum_id, topics.forum_id),
                        last_post_time = COALESCE(excluded.last_post_time, topics.last_post_time),
                        updated_at = excluded.updated_at
                    """,
                    (topic_id, url, title, forum_id, last_post_time, status, now),
                )
        finally:
            conn.close()

    def upsert_topics_batch(self, topics_list: Iterable[dict[str, Any]]) -> int:
        now = now_utc_iso()
        count = 0
        conn = self.get_connection()
        try:
            with conn:
                for item in topics_list:
                    conn.execute(
                        """
                        INSERT INTO topics (topic_id, url, title, forum_id, last_post_time, status, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(topic_id) DO UPDATE SET
                            url = excluded.url,
                            title = COALESCE(excluded.title, topics.title),
                            forum_id = COALESCE(excluded.forum_id, topics.forum_id),
                            last_post_time = COALESCE(excluded.last_post_time, topics.last_post_time),
                            updated_at = excluded.updated_at
                        """,
                        (
                            item["topic_id"],
                            item["url"],
                            item.get("title"),
                            item.get("forum_id"),
                            item.get("last_post_time"),
                            item.get("status", "pending"),
                            now,
                        ),
                    )
                    count += 1
        finally:
            conn.close()
        return count

    def get_topic(self, topic_id: int) -> dict[str, Any] | None:
        conn = self.get_connection()
        try:
            cur = conn.execute("SELECT * FROM topics WHERE topic_id = ?", (topic_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_pending_topics(self, limit: int | None = None, forum_id: int | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM topics WHERE status = 'pending'"
        params: list[Any] = []
        if forum_id is not None:
            query += " AND forum_id = ?"
            params.append(forum_id)
        query += " ORDER BY topic_id ASC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        conn = self.get_connection()
        try:
            cur = conn.execute(query, tuple(params))
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def set_topic_status(self, topic_id: int, status: str) -> None:
        now = now_utc_iso()
        conn = self.get_connection()
        try:
            with conn:
                conn.execute(
                    "UPDATE topics SET status = ?, updated_at = ? WHERE topic_id = ?",
                    (status, now, topic_id),
                )
        finally:
            conn.close()

    def save_topic_details(
        self,
        topic_id: int,
        info_hash: str | None = None,
        registered_at: str | None = None,
        size_bytes: int | None = None,
        raw_html: str | None = None,
        filelist_json: str | list[Any] | None = None,
    ) -> None:
        now = now_utc_iso()
        if isinstance(filelist_json, (list, dict)):
            filelist_str = json.dumps(filelist_json, ensure_ascii=False)
        else:
            filelist_str = filelist_json

        conn = self.get_connection()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO topic_details (topic_id, info_hash, registered_at, size_bytes, raw_html, filelist_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(topic_id) DO UPDATE SET
                        info_hash = COALESCE(excluded.info_hash, topic_details.info_hash),
                        registered_at = COALESCE(excluded.registered_at, topic_details.registered_at),
                        size_bytes = COALESCE(excluded.size_bytes, topic_details.size_bytes),
                        raw_html = COALESCE(excluded.raw_html, topic_details.raw_html),
                        filelist_json = COALESCE(excluded.filelist_json, topic_details.filelist_json),
                        updated_at = excluded.updated_at
                    """,
                    (topic_id, info_hash, registered_at, size_bytes, raw_html, filelist_str, now),
                )
        finally:
            conn.close()

    def get_topic_details(self, topic_id: int) -> dict[str, Any] | None:
        conn = self.get_connection()
        try:
            cur = conn.execute("SELECT * FROM topic_details WHERE topic_id = ?", (topic_id,))
            row = cur.fetchone()
            if not row:
                return None
            res = dict(row)
            if res.get("filelist_json"):
                try:
                    res["filelist"] = json.loads(res["filelist_json"])
                except Exception:
                    res["filelist"] = []
            return res
        finally:
            conn.close()

    def save_catalog_entry(self, topic_id: int, title_id: str | None, entry_dict: dict[str, Any]) -> None:
        now = now_utc_iso()
        entry_json = json.dumps(entry_dict, ensure_ascii=False, sort_keys=True)
        conn = self.get_connection()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO catalog_entries (topic_id, title_id, entry_json, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(topic_id) DO UPDATE SET
                        title_id = excluded.title_id,
                        entry_json = excluded.entry_json,
                        updated_at = excluded.updated_at
                    """,
                    (topic_id, title_id.upper() if title_id else None, entry_json, now),
                )
        finally:
            conn.close()

    def get_catalog_entry(self, topic_id: int) -> dict[str, Any] | None:
        conn = self.get_connection()
        try:
            cur = conn.execute("SELECT entry_json FROM catalog_entries WHERE topic_id = ?", (topic_id,))
            row = cur.fetchone()
            if not row:
                return None
            return json.loads(row["entry_json"])
        finally:
            conn.close()

    def get_all_catalog_entries(self) -> list[dict[str, Any]]:
        conn = self.get_connection()
        try:
            cur = conn.execute("SELECT entry_json FROM catalog_entries ORDER BY topic_id ASC")
            return [json.loads(row["entry_json"]) for row in cur.fetchall()]
        finally:
            conn.close()

    def get_stats(self) -> dict[str, Any]:
        conn = self.get_connection()
        try:
            total_topics = conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
            status_counts: dict[str, int] = {}
            for row in conn.execute("SELECT status, COUNT(*) FROM topics GROUP BY status"):
                status_counts[row[0]] = row[1]

            total_entries = conn.execute("SELECT COUNT(*) FROM catalog_entries").fetchone()[0]
            with_tid = conn.execute(
                "SELECT COUNT(*) FROM catalog_entries WHERE title_id IS NOT NULL AND title_id != ''"
            ).fetchone()[0]
            without_tid = total_entries - with_tid

            total_bytes = conn.execute(
                "SELECT SUM(size_bytes) FROM topic_details"
            ).fetchone()[0] or 0

            return {
                "total_topics": total_topics,
                "status_counts": status_counts,
                "total_catalog_entries": total_entries,
                "entries_with_title_id": with_tid,
                "entries_without_title_id": without_tid,
                "total_bytes": total_bytes,
            }
        finally:
            conn.close()
