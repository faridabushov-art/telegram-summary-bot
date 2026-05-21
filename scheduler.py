"""
scheduler.py — APScheduler weekly trigger for POS analysis.

Runs every Monday at 09:00 Baku time (UTC+4) by default.
Configurable via ANALYSIS_DAY and ANALYSIS_HOUR env vars.
"""

import logging
import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import storage

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None
_bot_instance = None


async def _run_weekly_analysis_job() -> None:
    """Scheduled job: run weekly analysis for all active chats."""
    from analyzer import analyze_weekly

    chat_ids_env = os.getenv("ANALYSIS_CHAT_IDS", "").strip()
    if chat_ids_env:
        chat_ids = [int(x.strip()) for x in chat_ids_env.split(",") if x.strip()]
    else:
        chat_ids = await storage.fetch_active_chat_ids(days=7)

    if not chat_ids:
        logger.info("Scheduled analysis: no active chats found, skipping.")
        return

    logger.info("Scheduled weekly analysis starting for %d chat(s): %s", len(chat_ids), chat_ids)
    for chat_id in chat_ids:
        try:
            await analyze_weekly(chat_id, bot=_bot_instance)
            logger.info("Scheduled analysis complete for chat %d", chat_id)
        except Exception:
            logger.exception("Scheduled analysis failed for chat %d", chat_id)


def start_scheduler(bot=None) -> None:
    """
    Start the APScheduler. Call this once from post_init.
    Pass the bot instance for Telegram delivery.
    """
    global _scheduler, _bot_instance
    _bot_instance = bot

    day_of_week = os.getenv("ANALYSIS_DAY", "mon")
    hour = int(os.getenv("ANALYSIS_HOUR", "9"))
    timezone = "Asia/Baku"  # UTC+4

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        _run_weekly_analysis_job,
        CronTrigger(day_of_week=day_of_week, hour=hour, minute=0, timezone=timezone),
        id="weekly_pos_analysis",
        replace_existing=True,
        misfire_grace_time=3600,  # Allow up to 1 hour late start
    )
    _scheduler.start()

    next_run = _scheduler.get_job("weekly_pos_analysis").next_run_time
    logger.info(
        "Scheduler started. Weekly analysis: %s at %02d:00 %s. Next run: %s",
        day_of_week.upper(), hour, timezone, next_run,
    )


def stop_scheduler() -> None:
    """Gracefully stop the scheduler on shutdown."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped.")


def get_scheduler() -> AsyncIOScheduler | None:
    return _scheduler
