"""
handlers.py — Telegram event handlers.

Messages are stored directly (no agent overhead).
The agent is only invoked for /summary generation.
New commands: /analyze, /playbook, /status, /digest
"""

import logging
import os

from telegram import Update
from telegram.error import Forbidden, BadRequest
from telegram.ext import ContextTypes

import storage
import ai
import agent

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sender_name(update: Update) -> str:
    u = update.effective_user
    return f"{u.first_name} {u.last_name or ''}".strip()


async def _is_admin(update: Update, context) -> bool:
    """
    Return True if the user who sent the command is a group administrator or creator.

    Uses get_chat_administrators() — works even when the bot is NOT itself an admin.

    Special case: user_id 1087968824 is Telegram's built-in 'Anonymous Admin' account.
    When a real admin posts with 'Send as group' enabled, Telegram replaces their
    user_id with this special ID. We treat it as always-admin.

    Always returns True in private chats (admin concept doesn't apply).
    """
    chat = update.effective_chat
    user_id = update.effective_user.id

    if chat.type == "private":
        return True

    ANONYMOUS_ADMIN_ID = 1087968824
    if user_id == ANONYMOUS_ADMIN_ID:
        logger.info("Admin check: anonymous admin in chat %d — allowed", chat.id)
        return True

    try:
        admins = await context.bot.get_chat_administrators(chat.id)
        admin_ids = {a.user.id for a in admins}
        is_admin = user_id in admin_ids
        logger.info(
            "Admin check: user_id=%d chat_id=%d result=%s",
            user_id, chat.id, is_admin,
        )
        return is_admin
    except Exception:
        logger.exception("Failed to fetch admin list for chat %d — denying access", chat.id)
        return False


async def _send_summary(context, chat_id: int, text: str) -> None:
    """Send summary text. Tries Markdown first, falls back to plain text."""
    try:
        await context.bot.send_message(chat_id, text=text, parse_mode="Markdown")
    except (BadRequest, Exception):
        await context.bot.send_message(chat_id, text=text)


async def _send_long_message(context, chat_id: int, text: str, parse_mode: str = None) -> None:
    """Split and send long messages respecting Telegram's 4096-char limit."""
    MAX = 4000
    chunks = [text[i:i+MAX] for i in range(0, len(text), MAX)]
    for chunk in chunks:
        try:
            if parse_mode:
                await context.bot.send_message(chat_id, text=chunk, parse_mode=parse_mode)
            else:
                await context.bot.send_message(chat_id, text=chunk)
        except Exception:
            await context.bot.send_message(chat_id, text=chunk)


# ── Message handlers (unchanged) ─────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Silently store any non-command text message."""
    try:
        await storage.insert_message(
            chat_id=update.effective_chat.id,
            sender_name=_sender_name(update),
            sender_id=update.effective_user.id,
            msg_type="text",
            content=update.message.text,
        )
        logger.info("Stored text from %s in chat %d", _sender_name(update), update.effective_chat.id)
    except Exception:
        logger.exception("Failed to store text message")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Transcribe and silently store a voice message."""
    try:
        file = await context.bot.get_file(update.message.voice.file_id)
        ogg_bytes = bytes(await file.download_as_bytearray())
        transcript = await ai.transcribe_voice(ogg_bytes)
        await storage.insert_message(
            chat_id=update.effective_chat.id,
            sender_name=_sender_name(update),
            sender_id=update.effective_user.id,
            msg_type="voice",
            content=transcript,
        )
        logger.info("Stored voice from %s in chat %d", _sender_name(update), update.effective_chat.id)
    except Exception:
        logger.exception("Failed to store voice message")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Describe and silently store a photo message."""
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        img_bytes = bytes(await file.download_as_bytearray())
        description = await ai.describe_image(img_bytes, "image/jpeg")
        caption = update.message.caption or ""
        content = description + (f"\n[Caption: {caption}]" if caption else "")
        await storage.insert_message(
            chat_id=update.effective_chat.id,
            sender_name=_sender_name(update),
            sender_id=update.effective_user.id,
            msg_type="image",
            content=content,
        )
        logger.info("Stored image from %s in chat %d", _sender_name(update), update.effective_chat.id)
    except Exception:
        logger.exception("Failed to store photo")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Store a document — describe it if it's an image, otherwise log it as text."""
    try:
        doc = update.message.document
        mime = doc.mime_type or ""
        file = await context.bot.get_file(doc.file_id)
        raw_bytes = bytes(await file.download_as_bytearray())

        if mime.startswith("image/"):
            description = await ai.describe_image(raw_bytes, mime)
            caption = update.message.caption or ""
            content = description + (f"\n[Caption: {caption}]" if caption else "")
            msg_type = "image"
        else:
            content = f"[File shared: {doc.file_name} ({mime})]"
            msg_type = "text"

        await storage.insert_message(
            chat_id=update.effective_chat.id,
            sender_name=_sender_name(update),
            sender_id=update.effective_user.id,
            msg_type=msg_type,
            content=content,
        )
        logger.info("Stored document from %s in chat %d", _sender_name(update), update.effective_chat.id)
    except Exception:
        logger.exception("Failed to store document")


# ── Original Commands ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 I'm your POS Support Analysis assistant.\n\n"
        "I silently read all messages — text, voice, and images.\n\n"
        "Commands:\n"
        "/summary           — post POS support summary in the group\n"
        "/summary private   — send summary as a private DM (admins only)\n"
        "/summary me        — alias for /summary private\n"
        "/clear             — clear the message log\n\n"
        "Analysis commands (admin only):\n"
        "/analyze           — run weekly analysis now\n"
        "/playbook [keyword] — view playbook entries\n"
        "/status            — show open tickets and known issues\n"
        "/digest            — resend the most recent weekly digest"
    )


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Generate a structured POS support summary.

    Usage:
      /summary           → public summary posted in the group (default)
      /summary public    → same as above, explicit
      /summary private   → summary sent as a private DM to the caller only
      /summary me        → alias for /summary private
    """
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    language = os.getenv("SUMMARY_LANGUAGE", "English")

    if not await _is_admin(update, context):
        await update.message.reply_text("❌ Only group administrators can use /summary.")
        return

    args = [a.lower() for a in (context.args or [])]
    private_mode = bool(args and args[0] in ("private", "me"))

    try:
        summary = await agent.process_summary(chat_id, language)
    except Exception:
        logger.exception("Failed to generate summary for chat %d", chat_id)
        await update.message.reply_text("⚠️ Could not generate summary. Please try again.")
        return

    if private_mode:
        try:
            await _send_summary(context, user_id, summary)
            await update.message.reply_text("✅ Full summary sent privately to you.")
            logger.info("Private summary sent to user %d for chat %d", user_id, chat_id)
        except Forbidden:
            await update.message.reply_text(
                "⚠️ Couldn't send you a private message.\n"
                "Please start a private chat with me first, then retry /summary private."
            )
        except Exception:
            logger.exception("Failed to send private summary to user %d", user_id)
            await update.message.reply_text("⚠️ Failed to send private summary. Please try again.")
    else:
        await _send_summary(context, chat_id, summary)
        logger.info("Public summary posted in chat %d", chat_id)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Wipe the conversation log for this chat (does NOT touch tickets/playbook/patterns)."""
    try:
        await storage.delete_messages(update.effective_chat.id)
        await update.message.reply_text("✅ Conversation log cleared. Starting fresh.")
    except Exception:
        logger.exception("Failed to clear messages")


# ── New Commands ──────────────────────────────────────────────────────────────

async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /analyze — Manually trigger the weekly analysis (admin-only).
    Useful for testing or mid-week runs.
    """
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ Only group administrators can use /analyze.")
        return

    chat_id = update.effective_chat.id
    await update.message.reply_text(
        "🔍 Starting POS analysis... This may take a minute."
    )

    try:
        from analyzer import analyze_weekly
        digest = await analyze_weekly(chat_id, bot=context.bot)
        # Send a short confirmation — the full digest is delivered via delivery.py
        lines = digest.splitlines()
        preview_lines = [l for l in lines if l.strip() and "═" not in l][:8]
        preview = "\n".join(preview_lines)
        await update.message.reply_text(
            f"✅ Analysis complete. Digest preview:\n\n{preview}\n\n"
            f"Full digest delivered via email/Drive/Telegram."
        )
    except Exception:
        logger.exception("Manual /analyze failed for chat %d", chat_id)
        await update.message.reply_text("⚠️ Analysis failed. Check logs for details.")


async def cmd_playbook(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /playbook [keyword] — Show current playbook entries, optionally filtered by keyword.
    """
    keyword = " ".join(context.args).strip() if context.args else ""
    chat_id = update.effective_chat.id

    try:
        if keyword:
            entries = await storage.search_playbook(keyword)
            header = f"📖 *Playbook — search: \"{keyword}\"*\n"
        else:
            entries = await storage.fetch_playbook()
            header = "📖 *Playbook — All Active Entries*\n"

        if not entries:
            await update.message.reply_text(
                f"📖 No playbook entries found"
                + (f" for \"{keyword}\"." if keyword else ". The playbook is empty — it grows after each analysis run.")
            )
            return

        lines = [header, f"_{len(entries)} entr{'y' if len(entries)==1 else 'ies'}_\n"]
        for e in entries[:10]:  # Cap at 10 for readability
            lines.append(
                f"*[{e['id']}] {e['title']}*\n"
                f"Root cause: `{e.get('root_cause', 'unknown')}`  |  Used: {e.get('times_used', 0)}x\n"
                f"_{e['symptoms'][:100]}..._\n"
                f"Fix: {e['fix_steps'][:150]}...\n"
            )

        if len(entries) > 10:
            lines.append(f"_...and {len(entries)-10} more. Full list in Google Drive._")

        await _send_long_message(context, chat_id, "\n".join(lines), parse_mode="Markdown")
    except Exception:
        logger.exception("Failed to fetch playbook for chat %d", chat_id)
        await update.message.reply_text("⚠️ Could not fetch playbook. Please try again.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /status — Show currently open tickets and known issues.
    """
    chat_id = update.effective_chat.id

    try:
        open_tickets = await storage.fetch_open_tickets(chat_id)
        known_issues = await storage.fetch_known_issues(status="open")

        lines = ["📊 *POS Support Status*\n"]

        # Open tickets
        if open_tickets:
            lines.append(f"*🎫 Open Tickets ({len(open_tickets)})*")
            for t in open_tickets[:8]:
                store = t.get("store_name") or "Unknown store"
                symptom = (t.get("symptom", "")[:70] + "…") if len(t.get("symptom", "")) > 70 else t.get("symptom", "")
                rc = t.get("root_cause", "unknown")
                lines.append(f"• [{rc}] *{store}*: {symptom}")
            if len(open_tickets) > 8:
                lines.append(f"  _…and {len(open_tickets)-8} more_")
        else:
            lines.append("*🎫 Open Tickets*\n✅ No open tickets — all clear!")

        lines.append("")

        # Known issues
        if known_issues:
            lines.append(f"*⚠️ Known Issues ({len(known_issues)})*")
            for ki in known_issues[:5]:
                status_icon = {"open": "🔴", "investigating": "🟡", "resolved": "🟢"}.get(
                    ki.get("status", "open"), "⚪"
                )
                lines.append(f"{status_icon} *{ki['title'][:60]}*")
                if ki.get("affected_stores"):
                    lines.append(f"   Stores: {ki['affected_stores'][:50]}")
            if len(known_issues) > 5:
                lines.append(f"  _…and {len(known_issues)-5} more known issues_")
        else:
            lines.append("*⚠️ Known Issues*\n✅ No open known issues.")

        await _send_long_message(context, chat_id, "\n".join(lines), parse_mode="Markdown")
    except Exception:
        logger.exception("Failed to fetch status for chat %d", chat_id)
        await update.message.reply_text("⚠️ Could not fetch status. Please try again.")


async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /digest — Re-send the most recent weekly digest to the chat.
    """
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ Only group administrators can use /digest.")
        return

    chat_id = update.effective_chat.id

    try:
        digest = await storage.fetch_latest_digest()
        if not digest:
            await update.message.reply_text(
                "📊 No digest found. Run /analyze to generate one."
            )
            return

        header = (
            f"📊 *Re-sending digest for {digest['week_number']}*\n"
            f"_{digest['start_date'][:10]} to {digest['end_date'][:10]}_\n\n"
        )
        await _send_long_message(context, chat_id, header + digest["digest_text"])
        logger.info("Digest re-sent to chat %d for week %s", chat_id, digest["week_number"])
    except Exception:
        logger.exception("Failed to send digest for chat %d", chat_id)
        await update.message.reply_text("⚠️ Could not retrieve digest. Please try again.")
