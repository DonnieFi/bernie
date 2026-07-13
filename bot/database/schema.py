"""database.schema — domain module (8lx.1 Phase 1)."""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

import sqlite_async

from database.conn import (
    HFX,
    _db_conn,
    _get_connection,
    _get_init_lock,
    _get_lock,
    _pkg,
    _resolve_db_path,
    close_db,
    db_conn,
    wal_checkpoint_passive,
)

log = logging.getLogger("database.schema")

# ── Shared DDL fragments (8lx.4) ────────────────────────────────────────────
# Single source used by greenfield executescript AND brownfield ensure_*.
# When adding schema: put DDL here, reference in greenfield string, and add
# ensure_* + MIGRATION_SPECS entry so empty + existing DBs stay in sync.

CONVERSATION_HISTORY_FTS_DDL = """
-- 5hy.11 / Hermes U2: FTS5 over conversation_history (session_search)
CREATE VIRTUAL TABLE IF NOT EXISTS conversation_history_fts USING fts5(
    content,
    content='conversation_history',
    content_rowid='id',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS conversation_history_ai AFTER INSERT ON conversation_history BEGIN
    INSERT INTO conversation_history_fts(rowid, content)
    VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS conversation_history_ad AFTER DELETE ON conversation_history BEGIN
    INSERT INTO conversation_history_fts(conversation_history_fts, rowid, content)
    VALUES('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS conversation_history_au AFTER UPDATE OF content ON conversation_history BEGIN
    INSERT INTO conversation_history_fts(conversation_history_fts, rowid, content)
    VALUES('delete', old.id, old.content);
    INSERT INTO conversation_history_fts(rowid, content)
    VALUES (new.id, new.content);
END;
"""


async def ensure_conversation_history_fts() -> None:
    """Brownfield: create conversation_history FTS5 + triggers if missing (5hy.11)."""
    async with _db_conn() as db:
        await db.executescript(CONVERSATION_HISTORY_FTS_DDL)
        # One-time rebuild when index empty but history has rows
        try:
            empty = await (await db.execute("SELECT COUNT(*) FROM conversation_history_fts")).fetchone()
            rows = await (await db.execute("SELECT COUNT(*) FROM conversation_history")).fetchone()
            if empty and empty[0] == 0 and rows and rows[0] > 0:
                log.info("conversation_history_fts empty — one-time rebuild")
                await db.execute(
                    "INSERT INTO conversation_history_fts(conversation_history_fts) VALUES('rebuild')"
                )
        except Exception as e:
            log.warning("conversation_history_fts rebuild skipped: %s", e)
        await db.commit()


# c79.2 — composite index for activity_log filters (event_type + time range)
ACTIVITY_LOG_EVENT_TIME_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_activity_log_event_type_logged_at
    ON activity_log(event_type, logged_at);
"""


async def ensure_activity_log_event_time_index() -> None:
    """Brownfield: (event_type, logged_at) composite index (c79.2 option a).

    If activity_log is missing (minimal migration-test seeds), still apply a
    no-op-safe CREATE INDEX IF NOT EXISTS only when the table exists. Callers
    that record v9 should re-run ensure after full schema exists; production
    always has activity_log from greenfield or prior migrations.
    """
    async with _db_conn() as db:
        if not await _table_exists(db, "activity_log"):
            # Create minimal stub so later inserts + index stay consistent on
            # partial test DBs that only pre-seed conversation_history.
            await db.execute(
                """CREATE TABLE IF NOT EXISTS activity_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    logged_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    event_type TEXT NOT NULL,
                    description TEXT,
                    person_id TEXT,
                    metadata TEXT
                )"""
            )
        await db.executescript(ACTIVITY_LOG_EVENT_TIME_INDEX_DDL)
        await db.commit()


async def _col_exists(db, table: str, col: str) -> bool:
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        async for row in cur:
            if row[1] == col:
                return True
        return False

async def _db_already_initialized() -> bool:
    """Fast read-only check — bypasses _get_lock() so startup never queues behind writers."""
    try:
        async with sqlite_async.connect(_resolve_db_path(), timeout=5.0) as db:
            cur = await db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_history'"
            )
            return await cur.fetchone() is not None
    except Exception:
        return False

async def init_db():
    if await _db_already_initialized():
        from db_migrations import run_schema_migrations
        import sys
        from db_binding import bind_database

        await run_schema_migrations()
        # Same package bind as greenfield (tests patch database.* on package).
        bind_database(sys.modules["database"])
        return  # Schema already set up; skip the exclusive-lock executescript
    async with _db_conn() as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS conversation_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id  INTEGER NOT NULL,
                role        TEXT NOT NULL,
                content     TEXT NOT NULL,
                created_at  TEXT NOT NULL
            );

            """ + CONVERSATION_HISTORY_FTS_DDL + """

            CREATE TABLE IF NOT EXISTS sent_reminders (
                event_id    TEXT NOT NULL,
                remind_min  INTEGER NOT NULL,
                sent_at     TEXT NOT NULL,
                PRIMARY KEY (event_id, remind_min)
            );

            CREATE TABLE IF NOT EXISTS rsvps (
                event_id    TEXT NOT NULL,
                discord_id  INTEGER NOT NULL,
                name        TEXT NOT NULL,
                status      TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                PRIMARY KEY (event_id, discord_id)
            );

            CREATE TABLE IF NOT EXISTS message_event_map (
                message_id   INTEGER PRIMARY KEY,
                event_id     TEXT NOT NULL,
                event_title  TEXT,
                message_type TEXT NOT NULL DEFAULT 'event'
            );

            CREATE TABLE IF NOT EXISTS unified_tasks (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                type                TEXT NOT NULL DEFAULT 'chore',   -- chore|research|bernie|code|system
                status              TEXT NOT NULL DEFAULT 'todo',    -- triage|todo|ready|running|blocked|done|archived
                title               TEXT NOT NULL,
                details             TEXT,
                horizon             TEXT,                            -- 'YYYY-MM' | 'someday'
                assigned_to         TEXT,                            -- namespaced id; NULL = open to claim
                assigned_by         TEXT NOT NULL,
                acceptable_assignees TEXT NOT NULL DEFAULT '[]',     -- JSON array
                visibility          TEXT NOT NULL DEFAULT 'family',  -- family|internal
                priority            TEXT NOT NULL DEFAULT 'normal',
                urgency             TEXT,                            -- low|normal|high (nullable; Wave B)
                is_recurring        INTEGER NOT NULL DEFAULT 0,
                due_at              TEXT,
                snooze_until        TEXT,
                snooze_count        INTEGER NOT NULL DEFAULT 0,
                escalated_at        TEXT,
                last_prompted_at    TEXT,
                remind_visibility   TEXT NOT NULL DEFAULT 'private',
                remind_channel_id   INTEGER,
                category            TEXT DEFAULT 'Task',
                requires_approval   INTEGER NOT NULL DEFAULT 0,
                approver_id         TEXT,
                approved_at         TEXT,
                completion_note     TEXT,
                payload             TEXT NOT NULL DEFAULT '{}',
                heartbeat           TEXT,
                error               TEXT,
                current_run_id      TEXT,
                max_runtime_seconds INTEGER,
                max_retries         INTEGER NOT NULL DEFAULT 0,
                workspace           TEXT,
                created_at          TEXT NOT NULL,
                updated_at          TEXT,
                completed_at        TEXT
            );

            CREATE TABLE IF NOT EXISTS task_links (
                parent_id INTEGER NOT NULL,
                child_id  INTEGER NOT NULL,
                PRIMARY KEY (parent_id, child_id)
            );

            CREATE TABLE IF NOT EXISTS task_executions (
                execution_id TEXT PRIMARY KEY,
                task_id      INTEGER NOT NULL,
                status       TEXT NOT NULL,        -- active|completed|blocked|crashed
                started_at   TEXT NOT NULL,
                completed_at TEXT,
                logs         TEXT,
                metrics      TEXT
            );

            CREATE TABLE IF NOT EXISTS task_events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id         INTEGER NOT NULL,
                event_type      TEXT NOT NULL,
                actor_person_id TEXT,
                metadata        TEXT,
                created_at      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS automations (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                title           TEXT NOT NULL,
                message         TEXT NOT NULL,
                person_id       TEXT NOT NULL,
                audience_scope  TEXT NOT NULL DEFAULT 'self',
                schedule_kind   TEXT NOT NULL DEFAULT 'weekly',
                schedule_payload TEXT NOT NULL DEFAULT '{}',
                timezone        TEXT NOT NULL,
                next_run_at     TEXT,
                is_active       INTEGER NOT NULL DEFAULT 1,
                created_by      TEXT NOT NULL,
                created_at      TEXT NOT NULL,
                updated_at      TEXT,
                last_triggered_at TEXT
            );

            -- Per-person preferences (canonical)
            CREATE TABLE IF NOT EXISTS person_preferences (
                person_id           TEXT PRIMARY KEY,
                discord_id          INTEGER,
                reminders_enabled   BOOLEAN NOT NULL DEFAULT 1,
                dm_mode             BOOLEAN NOT NULL DEFAULT 1,
                reminder_minutes    INTEGER NOT NULL DEFAULT 30,
                preferred_channels  TEXT DEFAULT 'discord',
                quiet_hours_start   TEXT,
                quiet_hours_end     TEXT,
                updated_at          TEXT
            );

            CREATE TABLE IF NOT EXISTS meals (
                date        TEXT NOT NULL,
                meal_type   TEXT NOT NULL,
                dish        TEXT NOT NULL,
                notes       TEXT,
                PRIMARY KEY (date, meal_type)
            );

            CREATE TABLE IF NOT EXISTS groceries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                item        TEXT NOT NULL,
                category    TEXT NOT NULL DEFAULT 'Other',
                added_at    TEXT NOT NULL
            );

            -- Replaces memory_service.py JSON file
            CREATE TABLE IF NOT EXISTS memory_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                logged_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
                person_id   TEXT NOT NULL,
                event_type  TEXT NOT NULL,   -- acknowledged | missed | pattern
                event_title TEXT,
                metadata    TEXT             -- JSON blob
            );

            -- Who's home right now (single row per person, upserted)
            CREATE TABLE IF NOT EXISTS presence_current (
                person_id       TEXT PRIMARY KEY,
                is_home         BOOLEAN NOT NULL,
                last_seen       DATETIME,
                last_arrived    DATETIME,
                last_departed   DATETIME,
                last_home_signal REAL
            );

            -- Presence history for patterns
            CREATE TABLE IF NOT EXISTS presence_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                logged_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
                person_id   TEXT NOT NULL,
                event       TEXT NOT NULL,   -- arrived | departed
                device_mac  TEXT
            );

            -- HA entities Bernie manages
            CREATE TABLE IF NOT EXISTS ha_devices (
                entity_id   TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                room        TEXT,
                type        TEXT,
                last_state  TEXT,
                last_updated DATETIME
            );

            -- All notifications Bernie has sent
            CREATE TABLE IF NOT EXISTS notification_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                sent_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
                recipient_id TEXT NOT NULL,
                channel      TEXT NOT NULL,
                message      TEXT NOT NULL,
                success      BOOLEAN NOT NULL,
                error        TEXT
            );

            -- Full Bernie activity log
            CREATE TABLE IF NOT EXISTS activity_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                logged_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
                event_type  TEXT NOT NULL,
                description TEXT,
                person_id   TEXT,
                metadata    TEXT
            );

            -- FTS5 full-text search substrate over activity_log (Phase 28 Wave 2c)
            -- External content table keeps storage minimal.
            CREATE VIRTUAL TABLE IF NOT EXISTS activity_log_fts USING fts5(
                description,
                metadata,
                content='activity_log',
                content_rowid='id',
                tokenize='unicode61'
            );

            -- Keep FTS5 index in sync automatically
            CREATE TRIGGER IF NOT EXISTS activity_log_ai AFTER INSERT ON activity_log BEGIN
                INSERT INTO activity_log_fts(rowid, description, metadata)
                VALUES (new.id, new.description, new.metadata);
            END;

            CREATE TRIGGER IF NOT EXISTS activity_log_ad AFTER DELETE ON activity_log BEGIN
                INSERT INTO activity_log_fts(activity_log_fts, rowid, description, metadata)
                VALUES('delete', old.id, old.description, old.metadata);
            END;

            CREATE TRIGGER IF NOT EXISTS activity_log_au AFTER UPDATE ON activity_log BEGIN
                INSERT INTO activity_log_fts(activity_log_fts, rowid, description, metadata)
                VALUES('delete', old.id, old.description, old.metadata);
                INSERT INTO activity_log_fts(rowid, description, metadata)
                VALUES (new.id, new.description, new.metadata);
            END;

            -- FTS5 rebuild is now conditional (see ensure_activity_log_fts below)
            -- to avoid startup cost as activity_log grows.

            CREATE TABLE IF NOT EXISTS chat_threads (
                id          TEXT PRIMARY KEY,
                title       TEXT NOT NULL,
                person_id   TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                is_mined    BOOLEAN NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS chat_messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id   TEXT NOT NULL,
                role        TEXT NOT NULL,
                content     TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                FOREIGN KEY (thread_id) REFERENCES chat_threads(id) ON DELETE CASCADE
            );

            -- Claude token spend
            CREATE TABLE IF NOT EXISTS token_usage (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                logged_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
                input_tokens    INTEGER,
                output_tokens   INTEGER,
                model           TEXT,
                conversation_id TEXT,
                triggered_by    TEXT,   -- discord | web | scheduler
                surface         TEXT DEFAULT 'discord',   -- discord | shadow | shadow_harness
                cache_creation_tokens INTEGER DEFAULT 0,
                cache_read_tokens     INTEGER DEFAULT 0,
                session_id      TEXT
            );

            -- Weather cache (avoid redundant API calls)
            CREATE TABLE IF NOT EXISTS weather_cache (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                fetched_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                source      TEXT,
                data        TEXT,    -- JSON blob
                confidence  TEXT     -- High | Medium | Low
            );

            -- Permanent cache for resolved city lookups
            CREATE TABLE IF NOT EXISTS weather_location_cache (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                query_normalized   TEXT NOT NULL UNIQUE,
                display_name       TEXT NOT NULL,
                lat               REAL NOT NULL,
                lon               REAL NOT NULL,
                country_code      TEXT,
                country           TEXT,
                admin1            TEXT,
                timezone          TEXT,
                source            TEXT,
                created_at        TEXT NOT NULL
            );

            -- Per-person insights from nightly digest
            CREATE TABLE IF NOT EXISTS family_insights (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                generated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                person_id   TEXT NOT NULL,
                insight     TEXT NOT NULL,
                source_date TEXT,
                is_permanent INTEGER DEFAULT 0,
                expires_at  DATETIME
            );

            -- Deduplication log for nightly digest
            CREATE TABLE IF NOT EXISTS digest_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                digest_date TEXT NOT NULL,
                completed_at DATETIME,
                UNIQUE(digest_date)
            );
            CREATE TABLE IF NOT EXISTS pending_drafts (
                draft_id      TEXT PRIMARY KEY,
                summary       TEXT NOT NULL,
                start_time    TEXT NOT NULL,
                end_time      TEXT NOT NULL,
                attendees     TEXT DEFAULT '[]',
                location      TEXT DEFAULT '',
                description   TEXT DEFAULT '',
                remind_minutes TEXT,
                posted        INTEGER DEFAULT 0,
                created_at    TEXT DEFAULT (datetime('now'))
            );

            -- Identity graph tables (Phase 30)
            CREATE TABLE IF NOT EXISTS identity_nodes (
                node_id      TEXT PRIMARY KEY,
                canonical_id TEXT UNIQUE NOT NULL,
                type         TEXT NOT NULL DEFAULT 'person',
                metadata     TEXT NOT NULL DEFAULT '{}',
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS identity_aliases (
                alias       TEXT PRIMARY KEY,
                node_id     TEXT NOT NULL REFERENCES identity_nodes(node_id) ON DELETE CASCADE,
                confidence  REAL NOT NULL DEFAULT 0.95,
                source      TEXT NOT NULL,
                verified    INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS identity_edges (
                edge_id     TEXT PRIMARY KEY,
                source_id   TEXT NOT NULL REFERENCES identity_nodes(node_id) ON DELETE CASCADE,
                target_id   TEXT NOT NULL REFERENCES identity_nodes(node_id) ON DELETE CASCADE,
                rel_type    TEXT NOT NULL,
                confidence  REAL NOT NULL DEFAULT 0.7,
                evidence    TEXT,
                verified    INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS unresolved_entities (
                entity_key       TEXT NOT NULL,
                type             TEXT NOT NULL,
                first_seen       TEXT NOT NULL,
                last_seen        TEXT NOT NULL,
                count            INTEGER NOT NULL DEFAULT 1,
                context_snapshot TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (entity_key, type)
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS identity_search
                USING fts5(alias, node_id UNINDEXED);

            -- Shadow eval call log (Phase 25)
            CREATE TABLE IF NOT EXISTS shadow_calls (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                primary_trace_id      TEXT,
                primary_model         TEXT,
                shadow_model          TEXT NOT NULL,
                prompt_hash           TEXT NOT NULL,
                primary_response      TEXT,
                shadow_response       TEXT,
                channel_id            TEXT,
                actor_id              TEXT,
                created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                primary_score_intent  REAL,
                primary_score_tool    REAL,
                shadow_score_intent   REAL,
                shadow_score_tool     REAL,
                judge_ran_at          TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_shadow_calls_created_at ON shadow_calls(created_at);
            CREATE INDEX IF NOT EXISTS idx_shadow_calls_judge_ran_at ON shadow_calls(judge_ran_at);

            CREATE TABLE IF NOT EXISTS pending_hitl (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                status              TEXT NOT NULL,   -- pending | approved | denied | expired
                tool_name           TEXT NOT NULL,
                args_json           TEXT NOT NULL,
                ctx_json            TEXT NOT NULL,
                reasoning           TEXT,
                requested_at        TEXT NOT NULL,   -- UTC Z
                expires_at          TEXT NOT NULL,
                decided_at          TEXT,
                decided_by          TEXT,
                result_preview      TEXT,
                notify_message_ids  TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_pending_hitl_status_expires
                ON pending_hitl (status, expires_at);
        """)
        
        await db.execute("""
            ALTER TABLE shadow_calls ADD COLUMN executor TEXT DEFAULT 'native'
        """) if not await _col_exists(db, "shadow_calls", "executor") else None

        await db.execute("""
            ALTER TABLE shadow_calls ADD COLUMN primary_model TEXT
        """) if not await _col_exists(db, "shadow_calls", "primary_model") else None

        await db.execute("""
            ALTER TABLE shadow_calls ADD COLUMN surface TEXT DEFAULT 'chat'
        """) if not await _col_exists(db, "shadow_calls", "surface") else None

        await db.execute("ALTER TABLE shadow_calls ADD COLUMN harness_shadow_response TEXT") \
            if not await _col_exists(db, "shadow_calls", "harness_shadow_response") else None
        
        await db.execute("ALTER TABLE shadow_calls ADD COLUMN harness_executor TEXT DEFAULT 'smol'") \
            if not await _col_exists(db, "shadow_calls", "harness_executor") else None

        await db.execute("""
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
        await db.execute("CREATE INDEX IF NOT EXISTS idx_shadow_judgments_request ON shadow_judgments(request_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_shadow_judgments_kind ON shadow_judgments(judge_kind, created_at)")

        # Migration: Automatically add last_home_signal to existing tables
        try:
            await db.execute("ALTER TABLE presence_current ADD COLUMN last_home_signal REAL")
        except Exception:
            pass

        try:
            await db.execute("ALTER TABLE pending_hitl ADD COLUMN notify_message_ids TEXT")
        except Exception:
            pass
            
        try:
            await db.execute("ALTER TABLE message_event_map ADD COLUMN event_title TEXT")
        except Exception:
            pass

        try:
            await db.execute("ALTER TABLE message_event_map ADD COLUMN message_type TEXT NOT NULL DEFAULT 'event'")
        except Exception:
            pass

        try:
            await db.execute("ALTER TABLE pending_notifications ADD COLUMN event_id TEXT")
        except Exception:
            pass

        try:
            await db.execute("ALTER TABLE pending_notifications ADD COLUMN message_type TEXT")
        except Exception:
            pass

        for col, typedef in (
            ("reply_to_gmail_id", "TEXT"),
            ("thread_id", "TEXT"),
        ):
            try:
                await db.execute(f"ALTER TABLE email_pending ADD COLUMN {col} {typedef}")
            except Exception:
                pass

        # Fix Mom's running-club routine: add explicit day constraint + reset confidence.
        # Read the row first and only patch if no day_of_week key exists — this fires even
        # if the WAL had already written a value other than '{}' on a prior boot.
        try:
            import json as _json
            _r49 = await (await db.execute("SELECT pattern_json FROM routines WHERE id=49")).fetchone()
            if _r49:
                try:
                    _pj49 = _json.loads(_r49[0]) if _r49[0] else {}
                except Exception:
                    _pj49 = {}
                if isinstance(_pj49, dict) and "day_of_week" not in _pj49 and "days_of_week" not in _pj49:
                    await db.execute(
                        "UPDATE routines SET pattern_json = ?, confidence = 0.75 WHERE id = 49",
                        (_json.dumps({"day_of_week": "wednesday"}),)
                    )
        except Exception:
            pass

        # Clean up legacy routines where pattern_json was stored as a bare JSON string
        # rather than a JSON object (emitted by an old consolidation run). Reset to '{}'
        # so collect_nudge_candidates sees an empty dict and falls back gracefully.
        try:
            import json as _json
            _bad = await (await db.execute(
                "SELECT id, pattern_json FROM routines WHERE pattern_json IS NOT NULL AND pattern_json != '{}'"
            )).fetchall()
            for _br in _bad:
                try:
                    _parsed = _json.loads(_br[1])
                    if not isinstance(_parsed, dict):
                        await db.execute("UPDATE routines SET pattern_json = '{}' WHERE id = ?", (_br[0],))
                except Exception:
                    await db.execute("UPDATE routines SET pattern_json = '{}' WHERE id = ?", (_br[0],))
        except Exception:
            pass

        for stmt in [
            "ALTER TABLE tasks ADD COLUMN requires_approval INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE tasks ADD COLUMN approver_person_id TEXT",
            "ALTER TABLE tasks ADD COLUMN snooze_until TEXT",
            "ALTER TABLE tasks ADD COLUMN snooze_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE tasks ADD COLUMN escalated_at TEXT",
            "ALTER TABLE tasks ADD COLUMN last_prompted_at TEXT",
            "ALTER TABLE tasks ADD COLUMN remind_visibility TEXT NOT NULL DEFAULT 'private'",
            "ALTER TABLE tasks ADD COLUMN remind_channel_id INTEGER",
            "ALTER TABLE tasks ADD COLUMN updated_at TEXT",
            "ALTER TABLE automations ADD COLUMN audience_scope TEXT NOT NULL DEFAULT 'self'",
            "ALTER TABLE automations ADD COLUMN schedule_kind TEXT NOT NULL DEFAULT 'weekly'",
            "ALTER TABLE automations ADD COLUMN schedule_payload TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE automations ADD COLUMN next_run_at TEXT",
            "ALTER TABLE automations ADD COLUMN updated_at TEXT",
            "ALTER TABLE tasks ADD COLUMN category TEXT DEFAULT 'Task'",
            "ALTER TABLE tasks ADD COLUMN is_recurring INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE tasks ADD COLUMN in_progress INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE tasks ADD COLUMN priority TEXT NOT NULL DEFAULT 'normal'",
        ]:
            try:
                await db.execute(stmt)
            except Exception:
                pass

        # Migration: rename display-name person_id keys to canonical IDs
        # Old rows used "Dad"/"Mom"/"Child1" — new code uses "dad"/"mom"/"child1"
        _display_to_canonical = {"Dad": "dad", "Mom": "mom", "Child1": "child1", "Child2": "child2"}
        for old, new in _display_to_canonical.items():
            # Only migrate if old key exists and new key does not
            cur = await db.execute("SELECT 1 FROM presence_current WHERE person_id=?", (old,))
            if await cur.fetchone():
                new_exists = await (await db.execute("SELECT 1 FROM presence_current WHERE person_id=?", (new,))).fetchone()
                if not new_exists:
                    await db.execute("UPDATE presence_current SET person_id=? WHERE person_id=?", (new, old))
                    log.info(f"DB migration: presence_current {old!r} → {new!r}")
                else:
                    await db.execute("DELETE FROM presence_current WHERE person_id=?", (old,))
                    log.info(f"DB migration: removed duplicate presence_current row for {old!r}")

        # Migration: canonicalize old display-name IDs in unified_tasks
        for old, new in _display_to_canonical.items():
            await db.execute(
                "UPDATE unified_tasks SET assigned_to=? WHERE LOWER(assigned_to)=LOWER(?)", (new, old))
            await db.execute(
                "UPDATE unified_tasks SET assigned_by=? WHERE LOWER(assigned_by)=LOWER(?)", (new, old))
            await db.execute(
                "UPDATE unified_tasks SET approver_id=? WHERE LOWER(approver_id)=LOWER(?)", (new, old))
        log.info("DB migration: task identity canonicalization complete.")

        # Indices for tables that grow unboundedly and are queried by timestamp
        await db.executescript("""
            CREATE INDEX IF NOT EXISTS idx_activity_log_logged_at     ON activity_log(logged_at);
            CREATE INDEX IF NOT EXISTS idx_activity_log_event_type_logged_at
                ON activity_log(event_type, logged_at);
            CREATE INDEX IF NOT EXISTS idx_notification_log_sent_at   ON notification_log(sent_at);
            CREATE INDEX IF NOT EXISTS idx_token_usage_logged_at      ON token_usage(logged_at);
            CREATE INDEX IF NOT EXISTS idx_conversation_channel       ON conversation_history(channel_id);
            CREATE INDEX IF NOT EXISTS idx_chat_messages_thread       ON chat_messages(thread_id);
            CREATE INDEX IF NOT EXISTS idx_automations_owner_active   ON automations(person_id, is_active, schedule_kind);
            CREATE INDEX IF NOT EXISTS idx_unified_assigned_to_status   ON unified_tasks(assigned_to, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_unified_assigned_by_status   ON unified_tasks(assigned_by, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_unified_due_pending          ON unified_tasks(status, due_at, snooze_until);
            CREATE INDEX IF NOT EXISTS idx_token_usage_logged_at      ON token_usage(logged_at);
            CREATE INDEX IF NOT EXISTS idx_task_events_task_id        ON task_events(task_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_automations_next_run       ON automations(is_active, next_run_at);

            CREATE TABLE IF NOT EXISTS pending_notifications (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                recipient_id    TEXT NOT NULL,
                message         TEXT,
                title           TEXT,
                embed_json      TEXT,
                urgency         TEXT DEFAULT 'normal',
                created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_pending_notifications_recipient ON pending_notifications(recipient_id);

            CREATE TABLE IF NOT EXISTS cognitive_tasks (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                type         TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'queued',
                payload      TEXT NOT NULL DEFAULT '{}',
                result       TEXT,
                priority     INTEGER NOT NULL DEFAULT 0,
                run_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                started_at   TEXT,
                completed_at TEXT,
                heartbeat    TEXT,
                retry_count  INTEGER NOT NULL DEFAULT 0,
                max_retries  INTEGER NOT NULL DEFAULT 3,
                actor_id     TEXT,
                channel_id   TEXT,
                error        TEXT,
                model_used   TEXT,
                tokens_in    INTEGER,
                tokens_out   INTEGER,
                duration_ms  INTEGER,
                gpu_ms       INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_cognitive_tasks_status ON cognitive_tasks(status, run_at);

            CREATE TABLE IF NOT EXISTS semantic_observations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id   TEXT NOT NULL,
                observation TEXT NOT NULL,
                source      TEXT NOT NULL DEFAULT 'claude',
                confidence  REAL NOT NULL DEFAULT 0.8,
                created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                expires_at  TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_semantic_observations_person ON semantic_observations(person_id, created_at);

            CREATE TABLE IF NOT EXISTS tomorrow_context (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                for_date        TEXT NOT NULL,
                person_id       TEXT,
                summary         TEXT NOT NULL,
                confidence      REAL,
                source_task_id  INTEGER,
                created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                UNIQUE(for_date, person_id)
            );
            CREATE INDEX IF NOT EXISTS idx_tomorrow_context_date ON tomorrow_context(for_date);

            CREATE TABLE IF NOT EXISTS routines (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id         TEXT NOT NULL,
                name              TEXT NOT NULL,
                pattern_json      TEXT NOT NULL,
                confidence        REAL NOT NULL DEFAULT 0.6,
                last_observed_at  TEXT,
                times_observed    INTEGER NOT NULL DEFAULT 1,
                created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                UNIQUE(person_id, name)
            );
            CREATE INDEX IF NOT EXISTS idx_routines_person ON routines(person_id);

            CREATE TABLE IF NOT EXISTS task_outputs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id     INTEGER NOT NULL,
                key         TEXT NOT NULL,
                content     TEXT NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                UNIQUE(task_id, key)
            );
            CREATE INDEX IF NOT EXISTS idx_task_outputs_key ON task_outputs(key);

            CREATE TABLE IF NOT EXISTS session_titles (
                session_id  TEXT PRIMARY KEY,
                title       TEXT NOT NULL,
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS email_signals (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                gmail_id            TEXT NOT NULL UNIQUE,
                thread_id           TEXT,
                received_at         TEXT NOT NULL,
                subject             TEXT NOT NULL DEFAULT '',
                sender_email        TEXT NOT NULL DEFAULT '',
                sender_person_id    TEXT,
                forwarder_email     TEXT,
                forwarder_person_id TEXT,
                from_header         TEXT,
                delivered_to_header TEXT,
                parse_confidence    REAL,
                summary             TEXT NOT NULL DEFAULT '',
                topics              TEXT NOT NULL DEFAULT '[]',
                ingested_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_email_signals_received ON email_signals(received_at);
            CREATE INDEX IF NOT EXISTS idx_email_signals_forwarder ON email_signals(forwarder_person_id);
            CREATE INDEX IF NOT EXISTS idx_email_signals_sender ON email_signals(sender_person_id);

            CREATE TABLE IF NOT EXISTS email_pending (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                recipient         TEXT NOT NULL,
                cc                TEXT NOT NULL DEFAULT '[]',
                subject           TEXT NOT NULL,
                body              TEXT NOT NULL,
                requester_id      TEXT NOT NULL,
                requester_role    TEXT NOT NULL,
                reply_to_gmail_id TEXT,
                thread_id         TEXT,
                status            TEXT NOT NULL DEFAULT 'pending',
                smithy_message_id TEXT,
                created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                decided_at        TEXT,
                decided_by        TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_email_pending_status ON email_pending(status, created_at);

            CREATE TABLE IF NOT EXISTS email_ingest_cursor (
                id              INTEGER PRIMARY KEY CHECK (id = 1),
                history_id      TEXT,
                last_synced_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS email_send_rate (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                requester_id     TEXT NOT NULL,
                recipient        TEXT NOT NULL,
                recipient_domain TEXT NOT NULL,
                sent_at          TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_email_send_rate_sent ON email_send_rate(sent_at);
            CREATE INDEX IF NOT EXISTS idx_email_send_rate_requester ON email_send_rate(requester_id, sent_at);
            """)

        await db.commit()

    # Phase 26-01 migration: add cost-tracking columns to existing cognitive_tasks tables
    async with _db_conn() as db:
        cur = await db.execute("PRAGMA table_info(cognitive_tasks)")
        existing_cols = {row[1] for row in await cur.fetchall()}
        for col, decl in (
            ("model_used", "TEXT"),
            ("tokens_in", "INTEGER"),
            ("tokens_out", "INTEGER"),
            ("duration_ms", "INTEGER"),
            ("gpu_ms", "INTEGER"),
        ):
            if col not in existing_cols:
                await db.execute(f"ALTER TABLE cognitive_tasks ADD COLUMN {col} {decl}")
        await db.commit()

    # Shadow eval enrichment migration: tokens, latency, cost, user_message for judge context
    async with _db_conn() as db:
        cur = await db.execute("PRAGMA table_info(shadow_calls)")
        existing_cols = {row[1] for row in await cur.fetchall()}
        for col, decl in (
            ("user_message",  "TEXT"),
            ("tokens_in",     "INTEGER"),
            ("tokens_out",    "INTEGER"),
            ("duration_ms",   "INTEGER"),
            ("cost_usd",      "REAL"),
        ):
            if col not in existing_cols:
                await db.execute(f"ALTER TABLE shadow_calls ADD COLUMN {col} {decl}")
        await db.commit()

    # Usage dashboard migration: cache token accounting + session_id on token_usage
    async with _db_conn() as db:
        cur = await db.execute("PRAGMA table_info(token_usage)")
        existing_cols = {row[1] for row in await cur.fetchall()}
        for col, decl in (
            ("cache_creation_tokens", "INTEGER DEFAULT 0"),
            ("cache_read_tokens",     "INTEGER DEFAULT 0"),
            ("session_id",            "TEXT"),
        ):
            if col not in existing_cols:
                await db.execute(f"ALTER TABLE token_usage ADD COLUMN {col} {decl}")
        await db.commit()

    # Perf instrumentation: surface column for primary vs shadow cost split
    async with _db_conn() as db:
        cur = await db.execute("PRAGMA table_info(token_usage)")
        existing_cols = {row[1] for row in await cur.fetchall()}
        if "surface" not in existing_cols:
            await db.execute("ALTER TABLE token_usage ADD COLUMN surface TEXT DEFAULT 'discord'")
            await db.commit()

    # Phase 28 Wave 2c: Conditional FTS5 rebuild (only on first run / empty index)
    # Prevents expensive full rebuild on every startup as activity_log grows.
    try:
        async with _db_conn() as db:
            row = await (await db.execute("SELECT COUNT(*) FROM activity_log_fts")).fetchone()
            if row and row[0] == 0:
                log.info("FTS5 index empty — performing one-time rebuild (external-content table)")
                await db.execute("INSERT INTO activity_log_fts(activity_log_fts) VALUES('rebuild')")
                await db.commit()
    except Exception as e:
        log.warning("FTS5 conditional rebuild skipped: %s", e)

    # 40B-2B / family-bot-1hs: greenfield must record schema_migrations 1–N
    # (brownfield early-return already calls run_schema_migrations).
    from db_migrations import run_schema_migrations

    await run_schema_migrations()

    log.info("Database initialised.")
    import sys
    from db_binding import bind_database
    # Always bind the public package (sys.modules['database']), never this
    # submodule. After Phase 0 package-wrap, __name__ is 'database._impl';
    # binding that breaks get_database().X patches and package-level state
    # (tests set database.DB_PATH / database.identity_health_check, etc.).
    bind_database(sys.modules["database"])

async def ensure_network_watchman_schema() -> None:
    """Create network watchman tables if missing (safe on every startup)."""
    async with _db_conn() as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS host_ip_snapshots (
                host_id     TEXT PRIMARY KEY,
                ip          TEXT NOT NULL DEFAULT '',
                is_wired    INTEGER NOT NULL DEFAULT 0,
                essid       TEXT,
                is_online   INTEGER NOT NULL DEFAULT 0,
                updated_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS network_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type  TEXT NOT NULL,
                host_id     TEXT,
                severity    TEXT NOT NULL DEFAULT 'info',
                summary     TEXT NOT NULL,
                details     TEXT,
                created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_network_events_created ON network_events(created_at);
        """)
        await db.commit()

async def ensure_email_schema() -> None:
    """Create Phase 34 email tables if missing (safe on every startup for existing DBs)."""
    import logging

    _log = logging.getLogger(__name__)
    async with _db_conn() as db:
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='email_signals'"
        )
        had_signals = await cur.fetchone() is not None
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS email_signals (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                gmail_id            TEXT NOT NULL UNIQUE,
                thread_id           TEXT,
                received_at         TEXT NOT NULL,
                subject             TEXT NOT NULL DEFAULT '',
                sender_email        TEXT NOT NULL DEFAULT '',
                sender_person_id    TEXT,
                forwarder_email     TEXT,
                forwarder_person_id TEXT,
                from_header         TEXT,
                delivered_to_header TEXT,
                parse_confidence    REAL,
                summary             TEXT NOT NULL DEFAULT '',
                topics              TEXT NOT NULL DEFAULT '[]',
                ingested_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_email_signals_received ON email_signals(received_at);
            CREATE INDEX IF NOT EXISTS idx_email_signals_forwarder ON email_signals(forwarder_person_id);
            CREATE INDEX IF NOT EXISTS idx_email_signals_sender ON email_signals(sender_person_id);

            CREATE TABLE IF NOT EXISTS email_pending (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                recipient         TEXT NOT NULL,
                cc                TEXT NOT NULL DEFAULT '[]',
                subject           TEXT NOT NULL,
                body              TEXT NOT NULL,
                requester_id      TEXT NOT NULL,
                requester_role    TEXT NOT NULL,
                reply_to_gmail_id TEXT,
                thread_id         TEXT,
                status            TEXT NOT NULL DEFAULT 'pending',
                smithy_message_id TEXT,
                created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                decided_at        TEXT,
                decided_by        TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_email_pending_status ON email_pending(status, created_at);

            CREATE TABLE IF NOT EXISTS email_ingest_cursor (
                id              INTEGER PRIMARY KEY CHECK (id = 1),
                history_id      TEXT,
                last_synced_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS email_send_rate (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                requester_id     TEXT NOT NULL,
                recipient        TEXT NOT NULL,
                recipient_domain TEXT NOT NULL,
                sent_at          TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_email_send_rate_sent ON email_send_rate(sent_at);
            CREATE INDEX IF NOT EXISTS idx_email_send_rate_requester ON email_send_rate(requester_id, sent_at);
        """)
        for col, typedef in (
            ("reply_to_gmail_id", "TEXT"),
            ("thread_id", "TEXT"),
        ):
            try:
                await db.execute(f"ALTER TABLE email_pending ADD COLUMN {col} {typedef}")
            except Exception:
                pass
        await db.commit()
        if not had_signals:
            _log.info("ensure_email_schema: created Phase 34 email tables on existing database")

async def email_schema_ready() -> bool:
    """True when all four Phase 34 email tables exist."""
    required = {
        "email_signals",
        "email_pending",
        "email_ingest_cursor",
        "email_send_rate",
    }
    async with _db_conn() as db:
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?,?,?)",
            tuple(required),
        ) as cur:
            found = {row[0] for row in await cur.fetchall()}
    return required.issubset(found)

async def ensure_pending_hitl_schema() -> None:
    """Create pending_hitl table if missing (safe on every startup for existing DBs)."""
    async with _db_conn() as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS pending_hitl (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                status              TEXT NOT NULL,
                tool_name           TEXT NOT NULL,
                args_json           TEXT NOT NULL,
                ctx_json            TEXT NOT NULL,
                reasoning           TEXT,
                requested_at        TEXT NOT NULL,
                expires_at          TEXT NOT NULL,
                decided_at          TEXT,
                decided_by          TEXT,
                result_preview      TEXT,
                notify_message_ids  TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_pending_hitl_status_expires
                ON pending_hitl (status, expires_at);
        """)
        try:
            await db.execute("ALTER TABLE pending_hitl ADD COLUMN notify_message_ids TEXT")
        except Exception:
            pass
        await db.commit()

async def ensure_pending_notifications_schema() -> None:
    """Ensure pending_notifications has the event_id and message_type columns."""
    async with _db_conn() as db:
        try:
            await db.execute("ALTER TABLE pending_notifications ADD COLUMN event_id TEXT")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE pending_notifications ADD COLUMN message_type TEXT")
        except Exception:
            pass
        await db.commit()

async def ensure_turn_instrumentation_schema() -> None:
    """Ensure token_usage instrumentation columns (surface, cache, session).

    Safe on every startup. Dual-path with greenfield CREATE columns.
    """
    async with _db_conn() as db:
        cur = await db.execute("PRAGMA table_info(token_usage)")
        existing_cols = {row[1] for row in await cur.fetchall()}
        alters = []
        if "surface" not in existing_cols:
            alters.append("ALTER TABLE token_usage ADD COLUMN surface TEXT DEFAULT 'discord'")
        if "cache_creation_tokens" not in existing_cols:
            alters.append("ALTER TABLE token_usage ADD COLUMN cache_creation_tokens INTEGER DEFAULT 0")
        if "cache_read_tokens" not in existing_cols:
            alters.append("ALTER TABLE token_usage ADD COLUMN cache_read_tokens INTEGER DEFAULT 0")
        if "session_id" not in existing_cols:
            alters.append("ALTER TABLE token_usage ADD COLUMN session_id TEXT")
        for sql in alters:
            try:
                await db.execute(sql)
            except Exception:
                pass
        if alters:
            await db.commit()

async def _table_exists(db, table: str) -> bool:
    async with db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ) as cur:
        return await cur.fetchone() is not None

async def _legacy_tasks_fully_migrated(db) -> bool:
    import json
    if not await _table_exists(db, "tasks"):
        return True
    async with db.execute("SELECT id FROM tasks") as cur:
        legacy = {int(r[0]) for r in await cur.fetchall()}
    if not legacy:
        return True
    migrated: set[int] = set()
    async with db.execute(
        "SELECT payload FROM unified_tasks WHERE payload LIKE '%migrated_from_task_id%'"
    ) as cur:
        for row in await cur.fetchall():
            try:
                payload = json.loads(row[0] or "{}")
                mid = payload.get("migrated_from_task_id")
                if mid is not None:
                    migrated.add(int(mid))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
    return legacy <= migrated

async def ensure_db_metadata_schema() -> None:
    """40B-2A: key/value metadata (last_vacuum_at, etc.) before 2B migration runner."""
    async with _db_conn() as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS db_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        await db.commit()

async def ensure_schema_migrations_table() -> None:
    """40B-2B: brownfield migration version bookkeeping."""
    async with _db_conn() as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                applied_at TEXT NOT NULL
            )"""
        )
        await db.commit()

async def get_applied_schema_migration_versions() -> set[int]:
    await ensure_schema_migrations_table()
    async with _db_conn() as db:
        cur = await db.execute("SELECT version FROM schema_migrations")
        return {int(row[0]) for row in await cur.fetchall()}

async def record_schema_migration(version: int, name: str, applied_at: str) -> None:
    async with _db_conn() as db:
        await db.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (version, name, applied_at),
        )
        await db.commit()

async def get_db_metadata(key: str) -> str | None:
    await ensure_db_metadata_schema()
    async with _db_conn() as db:
        cur = await db.execute("SELECT value FROM db_metadata WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else None

async def set_db_metadata(key: str, value: str) -> None:
    """Upsert a db_metadata key/value (public API — backup stamps, external IP, etc.)."""
    await ensure_db_metadata_schema()
    now = datetime.now(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    async with _db_conn() as db:
        await db.execute(
            """INSERT INTO db_metadata (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
               updated_at=excluded.updated_at""",
            (key, value, now),
        )
        await db.commit()

async def _set_last_vacuum_at() -> None:
    now = datetime.now(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    async with sqlite_async.connect(_resolve_db_path(), timeout=5.0) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS db_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        await db.execute(
            """INSERT INTO db_metadata (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
               updated_at=excluded.updated_at""",
            ("last_vacuum_at", now, now),
        )
        await db.commit()

async def ensure_legacy_schema_cleanup() -> None:
    """40B-1f: migrate legacy pref tables → person_preferences; drop legacy tasks when migrated."""
    now = datetime.now(dt_timezone.utc).isoformat()
    async with _db_conn() as db:
        if await _table_exists(db, "user_preferences"):
            async with db.execute(
                "SELECT discord_id, name, reminders, dm_mode, updated_at FROM user_preferences"
            ) as cur:
                rows = await cur.fetchall()
            from database.misc import _person_id_for_discord
            for row in rows:
                person_id = _person_id_for_discord(int(row["discord_id"]), row["name"])
                async with db.execute(
                    "SELECT 1 FROM person_preferences WHERE person_id=?", (person_id,)
                ) as cur:
                    exists = await cur.fetchone()
                if exists:
                    await db.execute(
                        """UPDATE person_preferences
                           SET discord_id=?, reminders_enabled=?, dm_mode=?, updated_at=?
                           WHERE person_id=?""",
                        (row["discord_id"], int(row["reminders"]), int(row["dm_mode"]),
                         row["updated_at"] or now, person_id),
                    )
                else:
                    await db.execute(
                        """INSERT INTO person_preferences
                           (person_id, discord_id, reminders_enabled, dm_mode, reminder_minutes, updated_at)
                           VALUES (?, ?, ?, ?, 30, ?)""",
                        (person_id, row["discord_id"], int(row["reminders"]),
                         int(row["dm_mode"]), row["updated_at"] or now),
                    )
            await db.execute("DROP TABLE user_preferences")
            log.info("ensure_legacy_schema_cleanup: migrated user_preferences → person_preferences")

        if await _table_exists(db, "family_prefs"):
            async with db.execute(
                """SELECT person_id, reminders_enabled, reminder_minutes,
                          preferred_channels, quiet_hours_start, quiet_hours_end
                   FROM family_prefs"""
            ) as cur:
                rows = await cur.fetchall()
            for row in rows:
                person_id = str(row["person_id"]).lower()
                async with db.execute(
                    "SELECT 1 FROM person_preferences WHERE person_id=?", (person_id,)
                ) as cur:
                    exists = await cur.fetchone()
                if exists:
                    await db.execute(
                        """UPDATE person_preferences
                           SET reminders_enabled=?, reminder_minutes=?,
                               preferred_channels=COALESCE(?, preferred_channels),
                               quiet_hours_start=COALESCE(?, quiet_hours_start),
                               quiet_hours_end=COALESCE(?, quiet_hours_end),
                               updated_at=?
                           WHERE person_id=?""",
                        (int(row["reminders_enabled"]), row["reminder_minutes"],
                         row["preferred_channels"], row["quiet_hours_start"],
                         row["quiet_hours_end"], now, person_id),
                    )
                else:
                    await db.execute(
                        """INSERT INTO person_preferences
                           (person_id, reminders_enabled, dm_mode, reminder_minutes,
                            preferred_channels, quiet_hours_start, quiet_hours_end, updated_at)
                           VALUES (?, ?, 1, ?, ?, ?, ?, ?)""",
                        (person_id, int(row["reminders_enabled"]), row["reminder_minutes"],
                         row["preferred_channels"], row["quiet_hours_start"],
                         row["quiet_hours_end"], now),
                    )
            await db.execute("DROP TABLE family_prefs")
            log.info("ensure_legacy_schema_cleanup: migrated family_prefs → person_preferences")

        if await _legacy_tasks_fully_migrated(db):
            if await _table_exists(db, "tasks"):
                await db.execute("DROP TABLE tasks")
                log.info("ensure_legacy_schema_cleanup: dropped legacy tasks table")

        await db.commit()

