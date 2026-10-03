"""Cases (matters) the firm is working on, with the law staff assigned to each.

A case is registered when an intake call ends (``source="call"``) or when an admin adds one by hand
(``source="manual"``). It belongs to one workspace; ``assigned_staff`` holds the user_ids of the
members working it. Staff are ordinary workspace users (usually Member Admins), so checking that an
id belongs to the workspace is the caller's job (scripts/serve.py does it against auth_manager).

Storage follows the auth store: the ``cases`` collection in PostgreSQL when DATABASE_URL is set,
config/cases.json otherwise (local tests). Each change writes just that case's document.
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


class CaseManager:
    def __init__(self, path: str = CASES_PATH):
        self.path = path
        self._lock = threading.RLock()
        self._cases: Dict[str, Case] = {}
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
        elif os.path.isfile(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    docs = json.load(f).get("cases") or []
            except Exception as e:
                log.warning("Failed to load cases from %s: %s", self.path, e)
        known = set(Case.__dataclass_fields__)
        for d in docs:
            try:
                c = Case(**{k: v for k, v in d.items() if k in known})
                self._cases[c.case_id] = c
            except TypeError as e:
                log.warning("Skipping malformed case document: %s", e)

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

    # ── reads ────────────────────────────────────────────────────────────────
    def get(self, case_id: str) -> Optional[Case]:
        return self._cases.get(case_id)

    def find_by_call(self, call_id: str) -> Optional[Case]:
        if not call_id:
            return None
        return next((c for c in self._cases.values() if c.call_id == call_id), None)

    def list_cases(self, workspace_id: str, assigned_to: Optional[str] = None, status: Optional[str] = None,
                   registered_from: Optional[float] = None, registered_to: Optional[float] = None) -> List[Dict[str, Any]]:
        """Cases in one workspace, newest registration first. ``registered_to`` is exclusive."""
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


case_manager = CaseManager()
