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
        now = int(time.time())
        with self._conn() as conn:
            try:
                conn.execute("INSERT INTO dedup (k, ts) VALUES (?, ?)", (key, now))
                conn.commit()
                return False
            except sqlite3.IntegrityError:
                return True

    def prune(self) -> int:
        """Prune old records if ttl_seconds > 0. Returns deleted rows."""
        if self.ttl_seconds <= 0:
            return 0
        cutoff = int(time.time()) - self.ttl_seconds
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM dedup WHERE ts < ?", (cutoff,))
            conn.commit()
            return int(cur.rowcount)
