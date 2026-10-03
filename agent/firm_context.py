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
    "- When a caller asks about the firm, answer from the ABOUT THE FIRM section below and nothing else; if it "
    "doesn't say, offer to have someone from the firm call back.\n"
    "- Ending the call: once you have what you need and the caller has nothing else, finish with one short "
    "closing line that ends in a goodbye, e.g. \"Thank you for calling. Someone from our team will call you back. "
    "Goodbye.\" Don't ask another question in that line; the call hangs up after it. If the caller says goodbye "
    "first, say goodbye back in one short line.\n"
    "\nAGENT INSTRUCTIONS:"
)


def call_instructions(prompt: str, workspace_id: str = "") -> str:
    """The instructions a call runs with, in order: the global instructions, then the agent's own prompt,
    then the firm's current saved details (ABOUT THE FIRM) as reference."""
    body = with_firm_details(prompt, workspace_id)
    if GLOBAL_MARK in body:
        return body
    return GLOBAL_INSTRUCTIONS + "\n" + body.lstrip()
