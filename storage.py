"""
storage.py — SQLite persistence layer via aiosqlite.

All tables live in a single DB file at /data/messages.db.
Existing `messages` table is untouched; new tables are appended.
"""

import aiosqlite
import os
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "/data/messages.db")


# ── Schema ────────────────────────────────────────────────────────────────────

async def init_db() -> None:
    """Create all tables if they don't exist. Called once at startup."""
    async with aiosqlite.connect(DB_PATH) as db:
        # ── Original table (untouched) ──────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id      INTEGER NOT NULL,
                timestamp    TEXT    NOT NULL,
                sender_name  TEXT    NOT NULL,
                sender_id    INTEGER NOT NULL,
                msg_type     TEXT    NOT NULL,
                content      TEXT    NOT NULL
            )
        """)

        # ── Support tickets ─────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id             INTEGER NOT NULL,
                created_at          TEXT NOT NULL,
                resolved_at         TEXT,
                store_name          TEXT,
                symptom             TEXT NOT NULL,
                trigger_conditions  TEXT,
                impact              TEXT,
                workaround          TEXT,
                resolution_path     TEXT,
                resolution_minutes  INTEGER,
                delay_location      TEXT,
                root_cause          TEXT NOT NULL DEFAULT 'unknown',
                status              TEXT NOT NULL DEFAULT 'open',
                week_number         TEXT
            )
        """)

        # ── Operators per ticket ────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ticket_operators (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id         INTEGER NOT NULL REFERENCES tickets(id),
                operator_name     TEXT NOT NULL,
                operator_id       INTEGER NOT NULL,
                role              TEXT NOT NULL DEFAULT 'responder',
                engaged_at        TEXT NOT NULL,
                continuity_intact BOOLEAN DEFAULT 1
            )
        """)

        # ── Persistent playbook ─────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS playbook (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at         TEXT NOT NULL,
                updated_at         TEXT NOT NULL,
                title              TEXT NOT NULL,
                symptoms           TEXT NOT NULL,
                diagnostic_steps   TEXT NOT NULL,
                fix_steps          TEXT NOT NULL,
                verification       TEXT NOT NULL,
                escalation_trigger TEXT,
                root_cause         TEXT,
                times_used         INTEGER DEFAULT 0,
                status             TEXT DEFAULT 'active'
            )
        """)

        # ── Known systemic issues ───────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS known_issues (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at       TEXT NOT NULL,
                updated_at       TEXT NOT NULL,
                title            TEXT NOT NULL,
                description      TEXT NOT NULL,
                root_cause       TEXT,
                affected_stores  TEXT,
                status           TEXT DEFAULT 'open',
                owner            TEXT,
                resolution_notes TEXT
            )
        """)

        # ── Weekly digest archive ───────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS weekly_digests (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                week_number         TEXT NOT NULL,
                start_date          TEXT NOT NULL,
                end_date            TEXT NOT NULL,
                digest_text         TEXT NOT NULL,
                created_at          TEXT NOT NULL,
                delivered_email     BOOLEAN DEFAULT 0,
                delivered_drive     BOOLEAN DEFAULT 0,
                delivered_telegram  BOOLEAN DEFAULT 0
            )
        """)

        # ── Patterns registry ───────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS patterns (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at           TEXT NOT NULL,
                updated_at           TEXT NOT NULL,
                pattern_name         TEXT NOT NULL,
                description          TEXT NOT NULL,
                root_cause           TEXT,
                occurrence_count     INTEGER DEFAULT 1,
                stores_affected      TEXT,
                systemic             BOOLEAN DEFAULT 0,
                trend                TEXT DEFAULT 'new',
                timing_correlation   TEXT,
                last_seen            TEXT
            )
        """)

        await db.commit()
    logger.info("All DB tables verified/created.")


# ── Original helpers (untouched) ──────────────────────────────────────────────

async def insert_message(
    chat_id: int,
    sender_name: str,
    sender_id: int,
    msg_type: str,
    content: str,
) -> None:
    """Insert one message row."""
    timestamp = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO messages (chat_id, timestamp, sender_name, sender_id, msg_type, content)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (chat_id, timestamp, sender_name, sender_id, msg_type, content),
        )
        await db.commit()
    logger.info(
        "DB WRITE: chat_id=%d sender=%s type=%s content_len=%d",
        chat_id, sender_name, msg_type, len(content),
    )


async def fetch_messages(chat_id: int, limit: int = 200) -> list[dict]:
    """Return up to `limit` most recent rows for chat_id, oldest-first."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT timestamp, sender_name, sender_id, msg_type, content
            FROM (
                SELECT timestamp, sender_name, sender_id, msg_type, content
                FROM messages
                WHERE chat_id = ?
                ORDER BY id DESC
                LIMIT ?
            )
            ORDER BY timestamp ASC
            """,
            (chat_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def delete_messages(chat_id: int) -> None:
    """Delete all message rows for this chat_id (does NOT touch other tables)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        await db.commit()


# ── New helpers ───────────────────────────────────────────────────────────────

async def fetch_messages_since(chat_id: int, since_iso: str) -> list[dict]:
    """Return all messages for chat_id after a given ISO timestamp, oldest-first."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT timestamp, sender_name, sender_id, msg_type, content
            FROM messages
            WHERE chat_id = ? AND timestamp >= ?
            ORDER BY timestamp ASC
            """,
            (chat_id, since_iso),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def fetch_active_chat_ids(days: int = 7) -> list[int]:
    """Return distinct chat_ids that had activity in the last `days` days."""
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT DISTINCT chat_id FROM messages WHERE timestamp >= ?",
            (since,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]


# ── Tickets ───────────────────────────────────────────────────────────────────

async def insert_ticket(
    chat_id: int,
    symptom: str,
    root_cause: str = "unknown",
    status: str = "open",
    week_number: str = "",
    store_name: str = "",
    trigger_conditions: str = "",
    impact: str = "",
    workaround: str = "",
    resolution_path: str = "",
    resolution_minutes: int = None,
    delay_location: str = "",
    created_at: str = "",
    resolved_at: str = None,
) -> int:
    """Insert a ticket and return its id."""
    now = created_at or datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO tickets (
                chat_id, created_at, resolved_at, store_name, symptom,
                trigger_conditions, impact, workaround, resolution_path,
                resolution_minutes, delay_location, root_cause, status, week_number
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                chat_id, now, resolved_at, store_name, symptom,
                trigger_conditions, impact, workaround, resolution_path,
                resolution_minutes, delay_location, root_cause, status, week_number,
            ),
        )
        await db.commit()
        return cur.lastrowid


async def update_ticket(ticket_id: int, **kwargs) -> None:
    """Update arbitrary fields on a ticket."""
    if not kwargs:
        return
    cols = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [ticket_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE tickets SET {cols} WHERE id = ?", vals)
        await db.commit()


async def fetch_tickets(chat_id: int, week_number: str = None) -> list[dict]:
    """Fetch tickets for a chat, optionally filtered by week."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if week_number:
            async with db.execute(
                "SELECT * FROM tickets WHERE chat_id = ? AND week_number = ? ORDER BY created_at",
                (chat_id, week_number),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        else:
            async with db.execute(
                "SELECT * FROM tickets WHERE chat_id = ? ORDER BY created_at DESC LIMIT 100",
                (chat_id,),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]


async def fetch_open_tickets(chat_id: int) -> list[dict]:
    """Fetch all open (unresolved) tickets for a chat."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tickets WHERE chat_id = ? AND status = 'open' ORDER BY created_at",
            (chat_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


# ── Ticket Operators ──────────────────────────────────────────────────────────

async def insert_ticket_operator(
    ticket_id: int,
    operator_name: str,
    operator_id: int,
    role: str = "responder",
    engaged_at: str = "",
    continuity_intact: bool = True,
) -> None:
    now = engaged_at or datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO ticket_operators (ticket_id, operator_name, operator_id, role, engaged_at, continuity_intact)
            VALUES (?,?,?,?,?,?)
            """,
            (ticket_id, operator_name, operator_id, role, now, int(continuity_intact)),
        )
        await db.commit()


async def fetch_ticket_operators(ticket_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM ticket_operators WHERE ticket_id = ? ORDER BY engaged_at",
            (ticket_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


# ── Playbook ──────────────────────────────────────────────────────────────────

async def insert_playbook_entry(
    title: str,
    symptoms: str,
    diagnostic_steps: str,
    fix_steps: str,
    verification: str,
    escalation_trigger: str = "",
    root_cause: str = "",
) -> int:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO playbook (created_at, updated_at, title, symptoms,
                diagnostic_steps, fix_steps, verification, escalation_trigger, root_cause)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (now, now, title, symptoms, diagnostic_steps, fix_steps,
             verification, escalation_trigger, root_cause),
        )
        await db.commit()
        return cur.lastrowid


async def update_playbook_entry(entry_id: int, **kwargs) -> None:
    kwargs["updated_at"] = datetime.utcnow().isoformat()
    cols = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [entry_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE playbook SET {cols} WHERE id = ?", vals)
        await db.commit()


async def fetch_playbook(status: str = "active") -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM playbook WHERE status = ? ORDER BY times_used DESC",
            (status,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def search_playbook(keyword: str) -> list[dict]:
    like = f"%{keyword}%"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM playbook
            WHERE status = 'active'
              AND (title LIKE ? OR symptoms LIKE ? OR root_cause LIKE ?)
            ORDER BY times_used DESC
            """,
            (like, like, like),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def increment_playbook_usage(entry_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE playbook SET times_used = times_used + 1, updated_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), entry_id),
        )
        await db.commit()


# ── Known Issues ──────────────────────────────────────────────────────────────

async def insert_known_issue(
    title: str,
    description: str,
    root_cause: str = "",
    affected_stores: str = "",
    owner: str = "",
) -> int:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO known_issues (created_at, updated_at, title, description,
                root_cause, affected_stores, owner)
            VALUES (?,?,?,?,?,?,?)
            """,
            (now, now, title, description, root_cause, affected_stores, owner),
        )
        await db.commit()
        return cur.lastrowid


async def update_known_issue(issue_id: int, **kwargs) -> None:
    kwargs["updated_at"] = datetime.utcnow().isoformat()
    cols = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [issue_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE known_issues SET {cols} WHERE id = ?", vals)
        await db.commit()


async def fetch_known_issues(status: str = None) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if status:
            async with db.execute(
                "SELECT * FROM known_issues WHERE status = ? ORDER BY updated_at DESC",
                (status,),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        else:
            async with db.execute(
                "SELECT * FROM known_issues ORDER BY updated_at DESC",
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]


# ── Weekly Digests ────────────────────────────────────────────────────────────

async def insert_weekly_digest(
    week_number: str,
    start_date: str,
    end_date: str,
    digest_text: str,
) -> int:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO weekly_digests (week_number, start_date, end_date, digest_text, created_at)
            VALUES (?,?,?,?,?)
            """,
            (week_number, start_date, end_date, digest_text, now),
        )
        await db.commit()
        return cur.lastrowid


async def fetch_latest_digest() -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM weekly_digests ORDER BY id DESC LIMIT 1",
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def update_digest_delivery(digest_id: int, channel: str) -> None:
    """Mark a delivery channel as done. channel: 'email'|'drive'|'telegram'"""
    col = f"delivered_{channel}"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE weekly_digests SET {col} = 1 WHERE id = ?",
            (digest_id,),
        )
        await db.commit()


# ── Patterns ──────────────────────────────────────────────────────────────────

async def insert_pattern(
    pattern_name: str,
    description: str,
    root_cause: str = "",
    stores_affected: str = "",
    systemic: bool = False,
    timing_correlation: str = "",
) -> int:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO patterns (created_at, updated_at, pattern_name, description,
                root_cause, stores_affected, systemic, timing_correlation, last_seen)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (now, now, pattern_name, description, root_cause, stores_affected,
             int(systemic), timing_correlation, now),
        )
        await db.commit()
        return cur.lastrowid


async def update_pattern(pattern_id: int, **kwargs) -> None:
    kwargs["updated_at"] = datetime.utcnow().isoformat()
    cols = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [pattern_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE patterns SET {cols} WHERE id = ?", vals)
        await db.commit()


async def fetch_patterns() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM patterns ORDER BY occurrence_count DESC",
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def find_pattern_by_name(name: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM patterns WHERE pattern_name = ?",
            (name,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None
