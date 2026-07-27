"""SQLite-backed storage for adaptive element fingerprints.

Mirrors Scrapling's approach: when an element is selected with ``auto_save=True``
its structural fingerprint is persisted, keyed by domain and identifier. On a
later ``adaptive=True`` lookup the stored fingerprint is retrieved and compared
against every element in the current document to relocate the element even if
the website's markup changed.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

from typing_extensions import Self

DEFAULT_DB_PATH = Path.home() / ".web_crawler" / "adaptive.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS adaptive_elements (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    domain      TEXT NOT NULL,
    identifier  TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    tag         TEXT,
    text        TEXT,
    url         TEXT,
    created_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (domain, identifier)
);
CREATE INDEX IF NOT EXISTS idx_adaptive_domain ON adaptive_elements(domain);
"""


class AdaptiveStorage:
    """Thread-safe SQLite store for adaptive element fingerprints."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        path = Path(db_path) if db_path else DEFAULT_DB_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        # 实例级锁：不同 AdaptiveStorage 实例（不同 DB）互不阻塞
        self._lock = threading.Lock()
        # check_same_thread=False because crawlers use thread pools.
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def save(
        self,
        domain: str,
        identifier: str,
        fingerprint: str,
        tag: str = "",
        text: str = "",
        url: str = "",
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO adaptive_elements (domain, identifier, fingerprint, tag, text, url) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(domain, identifier) DO UPDATE SET "
                "fingerprint=excluded.fingerprint, tag=excluded.tag, text=excluded.text, url=excluded.url",
                (domain, identifier, fingerprint, tag, text[:500], url),
            )
            self._conn.commit()

    def load(self, domain: str, identifier: str) -> dict[str, Any] | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT fingerprint, tag, text, url FROM adaptive_elements "
                "WHERE domain=? AND identifier=?",
                (domain, identifier),
            )
            row = cur.fetchone()
        if not row:
            return None
        return {"fingerprint": row[0], "tag": row[1], "text": row[2], "url": row[3]}

    def load_all(self, domain: str) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT identifier, fingerprint, tag, text, url FROM adaptive_elements WHERE domain=?",
                (domain,),
            )
            rows = cur.fetchall()
        return [
            {"identifier": r[0], "fingerprint": r[1], "tag": r[2], "text": r[3], "url": r[4]}
            for r in rows
        ]

    def delete(self, domain: str, identifier: str | None = None) -> int:
        with self._lock:
            if identifier is None:
                cur = self._conn.execute("DELETE FROM adaptive_elements WHERE domain=?", (domain,))
            else:
                cur = self._conn.execute(
                    "DELETE FROM adaptive_elements WHERE domain=? AND identifier=?",
                    (domain, identifier),
                )
            self._conn.commit()
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = ["DEFAULT_DB_PATH", "AdaptiveStorage"]
