"""
handlers.py — Thin Telegram event handlers.

Extracts raw data from Telegram updates and delegates to agent.py.
No AI logic lives here.
"""

import logging
import os

from telegram import Update
from telegram.ext import ContextTypes

import agent
import storage

logger = logging.getLogger(__name__)


def _sender_name(update: Update) -> str:
    u = update.effective_user
    return f"{u.first_name} {u.last_name or ''}".strip()


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await agent.process_message(
            chat_id=update.effective_chat.id,
            sender_name=_sender_name(update),
            sender_id=update.effective_user.id,
            msg_type="text",
            raw_payload=update.message.text,
        )
    except Exception:
        logger.exception("Failed to process text message")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        file = await context.bot.get_file(update.message.voice.file_id)
        ogg_bytes = bytes(await file.download_as_bytearray())
        await agent.process_message(
            chat_id=update.effective_chat.id,
            sender_name=_sender_name(update),
            sender_id=update.effective_user.id,
            msg_type="voice",
            raw_payload=ogg_bytes,
        )
    except Exception:
        logger.exception("Failed to process voice message")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        photo = update.message.photo[-1]  # largest resolution
        file = await context.bot.get_file(photo.file_id)
        img_bytes = bytes(await file.download_as_bytearray())
        caption = update.message.caption or ""

        await agent.process_message(
            chat_id=update.effective_chat.id,
            sender_name=_sender_name(update),
            sender_id=update.effective_user.id,
            msg_type="image",
            raw_payload=img_bytes,
            mime_type="image/jpeg",
        )

        # If there is a caption, store it as a follow-up text message
        if caption:
            await agent.process_message(
                chat_id=update.effective_chat.id,
                sender_name=_sender_name(update),
                sender_id=update.effective_user.id,
                msg_type="text",
                raw_payload=f"[Image caption]: {caption}",
            )
    except Exception:
        logger.exception("Failed to process photo")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        doc = update.message.document
        mime = doc.mime_type or ""
        file = await context.bot.get_file(doc.file_id)
        raw_bytes = bytes(await file.download_as_bytearray())

        if mime.startswith("image/"):
            await agent.process_message(
                chat_id=update.effective_chat.id,
                sender_name=_sender_name(update),
                sender_id=update.effective_user.id,
                msg_type="image",
                raw_payload=raw_bytes,
                mime_type=mime,
            )
        else:
            await agent.process_message(
                chat_id=update.effective_chat.id,
                sender_name=_sender_name(update),
                sender_id=update.effective_user.id,
                msg_type="file",
                raw_payload=f"[File shared: {doc.file_name} ({mime})]",
            )
    except Exception:
        logger.exception("Failed to process document")


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        language = os.getenv("SUMMARY_LANGUAGE", "English")
        summary = await agent.process_summary(update.effective_chat.id, language)
        await update.message.reply_text(summary, parse_mode="Markdown")
    except Exception:
        logger.exception("Failed to generate summary")
        await update.message.reply_text(
            "⚠️ Could not generate summary. Please try again."
        )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
