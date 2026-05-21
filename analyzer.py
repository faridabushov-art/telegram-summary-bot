"""
analyzer.py — Weekly POS support pattern analysis engine.

Pipeline:
  Step 1 — Ingest & Tag:     Extract structured tickets from raw messages (LLM)
  Step 2 — Pattern Detection: Update patterns table from ticket data
  Step 3 — Cross-reference:  Check tickets against playbook / known_issues
  Step 4 — Update Playbook:  Persist new resolutions discovered this week
  Step 5 — Generate Digest:  Build the full weekly digest text
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

from anthropic import Anthropic

import storage

logger = logging.getLogger(__name__)

_ANALYSIS_MODEL = os.getenv("ANALYSIS_MODEL", "claude-haiku-4-5")


def _anthropic() -> Anthropic:
    return Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def _week_label(dt: datetime) -> str:
    """Return ISO week label, e.g. '2026-W21'."""
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _week_bounds(dt: datetime) -> tuple[datetime, datetime]:
    """Return (Monday 00:00, Sunday 23:59:59) for the week containing dt."""
    monday = dt - timedelta(days=dt.weekday())
    monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    sunday = monday + timedelta(days=6, hours=23, minutes=59, seconds=59)
    return monday, sunday


# ── Step 1 — Ingest & Tag ─────────────────────────────────────────────────────

async def _fetch_week_messages(chat_id: int) -> list[dict]:
    """Return all messages from the past 7 days for this chat."""
    since = (datetime.utcnow() - timedelta(days=7)).isoformat()
    return await storage.fetch_messages_since(chat_id, since)


def _build_extraction_prompt(messages: list[dict]) -> str:
    lines = [
        f"[{m['timestamp']}] {m['sender_name']} (id:{m['sender_id']}) ({m['msg_type']}): {m['content']}"
        for m in messages
    ]
    transcript = "\n".join(lines)

    return f"""You are a POS/ERP support analyst. Analyze the following Telegram group chat log between franchise store staff and HQ ERP operators.

IMPORTANT: Messages may be in Azerbaijani, Russian, English, or mixed languages. Analyze all of them regardless of language; write your output in English.

Your task: extract every distinct support issue/ticket from this conversation. A ticket is a distinct problem reported by store staff that required operator involvement.

For each ticket return a JSON object with these exact fields:
- store_name: which franchise location reported it (infer from context, use "Unknown Store" if unclear)
- symptom: exact words the store used to describe the problem (translate to English if needed)
- trigger_conditions: time of day, transaction type, network state, shift change, ERP update, etc. (null if unknown)
- impact: blocked sales, queue backup, data mismatch, void/manual entry, etc. (null if unknown)
- workaround: what the store tried before operator responded (null if none mentioned)
- resolution_path: how it was actually resolved; "still open" if unresolved (null if unknown)
- resolution_minutes: estimated minutes from first report to resolution confirmation (null if open or unknown)
- delay_location: where the delay sat — "store-side", "operator-side", "handoff", "system" (null if unknown)
- root_cause: one of: config | data-sync | network | hardware | user-error | software-bug | integration | process-gap | unknown. Use "data-sync (suspected)" style when unsure but symptoms suggest a category.
- status: "resolved" | "workaround" | "open"
- created_at: ISO timestamp of first message about this issue
- resolved_at: ISO timestamp of resolution confirmation (null if open)
- operators: array of objects with fields:
    - operator_name: name as it appears in chat
    - operator_id: numeric sender_id from transcript (0 if not determinable)
    - role: "first_responder" | "handoff"
    - engaged_at: ISO timestamp when this operator first engaged
    - continuity_intact: true if store did NOT have to re-explain; false if they did

Return ONLY valid JSON in this exact structure — no markdown, no explanation:
{{
  "tickets": [ ...ticket objects... ]
}}

If there are no support issues in the log, return: {{"tickets": []}}

Conversation log:
{transcript}"""


async def _extract_tickets_llm(messages: list[dict], chat_id: int, week_number: str) -> list[dict]:
    """Call the LLM to extract structured tickets from raw messages."""
    if not messages:
        logger.info("No messages to analyze for chat %d", chat_id)
        return []

    prompt = _build_extraction_prompt(messages)
    client = _anthropic()

    try:
        response = client.messages.create(
            model=_ANALYSIS_MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()

        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        data = json.loads(raw)
        tickets = data.get("tickets", [])
        logger.info("LLM extracted %d tickets for chat %d week %s", len(tickets), chat_id, week_number)
        return tickets
    except json.JSONDecodeError as e:
        logger.error("Failed to parse LLM ticket extraction JSON: %s", e)
        return []
    except Exception:
        logger.exception("LLM ticket extraction failed for chat %d", chat_id)
        return []


async def _store_tickets(tickets: list[dict], chat_id: int, week_number: str) -> list[int]:
    """Insert extracted tickets (and their operators) into the DB. Returns list of ticket IDs."""
    inserted_ids = []
    for t in tickets:
        try:
            ticket_id = await storage.insert_ticket(
                chat_id=chat_id,
                symptom=t.get("symptom", "unknown"),
                root_cause=t.get("root_cause", "unknown"),
                status=t.get("status", "open"),
                week_number=week_number,
                store_name=t.get("store_name", ""),
                trigger_conditions=t.get("trigger_conditions") or "",
                impact=t.get("impact") or "",
                workaround=t.get("workaround") or "",
                resolution_path=t.get("resolution_path") or "",
                resolution_minutes=t.get("resolution_minutes"),
                delay_location=t.get("delay_location") or "",
                created_at=t.get("created_at", ""),
                resolved_at=t.get("resolved_at"),
            )
            inserted_ids.append(ticket_id)

            for op in t.get("operators", []):
                await storage.insert_ticket_operator(
                    ticket_id=ticket_id,
                    operator_name=op.get("operator_name", "unknown"),
                    operator_id=op.get("operator_id", 0),
                    role=op.get("role", "responder"),
                    engaged_at=op.get("engaged_at", ""),
                    continuity_intact=bool(op.get("continuity_intact", True)),
                )
        except Exception:
            logger.exception("Failed to store ticket: %s", t.get("symptom", "?"))

    return inserted_ids


# ── Step 2 — Pattern Detection ────────────────────────────────────────────────

async def _detect_patterns(tickets: list[dict], chat_id: int, week_number: str) -> None:
    """Update patterns table from this week's tickets."""
    if not tickets:
        return

    tickets_json = json.dumps(tickets, indent=2)
    existing_patterns = await storage.fetch_patterns()
    existing_json = json.dumps([
        {"id": p["id"], "pattern_name": p["pattern_name"], "description": p["description"],
         "root_cause": p["root_cause"], "occurrence_count": p["occurrence_count"]}
        for p in existing_patterns
    ], indent=2)

    prompt = f"""You are a POS support patterns analyst. Given this week's support tickets and the existing pattern registry, identify patterns.

This week's tickets:
{tickets_json}

Existing patterns registry:
{existing_json}

For each pattern you identify:
1. If it matches an existing pattern (by name or similar description), return it with trend: "strengthening" if more occurrences, or "weakening" if fewer
2. If it's new, return it with trend: "new"

A pattern is: same root cause recurring across 2+ tickets, or same symptom type, or same store repeatedly affected.

Return ONLY valid JSON:
{{
  "patterns": [
    {{
      "existing_id": null,
      "pattern_name": "short descriptive name",
      "description": "what is recurring and why it matters",
      "root_cause": "same categories as tickets",
      "occurrence_count": <int — count this week>,
      "stores_affected": "comma-separated store names or 'single-store'",
      "systemic": <true if multiple stores affected>,
      "trend": "new|strengthening|weakening|resolved",
      "timing_correlation": "e.g. spikes after weekly sync, or null"
    }}
  ]
}}

If no patterns found, return: {{"patterns": []}}"""

    client = _anthropic()
    try:
        response = client.messages.create(
            model=_ANALYSIS_MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)

        now = datetime.utcnow().isoformat()
        for p in data.get("patterns", []):
            existing_id = p.get("existing_id")
            if existing_id:
                # Update existing pattern
                existing = next((e for e in existing_patterns if e["id"] == existing_id), None)
                if existing:
                    await storage.update_pattern(
                        existing_id,
                        occurrence_count=existing["occurrence_count"] + p.get("occurrence_count", 1),
                        trend=p.get("trend", "strengthening"),
                        stores_affected=p.get("stores_affected", existing["stores_affected"]),
                        systemic=int(p.get("systemic", existing["systemic"])),
                        last_seen=now,
                        timing_correlation=p.get("timing_correlation") or existing.get("timing_correlation", ""),
                    )
            else:
                # Check if same-named pattern exists
                existing_match = next(
                    (e for e in existing_patterns if e["pattern_name"] == p["pattern_name"]), None
                )
                if existing_match:
                    await storage.update_pattern(
                        existing_match["id"],
                        occurrence_count=existing_match["occurrence_count"] + p.get("occurrence_count", 1),
                        trend=p.get("trend", "strengthening"),
                        last_seen=now,
                    )
                else:
                    await storage.insert_pattern(
                        pattern_name=p["pattern_name"],
                        description=p["description"],
                        root_cause=p.get("root_cause", ""),
                        stores_affected=p.get("stores_affected", ""),
                        systemic=bool(p.get("systemic", False)),
                        timing_correlation=p.get("timing_correlation") or "",
                    )
        logger.info("Pattern detection complete for week %s", week_number)
    except Exception:
        logger.exception("Pattern detection failed")


# ── Step 3 — Cross-reference ──────────────────────────────────────────────────

async def _cross_reference_tickets(tickets: list[dict], ticket_ids: list[int]) -> dict:
    """
    For each ticket, check against playbook and known_issues.
    Returns a summary dict with flags for training gaps, fix failures, and new issues.
    """
    playbook = await storage.fetch_playbook()
    known_issues = await storage.fetch_known_issues()

    cross_ref = {
        "training_gap_store": [],   # fix existed, store didn't know
        "training_gap_operator": [],  # fix existed, operator didn't apply it
        "fix_failure": [],           # known issue recurred despite fix
        "new_issues": [],            # genuinely new, added to known_issues
        "playbook_hits": [],         # playbook entry used successfully
    }

    if not tickets:
        return cross_ref

    playbook_json = json.dumps([
        {"id": p["id"], "title": p["title"], "symptoms": p["symptoms"], "root_cause": p["root_cause"]}
        for p in playbook
    ])
    known_json = json.dumps([
        {"id": k["id"], "title": k["title"], "description": k["description"], "status": k["status"]}
        for k in known_issues
    ])

    for i, ticket in enumerate(tickets):
        ticket_id = ticket_ids[i] if i < len(ticket_ids) else None

        prompt = f"""A POS support ticket was logged this week:
Symptom: {ticket.get('symptom', '')}
Root cause: {ticket.get('root_cause', '')}
Status: {ticket.get('status', '')}
Resolution path: {ticket.get('resolution_path', '') or 'none'}

Existing playbook entries:
{playbook_json}

Known issues:
{known_json}

Classify this ticket as exactly ONE of:
- "playbook_hit": a playbook entry exists AND was applied (resolution matches playbook)
- "training_gap_store": a playbook entry exists but the store didn't know about it (had to escalate something self-serviceable)
- "training_gap_operator": a playbook entry exists but the operator didn't use it (used a different/slower fix)
- "fix_failure": this is a known issue that was supposed to be fixed but recurred
- "new_issue": genuinely new, not in playbook or known issues

Return ONLY JSON:
{{"classification": "<one of the above>", "playbook_id": <id or null>, "known_issue_id": <id or null>, "confidence": "high|medium|low"}}"""

        try:
            client = _anthropic()
            response = client.messages.create(
                model=_ANALYSIS_MODEL,
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            result = json.loads(raw)
            classification = result.get("classification", "new_issue")

            if classification == "playbook_hit":
                cross_ref["playbook_hits"].append({
                    "ticket": ticket, "ticket_id": ticket_id,
                    "playbook_id": result.get("playbook_id"),
                })
                pb_id = result.get("playbook_id")
                if pb_id:
                    await storage.increment_playbook_usage(pb_id)
            elif classification == "training_gap_store":
                cross_ref["training_gap_store"].append({
                    "ticket": ticket, "ticket_id": ticket_id,
                    "playbook_id": result.get("playbook_id"),
                })
            elif classification == "training_gap_operator":
                cross_ref["training_gap_operator"].append({
                    "ticket": ticket, "ticket_id": ticket_id,
                    "playbook_id": result.get("playbook_id"),
                })
            elif classification == "fix_failure":
                cross_ref["fix_failure"].append({
                    "ticket": ticket, "ticket_id": ticket_id,
                    "known_issue_id": result.get("known_issue_id"),
                })
                ki_id = result.get("known_issue_id")
                if ki_id:
                    await storage.update_known_issue(ki_id, status="investigating",
                                                     resolution_notes="Recurred after fix attempt")
            elif classification == "new_issue":
                cross_ref["new_issues"].append({"ticket": ticket, "ticket_id": ticket_id})
                # Insert into known_issues
                await storage.insert_known_issue(
                    title=ticket.get("symptom", "Unknown issue")[:100],
                    description=ticket.get("symptom", ""),
                    root_cause=ticket.get("root_cause", ""),
                    affected_stores=ticket.get("store_name", ""),
                )
        except Exception:
            logger.exception("Cross-reference failed for ticket: %s", ticket.get("symptom", "?"))

    return cross_ref


# ── Step 4 — Update Playbook ──────────────────────────────────────────────────

async def _update_playbook(tickets: list[dict]) -> list[dict]:
    """
    For each resolved ticket with no existing playbook entry, generate and insert
    a new playbook entry. Returns list of new entries added.
    """
    resolved = [
        t for t in tickets
        if t.get("status") in ("resolved", "workaround") and t.get("resolution_path")
    ]
    if not resolved:
        return []

    new_entries = []
    client = _anthropic()

    for ticket in resolved:
        prompt = f"""A POS/ERP support issue was resolved this week. Create a reusable playbook entry for it.

Issue details:
- Symptom: {ticket.get('symptom', '')}
- Trigger conditions: {ticket.get('trigger_conditions', '') or 'unknown'}
- Impact: {ticket.get('impact', '') or 'unknown'}
- Workaround attempted: {ticket.get('workaround', '') or 'none'}
- Actual resolution: {ticket.get('resolution_path', '')}
- Root cause: {ticket.get('root_cause', '')}

Create a playbook entry that any operator can follow WITHOUT needing the original resolver.
Write steps that are executable independently by either store staff OR an operator.

Return ONLY valid JSON:
{{
  "title": "Short name for this fix (max 60 chars)",
  "symptoms": "How the store typically describes this — in their words",
  "diagnostic_steps": "1. Check X\\n2. Verify Y\\n3. ...",
  "fix_steps": "1. Do A\\n2. Then B\\n3. ...",
  "verification": "How to confirm the issue is resolved",
  "escalation_trigger": "Stop and escalate if: X or Y",
  "root_cause": "{ticket.get('root_cause', '')}"
}}"""

        try:
            response = client.messages.create(
                model=_ANALYSIS_MODEL,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            entry = json.loads(raw)

            entry_id = await storage.insert_playbook_entry(
                title=entry["title"],
                symptoms=entry["symptoms"],
                diagnostic_steps=entry["diagnostic_steps"],
                fix_steps=entry["fix_steps"],
                verification=entry["verification"],
                escalation_trigger=entry.get("escalation_trigger", ""),
                root_cause=entry.get("root_cause", ticket.get("root_cause", "")),
            )
            entry["id"] = entry_id
            new_entries.append(entry)
            logger.info("New playbook entry added: '%s'", entry["title"])
        except Exception:
            logger.exception("Failed to create playbook entry for ticket: %s", ticket.get("symptom", "?"))

    return new_entries


# ── Step 5 — Generate Digest ──────────────────────────────────────────────────

def _safe_avg(values: list) -> float:
    vals = [v for v in values if v is not None and v > 0]
    return sum(vals) / len(vals) if vals else 0.0


async def _generate_digest(
    chat_id: int,
    tickets: list[dict],
    patterns: list[dict],
    cross_ref: dict,
    new_playbook: list[dict],
    week_number: str,
    start_date: datetime,
    end_date: datetime,
) -> str:
    """Produce the full structured weekly digest text."""

    total = len(tickets)
    resolved = [t for t in tickets if t.get("status") == "resolved"]
    open_t = [t for t in tickets if t.get("status") == "open"]
    workaround_t = [t for t in tickets if t.get("status") == "workaround"]

    avg_resolution = _safe_avg([t.get("resolution_minutes") for t in resolved])
    avg_hours = avg_resolution / 60 if avg_resolution > 0 else 0

    # Handoff analysis — tickets with >1 operator
    handoff_tickets = []
    re_explain_count = 0
    for t in tickets:
        ops = t.get("operators", [])
        if len(ops) > 1:
            handoff_tickets.append(t)
            if any(not op.get("continuity_intact", True) for op in ops):
                re_explain_count += 1

    bottleneck_tickets = [
        t for t in resolved
        if t.get("resolution_minutes") and t["resolution_minutes"] > 240
    ]

    repeat_issues = len(cross_ref.get("training_gap_store", [])) + \
                    len(cross_ref.get("training_gap_operator", [])) + \
                    len(cross_ref.get("fix_failure", []))

    # Build top issues via LLM
    top_issues_text = await _summarize_top_issues(tickets)
    systemic_text = await _summarize_systemic_issues(patterns, cross_ref)
    operator_notes = await _build_operator_notes(tickets, handoff_tickets)

    # Wins: weakening patterns + playbook hits
    wins = []
    for p in patterns:
        if p.get("trend") == "weakening":
            wins.append(f"• Pattern declining: {p['pattern_name']}")
        elif p.get("trend") == "resolved":
            wins.append(f"• Resolved: {p['pattern_name']}")
    for hit in cross_ref.get("playbook_hits", []):
        wins.append(f"• Playbook prevented escalation: {hit['ticket'].get('symptom', '')[:60]}")

    wins_text = "\n".join(wins) if wins else "No notable wins to highlight this week."

    # Playbook gaps section
    if new_playbook:
        pb_items = "\n".join(
            f"• [{e['root_cause']}] {e['title']}" for e in new_playbook
        )
        playbook_gap_text = (
            f"{len(new_playbook)} new entr{'y' if len(new_playbook)==1 else 'ies'} added to playbook:\n"
            f"{pb_items}"
        )
    else:
        playbook_gap_text = "All resolved issues already had playbook coverage."

    date_fmt = "%d %b %Y"
    digest = f"""Subject: POS Support Digest — Week of {start_date.strftime(date_fmt)} to {end_date.strftime(date_fmt)}

═══════════════════════════════════════════
THIS WEEK AT A GLANCE
═══════════════════════════════════════════
• Total tickets:                    {total}
• Resolved:                         {len(resolved)}
• Workaround (not fully fixed):     {len(workaround_t)}
• Still open:                       {len(open_t)}
• Avg time to resolution:           {avg_hours:.1f} hours
• Tickets requiring handoff:        {len(handoff_tickets)} ({int(len(handoff_tickets)/total*100) if total else 0}% of total)
• Repeat issues (fix existed):      {repeat_issues}
• Bottleneck (>4 hrs to resolve):   {len(bottleneck_tickets)}

═══════════════════════════════════════════
TOP ISSUES THIS WEEK
═══════════════════════════════════════════
{top_issues_text}

═══════════════════════════════════════════
HANDOFF & PIPELINE HEALTH
═══════════════════════════════════════════
• Tickets that changed hands:                    {len(handoff_tickets)}
• Tickets where store re-explained after handoff: {re_explain_count}
• Bottleneck tickets (>4 hrs):                   {len(bottleneck_tickets)}
• Training gap — store:                          {len(cross_ref.get('training_gap_store', []))}
• Training gap — operator:                       {len(cross_ref.get('training_gap_operator', []))}

═══════════════════════════════════════════
SYSTEMIC ISSUES NEEDING ACTION
═══════════════════════════════════════════
{systemic_text}

═══════════════════════════════════════════
PLAYBOOK GAPS
═══════════════════════════════════════════
{playbook_gap_text}

═══════════════════════════════════════════
WINS
═══════════════════════════════════════════
{wins_text}

═══════════════════════════════════════════
OPERATOR NOTES
═══════════════════════════════════════════
{operator_notes}

---
Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} | Week {week_number}
"""
    return digest


async def _summarize_top_issues(tickets: list[dict]) -> str:
    """Use LLM to rank and describe the top 3-5 issues."""
    if not tickets:
        return "No support issues recorded this week."

    prompt = f"""Given these POS support tickets from this week, identify and rank the top 3-5 issues by frequency × impact.

Tickets:
{json.dumps(tickets, indent=2)}

For each top issue, write one concise paragraph (2-3 lines) covering:
- What happened (in plain English)
- Which store(s) were affected
- Root cause category
- Current status

Format as numbered list. Be factual and brief. No markdown headers."""

    try:
        client = _anthropic()
        response = client.messages.create(
            model=_ANALYSIS_MODEL,
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception:
        logger.exception("Top issues summary failed")
        lines = []
        for i, t in enumerate(tickets[:5], 1):
            lines.append(
                f"{i}. [{t.get('root_cause','?')}] {t.get('store_name','?')}: "
                f"{t.get('symptom','?')[:80]} — {t.get('status','?')}"
            )
        return "\n".join(lines)


async def _summarize_systemic_issues(patterns: list[dict], cross_ref: dict) -> str:
    """Describe systemic issues that need deliberate action."""
    systemic = [p for p in patterns if p.get("systemic") or p.get("trend") == "strengthening"]
    fix_failures = cross_ref.get("fix_failure", [])

    if not systemic and not fix_failures:
        return "No systemic issues identified this week."

    items = []
    for p in systemic:
        items.append({
            "type": "pattern",
            "name": p["pattern_name"],
            "description": p["description"],
            "root_cause": p["root_cause"],
            "trend": p["trend"],
            "stores": p.get("stores_affected", "multiple"),
        })
    for f in fix_failures:
        items.append({
            "type": "fix_failure",
            "symptom": f["ticket"].get("symptom", ""),
            "root_cause": f["ticket"].get("root_cause", ""),
        })

    prompt = f"""These systemic POS issues need deliberate action. For each, provide:
- Brief description of the issue
- Recommended action (specific, executable)
- Suggested owner: store staff | ERP operator | IT infrastructure | management
- Urgency: urgent | this-week | next-sprint

Issues:
{json.dumps(items, indent=2)}

Write as a numbered list. Be constructive and solution-focused. No markdown headers."""

    try:
        client = _anthropic()
        response = client.messages.create(
            model=_ANALYSIS_MODEL,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception:
        logger.exception("Systemic issues summary failed")
        return "\n".join(
            f"• {p['pattern_name']}: {p['description'][:80]}" for p in systemic
        )


async def _build_operator_notes(tickets: list[dict], handoff_tickets: list[dict]) -> str:
    """Generate constructive, non-judgmental operator observations."""
    if not tickets:
        return "No operator activity to analyze this week."

    # Aggregate operator stats
    op_stats: dict[str, dict] = {}
    for t in tickets:
        for op in t.get("operators", []):
            name = op.get("operator_name", "unknown")
            if name not in op_stats:
                op_stats[name] = {"tickets": 0, "resolved": 0, "handoffs_out": 0}
            op_stats[name]["tickets"] += 1
            if t.get("status") == "resolved":
                op_stats[name]["resolved"] += 1

    for t in handoff_tickets:
        ops = t.get("operators", [])
        if len(ops) > 1:
            # First operator handed off
            first = ops[0].get("operator_name", "unknown")
            if first in op_stats:
                op_stats[first]["handoffs_out"] = op_stats[first].get("handoffs_out", 0) + 1

    prompt = f"""Write constructive operator notes for a POS support team digest.

Operator activity this week:
{json.dumps(op_stats, indent=2)}

Handoff tickets: {len(handoff_tickets)}

RULES:
- Be neutral and system-focused, not individual-focused
- Frame observations as "what the system/process can improve" not "who was slow"
- Highlight positive patterns as much as friction points
- Maximum 5 bullet points total
- Do not name specific operators in negative contexts; use "the team" or "support flow"
- DO name operators in positive contexts if their metrics clearly show it

Write as bullet points only."""

    try:
        client = _anthropic()
        response = client.messages.create(
            model=_ANALYSIS_MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception:
        logger.exception("Operator notes generation failed")
        return "• Operator activity data collected. Review individual ticket timelines for details."


# ── Main Entry Point ──────────────────────────────────────────────────────────

async def analyze_weekly(chat_id: int, bot=None) -> str:
    """
    Run the full 5-step weekly analysis for a chat.
    Returns the digest text. Calls delivery if bot is provided.
    """
    now = datetime.utcnow()
    week_number = _week_label(now)
    start_date, end_date = _week_bounds(now)

    logger.info("Starting weekly analysis for chat %d, week %s", chat_id, week_number)

    # Step 1 — Ingest & Tag
    messages = await _fetch_week_messages(chat_id)
    logger.info("Fetched %d messages for analysis", len(messages))

    tickets = await _extract_tickets_llm(messages, chat_id, week_number)
    ticket_ids = await _store_tickets(tickets, chat_id, week_number)

    # Step 2 — Pattern Detection
    await _detect_patterns(tickets, chat_id, week_number)
    patterns = await storage.fetch_patterns()

    # Step 3 — Cross-reference
    cross_ref = await _cross_reference_tickets(tickets, ticket_ids)

    # Step 4 — Update Playbook (only for new issues)
    new_issue_tickets = [item["ticket"] for item in cross_ref.get("new_issues", [])]
    new_playbook = await _update_playbook(new_issue_tickets)

    # Step 5 — Generate Digest
    digest_text = await _generate_digest(
        chat_id=chat_id,
        tickets=tickets,
        patterns=patterns,
        cross_ref=cross_ref,
        new_playbook=new_playbook,
        week_number=week_number,
        start_date=start_date,
        end_date=end_date,
    )

    # Persist digest
    digest_id = await storage.insert_weekly_digest(
        week_number=week_number,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        digest_text=digest_text,
    )

    # Deliver
    if bot is not None or True:  # Always attempt delivery (bot may be None for email/drive)
        try:
            from delivery import deliver_digest
            await deliver_digest(digest_id, digest_text, week_number, start_date, end_date, chat_id, bot)
        except Exception:
            logger.exception("Digest delivery failed (analysis itself succeeded)")

    logger.info("Weekly analysis complete for chat %d, week %s", chat_id, week_number)
    return digest_text
