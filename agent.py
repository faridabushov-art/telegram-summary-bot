"""
agent.py — LangGraph ReAct managed agent.

All AI and storage logic lives here. handlers.py calls only:
  - process_message(...)
  - process_summary(...)

Tools are defined as async functions so they integrate cleanly with the
running asyncio event loop managed by python-telegram-bot v21.
"""

import base64
import logging
import os
import tempfile

from langchain_anthropic import ChatAnthropic
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage
from langgraph.prebuilt import create_react_agent

import storage

logger = logging.getLogger(__name__)

# ── Model (lazy) ─────────────────────────────────────────────────────────────
# Instantiated on first use so the module can be imported before .env is loaded.
_llm: ChatAnthropic | None = None
_agent = None


def _get_llm() -> ChatAnthropic:
    global _llm
    if _llm is None:
        _llm = ChatAnthropic(
            model="claude-haiku-4-5",
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
            max_tokens=2048,
        )
    return _llm


def _get_agent():
    global _agent
    if _agent is None:
        _agent = create_react_agent(
            model=_get_llm(),
            tools=tools,
        )
    return _agent


# ── Tools ────────────────────────────────────────────────────────────────────

@tool
async def store_message(
    chat_id: int,
    sender_name: str,
    sender_id: int,
    msg_type: str,
    content: str,
) -> str:
    """
    Save a processed message to the conversation log database.
    Call this after every message is processed (text, voice transcript, or image
    description). msg_type must be one of: text, voice, image, file.
    Returns a confirmation string.
    """
    await storage.insert_message(chat_id, sender_name, sender_id, msg_type, content)
    return f"Stored {msg_type} message from {sender_name} in chat {chat_id}."


@tool
def transcribe_voice(ogg_b64: str) -> str:
    """
    Transcribe a voice message into text using OpenAI Whisper.
    Input: base64-encoded bytes of an OGG audio file (as a string).
    Returns the transcription as plain text, or a fallback error string.
    Use this tool whenever a voice/audio message arrives.
    """
    tmp_path = None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        ogg_bytes = base64.b64decode(ogg_b64)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(ogg_bytes)
            tmp_path = f.name

        with open(tmp_path, "rb") as audio_file:
            result = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
            )
        return result.text
    except Exception as e:
        logger.exception("Voice transcription failed")
        return f"[voice message — transcription failed: {str(e)}]"
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


@tool
def describe_image(image_b64: str, mime_type: str) -> str:
    """
    Describe the content of an image using Claude's vision capability.
    Input:
      image_b64: base64-encoded image bytes as a string.
      mime_type: MIME type string, e.g. "image/jpeg" or "image/png".
    Returns a 2-3 sentence business-focused description of what is visible.
    Use this tool whenever a photo or image document arrives.
    """
    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

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
                            "data": image_b64,
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
    except Exception as e:
        logger.exception("Image description failed")
        return f"[image — description unavailable: {str(e)}]"


@tool
async def get_history(chat_id: int, limit: int = 200) -> str:
    """
    Retrieve the stored conversation history for a given chat.
    Returns a plain-text transcript formatted as:
      [timestamp] SenderName (msg_type): content
    Use this tool before generating a summary.
    """
    messages = await storage.fetch_messages(chat_id, limit)
    if not messages:
        return "No messages stored yet for this chat."

    lines = []
    for m in messages:
        lines.append(
            f"[{m['timestamp']}] {m['sender_name']} ({m['msg_type']}): {m['content']}"
        )
    return "\n".join(lines)


@tool
def build_summary(transcript: str, language: str = "English") -> str:
    """
    Generate a structured summary of a conversation transcript.
    Input:
      transcript: the full conversation text returned by get_history.
      language: output language for the summary (default: English).
    Returns a Markdown-formatted summary with four sections.
    Use this tool after retrieving the history with get_history.
    """
    if transcript.count("\n") < 2:
        return (
            "Not enough conversation to summarize yet. "
            "Keep chatting and run /summary again."
        )

    from anthropic import Anthropic
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1024,
        system=(
            f"You are a business assistant summarizing internal Telegram group "
            f"conversations between sales staff and ERP administrators. "
            f"Be concise and factual. Use bullet points. "
            f"Do not invent information. Write in {language}."
        ),
        messages=[{
            "role": "user",
            "content": (
                "Summarize this conversation using exactly these four sections:\n\n"
                "**📋 What Sales Requested**\n"
                "- Each distinct request or question from sales staff.\n\n"
                "**✅ What Was Resolved**\n"
                "- Each request that received a clear answer or action.\n\n"
                "**⏳ Still Open / Needs Follow-Up**\n"
                "- Requests without a clear answer or pending action.\n\n"
                "**👥 Participants**\n"
                "- List each person and their apparent role "
                "(sales / ERP admin / unclear).\n\n"
                f"Conversation log:\n{transcript}"
            ),
        }],
    )
    return response.content[0].text


# ── Tool registry ────────────────────────────────────────────────────────────
tools = [store_message, transcribe_voice, describe_image, get_history, build_summary]


# ── Public API called by handlers.py ────────────────────────────────────────

async def process_message(
    chat_id: int,
    sender_name: str,
    sender_id: int,
    msg_type: str,
    raw_payload: str | bytes,
    mime_type: str = "image/jpeg",
) -> None:
    """
    Process one incoming Telegram message through the agent.
    The agent decides which tools to call based on msg_type.
    No return value — the agent stores the result via tools.
    """
    if msg_type in ("text", "file"):
        payload_str = raw_payload if isinstance(raw_payload, str) else raw_payload.decode()
    else:
        payload_str = (
            base64.b64encode(raw_payload).decode()
            if isinstance(raw_payload, (bytes, bytearray))
            else raw_payload
        )

    prompt = (
        f"A new message arrived in Telegram group chat {chat_id}.\n"
        f"Sender: {sender_name} (ID: {sender_id})\n"
        f"Type: {msg_type}\n"
        f"Payload: {payload_str}\n"
        f"Mime type (if image): {mime_type}\n\n"
        f"Process this message:\n"
        f"1. If type is 'voice', call transcribe_voice with the payload, "
        f"   then call store_message with the transcript.\n"
        f"2. If type is 'image', call describe_image with the payload and mime_type, "
        f"   then call store_message with the description.\n"
        f"3. If type is 'text' or 'file', call store_message directly with the payload.\n"
        f"Use chat_id={chat_id}, sender_name='{sender_name}', sender_id={sender_id}."
    )

    await _get_agent().ainvoke({"messages": [HumanMessage(content=prompt)]})


async def process_summary(chat_id: int, language: str = "English") -> str:
    """
    Generate a summary of the conversation in chat_id.
    Returns the summary text to be posted back to Telegram.
    """
    prompt = (
        f"Generate a structured summary of the conversation in Telegram group "
        f"chat {chat_id}.\n"
        f"Steps:\n"
        f"1. Call get_history with chat_id={chat_id}.\n"
        f"2. Call build_summary with the returned transcript and language='{language}'.\n"
        f"3. Return the summary text as your final answer."
    )

    result = await _get_agent().ainvoke({"messages": [HumanMessage(content=prompt)]})
    return result["messages"][-1].content
