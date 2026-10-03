"""Task 5.5 — Hand the sign-up data to the firm's first agent.

Sign-up only creates the account. After the first sign-in the admin fills in the firm details on
/onboarding; when that is finished (``AuthManager.complete_onboarding``) the firm gets an intake
agent built from its profile: greeting with the firm name, office hours and
after-hours behaviour, languages, practice-area intake questions and the lead-notification emails.
The agent is created *not live*: there is one live agent for the whole server, so a new firm must
never take over everyone's calls. It answers once a number is routed to it or an admin activates it.

What was seeded is recorded on the workspace as ``metadata["onboarding"]`` so it happens once, and
the dashboard reads it for the first-login checklist (open the agent, connect Clio, firm logo).

``notify_leads`` emails a short call summary to the agent's ``lead_emails`` after each call.
"""

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from agent.firm_profile import AFTER_HOURS_LABELS, PRACTICE_AREAS, WEEKDAYS

log = logging.getLogger("voice-agent.onboarding")

DEFAULT_LLM_MODEL = "gemini-3.5-flash-lite"

DAY_NAMES = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
             "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}

LANGUAGE_NAMES = {"en": "English", "es": "Spanish", "fr": "French", "de": "German", "it": "Italian",
                  "pt": "Portuguese", "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "vi": "Vietnamese",
                  "tl": "Tagalog", "ar": "Arabic", "ru": "Russian", "hi": "Hindi", "ta": "Tamil",
                  "pl": "Polish", "uk": "Ukrainian", "fa": "Persian", "ht": "Haitian Creole"}

# What to ask on top of the common intake questions, per practice area.
PRACTICE_QUESTIONS = {
    "personal_injury": "For an injury, ask when and where it happened, how they were hurt, and whether they are getting medical treatment.",
    "family": "For a family matter, ask whether it is a divorce, custody, support or something else, and if there is an upcoming court date.",
    "criminal_defense": "For a criminal matter, ask what the charge is, whether anyone is in custody, and the next court date.",
    "immigration": "For immigration, ask what kind of help they need and whether they have a deadline or hearing date.",
    "estate_planning": "For estate planning, ask whether they need a will, a trust, or help with an estate after a death.",
    "business": "For a business matter, ask the company name and what the legal issue is.",
    "employment": "For an employment matter, ask the employer's name and whether they are still employed there.",
    "real_estate": "For real estate, ask about the property and whether a closing date is coming up.",
}

# Extra data points pulled from the transcript per practice area (Agent Builder → Data & webhooks).
PRACTICE_FIELDS = {
    "personal_injury": [
        {"name": "incident_date", "type": "date", "description": "When the accident or injury happened"},
        {"name": "injury_description", "type": "string", "description": "How the caller was hurt"},
    ],
    "family": [{"name": "family_matter_type", "type": "string", "description": "Divorce, custody, support or other"}],
    "criminal_defense": [
        {"name": "charge", "type": "string", "description": "What the caller or their relative is charged with"},
        {"name": "court_date", "type": "date", "description": "Next court date, if any"},
    ],
    "immigration": [{"name": "immigration_deadline", "type": "date", "description": "Any deadline or hearing date"}],
    "employment": [{"name": "employer_name", "type": "string", "description": "The employer involved"}],
    "business": [{"name": "company_name", "type": "string", "description": "The caller's company"}],
    "real_estate": [{"name": "property_address", "type": "string", "description": "The property involved"}],
}


def _join(items: List[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _clock(hhmm: str) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    suffix = "AM" if h < 12 else "PM"
    h = h % 12 or 12
    return f"{h} {suffix}" if m == 0 else f"{h}:{m:02d} {suffix}"


def describe_hours(hours: Dict[str, Any]) -> str:
    """{"mon": {"open": "09:00", "close": "17:00"}, ...} -> 'Monday to Friday 9 AM to 5 PM; ...'.
    Consecutive days with the same hours are grouped; days not listed are closed."""
    groups: List[List[Any]] = []   # [first_day, last_day, slot]
    for day in WEEKDAYS:
        slot = hours.get(day)
        if groups and groups[-1][2] == slot and WEEKDAYS.index(groups[-1][1]) == WEEKDAYS.index(day) - 1:
            groups[-1][1] = day
        else:
            groups.append([day, day, slot])
    parts = []
    for first, last, slot in groups:
        span = WEEKDAYS.index(last) - WEEKDAYS.index(first)
        days = DAY_NAMES[first] if span == 0 else f"{DAY_NAMES[first]} {'and' if span == 1 else 'to'} {DAY_NAMES[last]}"
        parts.append(f"{days} {_clock(slot['open'])} to {_clock(slot['close'])}" if slot else f"closed {days}")
    return "; ".join(parts)


def language_names(codes: List[str]) -> List[str]:
    return [LANGUAGE_NAMES.get(c.split("-")[0].lower(), c) for c in codes]


def agent_language(codes: List[str]) -> str:
    """Primary speech language for the agent: the first one the firm listed, as a locale."""
    if not codes:
        return "en-US"
    code = codes[0]
    if "-" in code:
        return code
    return {"en": "en-US", "es": "es-US", "pt": "pt-BR", "fr": "fr-FR", "zh": "zh-CN"}.get(code, code)


def logo_url(website: str) -> str:
    """Default firm logo: the site's /favicon.ico. Only the URL is stored; the browser loads it,
    so the server never fetches an address a sign-up typed in."""
    if not website:
        return ""
    parts = urlsplit(website)
    return f"{parts.scheme}://{parts.netloc}/favicon.ico" if parts.scheme and parts.netloc else ""


def build_agent_config(firm_name: str, profile: Dict[str, Any], available_tools: Optional[List[str]] = None) -> Dict[str, Any]:
    """Firm profile -> create_agent() kwargs. Pure: no I/O, so it is easy to test and preview."""
    profile = profile or {}
    areas = [a for a in profile.get("practice_areas", []) if a != "other"]
    area_labels = [PRACTICE_AREAS[a] for a in areas]
    if profile.get("practice_areas_other"):
        area_labels.append(profile["practice_areas_other"])
    office_phone = profile.get("office_phone", "")
    languages = profile.get("languages", [])
    after_hours = profile.get("after_hours", "")
    on_call = profile.get("after_hours_transfer_number", "")
    hours = profile.get("business_hours") or {}
    tools_wanted = ["check_availability", "book_appointment", "transfer_call"]
    tools = [t for t in tools_wanted if available_tools is None or t in available_tools]
    can_transfer = "transfer_call" in tools
    can_book = {"check_availability", "book_appointment"} <= set(tools)

    practice = f", a law firm practicing {_join([a.lower() for a in area_labels])}" if area_labels else ", a law firm"
    lines = [f"You are the AI intake assistant answering calls for {firm_name}{practice}.", ""]
    lines += [
        "Answer in one or two short sentences, then stop.",
        "Do not recap what the caller just said.",
        "Ask one question at a time.",
        "Never read case numbers, URLs or email addresses aloud unless asked.",
        "You are not a lawyer. Never give legal advice or predict an outcome; say an attorney will review the matter.",
        "",
        "For a new matter, collect the caller's full name, the best number to call them back, their email "
        "address, and a short description of what happened.",
    ]
    lines += [PRACTICE_QUESTIONS[a] for a in areas if a in PRACTICE_QUESTIONS]
    lines.append("")

    tz = profile.get("timezone", "")
    if hours:
        lines.append(f"Office hours{f' ({tz} time)' if tz else ''}: {describe_hours(hours)}.")
    if after_hours == "transfer" and on_call and can_transfer:
        lines.append(f"Outside office hours, if the caller needs someone now, transfer them to {on_call} using transfer_call.")
    elif after_hours == "book_callback" and can_book:
        lines.append("Outside office hours, offer to book a callback: use check_availability, then book_appointment.")
    elif after_hours:
        lines.append("Outside office hours, take a message: get the caller's name, number and reason for calling, "
                     "and tell them someone from the firm will call back the next business day.")
    if can_transfer and office_phone:
        lines.append(f"If the caller describes an emergency or asks for a person during office hours, "
                     f"transfer them to {office_phone} using transfer_call.")
    elif can_transfer:
        lines.append("If the caller describes an emergency or asks for a person, transfer them using transfer_call.")
    if office_phone:
        lines.append(f"If asked, the office number is {office_phone}.")
    if len(languages) > 1:
        lines.append(f"You can speak {_join(language_names(languages))}. Answer in the language the caller uses.")

    fields = [
        {"name": "caller_name", "type": "string", "description": "Caller's full name", "required": True},
        {"name": "callback_number", "type": "phone", "description": "Best number to call the caller back", "required": True},
        {"name": "email", "type": "email", "description": "Caller's email address"},
        {"name": "case_summary", "type": "string", "description": "Short description of the caller's legal issue", "required": True},
    ]
    if len(area_labels) > 1:
        fields.append({"name": "practice_area", "type": "enum", "description": "Which practice area the matter belongs to",
                       "options": area_labels})
    seen = {f["name"] for f in fields}
    for a in areas:
        for f in PRACTICE_FIELDS.get(a, []):
            if f["name"] not in seen:
                fields.append(dict(f))
                seen.add(f["name"])

    first = f"Thanks for calling {firm_name}. I'm an AI assistant. How can I help you today?"
    description = f"Intake agent for {firm_name}, set up from sign-up"
    if after_hours:
        description += f" (after hours: {AFTER_HOURS_LABELS[after_hours].lower()})"
    return {
        "name": f"{firm_name} Intake"[:80],
        "first_message": first,
        "system_prompt": "\n".join(lines).strip(),
        "description": description,
        "llm_model": DEFAULT_LLM_MODEL,
        "language": agent_language(languages),
        "tools": tools,
        "extraction_fields": fields,
        "lead_emails": list(profile.get("lead_emails", [])),
        # Off on purpose: webhook endpoints are server-wide, so a firm's call data must not go to
        # them until the firm's admin turns this on for an endpoint they own.
        "webhook_enabled": False,
    }


def seed_firm_agent(auth_manager, agent_builder, workspace_id: str) -> Optional[Dict[str, Any]]:
    """Create the firm's intake agent once, when onboarding is complete. Returns the onboarding
    record, or None if the workspace is not a finished self-service sign-up or was already seeded."""
    ws = auth_manager.get_workspace(workspace_id)
    existing = (ws.metadata.get("onboarding") or {}) if ws else {}
    if not ws or not ws.metadata.get("signup") or existing.get("status") != auth_manager.ONBOARDING_COMPLETE:
        return None
    if existing.get("agent_id") and agent_builder.get_agent(existing["agent_id"]):
        return None
    profile = ws.metadata.get("firm_profile") or {}
    tool_names = [t["name"] for t in agent_builder.available_tools()]
    cfg = build_agent_config(ws.name, profile, tool_names or None)
    admin = auth_manager.workspace_owner(workspace_id)
    agent = agent_builder.create_agent(owner_id=admin.user_id if admin else "", workspace_id=workspace_id,
                                       active=False, **cfg)
    record = {
        "agent_id": agent.agent_id,
        "seeded_at": time.time(),
        "logo_url": logo_url(profile.get("website", "")),
        "clio_selected": profile.get("practice_software") == "clio",
        "dismissed": False,
    }
    auth_manager.set_onboarding(workspace_id, record)
    log.info("Seeded agent %s for sign-up %s (%s)", agent.agent_id, ws.name, workspace_id)
    return record


# ── Lead notifications ──────────────────────────────────────────────────────

def _lead_emails_for(agent_id: str, agent_name: str) -> List[str]:
    """Read from disk: the post-call worker may be a different process from the one that created
    the agent, so its in-memory registry can be older than the file."""
    from agent.agent_builder import STATE_FILE
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            agents = json.load(f).get("agents", [])
    except Exception:
        return []
    match = next((a for a in agents if agent_id and a.get("agent_id") == agent_id), None)
    if match is None and agent_name:
        match = next((a for a in agents if a.get("name") == agent_name), None)
    return list((match or {}).get("lead_emails") or [])


def lead_email_body(agent_name: str, md: Dict[str, Any], values: Dict[str, Any]) -> str:
    summary = md.get("summary") or {}
    text = summary.get("executive_summary", "") if isinstance(summary, dict) else str(summary)
    lines = [f"New call handled by {agent_name or 'your AI intake agent'}.", ""]
    if md.get("from_number"):
        lines.append(f"Caller number: {md['from_number']}")
    if md.get("duration_seconds"):
        lines.append(f"Duration: {int(float(md['duration_seconds']))} seconds")
    shown = {k: v for k, v in (values or {}).items() if v not in (None, "", [], {})}
    if shown:
        lines += ["", "Details collected:"]
        lines += [f"  {k.replace('_', ' ').capitalize()}: {v}" for k, v in shown.items()]
    if text:
        lines += ["", "Summary:", str(text)]
    lines += ["", "Open the dashboard for the full transcript and recording."]
    return "\n".join(lines)


def notify_leads(job) -> None:
    """Email the agent's lead_emails about a finished call. Never raises."""
    try:
        md = job.metadata or {}
        recipients = _lead_emails_for(md.get("agent_id") or "", md.get("agent_name") or "")
        if not recipients:
            return
        from agent import mailer
        if not mailer.configured().get("ready"):
            log.info("Lead email for call %s skipped: %s", job.call_id, mailer.configured().get("reason"))
            return
        values = (md.get("agent_extraction") or {}).get("values") or (md.get("crm_payloads") or {}).get("legal_intake") or {}
        caller = values.get("caller_name") or values.get("client_name") or md.get("from_number") or "new caller"
        subject = f"New lead: {caller}"
        body = lead_email_body(md.get("agent_name") or "", md, values)
        for to in recipients:
            res = mailer.send_email(to, subject, body)
            if not res.get("ok"):
                log.warning("Lead email to %s for call %s failed: %s", to, job.call_id, res.get("error"))
    except Exception as e:
        log.warning("Lead notification skipped: %s", e)


def attach_lead_notifier(worker) -> None:
    """Send lead emails when a post-call job completes (in a thread: SMTP must not block the pipeline)."""
    def on_event(evt: Dict[str, Any]):
        if evt.get("event") != "job_completed":
            return None
        job = worker.get_job(job_id=evt.get("job_id"))
        if job is not None:
            threading.Thread(target=notify_leads, args=(job,), daemon=True, name="lead-email").start()
        return None
    worker.subscribe(on_event)
