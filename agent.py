"""
agent.py — LangGraph ReAct agent for /summary generation + POS analysis tools.

Summary agent: invoked on /summary — uses get_history + build_summary.
Analysis tools: used by the weekly analysis pipeline — extract_tickets,
                categorize_root_cause, check_playbook, update_patterns.
"""

import json
import logging
import os

from langchain_anthropic import ChatAnthropic
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage
from langgraph.prebuilt import create_react_agent

import storage

logger = logging.getLogger(__name__)

_llm = None
_summary_agent = None


def _get_llm():
    global _llm
    if _llm is None:
        _llm = ChatAnthropic(
            model="claude-haiku-4-5",
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
            max_tokens=2048,
        )
    return _llm


def _get_summary_agent():
    global _summary_agent
    if _summary_agent is None:
        _summary_agent = create_react_agent(model=_get_llm(), tools=summary_tools)
    return _summary_agent


# ── Summary Tools ─────────────────────────────────────────────────────────────

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
    Call this after get_history. Returns a five-section POS support summary.
    """
    if not transcript or transcript.startswith("No messages"):
        return "No conversation history found. Send some messages first, then run /summary."

    from anthropic import Anthropic
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1500,
        system=(
            f"You are an ERP POS support analyst summarizing internal Telegram group "
            f"conversations between franchise store staff and HQ ERP operators/administrators. "
            f"There are 2+ HQ operators who connect interchangeably to resolve store issues.\n\n"
            f"Be concise and factual. Use bullet points. Do not invent information. "
            f"The conversation may be in Azerbaijani, Russian, English, or mixed. "
            f"Write your summary in {language}.\n\n"
            f"Pay special attention to:\n"
            f"- Which store reported each issue\n"
            f"- Which operator(s) responded\n"
            f"- Whether issues were resolved or are still open\n"
            f"- Any handoffs between operators and whether context was preserved\n"
            f"- Time gaps between store reports and operator responses"
        ),
        messages=[{
            "role": "user",
            "content": (
                "Summarize this conversation using exactly these five sections:\n\n"
                "**🏪 Issues Reported by Stores**\n"
                "- Each distinct POS/ERP issue, which store, exact symptom described.\n\n"
                "**🔧 Resolved by Operators**\n"
                "- Each issue that was fixed, which operator resolved it, how.\n\n"
                "**⏳ Still Open / Needs Follow-Up**\n"
                "- Unresolved issues with current status.\n\n"
                "**🔄 Handoffs**\n"
                "- Any issues that changed operators, whether context was preserved. "
                "Write 'None' if no handoffs occurred.\n\n"
                "**👥 Participants**\n"
                "- Each person and their role (store staff / HQ operator / unclear).\n\n"
                f"Conversation log:\n{transcript}"
            ),
        }],
    )
    return response.content[0].text


summary_tools = [get_history, build_summary]


# ── Analysis Tools ────────────────────────────────────────────────────────────

@tool
async def extract_tickets(messages_json: str) -> str:
    """
    LLM-powered ticket extraction from a JSON array of raw messages.
    messages_json: JSON string of message objects with timestamp, sender_name, sender_id, msg_type, content.
    Returns a JSON string with extracted tickets.
    """
    try:
        messages = json.loads(messages_json)
    except json.JSONDecodeError:
        return json.dumps({"error": "Invalid JSON input", "tickets": []})

    from anthropic import Anthropic
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    lines = [
        f"[{m['timestamp']}] {m['sender_name']} (id:{m.get('sender_id', 0)}) ({m['msg_type']}): {m['content']}"
        for m in messages
    ]
    transcript = "\n".join(lines)

    prompt = f"""Analyze this POS/ERP support chat log (may be in Azerbaijani, Russian, English, or mixed).
Extract each distinct support issue as a structured ticket.

Return ONLY valid JSON:
{{"tickets": [
  {{
    "store_name": "...",
    "symptom": "...",
    "trigger_conditions": "...",
    "impact": "...",
    "workaround": "...",
    "resolution_path": "...",
    "resolution_minutes": null,
    "delay_location": "...",
    "root_cause": "config|data-sync|network|hardware|user-error|software-bug|integration|process-gap|unknown",
    "status": "open|resolved|workaround",
    "created_at": "ISO timestamp",
    "resolved_at": "ISO timestamp or null",
    "operators": [{{"operator_name":"...","operator_id":0,"role":"first_responder|handoff","engaged_at":"...","continuity_intact":true}}]
  }}
]}}

Log:
{transcript}"""

    try:
        response = client.messages.create(
            model=os.getenv("ANALYSIS_MODEL", "claude-haiku-4-5"),
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return raw
    except Exception as e:
        logger.exception("extract_tickets tool failed")
        return json.dumps({"error": str(e), "tickets": []})


@tool
def categorize_root_cause(symptom: str, context: str = "") -> str:
    """
    Categorize the root cause of a POS/ERP issue from its symptom description.
    Returns a JSON string with root_cause and confidence.
    Valid categories: config | data-sync | network | hardware | user-error |
                      software-bug | integration | process-gap | unknown
    """
    from anthropic import Anthropic
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    prompt = f"""Classify the root cause of this POS/ERP support issue.

Symptom: {symptom}
Context: {context or 'none provided'}

Categories:
- config: wrong setting, misconfigured parameter
- data-sync: data not syncing between POS and ERP, stale data
- network: connectivity issues, timeouts
- hardware: physical device failure, printer, scanner, terminal
- user-error: operator or store staff mistake
- software-bug: application crash, unexpected behavior
- integration: API failure, third-party system issue
- process-gap: no procedure exists or wasn't followed
- unknown: insufficient information

Prefer specificity. Use "data-sync (suspected)" when symptoms suggest but aren't conclusive.

Return ONLY JSON: {{"root_cause": "...", "confidence": "high|medium|low", "reasoning": "one sentence"}}"""

    try:
        response = client.messages.create(
            model=os.getenv("ANALYSIS_MODEL", "claude-haiku-4-5"),
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        logger.exception("categorize_root_cause tool failed")
        return json.dumps({"root_cause": "unknown", "confidence": "low", "reasoning": str(e)})


@tool
async def check_playbook(symptom: str) -> str:
    """
    Search the playbook for entries matching a symptom description.
    Returns a JSON string with matching playbook entries (empty list if none found).
    """
    try:
        # Search by words in symptom
        words = [w.strip() for w in symptom.split() if len(w.strip()) > 3]
        results = []
        seen_ids = set()

        for word in words[:5]:  # Check first 5 significant words
            matches = await storage.search_playbook(word)
            for m in matches:
                if m["id"] not in seen_ids:
                    seen_ids.add(m["id"])
                    results.append({
                        "id": m["id"],
                        "title": m["title"],
                        "symptoms": m["symptoms"][:200],
                        "fix_steps": m["fix_steps"][:300],
                        "root_cause": m["root_cause"],
                        "times_used": m["times_used"],
                    })

        return json.dumps({"matches": results[:5], "total": len(results)})
    except Exception as e:
        logger.exception("check_playbook tool failed")
        return json.dumps({"matches": [], "error": str(e)})


@tool
async def update_patterns(tickets_json: str) -> str:
    """
    Update the patterns table based on a JSON array of tickets.
    Returns a summary of pattern changes made.
    """
    try:
        data = json.loads(tickets_json)
        tickets = data if isinstance(data, list) else data.get("tickets", [])
    except json.JSONDecodeError:
        return json.dumps({"error": "Invalid JSON", "updated": 0})

    if not tickets:
        return json.dumps({"updated": 0, "message": "No tickets to process"})

    # Group by root_cause
    root_cause_groups: dict[str, list] = {}
    for t in tickets:
        rc = t.get("root_cause", "unknown")
        root_cause_groups.setdefault(rc, []).append(t)

    updated = 0
    created = 0
    existing_patterns = await storage.fetch_patterns()

    for root_cause, group_tickets in root_cause_groups.items():
        if len(group_tickets) < 2:
            continue  # Only track patterns with 2+ occurrences

        stores = list({t.get("store_name", "unknown") for t in group_tickets})
        stores_str = ", ".join(stores)
        systemic = len(stores) > 1
        pattern_name = f"Recurring {root_cause} issue"

        existing = next(
            (p for p in existing_patterns if p["pattern_name"] == pattern_name), None
        )
        if existing:
            await storage.update_pattern(
                existing["id"],
                occurrence_count=existing["occurrence_count"] + len(group_tickets),
                trend="strengthening",
                stores_affected=stores_str,
                systemic=int(systemic),
                last_seen=existing["updated_at"],
            )
            updated += 1
        else:
            await storage.insert_pattern(
                pattern_name=pattern_name,
                description=f"{len(group_tickets)} tickets with {root_cause} root cause",
                root_cause=root_cause,
                stores_affected=stores_str,
                systemic=systemic,
            )
            created += 1

    return json.dumps({
        "updated": updated,
        "created": created,
        "message": f"Processed {len(tickets)} tickets, updated {updated} patterns, created {created} new patterns",
    })


analysis_tools = [extract_tickets, categorize_root_cause, check_playbook, update_patterns]
all_tools = summary_tools + analysis_tools


# ── Public API ────────────────────────────────────────────────────────────────

async def process_summary(chat_id: int, language: str = "English") -> str:
    """
    Retrieve history and generate a summary for the given chat.
    Called by handlers.cmd_summary.
    """
    prompt = (
        f"Generate a structured POS support summary for Telegram group chat {chat_id}.\n"
        f"Steps:\n"
        f"1. Call get_history with chat_id={chat_id}.\n"
        f"2. Call build_summary with the transcript and language='{language}'.\n"
        f"3. Return the summary as your final answer."
    )
    result = await _get_summary_agent().ainvoke({"messages": [HumanMessage(content=prompt)]})
    return result["messages"][-1].content
