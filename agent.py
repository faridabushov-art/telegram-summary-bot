"""
agent.py — LangGraph ReAct agent used ONLY for /summary generation.

Handlers store messages directly (no agent needed for that).
The agent is only invoked when a user runs /summary.
"""

import logging
import os

from langchain_anthropic import ChatAnthropic
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage
from langgraph.prebuilt import create_react_agent

import storage

logger = logging.getLogger(__name__)

_llm = None
_agent = None


def _get_llm():
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
        _agent = create_react_agent(model=_get_llm(), tools=tools)
    return _agent


@tool
async def get_history(chat_id: int, limit: int = 200) -> str:
    """
    Retrieve the stored conversation history for a given Telegram group chat.
    Returns a plain-text transcript. Call this first before build_summary.
    """
    messages = await storage.fetch_messages(chat_id, limit)
    if not messages:
        return f"No messages stored yet for chat_id={chat_id}."
    lines = [
        f"[{m['timestamp']}] {m['sender_name']} ({m['msg_type']}): {m['content']}"
        for m in messages
    ]
    return "\n".join(lines)


@tool
def build_summary(transcript: str, language: str = "English") -> str:
    """
    Generate a structured Markdown summary from a conversation transcript.
    Call this after get_history. Returns a four-section summary.
    """
    if not transcript or transcript.startswith("No messages"):
        return "No conversation history found. Send some messages first, then run /summary."

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
                "- List each person and their apparent role (sales / ERP admin / unclear).\n\n"
                f"Conversation log:\n{transcript}"
            ),
        }],
    )
    return response.content[0].text


tools = [get_history, build_summary]


async def process_summary(chat_id: int, language: str = "English") -> str:
    """
    Retrieve history and generate a summary for the given chat.
    Called by handlers.cmd_summary.
    """
    prompt = (
        f"Generate a structured summary for Telegram group chat {chat_id}.\n"
        f"Steps:\n"
        f"1. Call get_history with chat_id={chat_id}.\n"
        f"2. Call build_summary with the transcript and language='{language}'.\n"
        f"3. Return the summary as your final answer."
    )
    result = await _get_agent().ainvoke({"messages": [HumanMessage(content=prompt)]})
    return result["messages"][-1].content
