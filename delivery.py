"""
delivery.py — Three-channel digest delivery: Email (SMTP), Google Drive, Telegram.

All channels are optional and fail-safe: if credentials are missing, that channel
is skipped with a warning. The analysis itself is never blocked by delivery failures.
"""

import io
import logging
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import storage

logger = logging.getLogger(__name__)


# ── Email ─────────────────────────────────────────────────────────────────────

async def send_email(
    digest_text: str,
    week_label: str,
    start_date: datetime,
    end_date: datetime,
    drive_link: str = "",
) -> bool:
    """
    Send digest via SMTP. Returns True on success.
    Required env vars: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, DIGEST_RECIPIENTS
    """
    recipients_raw = os.getenv("DIGEST_RECIPIENTS", "")
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_password = os.getenv("SMTP_PASSWORD", "")

    if not all([recipients_raw, smtp_user, smtp_password]):
        logger.warning("Email delivery skipped — SMTP credentials not configured.")
        return False

    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]
    if not recipients:
        logger.warning("Email delivery skipped — no recipients configured.")
        return False

    date_fmt = "%d %b %Y"
    subject = (
        f"POS Support Digest — Week of {start_date.strftime(date_fmt)} "
        f"to {end_date.strftime(date_fmt)}"
    )

    body = digest_text
    if drive_link:
        body += f"\n\nFull report on Google Drive: {drive_link}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.sendmail(smtp_user, recipients, msg.as_string())
        logger.info("Digest email sent to %d recipient(s) for %s", len(recipients), week_label)
        return True
    except smtplib.SMTPAuthenticationError:
        logger.error("SMTP authentication failed — check SMTP_USER and SMTP_PASSWORD.")
        return False
    except Exception:
        logger.exception("Email delivery failed for %s", week_label)
        return False


# ── Google Drive ──────────────────────────────────────────────────────────────

def _build_drive_service():
    """Build and return a Google Drive API service object, or None if not configured."""
    sa_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa_path or not os.path.exists(sa_path):
        logger.warning("Google Drive skipped — GOOGLE_SERVICE_ACCOUNT_JSON not set or file not found.")
        return None

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_file(
            sa_path,
            scopes=["https://www.googleapis.com/auth/drive.file"],
        )
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        return service
    except ImportError:
        logger.error("google-api-python-client not installed. Run: pip install google-api-python-client google-auth")
        return None
    except Exception:
        logger.exception("Failed to build Google Drive service")
        return None


async def upload_to_drive(
    digest_text: str,
    week_label: str,
    start_date: datetime,
    playbook_entries: list[dict] = None,
    known_issues: list[dict] = None,
    patterns: list[dict] = None,
) -> str:
    """
    Upload digest + persistent summaries to Google Drive.
    Returns the shareable link (empty string on failure).
    Required env vars: GDRIVE_FOLDER_ID, GOOGLE_SERVICE_ACCOUNT_JSON
    """
    folder_id = os.getenv("GDRIVE_FOLDER_ID", "")
    if not folder_id:
        logger.warning("Google Drive skipped — GDRIVE_FOLDER_ID not configured.")
        return ""

    service = _build_drive_service()
    if not service:
        return ""

    try:
        from googleapiclient.http import MediaInMemoryUpload

        date_str = start_date.strftime("%Y-%m-%d")
        digest_filename = f"POS_Digest_{week_label}_{date_str}.txt"

        # Upload weekly digest
        digest_bytes = digest_text.encode("utf-8")
        media = MediaInMemoryUpload(digest_bytes, mimetype="text/plain", resumable=False)
        file_meta = {
            "name": digest_filename,
            "parents": [folder_id],
        }
        uploaded = service.files().create(
            body=file_meta,
            media_body=media,
            fields="id, webViewLink",
        ).execute()

        # Make it readable by anyone with the link
        service.permissions().create(
            fileId=uploaded["id"],
            body={"type": "anyone", "role": "reader"},
        ).execute()

        drive_link = uploaded.get("webViewLink", "")
        logger.info("Digest uploaded to Drive: %s", drive_link)

        # Upload/overwrite persistent playbook export if data provided
        if playbook_entries:
            await _upsert_drive_file(
                service=service,
                folder_id=folder_id,
                filename="POS_Playbook_Current.txt",
                content=_format_playbook_export(playbook_entries),
            )

        if known_issues:
            await _upsert_drive_file(
                service=service,
                folder_id=folder_id,
                filename="POS_KnownIssues_Current.txt",
                content=_format_known_issues_export(known_issues),
            )

        if patterns:
            await _upsert_drive_file(
                service=service,
                folder_id=folder_id,
                filename="POS_Patterns_Current.txt",
                content=_format_patterns_export(patterns),
            )

        return drive_link
    except Exception:
        logger.exception("Google Drive upload failed")
        return ""


async def _upsert_drive_file(service, folder_id: str, filename: str, content: str) -> None:
    """Create or overwrite a named file in the Drive folder."""
    try:
        from googleapiclient.http import MediaInMemoryUpload

        # Search for existing file
        query = f"name='{filename}' and '{folder_id}' in parents and trashed=false"
        results = service.files().list(q=query, fields="files(id)").execute()
        existing = results.get("files", [])

        content_bytes = content.encode("utf-8")
        media = MediaInMemoryUpload(content_bytes, mimetype="text/plain", resumable=False)

        if existing:
            file_id = existing[0]["id"]
            service.files().update(fileId=file_id, media_body=media).execute()
            logger.info("Updated Drive file: %s", filename)
        else:
            meta = {"name": filename, "parents": [folder_id]}
            uploaded = service.files().create(body=meta, media_body=media, fields="id").execute()
            service.permissions().create(
                fileId=uploaded["id"],
                body={"type": "anyone", "role": "reader"},
            ).execute()
            logger.info("Created Drive file: %s", filename)
    except Exception:
        logger.exception("Failed to upsert Drive file: %s", filename)


def _format_playbook_export(entries: list[dict]) -> str:
    lines = [f"POS SUPPORT PLAYBOOK — Updated {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n"]
    lines.append("=" * 60 + "\n")
    for e in entries:
        lines.append(f"\n[{e.get('id', '?')}] {e.get('title', 'Untitled')}")
        lines.append(f"Root cause: {e.get('root_cause', 'unknown')}")
        lines.append(f"Times used: {e.get('times_used', 0)}")
        lines.append(f"\nSymptoms:\n{e.get('symptoms', '')}")
        lines.append(f"\nDiagnostic Steps:\n{e.get('diagnostic_steps', '')}")
        lines.append(f"\nFix Steps:\n{e.get('fix_steps', '')}")
        lines.append(f"\nVerification:\n{e.get('verification', '')}")
        if e.get("escalation_trigger"):
            lines.append(f"\nEscalate if:\n{e['escalation_trigger']}")
        lines.append("\n" + "-" * 60)
    return "\n".join(lines)


def _format_known_issues_export(issues: list[dict]) -> str:
    lines = [f"KNOWN ISSUES — Updated {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n"]
    lines.append("=" * 60 + "\n")
    for i in issues:
        lines.append(f"\n[{i.get('status', 'open').upper()}] {i.get('title', 'Untitled')}")
        lines.append(f"Root cause: {i.get('root_cause', 'unknown')}")
        lines.append(f"Affected: {i.get('affected_stores', 'unknown')}")
        lines.append(f"Description: {i.get('description', '')}")
        if i.get("resolution_notes"):
            lines.append(f"Notes: {i['resolution_notes']}")
        lines.append("-" * 40)
    return "\n".join(lines)


def _format_patterns_export(patterns: list[dict]) -> str:
    lines = [f"RECURRING PATTERNS — Updated {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n"]
    lines.append("=" * 60 + "\n")
    for p in patterns:
        trend_arrow = {"new": "🆕", "strengthening": "📈", "weakening": "📉", "resolved": "✅"}.get(
            p.get("trend", "new"), "•"
        )
        lines.append(f"\n{trend_arrow} [{p.get('occurrence_count', 1)}x] {p.get('pattern_name', 'Unknown')}")
        lines.append(f"Root cause: {p.get('root_cause', 'unknown')}")
        lines.append(f"Stores: {p.get('stores_affected', 'unknown')}")
        lines.append(f"Systemic: {'Yes' if p.get('systemic') else 'No'}")
        lines.append(f"Description: {p.get('description', '')}")
        if p.get("timing_correlation"):
            lines.append(f"Timing: {p['timing_correlation']}")
        lines.append("-" * 40)
    return "\n".join(lines)


# ── Telegram ──────────────────────────────────────────────────────────────────

async def send_telegram_summary(
    bot,
    chat_id: int,
    digest_text: str,
    week_label: str,
    start_date: datetime,
    end_date: datetime,
    total_tickets: int = 0,
    resolved_count: int = 0,
    open_count: int = 0,
    top_issue: str = "",
    biggest_win: str = "",
    drive_link: str = "",
) -> bool:
    """
    Post a ≤5-line Telegram summary to the group.
    Returns True on success.
    """
    if bot is None:
        logger.warning("Telegram delivery skipped — no bot instance provided.")
        return False

    date_fmt = "%d %b"
    date_range = f"{start_date.strftime(date_fmt)} – {end_date.strftime(date_fmt)}"

    drive_part = f"\nFull report → {drive_link}" if drive_link else ""

    top_issue_short = (top_issue[:60] + "…") if len(top_issue) > 60 else top_issue
    win_short = (biggest_win[:60] + "…") if len(biggest_win) > 60 else biggest_win

    lines = [
        f"📊 *POS Weekly Digest — {date_range}*",
        f"Tickets: {total_tickets} total | {resolved_count} resolved | {open_count} open",
        f"Top issue: {top_issue_short or 'See full report'}",
        f"Biggest win: {win_short or 'See full report'}",
    ]
    if drive_part:
        lines.append(f"Full report → {drive_link}")

    message = "\n".join(lines)

    try:
        await bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode="Markdown",
        )
        logger.info("Telegram digest summary sent to chat %d", chat_id)
        return True
    except Exception:
        logger.exception("Telegram delivery failed for chat %d", chat_id)
        try:
            await bot.send_message(chat_id=chat_id, text=message.replace("*", ""))
            return True
        except Exception:
            logger.exception("Telegram delivery fallback also failed")
            return False


# ── Orchestrator ──────────────────────────────────────────────────────────────

async def deliver_digest(
    digest_id: int,
    digest_text: str,
    week_label: str,
    start_date: datetime,
    end_date: datetime,
    chat_id: int,
    bot=None,
) -> None:
    """
    Orchestrate all three delivery channels. Update delivery flags in DB.
    Failures in one channel do not block the others.
    """
    # Fetch persistent data for Drive exports
    playbook_entries = await storage.fetch_playbook()
    known_issues = await storage.fetch_known_issues()
    patterns = await storage.fetch_patterns()

    # Extract quick stats from digest_text for the Telegram summary
    total, resolved, open_c = _parse_digest_stats(digest_text)
    top_issue, biggest_win = _parse_digest_highlights(digest_text)

    # 1. Google Drive (get link first so email can include it)
    drive_link = await upload_to_drive(
        digest_text=digest_text,
        week_label=week_label,
        start_date=start_date,
        playbook_entries=playbook_entries,
        known_issues=known_issues,
        patterns=patterns,
    )
    if drive_link:
        await storage.update_digest_delivery(digest_id, "drive")

    # 2. Email
    email_ok = await send_email(
        digest_text=digest_text,
        week_label=week_label,
        start_date=start_date,
        end_date=end_date,
        drive_link=drive_link,
    )
    if email_ok:
        await storage.update_digest_delivery(digest_id, "email")

    # 3. Telegram
    tg_ok = await send_telegram_summary(
        bot=bot,
        chat_id=chat_id,
        digest_text=digest_text,
        week_label=week_label,
        start_date=start_date,
        end_date=end_date,
        total_tickets=total,
        resolved_count=resolved,
        open_count=open_c,
        top_issue=top_issue,
        biggest_win=biggest_win,
        drive_link=drive_link,
    )
    if tg_ok:
        await storage.update_digest_delivery(digest_id, "telegram")


def _parse_digest_stats(digest_text: str) -> tuple[int, int, int]:
    """Quick parse of at-a-glance numbers from digest text."""
    total = resolved = open_c = 0
    for line in digest_text.splitlines():
        line = line.strip()
        if line.startswith("• Total tickets:"):
            try:
                total = int(line.split(":")[-1].strip())
            except ValueError:
                pass
        elif line.startswith("• Resolved:"):
            try:
                resolved = int(line.split(":")[-1].strip())
            except ValueError:
                pass
        elif line.startswith("• Still open:"):
            try:
                open_c = int(line.split(":")[-1].strip())
            except ValueError:
                pass
    return total, resolved, open_c


def _parse_digest_highlights(digest_text: str) -> tuple[str, str]:
    """Extract top issue and biggest win from the digest."""
    top_issue = ""
    biggest_win = ""
    in_top = False
    in_wins = False

    for line in digest_text.splitlines():
        stripped = line.strip()
        if "TOP ISSUES THIS WEEK" in stripped:
            in_top = True
            in_wins = False
            continue
        if "HANDOFF & PIPELINE" in stripped:
            in_top = False
            continue
        if "WINS" in stripped and "═" not in stripped:
            in_wins = True
            in_top = False
            continue
        if "OPERATOR NOTES" in stripped:
            in_wins = False
            continue

        if in_top and stripped and not stripped.startswith("═") and not top_issue:
            top_issue = stripped[:80]
        if in_wins and stripped.startswith("•") and not biggest_win:
            biggest_win = stripped[1:].strip()[:80]

    return top_issue, biggest_win
