import os
import sqlite3
import time
from contextlib import contextmanager


class DedupStore:
    """SQLite-based de-dup store.

    Key is a string like: "tg:<chat_id>:<msg_id>".
    We persist keys so that container restarts won't re-forward old messages.

    TTL is optional: expired records can be pruned.
    """

    def __init__(self, db_path: str, ttl_seconds: int = 0):
        self.db_path = db_path
        self.ttl_seconds = int(ttl_seconds or 0)
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dedup (
                    k TEXT PRIMARY KEY,
                    ts INTEGER NOT NULL
                )
                """
            )
            conn.commit()

    def seen_or_mark(self, key: str) -> bool:
        """Return True if key already seen; else insert and return False."""
        if self.seen(key):
            return True
        self.mark(key)
        return False

    def seen(self, key: str) -> bool:
        """Return True if key exists in store."""
        with self._conn() as conn:
            cur = conn.execute("SELECT 1 FROM dedup WHERE k = ? LIMIT 1", (key,))
            return cur.fetchone() is not None

    def mark(self, key: str) -> bool:
        """Insert key if absent. Returns True if inserted, False if existed."""
        now = int(time.time())
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO dedup (k, ts) VALUES (?, ?)",
                (key, now),
            )
            conn.commit()
            return int(cur.rowcount) > 0

    def prune(self) -> int:
        """Prune old records if ttl_seconds > 0. Returns deleted rows."""
        if self.ttl_seconds <= 0:
            return 0
        cutoff = int(time.time()) - self.ttl_seconds
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM dedup WHERE ts < ?", (cutoff,))
            conn.commit()
            return int(cur.rowcount)
