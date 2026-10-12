import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import server

SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, working_directory TEXT, model TEXT, created_at INTEGER,
  last_activity_at INTEGER, title TEXT, hidden INTEGER DEFAULT 0);
CREATE TABLE message_nodes (row_id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, node_id INTEGER,
  chat_message TEXT, created_at INTEGER);
CREATE INDEX idx_message_nodes_session ON message_nodes(session_id);
"""


def msg(role, rid=None, mid="m", model=None, inp=0, out=0, cw=0, cr=0):
    meta = {"request_id": rid, "generation_model": model}
    if role == "assistant" and rid:
        meta["metrics"] = {"input_tokens": inp, "output_tokens": out, "cache_creation_tokens": cw, "cache_read_tokens": cr}
    return json.dumps({"message_id": mid, "role": role, "content": "", "metadata": meta}, separators=(",", ":"))


class AggregationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "s.db"
        c = sqlite3.connect(self.db)
        c.executescript(SCHEMA)
        c.execute("INSERT INTO sessions VALUES ('s1',?,'',100,300,'T',0)", ("C:\\ws\\a",))
        nodes = [
            msg("user", mid="u1"),
            msg("assistant", "r1", "a1", "model-a", inp=4, out=10, cw=100, cr=1000),
            msg("assistant", "r1", "a1", "model-a", inp=4, out=10, cw=100, cr=1000),  # forked duplicate
            msg("tool", mid="t1"),
            msg("assistant", "r2", "a2", "model-b", inp=1, out=5, cw=None, cr=50),
            msg("assistant", "r3", "a3", "model-a", inp=2, out=3, cw=0, cr=7),
            msg("assistant", mid="a4"),  # no metrics
        ]
        c.executemany("INSERT INTO message_nodes (session_id,node_id,chat_message,created_at) VALUES ('s1',?,?,?)",
                      [(i, n, 100 + i) for i, n in enumerate(nodes)])
        c.commit()
        c.close()
        self.conn = server.connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_aggregates_per_model_and_dedupes(self):
        res = {m["model"]: m for m in server.aggregate_session(self.conn, "s1")}
        self.assertEqual(set(res), {"model-a", "model-b"})
        a = res["model-a"]
        self.assertEqual((a["messages"], a["input"], a["output"], a["cached"]), (2, 106, 13, 1007))
        self.assertEqual((a["first"], a["last"]), (101, 105))
        b = res["model-b"]
        self.assertEqual((b["messages"], b["input"], b["output"], b["cached"]), (1, 1, 5, 50))

    def test_refresh_reuses_cache_when_unchanged(self):
        fake = {"s1": {"last_activity": 300, "models": [{"model": "cached"}]}}
        out = server.refresh_sessions(self.conn, fake)
        self.assertEqual(out["s1"]["models"], [{"model": "cached"}])
        fake["s1"]["last_activity"] = 299
        out = server.refresh_sessions(self.conn, fake)
        self.assertEqual({m["model"] for m in out["s1"]["models"]}, {"model-a", "model-b"})

    def test_flatten_one_row_per_model(self):
        rows = server.flatten(server.refresh_sessions(self.conn, {}))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["id"] == "s1" and r["workspace"] == "C:\\ws\\a" for r in rows))


if __name__ == "__main__":
    unittest.main()
