import logging
import os

from dotenv import load_dotenv
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
)

import storage
import handlers

load_dotenv()

logging.basicConfig(
    format="%(asctime)s — %(name)s — %(levelname)s — %(message)s",
    level=logging.INFO,
)


async def post_init(application):
    await storage.init_db()
    logging.info("Database initialised.")


def main():
    # ── Validate env vars ────────────────────────────────────────────────────
    for var in ("TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        if not os.getenv(var):
            raise ValueError(f"Missing required environment variable: {var}")

    app = (
        ApplicationBuilder()
        .token(os.getenv("TELEGRAM_BOT_TOKEN"))
        .post_init(post_init)
        .build()
    )

    group_filter = filters.ChatType.GROUPS | filters.ChatType.SUPERGROUP

    # Commands
    app.add_handler(CommandHandler("start",   handlers.cmd_start))
    app.add_handler(CommandHandler("summary", handlers.cmd_summary))
    app.add_handler(CommandHandler("clear",   handlers.cmd_clear))

    # Message types — listen silently in groups
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & group_filter, handlers.handle_text))
    app.add_handler(MessageHandler(
        filters.VOICE & group_filter, handlers.handle_voice))
    app.add_handler(MessageHandler(
        filters.PHOTO & group_filter, handlers.handle_photo))
    app.add_handler(MessageHandler(
        filters.Document.ALL & group_filter, handlers.handle_document))

    logging.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
