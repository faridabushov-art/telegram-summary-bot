"""
handlers.py — Thin Telegram event handlers.

Messages are stored directly (no agent overhead).
The agent is only invoked for /summary generation.
"""

import logging
import os

from telegram import Update
from telegram.ext import ContextTypes

import storage
import ai
import agent

logger = logging.getLogger(__name__)


def _sender_name(update: Update) -> str:
    u = update.effective_user
    return f"{u.first_name} {u.last_name or ''}".strip()


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
    """Store a document — describe it if it's an image, otherwise log it."""
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


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate and post a structured summary via the LangGraph agent."""
    try:
        language = os.getenv("SUMMARY_LANGUAGE", "English")
        summary = await agent.process_summary(update.effective_chat.id, language)
        await update.message.reply_text(summary, parse_mode="Markdown")
    except Exception:
        logger.exception("Failed to generate summary")
        await update.message.reply_text("⚠️ Could not generate summary. Please try again.")


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
        "/summary — summarize this conversation\n"
        "/clear   — clear the log and start fresh"
    )
