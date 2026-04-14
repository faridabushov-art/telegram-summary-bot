import aiosqlite
import os
from datetime import datetime

# Default to /data/messages.db so Railway's persistent volume keeps the DB
# across deploys. Override with DB_PATH env var for local dev if needed.
DB_PATH = os.getenv("DB_PATH", "/data/messages.db")


async def init_db() -> None:
    """Create table if not exists. Called once at startup."""
    async with aiosqlite.connect(DB_PATH) as db:
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
        await db.commit()


async def insert_message(
    chat_id: int,
    sender_name: str,
    sender_id: int,
    msg_type: str,
    content: str,
) -> None:
    """Insert one row. Set timestamp = datetime.utcnow().isoformat()."""
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


async def fetch_messages(chat_id: int, limit: int = 200) -> list[dict]:
    """
    Return up to `limit` most recent rows for chat_id, oldest-first.
    Each dict has keys: timestamp, sender_name, msg_type, content.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT timestamp, sender_name, msg_type, content
            FROM (
                SELECT timestamp, sender_name, msg_type, content
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
    """Delete all rows for this chat_id."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        await db.commit()
