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
    for var in ("TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        if not os.getenv(var):
            raise ValueError(f"Missing required environment variable: {var}")

    app = (
        ApplicationBuilder()
        .token(os.getenv("TELEGRAM_BOT_TOKEN"))
        .post_init(post_init)
        .build()
    )

    # Commands — work in all chat types
    app.add_handler(CommandHandler("start",   handlers.cmd_start))
    app.add_handler(CommandHandler("summary", handlers.cmd_summary))
    app.add_handler(CommandHandler("clear",   handlers.cmd_clear))

    # Message handlers — no chat type filter so the bot works in groups AND private chats
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, handlers.handle_text))
    app.add_handler(MessageHandler(
        filters.VOICE, handlers.handle_voice))
    app.add_handler(MessageHandler(
        filters.PHOTO, handlers.handle_photo))
    app.add_handler(MessageHandler(
        filters.Document.ALL, handlers.handle_document))

    logging.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
