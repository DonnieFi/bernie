import os
import sqlite3
import tempfile
import unittest


class ShadowSchemaTests(unittest.TestCase):
    def test_shadow_calls_has_new_cols(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        with sqlite3.connect(db_path) as db:
            db.execute(
                """
                CREATE TABLE shadow_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    primary_trace_id TEXT,
                    primary_model TEXT,
                    shadow_model TEXT, prompt_hash TEXT,
                    primary_response TEXT, shadow_response TEXT,
                    channel_id TEXT, actor_id TEXT, user_message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    primary_score_intent REAL,
                    primary_score_tool REAL,
                    shadow_score_intent REAL,
                    shadow_score_tool REAL,
                    judge_ran_at TEXT,
                    tokens_in INTEGER,
                    tokens_out INTEGER,
                    duration_ms INTEGER,
                    cost_usd REAL
                )
                """
            )
            db.execute("ALTER TABLE shadow_calls ADD COLUMN executor TEXT DEFAULT 'native'")
            db.execute("ALTER TABLE shadow_calls ADD COLUMN surface TEXT DEFAULT 'chat'")
            db.execute("ALTER TABLE shadow_calls ADD COLUMN harness_shadow_response TEXT")
            db.execute("ALTER TABLE shadow_calls ADD COLUMN harness_executor TEXT DEFAULT 'smol'")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS shadow_judgments (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id  INTEGER NOT NULL REFERENCES shadow_calls(id),
                    judge_kind  TEXT NOT NULL,
                    winner      TEXT,
                    scores      TEXT,
                    judge_model TEXT,
                    actor_id    TEXT,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_shadow_judgments_request ON shadow_judgments(request_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_shadow_judgments_kind ON shadow_judgments(judge_kind, created_at)")
            db.commit()

        with sqlite3.connect(db_path) as db:
            cols = {row[1] for row in db.execute("PRAGMA table_info(shadow_calls)")}
            self.assertIn("executor", cols, f"executor col missing; got {cols}")
            self.assertIn("surface", cols, f"surface col missing; got {cols}")

    def test_shadow_judgments_table_exists(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        with sqlite3.connect(db_path) as db:
            db.execute(
                """
                CREATE TABLE shadow_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    primary_trace_id TEXT,
                    primary_model TEXT,
                    shadow_model TEXT, prompt_hash TEXT,
                    primary_response TEXT, shadow_response TEXT,
                    channel_id TEXT, actor_id TEXT, user_message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    primary_score_intent REAL,
                    primary_score_tool REAL,
                    shadow_score_intent REAL,
                    shadow_score_tool REAL,
                    judge_ran_at TEXT,
                    tokens_in INTEGER,
                    tokens_out INTEGER,
                    duration_ms INTEGER,
                    cost_usd REAL
                )
                """
            )
            db.execute("ALTER TABLE shadow_calls ADD COLUMN executor TEXT DEFAULT 'native'")
            db.execute("ALTER TABLE shadow_calls ADD COLUMN surface TEXT DEFAULT 'chat'")
            db.execute("ALTER TABLE shadow_calls ADD COLUMN harness_shadow_response TEXT")
            db.execute("ALTER TABLE shadow_calls ADD COLUMN harness_executor TEXT DEFAULT 'smol'")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS shadow_judgments (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id  INTEGER NOT NULL REFERENCES shadow_calls(id),
                    judge_kind  TEXT NOT NULL,
                    winner      TEXT,
                    scores      TEXT,
                    judge_model TEXT,
                    actor_id    TEXT,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_shadow_judgments_request ON shadow_judgments(request_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_shadow_judgments_kind ON shadow_judgments(judge_kind, created_at)")
            db.commit()

            row = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='shadow_judgments'"
            ).fetchone()
            self.assertIsNotNone(row, "shadow_judgments table missing")
            cols = {row[1] for row in db.execute("PRAGMA table_info(shadow_judgments)")}
            for col in ("request_id", "judge_kind", "winner", "scores", "judge_model", "actor_id"):
                self.assertIn(col, cols, f"{col} missing from shadow_judgments")
