import base64
import logging
import os
import tempfile

import anthropic
import openai

logger = logging.getLogger(__name__)


def _get_anthropic_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def _get_openai_client() -> openai.OpenAI:
    return openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


async def transcribe_voice(ogg_bytes: bytes) -> str:
    """
    Write ogg_bytes to a temporary .ogg file and transcribe via Whisper.
    Returns the transcription string, or a fallback message on failure.
    """
    try:
        client = _get_openai_client()
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(ogg_bytes)
            tmp_path = tmp.name

        with open(tmp_path, "rb") as audio_file:
            result = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
            )
        return result.text
    except Exception:
        logger.exception("Voice transcription failed")
        return "[voice message — transcription failed]"
    finally:
        try:
            import os as _os
            _os.unlink(tmp_path)
        except Exception:
            pass


async def describe_image(image_bytes: bytes, mime_type: str) -> str:
    """
    Base64-encode image_bytes and ask Claude Haiku to describe it.
    Returns the description string, or a fallback message on failure.
    """
    try:
        client = _get_anthropic_client()
        base64_string = base64.standard_b64encode(image_bytes).decode("utf-8")

        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=256,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime_type,
                                "data": base64_string,
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                "This image was shared in a business chat between sales staff "
                                "and ERP system administrators. Describe what is shown in 2-3 "
                                "sentences, focusing on any data, tables, error messages, "
                                "screenshots, or documents visible."
                            ),
                        },
                    ],
                }
            ],
        )
        return response.content[0].text
    except Exception:
        logger.exception("Image description failed")
        return "[image — description unavailable]"


async def generate_summary(messages: list[dict], language: str = "English") -> str:
    """
    Build a transcript from messages and generate a structured summary via Claude Haiku.
    Returns a fallback string if fewer than 3 messages exist.
    """
    if len(messages) < 3:
        return "Not enough conversation to summarize yet. Keep chatting and run /summary again."

    # Build plain-text transcript
    lines = []
    for msg in messages:
        # Parse timestamp — strip microseconds for readability
        ts = msg["timestamp"]
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(ts)
            ts_fmt = dt.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            ts_fmt = ts

        msg_type = msg["msg_type"]
        sender = msg["sender_name"]
        content = msg["content"]

        if msg_type == "image":
            line = f"[{ts_fmt}] {sender} (image): [Image shows: {content}]"
        else:
            line = f"[{ts_fmt}] {sender} ({msg_type}): {content}"
        lines.append(line)

    transcript = "\n".join(lines)

    system_prompt = (
        f"You are a business assistant summarizing internal chat conversations between "
        f"sales staff and ERP system administrators. Be concise and factual. Use bullet "
        f"points. Do not invent information. Write in {language}."
    )

    user_prompt = (
        "Below is a conversation log from a Telegram group. Summarize it with these sections:\n\n"
        "**📋 What Sales Requested**\n"
        "- List each distinct request or question from sales staff as a bullet point.\n\n"
        "**✅ What Was Resolved**\n"
        "- List each request that received a clear answer or was completed.\n\n"
        "**⏳ Still Open / Needs Follow-Up**\n"
        "- List any requests that were not answered or need further action.\n\n"
        "**👥 Participants**\n"
        "- List unique participants and their apparent role (sales / ERP admin / unclear).\n\n"
        f"Conversation log:\n{transcript}"
    )

    try:
        client = _get_anthropic_client()
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1024,
            system=system_prompt,
            messages=[
                {"role": "user", "content": user_prompt}
            ],
        )
        return response.content[0].text
    except Exception:
        logger.exception("Summary generation failed")
        return "[Summary generation failed. Please try again later.]"
