import sqlite3
import tempfile
import unittest


class TestShadowSchemaCompat(unittest.TestCase):
    def test_existing_shadow_rows_survive_migration(self):
        """Ensure old shadow_calls rows (without new cols) still work after migration."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        with sqlite3.connect(db_path) as db:
            # Create old-style shadow_calls row without the new columns.
            db.execute("""
                CREATE TABLE shadow_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    primary_trace_id TEXT,
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
            """)
            db.execute(
                "INSERT INTO shadow_calls (shadow_model, prompt_hash, primary_response, shadow_response) VALUES (?,?,?,?)",
                ("haiku", "abc123", "primary resp", "shadow resp")
            )
            db.commit()

            # Apply the same migration shape as init_db() for the shadow tables.
            db.execute("ALTER TABLE shadow_calls ADD COLUMN executor TEXT DEFAULT 'native'")
            db.execute("ALTER TABLE shadow_calls ADD COLUMN primary_model TEXT")
            db.execute("ALTER TABLE shadow_calls ADD COLUMN surface TEXT DEFAULT 'chat'")
            db.execute("ALTER TABLE shadow_calls ADD COLUMN harness_shadow_response TEXT")
            db.execute("ALTER TABLE shadow_calls ADD COLUMN harness_executor TEXT DEFAULT 'smol'")
            db.execute("""
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
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_shadow_judgments_request ON shadow_judgments(request_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_shadow_judgments_kind ON shadow_judgments(judge_kind, created_at)")
            db.commit()

            row = db.execute(
                "SELECT executor, primary_model, surface, harness_shadow_response FROM shadow_calls WHERE id=1"
            ).fetchone()
            self.assertEqual(row[0], "native", f"executor default wrong: {row[0]}")
            self.assertIsNone(row[1], f"primary_model should default to NULL: {row[1]}")
            self.assertEqual(row[2], "chat", f"surface default wrong: {row[2]}")
            self.assertIsNone(row[3], f"harness_shadow_response should be NULL: {row[3]}")
