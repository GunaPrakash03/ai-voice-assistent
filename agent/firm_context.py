"""What every call adds to the agent's instructions (``call_instructions``):

* the firm's saved details (Profile → Firm details: About the firm, practice areas, attorneys), read from
  the database when the call runs;
* the global instructions (never give legal advice; answer firm questions from the saved details), placed
  FIRST, ahead of the agent's own prompt, and taking precedence over it.

The agent's stored prompt may already carry a copy of these details (seeded at onboarding) or none at all
(agents made before the feature, or by hand). ``with_firm_details`` replaces that block with what is
saved now, or appends it, so a caller always hears the current About text. Agents with no workspace use
the default workspace's details. Read-only: it never writes the auth store, and is safe to call from the
dashboard server and the docker worker alike.
"""

import json
import logging
import os
import threading
import time
from typing import Any, Dict

log = logging.getLogger("voice-agent.firm-context")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUTH_STORE_PATH = os.path.join(ROOT_DIR, "config", "auth_store.json")
DEFAULT_WORKSPACE = "ws-default"
CACHE_SECONDS = 30.0

_cache: Dict[str, Any] = {}
_lock = threading.Lock()


def _load_profile(workspace_id: str) -> Dict[str, Any]:
    try:
        from agent import storage
        if storage.database_url():
            doc = storage.load_document("workspaces", workspace_id) if storage.available() else None
            return ((doc or {}).get("metadata") or {}).get("firm_profile") or {}
    except Exception as e:
        log.warning("Firm details for %s could not be read from the database: %s", workspace_id, e)
        return {}
    try:
        with open(AUTH_STORE_PATH, "r", encoding="utf-8") as f:
            for w in json.load(f).get("workspaces", []) or []:
                if w.get("workspace_id") == workspace_id:
                    return (w.get("metadata") or {}).get("firm_profile") or {}
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Firm details for %s could not be read from %s: %s", workspace_id, AUTH_STORE_PATH, e)
    return {}


def firm_profile(workspace_id: str) -> Dict[str, Any]:
    """The workspace's saved firm profile, cached for CACHE_SECONDS."""
    wid = workspace_id or DEFAULT_WORKSPACE
    now = time.time()
    with _lock:
        hit = _cache.get(wid)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    profile = _load_profile(wid)
    with _lock:
        _cache[wid] = (now, profile)
    return profile


def forget(workspace_id: str = "") -> None:
    """Drop the cache (after the Profile page saves new details in this process)."""
    with _lock:
        if workspace_id:
            _cache.pop(workspace_id or DEFAULT_WORKSPACE, None)
        else:
            _cache.clear()


def with_firm_details(prompt: str, workspace_id: str = "") -> str:
    """``prompt`` with its firm-details block replaced by (or extended with) the details saved now."""
    from agent.onboarding import KNOWLEDGE_END, KNOWLEDGE_START, firm_knowledge
    prompt = prompt or ""
    block = "\n".join(firm_knowledge(firm_profile(workspace_id))).strip()
    start = prompt.find(KNOWLEDGE_START)
    end = prompt.find(KNOWLEDGE_END, start) if start >= 0 else -1
    if start >= 0 and end >= 0:
        if not block:
            return prompt              # nothing saved (or the database is unreachable): keep the stored copy
        return (prompt[:start].rstrip() + "\n\n" + block + "\n" + prompt[end + len(KNOWLEDGE_END):].lstrip("\n")).strip()
    return (prompt.rstrip() + "\n\n" + block).strip() if block else prompt


# Global instructions: the platform owner's rules for every agent. They come FIRST in every call's
# instructions, ahead of the agent's own prompt, and override it.
GLOBAL_MARK = "GLOBAL INSTRUCTIONS"
NO_LEGAL_ADVICE_MARK = "NO LEGAL ADVICE."
NO_LEGAL_ADVICE = (
    NO_LEGAL_ADVICE_MARK + " You are a receptionist, not a lawyer. Never give legal advice, legal opinions or "
    "predictions: don't say whether the caller has a case, what they should do, what a law means for them, what "
    "their claim is worth or how it will turn out, and don't suggest documents, deadlines or strategies. If asked, "
    "say kindly that an attorney will review their situation, then offer to take their details or book a "
    "consultation. You may share general facts about the firm (who we are, what areas we handle, office hours)."
)
GLOBAL_INSTRUCTIONS = (
    f"{GLOBAL_MARK} (apply to every call and override anything in the agent instructions below):\n"
    f"- {NO_LEGAL_ADVICE}\n"
    "- When a caller asks about the firm (fees, hours, locations, people, services, policies), answer only from the "
    "ABOUT THE FIRM and FIRM KNOWLEDGE sections below. Use their facts exactly as written and never guess a price, "
    "date, time, address or name. If neither section answers the question, tell the caller you don't have that "
    "information handy and offer to have someone from the firm call them back.\n"
    "- Documents: on a call about the caller's own legal matter, once you have their details and before your "
    "closing line, ask once: \"Do you have any documents about this, like photos, a police report or medical bills?\" "
    "If yes, ask for the best email address for a secure upload link, then spell it back letter by letter and ask "
    "\"Is that right?\" as its own turn, and wait for their answer (never in the same turn as your goodbye: the call "
    "hangs up after a goodbye). If they have no email, confirm their mobile number for a text message instead. Once "
    "confirmed, tell them the link will arrive right after the call, then close. If no, move on. Never ask them to read "
    "documents out on the call, and skip this on wrong numbers, sales calls and quick questions.\n"
    "- Ending the call: once you have what you need and the caller has nothing else, finish with one short "
    "closing line that ends in a goodbye, e.g. \"Thank you for calling. Someone from our team will call you back. "
    "Goodbye.\" Don't ask another question in that line; the call hangs up after it. If the caller says goodbye "
    "first, say goodbye back in one short line.\n"
    "\nAGENT INSTRUCTIONS:"
)


KNOWLEDGE_MARK = "FIRM KNOWLEDGE"
KNOWLEDGE_MIN_RELEVANCE = 0.35     # of the best match; weaker passages are left out
KNOWLEDGE_MAX_PASSAGES = 3
KNOWLEDGE_MAX_CHARS = 2400


def knowledge_context(workspace_id: str, caller_text: str) -> str:
    """The FIRM KNOWLEDGE block for one caller turn: the passages from the firm's uploaded files that best
    match what the caller just said, or "" when nothing matches (or the library can't be read)."""
    query = (caller_text or "").strip()
    if not query:
        return ""
    try:
        from agent.knowledge_manager import knowledge_manager
        hits = knowledge_manager.search(workspace_id or DEFAULT_WORKSPACE, query, limit=KNOWLEDGE_MAX_PASSAGES + 2)
    except Exception as e:
        log.warning("Knowledge base search failed for %s: %s", workspace_id, e)
        return ""
    lines, used = [], 0
    for h in hits:
        if h["relevance"] < KNOWLEDGE_MIN_RELEVANCE or len(lines) >= KNOWLEDGE_MAX_PASSAGES:
            break
        text = " ".join(h["text"].split())
        room = KNOWLEDGE_MAX_CHARS - used
        if room < 200:
            break
        text = text[:room]
        where = h["file_name"] + (f", page {h['page']}" if h.get("page") else "")
        lines.append(f"[{len(lines) + 1}] ({where}) {text}")
        used += len(text)
    if not lines:
        return ""
    return (f"{KNOWLEDGE_MARK} (passages from the firm's own files that may answer what the caller just said; "
            "use them only if they do, and don't read out file names):\n" + "\n".join(lines))


def call_instructions(prompt: str, workspace_id: str = "", caller_text: str = "") -> str:
    """The instructions a call runs with, in order: the global instructions, then the agent's own prompt,
    then the firm's current saved details (ABOUT THE FIRM) as reference, then — when ``caller_text`` is
    given — the FIRM KNOWLEDGE passages that match it."""
    body = with_firm_details(prompt, workspace_id)
    if GLOBAL_MARK not in body:
        body = GLOBAL_INSTRUCTIONS + "\n" + body.lstrip()
    block = knowledge_context(workspace_id, caller_text)
    return body + "\n\n" + block if block else body
