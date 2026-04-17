"""
handlers.py — Thin Telegram event handlers.

Messages are stored directly (no agent overhead).
The agent is only invoked for /summary generation.

Branch: feature/summary-public-private
Changes vs main:
  - cmd_summary: admin-only check + public/private mode support
  - _is_admin(): new helper
  - _send_summary(): new helper to avoid duplicating send logic
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


# ── Helpers ──────────────────────────────────────────────────────────────────

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

    # Private chats — no admin concept, allow through
    if chat.type == "private":
        return True

    # Telegram Anonymous Admin — always a real admin posting under the group identity
    ANONYMOUS_ADMIN_ID = 1087968824
    if user_id == ANONYMOUS_ADMIN_ID:
        logger.info("Admin check: anonymous admin posting as group in chat %d — allowed", chat.id)
        return True

    try:
        admins = await context.bot.get_chat_administrators(chat.id)
        admin_ids = {a.user.id for a in admins}
        is_admin = user_id in admin_ids
        logger.info(
            "Admin check: user_id=%d chat_id=%d result=%s",
            user_id, chat.id, is_admin
        )
        return is_admin
    except Exception:
        logger.exception(
            "Failed to fetch admin list for chat %d — denying access", chat.id
        )
        return False


async def _send_summary(context, chat_id: int, text: str) -> None:
    """
    Send summary text to a chat. Tries Markdown first, falls back to plain text
    if Telegram rejects the formatting.
    """
    try:
        await context.bot.send_message(chat_id, text=text, parse_mode="Markdown")
    except (BadRequest, Exception):
        await context.bot.send_message(chat_id, text=text)


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


# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Generate a structured conversation summary.

    Usage:
      /summary           → public summary posted in the group (default)
      /summary public    → same as above, explicit
      /summary private   → summary sent as a private DM to the caller only
      /summary me        → alias for /summary private

    Both modes are restricted to group administrators only.
    In private chats the admin check is skipped.
    """

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    language = os.getenv("SUMMARY_LANGUAGE", "English")

    # ── 1. Admin check ────────────────────────────────────────────────────────
    if not await _is_admin(update, context):
        await update.message.reply_text(
            "❌ Only group administrators can use /summary commands."
        )
        return

    # ── 2. Parse the argument to determine public vs private mode ─────────────
    args = [a.lower() for a in (context.args or [])]
    private_mode = bool(args and args[0] in ("private", "me"))

    # ── 3. Generate the summary (same logic regardless of delivery mode) ───────
    try:
        summary = await agent.process_summary(chat_id, language)
    except Exception:
        logger.exception("Failed to generate summary for chat %d", chat_id)
        await update.message.reply_text(
            "⚠️ Could not generate summary. Please try again."
        )
        return

    # ── 4. Deliver the summary ────────────────────────────────────────────────
    if private_mode:
        # Send privately to the user who triggered the command
        try:
            await _send_summary(context, user_id, summary)
            # Post a short public confirmation so the group knows what happened
            await update.message.reply_text(
                "✅ Full summary sent privately to you."
            )
            logger.info(
                "Private summary sent to user %d for chat %d", user_id, chat_id
            )
        except Forbidden:
            # User has never started a private chat with the bot
            await update.message.reply_text(
                "⚠️ Couldn't send you a private message.\n"
                "Please start a private chat with me first: send me any message "
                "directly, then retry /summary private."
            )
        except Exception:
            logger.exception(
                "Failed to send private summary to user %d", user_id
            )
            await update.message.reply_text(
                "⚠️ Failed to send private summary. Please try again."
            )
    else:
        # Post the full summary publicly in the group (default behaviour)
        await _send_summary(context, chat_id, summary)
        logger.info("Public summary posted in chat %d", chat_id)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Wipe the conversation log for this chat."""
    try:
        await storage.delete_messages(update.effective_chat.id)
        await update.message.reply_text("✅ Conversation log cleared. Starting fresh.")
    except Exception:
        logger.exception("Failed to clear messages")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 I'm your group conversation assistant.\n\n"
        "I silently read all messages — text, voice, and images.\n\n"
        "Commands:\n"
        "/summary          — post full summary in the group\n"
        "/summary private  — send summary as a private DM (admins only)\n"
        "/summary me       — alias for /summary private\n"
        "/clear            — clear the log and start fresh"
    )
