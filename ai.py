"""
ai.py — Direct AI calls for voice transcription and image description.
Used by handlers.py to process messages before storing them.
"""

import base64
import logging
import os
import tempfile

logger = logging.getLogger(__name__)


async def transcribe_voice(ogg_bytes: bytes) -> str:
    """Transcribe a voice OGG file via OpenAI Whisper. Returns text or fallback."""
    tmp_path = None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(ogg_bytes)
            tmp_path = f.name

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
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


async def describe_image(image_bytes: bytes, mime_type: str) -> str:
    """Describe an image via Claude Haiku vision. Returns description or fallback."""
    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        b64 = base64.standard_b64encode(image_bytes).decode()

        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=256,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime_type,
                            "data": b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "This image was shared in a business group chat between "
                            "sales staff and ERP administrators. Describe what is "
                            "shown in 2-3 sentences, focusing on any data, tables, "
                            "error messages, screenshots, or documents visible."
                        ),
                    },
                ],
            }],
        )
        return response.content[0].text
    except Exception:
        logger.exception("Image description failed")
        return "[image — description unavailable]"
