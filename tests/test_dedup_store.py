import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app"))

from dedup_store import DedupStore


class DedupStoreTests(unittest.TestCase):
    def test_seen_and_mark(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "dedup.sqlite3")
            store = DedupStore(db_path=db, ttl_seconds=0)

            key = "tg:1:100"
            self.assertFalse(store.seen(key))
            self.assertTrue(store.mark(key))
            self.assertTrue(store.seen(key))
            self.assertFalse(store.mark(key))

    def test_success_mark_semantics(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "dedup.sqlite3")
            store = DedupStore(db_path=db, ttl_seconds=0)

            key = "tg:1:101"
            # Simulate send failure: not marked yet.
            self.assertFalse(store.seen(key))

            # Simulate send success: mark then becomes seen.
            store.mark(key)
            self.assertTrue(store.seen(key))


if __name__ == "__main__":
    unittest.main()
