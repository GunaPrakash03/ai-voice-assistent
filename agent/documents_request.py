"""
Did the caller ask to send documents? Read from a finished call's transcript (web/documents-plan.html, 2.2).

The global instructions (agent/firm_context.py) have every agent ask, before its closing line on an intake
call, whether the caller has documents about their case, and if so confirm an email address (spelled back)
or a mobile number for a private upload link. ``detect`` reads the transcript afterwards and returns:

    {"asked": bool, "wanted": bool, "email": str, "phone": str, "engine": "gemini" | "rules",
     "caller": {"client_name", "callback_phone", "case_type", "summary"}}

The same Gemini call also reads the caller's basics ("caller"), which the case uses when the agent has no
extraction fields of its own (the regex fallback often misses the name). Rules leave "caller" empty.

Gemini reads it when a key is set (it copes with spelled-out and corrected addresses); otherwise, or when
Gemini fails, plain rules do: find the agent's documents question, take the caller's next answer as yes/no,
then the email or phone the caller gave after it. Never raises.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

log = logging.getLogger("documents-request")

DOC_WORDS = re.compile(r"\b(documents?|paperwork|photos?|pictures?|police report|medical (?:bills?|records?)|records|files)\b", re.I)
YES = re.compile(r"^\W*(yes|yeah|yep|yup|sure|i do|i have|i've got|i got|absolutely|definitely|correct|of course|uh[- ]huh|mm[- ]hmm|a few|some)\b", re.I)
NO = re.compile(r"^\W*(no|nope|nah|not really|i don'?t|none|not yet|i do not|nothing)\b", re.I)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SYSTEM = (
    "You read the transcript of a law firm's intake phone call. Near the end the receptionist may ask whether the "
    "caller has documents about their case (photos, police report, medical bills...) and offer a link to upload them. "
    "Return JSON: {\"asked\": did the receptionist ask about documents, \"wanted\": did the caller say they have "
    "documents and want the upload link, \"email\": the email address the caller confirmed for the link in normal "
    "form (e.g. maria.lopez@gmail.com) or \"\", \"phone\": the mobile number the caller confirmed for a text instead, "
    "digits with country code if given, or \"\", \"caller\": {\"client_name\": the caller's own full name as they "
    "gave it, \"callback_phone\": their callback number, \"case_type\": a short label for their matter (e.g. Personal "
    "Injury, Car Accident, Divorce, Estate Planning) or \"\", \"summary\": one or two plain sentences on what happened "
    "to them}}. Use the caller's final corrected version. Never invent a name, address or number that wasn't said; "
    "use \"\" when it wasn't."
)


def _turns(transcript: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    out = []
    for t in transcript or []:
        who = str(t.get("speaker") or t.get("role") or "").lower()
        text = str(t.get("text") or t.get("content") or "").strip()
        if not text:
            continue
        out.append({"who": "caller" if who in ("caller", "user", "customer", "human") else "agent", "text": text})
    return out


def spoken_email(text: str) -> str:
    """An email address from what a caller said: "maria dot lopez at gmail dot com" -> maria.lopez@gmail.com."""
    m = EMAIL.search(text)
    if m:
        return m.group(0).rstrip(".").lower()
    t = text.lower()
    t = re.sub(r"\s+(?:at)\s+", "@", t)
    t = re.sub(r"\s+(?:dot|period)\s+", ".", t)
    t = re.sub(r"\s+(?:underscore)\s+", "_", t)
    t = re.sub(r"\s+(?:dash|hyphen)\s+", "-", t)
    # letters spelled out one by one ("m a r i a") join up around the @
    m = re.search(r"([a-z0-9._-]+(?:\s[a-z0-9._-])*)@([a-z0-9.-]+(?:\s[a-z0-9.-])*)", t)
    if not m:
        return ""
    cand = (m.group(1).replace(" ", "") + "@" + m.group(2).replace(" ", "")).strip(".")
    full = EMAIL.fullmatch(cand)
    return full.group(0) if full else ""


def _phone(text: str) -> str:
    digits = re.sub(r"[^\d+]", "", text)
    plain = digits.lstrip("+")
    if 10 <= len(plain) <= 15:
        return ("+" if digits.startswith("+") else ("+1" if len(plain) == 10 else "+")) + plain
    return ""


def _by_rules(turns: List[Dict[str, str]]) -> Dict[str, Any]:
    out = {"asked": False, "wanted": False, "email": "", "phone": "", "engine": "rules", "caller": {}}
    # The first documents question is the yes/no one; later mentions ("...the link for those documents?")
    # are follow-ups about where to send it.
    q = next((i for i, t in enumerate(turns) if t["who"] == "agent" and DOC_WORDS.search(t["text"]) and "?" in t["text"]), None)
    if q is None:
        return out
    out["asked"] = True
    answer = next((t["text"] for t in turns[q + 1:] if t["who"] == "caller"), "")
    if not answer or NO.search(answer) or not YES.search(answer):
        return out
    out["wanted"] = True
    later = [t["text"] for t in turns[q + 1:] if t["who"] == "caller"]
    for text in reversed(later):          # the caller's last version wins (corrections)
        if not out["email"]:
            out["email"] = spoken_email(text)
        if not out["phone"]:
            out["phone"] = _phone(text)
    return out


def _by_gemini(turns: List[Dict[str, str]]) -> Optional[Dict[str, Any]]:
    from agent.schema_extractor import _gemini_key, gemini_json
    if not _gemini_key():
        return None
    text = "\n".join(f"{'Caller' if t['who'] == 'caller' else 'Receptionist'}: {t['text']}" for t in turns[-40:])
    try:
        data = gemini_json(SYSTEM, text, max_tokens=300, timeout=25.0)
    except Exception as e:
        log.info("Documents question read by rules (Gemini: %s)", e)
        return None
    if not isinstance(data, dict):
        return None
    email = spoken_email(str(data.get("email") or ""))
    raw = data.get("caller") if isinstance(data.get("caller"), dict) else {}
    caller = {"client_name": str(raw.get("client_name") or "").strip()[:120],
              "callback_phone": _phone(str(raw.get("callback_phone") or "")),
              "case_type": str(raw.get("case_type") or "").strip()[:80],
              "summary": str(raw.get("summary") or "").strip()[:600]}
    return {"asked": bool(data.get("asked")), "wanted": bool(data.get("wanted")),
            "email": email, "phone": _phone(str(data.get("phone") or "")), "engine": "gemini",
            "caller": {k: v for k, v in caller.items() if v}}


def detect(transcript: List[Dict[str, Any]]) -> Dict[str, Any]:
    turns = _turns(transcript)
    try:
        if any(t["who"] == "caller" for t in turns):
            got = _by_gemini(turns)
            if got is not None:
                return got
        return _by_rules(turns)
    except Exception as e:
        log.warning("Documents question not read: %s", e)
        return {"asked": False, "wanted": False, "email": "", "phone": "", "engine": "error", "caller": {}}
