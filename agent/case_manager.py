"""Cases (matters) the firm is working on, with the law staff assigned to each.

A case is registered when an intake call ends (``source="call"``) or when an admin adds one by hand
(``source="manual"``). It belongs to one workspace; ``assigned_staff`` holds the user_ids of the
members working it. Staff are ordinary workspace users (usually Member Admins), so checking that an
id belongs to the workspace is the caller's job (scripts/serve.py does it against auth_manager).

Storage follows the auth store: the ``cases`` collection in PostgreSQL when DATABASE_URL is set,
config/cases.json otherwise (local tests). Each change writes just that case's document. Calls are
processed both by the dashboard server and by the docker worker, so each process re-reads the store
at most every REFRESH_SECONDS before answering, and cases one process registers show up in the other.
"""

import json
import logging
import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

log = logging.getLogger("voice-agent.cases")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASES_PATH = os.path.join(ROOT_DIR, "config", "cases.json")
COLLECTION = "cases"

STATUSES = ("new", "in_progress", "on_hold", "closed")
STATUS_LABELS = {"new": "New", "in_progress": "In progress", "on_hold": "On hold", "closed": "Closed"}
MAX_STAFF = 20
REFRESH_SECONDS = 2.0
DEFAULT_WORKSPACE = "ws-default"


@dataclass
class Case:
    case_id: str
    workspace_id: str
    client_name: str
    phone: str = ""
    email: str = ""
    case_type: str = ""
    summary: str = ""
    status: str = "new"
    source: str = "manual"          # "call" | "manual"
    call_id: str = ""
    clio_matter_id: str = ""
    registered_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    created_by: str = ""            # user_id, "" for the system (call pipeline)
    assigned_staff: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _text(value: Any, max_len: int) -> str:
    return " ".join(str(value or "").split())[:max_len]


def _person_name(raw: Any, agent_name: str = "") -> str:
    """A caller name from the regex extractor, or "" when it is not one. That extractor often catches
    the agent's own greeting ("Maya, an AI") or a phrase ("not an employee"), so keep the leading
    capitalised words and require two of them that are not the agent's name."""
    words = []
    for w in str(raw or "").replace(",", " ").split():
        w = w.strip(".;:!?")
        if not (w[:1].isupper() and w.replace("-", "").replace("'", "").isalpha()):
            break
        words.append(w)
    agent_first = (agent_name.split() or [""])[0].lower()
    if not 2 <= len(words) <= 4 or words[0].lower() == agent_first or "AI" in words:
        return ""
    return " ".join(words[:4])


# legal_intake's case_type enum (agent/schema_extractor.py). The regex extractor does not hold to it,
# so anything else it returns is a captured phrase, not a case type.
INTAKE_CASE_TYPES = {"personal_injury": "Personal Injury", "family_law": "Family Law", "criminal": "Criminal",
                     "immigration": "Immigration", "estate": "Estate", "contract": "Contract", "employment": "Employment"}


def _case_type(raw: Any) -> str:
    return INTAKE_CASE_TYPES.get(str(raw or "").strip().lower(), "")


class CaseManager:
    def __init__(self, path: str = CASES_PATH):
        self.path = path
        self._lock = threading.RLock()
        self._cases: Dict[str, Case] = {}
        self._loaded_at = 0.0
        self._file_mtime = 0.0
        self._load()

    # ── storage ──────────────────────────────────────────────────────────────
    def _db_backed(self) -> bool:
        try:
            from agent import storage
            return bool(storage.database_url()) and storage.available()
        except Exception:
            return False

    def _load(self) -> None:
        docs: List[Dict[str, Any]] = []
        if self._db_backed():
            from agent import storage
            docs = storage.load_collection(COLLECTION)
        elif os.getenv("DATABASE_URL"):
            return              # database configured but down: keep what we have rather than wipe it
        elif os.path.isfile(self.path):
            try:
                self._file_mtime = os.path.getmtime(self.path)
                with open(self.path, "r", encoding="utf-8") as f:
                    docs = json.load(f).get("cases") or []
            except Exception as e:
                log.warning("Failed to load cases from %s: %s", self.path, e)
                return
        known = set(Case.__dataclass_fields__)
        cases: Dict[str, Case] = {}
        for d in docs:
            try:
                c = Case(**{k: v for k, v in d.items() if k in known})
                cases[c.case_id] = c
            except TypeError as e:
                log.warning("Skipping malformed case document: %s", e)
        self._cases = cases
        self._loaded_at = time.time()

    def _refresh(self) -> None:
        """Picks up cases written by another process (see module docstring)."""
        with self._lock:
            if os.getenv("DATABASE_URL"):
                if time.time() - self._loaded_at >= REFRESH_SECONDS:
                    self._load()
            elif os.path.isfile(self.path) and os.path.getmtime(self.path) != self._file_mtime:
                self._load()

    def _save(self, case: Case) -> None:
        if os.getenv("DATABASE_URL"):
            from agent import storage
            if not storage.save_document(COLLECTION, case.case_id, case.to_dict(), case.updated_at):
                raise RuntimeError("Case not saved: database write failed")
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"cases": [c.to_dict() for c in self._cases.values()], "updated_at": time.time()}, f, indent=2)
        os.replace(tmp, self.path)
        self._file_mtime = os.path.getmtime(self.path)

    # ── reads ────────────────────────────────────────────────────────────────
    def get(self, case_id: str) -> Optional[Case]:
        self._refresh()
        return self._cases.get(case_id)

    def find_by_call(self, call_id: str) -> Optional[Case]:
        if not call_id:
            return None
        self._refresh()
        return next((c for c in self._cases.values() if c.call_id == call_id), None)

    def list_cases(self, workspace_id: str, assigned_to: Optional[str] = None, status: Optional[str] = None,
                   registered_from: Optional[float] = None, registered_to: Optional[float] = None) -> List[Dict[str, Any]]:
        """Cases in one workspace, newest registration first. ``registered_to`` is exclusive."""
        self._refresh()
        out = []
        for c in self._cases.values():
            if c.workspace_id != workspace_id:
                continue
            if assigned_to is not None and assigned_to not in c.assigned_staff:
                continue
            if status and c.status != status:
                continue
            if registered_from is not None and c.registered_at < registered_from:
                continue
            if registered_to is not None and c.registered_at >= registered_to:
                continue
            out.append(c.to_dict())
        out.sort(key=lambda d: d["registered_at"], reverse=True)
        return out

    def staff_workload(self, workspace_id: str) -> Dict[str, int]:
        """Open (not closed) cases per assigned user_id."""
        self._refresh()
        counts: Dict[str, int] = {}
        for c in self._cases.values():
            if c.workspace_id == workspace_id and c.status != "closed":
                for uid in c.assigned_staff:
                    counts[uid] = counts.get(uid, 0) + 1
        return counts

    # ── writes ───────────────────────────────────────────────────────────────
    def create(self, workspace_id: str, data: Dict[str, Any], created_by: str = "", source: str = "manual") -> Case:
        client_name = _text(data.get("client_name"), 120)
        if not client_name:
            raise ValueError("Client name is required")
        status = str(data.get("status") or "new")
        if status not in STATUSES:
            raise ValueError(f"Status must be one of: {', '.join(STATUSES)}")
        call_id = _text(data.get("call_id"), 120)
        with self._lock:
            existing = self.find_by_call(call_id)
            if existing:
                return existing
            now = time.time()
            case = Case(
                case_id=f"case-{secrets.token_hex(5)}",
                workspace_id=workspace_id,
                client_name=client_name,
                phone=_text(data.get("phone"), 40),
                email=_text(data.get("email"), 160),
                case_type=_text(data.get("case_type"), 80),
                summary=str(data.get("summary") or "").strip()[:2000],
                status=status,
                source=source if source in ("call", "manual") else "manual",
                call_id=call_id,
                clio_matter_id=_text(data.get("clio_matter_id"), 60),
                registered_at=float(data.get("registered_at") or now),
                updated_at=now,
                created_by=created_by,
                assigned_staff=[],
            )
            self._cases[case.case_id] = case
            try:
                self._save(case)
            except Exception:
                self._cases.pop(case.case_id, None)
                raise
            return case

    def _commit(self, case: Case, before: Dict[str, Any]) -> Case:
        case.updated_at = time.time()
        try:
            self._save(case)
        except Exception:
            self._cases[case.case_id] = Case(**before)
            raise
        return case

    def assign(self, case_id: str, user_ids: List[str], mode: str = "add") -> Case:
        """mode: "add" these staff, "remove" them, or "set" the list to exactly these."""
        if mode not in ("add", "remove", "set"):
            raise ValueError("mode must be add, remove or set")
        with self._lock:
            self._refresh()
            case = self._cases.get(case_id)
            if not case:
                raise KeyError(case_id)
            before = case.to_dict()
            ids = [u for u in dict.fromkeys(str(x) for x in user_ids) if u]
            if mode == "set":
                staff = ids
            elif mode == "add":
                staff = case.assigned_staff + [u for u in ids if u not in case.assigned_staff]
            else:
                staff = [u for u in case.assigned_staff if u not in ids]
            if len(staff) > MAX_STAFF:
                raise ValueError(f"A case can have at most {MAX_STAFF} staff assigned")
            case.assigned_staff = staff
            return self._commit(case, before)

    def set_status(self, case_id: str, status: str) -> Case:
        if status not in STATUSES:
            raise ValueError(f"Status must be one of: {', '.join(STATUSES)}")
        with self._lock:
            self._refresh()
            case = self._cases.get(case_id)
            if not case:
                raise KeyError(case_id)
            before = case.to_dict()
            case.status = status
            return self._commit(case, before)

    def unassign_everywhere(self, user_id: str) -> int:
        """Drops a removed member from every case. Returns how many cases changed."""
        changed = 0
        with self._lock:
            self._refresh()
            for case in list(self._cases.values()):
                if user_id in case.assigned_staff:
                    before = case.to_dict()
                    case.assigned_staff = [u for u in case.assigned_staff if u != user_id]
                    try:
                        self._commit(case, before)
                        changed += 1
                    except Exception as e:
                        log.warning("Could not unassign %s from %s: %s", user_id, case.case_id, e)
        return changed

    def register_from_call(self, call_id: str, metadata: Dict[str, Any]) -> Optional[Case]:
        """Registers the case for a finished intake call, once per call. Returns None when the call
        gave nothing to open a case with (no caller name or phone was captured, e.g. a wrong number).

        Reads what the pipeline left on the job: the agent's own fields extracted by Gemini first, the
        regex legal_intake fields as a fallback, the call summary, the Clio matter if one was created,
        and the agent, whose workspace the case goes to (server-wide agents file into the default)."""
        md = metadata or {}
        agent_name = ""
        workspace_id = DEFAULT_WORKSPACE
        try:
            from agent.agent_builder import agent_builder
            cfg = agent_builder.get_agent(md.get("agent_id") or "") if md.get("agent_id") else None
            if cfg:
                agent_name = cfg.name or ""
                workspace_id = cfg.workspace_id or DEFAULT_WORKSPACE
        except Exception as e:
            log.debug("Agent lookup for case workspace failed: %s", e)
        agent_name = agent_name or str(md.get("agent_name") or "")

        ai = md.get("agent_extraction") or {}
        ai_fields = (ai.get("values") or {}) if ai.get("engine") == "gemini" else {}
        rx_fields = (md.get("crm_payloads") or {}).get("legal_intake") or {}
        pick = lambda d, *keys: next((d[k] for k in keys if d.get(k) not in (None, "", [])), None)

        name = _text(pick(ai_fields, "full_name", "client_name", "caller_name", "name"), 120)
        if not name:
            name = _person_name(pick(rx_fields, "client_name"), agent_name)
        phone = _text(pick(ai_fields, "callback_phone", "contact_phone", "phone") or pick(rx_fields, "contact_phone"), 40)
        if not (name or phone):
            return None
        summary = md.get("summary") if isinstance(md.get("summary"), dict) else {}
        matter = (md.get("clio_manage_sync") or {}).get("matter") or {}
        registered_at = md.get("ended_at") or md.get("started_at")
        return self.create(workspace_id, {
            "client_name": name or "Unknown caller",
            "phone": phone or md.get("from_number"),
            "email": pick(ai_fields, "email_address", "email", "contact_email") or pick(rx_fields, "contact_email"),
            "case_type": pick(ai_fields, "case_type", "practice_area", "matter_type") or _case_type(pick(rx_fields, "case_type")),
            "summary": pick(ai_fields, "matter_summary", "situation_summary", "case_summary")
                       or pick(rx_fields, "case_summary") or summary.get("executive_summary") or "",
            "call_id": call_id,
            "clio_matter_id": matter.get("matter_id") if matter.get("status") == "success" else "",
            "registered_at": registered_at if isinstance(registered_at, (int, float)) else None,
        }, source="call")

case_manager = CaseManager()
