"""Task 4.3 — REST API & Multi-Tenant Access Control.

Provides:
1. Multi-Tenant Workspace Model & Data Isolation (workspaces, users, memberships).
2. API Key Management (rotatable live/test API keys, hashed storage, granular scopes).
3. Role-Based Access Control (RBAC): two roles — admin and user. Users get the whole dashboard
   (all agents, calls, webhooks); only admins manage provider keys, API keys and members.
4. Token-Bucket & Sliding-Window Rate Limiter with standard HTTP headers (X-RateLimit-*).
5. JWT-style signed authentication tokens for session/programmatic access.
6. Programmatic Call Dispatch & Webhook Trigger Authorization.
7. Request Authentication Middleware for HTTP Server endpoints.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

log = logging.getLogger("voice-agent.auth")
if not log.handlers:
    logging.basicConfig(level=logging.INFO)

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT_DIR, "config")
AUTH_STORE_PATH = os.path.join(CONFIG_DIR, "auth_store.json")

# Default system secret for signing session tokens
JWT_SECRET = os.getenv("AUTH_JWT_SECRET", "voice-agent-jwt-secret-key-prod-2026")


# ---------------------------------------------------------------------------
# Enums: Roles & Scopes
# ---------------------------------------------------------------------------

class UserRole(str, Enum):
    ADMIN = "admin"  # Everything: agents, calls, webhooks, provider keys, API keys, members
    USER  = "user"   # Everything in the dashboard except provider keys, API keys and members


# Roles from earlier builds; stored users and incoming payloads are folded into "user".
LEGACY_ROLES = {"operator": UserRole.USER.value, "analyst": UserRole.USER.value, "member": UserRole.USER.value}


def normalize_role(role: Optional[str]) -> str:
    r = (role or "").strip().lower()
    return LEGACY_ROLES.get(r, r or UserRole.USER.value)


class ApiScope(str, Enum):
    CALLS_READ       = "calls:read"
    CALLS_WRITE      = "calls:write"
    CALLS_DISPATCH   = "calls:dispatch"
    AGENTS_READ      = "agents:read"
    AGENTS_WRITE     = "agents:write"
    ANALYTICS_READ   = "analytics:read"
    TELEPHONY_DIAL   = "telephony:dial"
    WEBHOOKS_ADMIN   = "webhooks:admin"
    WORKSPACES_ADMIN = "workspaces:admin"
    ALL              = "*"


# Role-to-Scope Permissions Mapping
ROLE_SCOPES: Dict[UserRole, Set[str]] = {
    UserRole.ADMIN: {s.value for s in ApiScope},
    UserRole.USER: {
        ApiScope.CALLS_READ.value,
        ApiScope.CALLS_WRITE.value,
        ApiScope.CALLS_DISPATCH.value,
        ApiScope.AGENTS_READ.value,
        ApiScope.AGENTS_WRITE.value,
        ApiScope.ANALYTICS_READ.value,
        ApiScope.TELEPHONY_DIAL.value,
        ApiScope.WEBHOOKS_ADMIN.value,
    },
}


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class Workspace:
    workspace_id: str
    name: str
    slug: str
    created_at: float = field(default_factory=time.time)
    rate_limit_rpm: int = 120  # requests per minute per workspace
    metadata: Dict[str, Any] = field(default_factory=dict)
    active: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ApiKey:
    key_id: str
    workspace_id: str
    name: str
    key_prefix: str        # e.g. "ak_live_a1b2..."
    key_hash: str          # SHA-256 of the secret token
    scopes: List[str]      # e.g. ["calls:read", "calls:dispatch"]
    is_test: bool = False
    created_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None
    last_used_at: Optional[float] = None
    revoked: bool = False

    def to_dict(self, include_hash: bool = False) -> Dict[str, Any]:
        d = asdict(self)
        if not include_hash:
            d.pop("key_hash", None)
        return d


@dataclass
class AuthUser:
    user_id: str
    workspace_id: str
    email: str
    role: str               # UserRole value
    created_at: float = field(default_factory=time.time)
    active: bool = True
    # Profile shown in the sidebar (no sign-in flow yet: the dashboard runs as the workspace admin).
    name: str = ""
    title: str = ""
    phone: str = ""
    # Password login (PBKDF2-SHA256). Empty hash = no password set yet (first-run setup).
    password_hash: str = ""
    password_salt: str = ""
    last_login_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int
    retry_after: int = 0


# ---------------------------------------------------------------------------
# Token Bucket Rate Limiter
# ---------------------------------------------------------------------------

class SlidingWindowRateLimiter:
    """In-memory sliding window rate limiter per identifier (API key / workspace / IP)."""

    def __init__(self, default_limit: int = 60, window_seconds: int = 60):
        self.default_limit = default_limit
        self.window_seconds = window_seconds
        # key -> list of timestamps
        self._history: Dict[str, List[float]] = {}

    def check(self, key: str, limit: Optional[int] = None) -> RateLimitResult:
        now = time.time()
        max_reqs = limit if limit is not None else self.default_limit
        cutoff = now - self.window_seconds

        timestamps = self._history.setdefault(key, [])
        # prune old entries
        self._history[key] = [t for t in timestamps if t > cutoff]
        current_count = len(self._history[key])

        if current_count < max_reqs:
            self._history[key].append(now)
            remaining = max_reqs - (current_count + 1)
            oldest = self._history[key][0]
            reset_secs = max(1, int(oldest + self.window_seconds - now))
            return RateLimitResult(
                allowed=True,
                limit=max_reqs,
                remaining=remaining,
                reset_seconds=reset_secs,
            )
        else:
            oldest = self._history[key][0]
            retry_after = max(1, int(oldest + self.window_seconds - now))
            return RateLimitResult(
                allowed=False,
                limit=max_reqs,
                remaining=0,
                reset_seconds=retry_after,
                retry_after=retry_after,
            )

    def reset(self, key: Optional[str] = None):
        if key:
            self._history.pop(key, None)
        else:
            self._history.clear()


# ---------------------------------------------------------------------------
# Auth & Tenant Manager
# ---------------------------------------------------------------------------

class AuthManager:
    """Manages workspaces, API keys, users, RBAC permissions, and JWT session tokens."""

    def __init__(self, store_path: str = AUTH_STORE_PATH):
        self.store_path = store_path
        self._workspaces: Dict[str, Workspace] = {}
        self._api_keys: Dict[str, ApiKey] = {}         # key_id -> ApiKey
        self._key_hash_index: Dict[str, str] = {}      # key_hash -> key_id
        self._sessions: Dict[str, Dict[str, Any]] = {}  # sha256(session token) -> session doc
        self._users: Dict[str, AuthUser] = {}          # user_id -> AuthUser
        self.rate_limiter = SlidingWindowRateLimiter(default_limit=120, window_seconds=60)
        self._init_defaults()
        self._load_store()

    def _init_defaults(self):
        """Initializes default workspace, admin user, and seed API keys."""
        default_ws = Workspace(
            workspace_id="ws-default",
            name="Primary Workspace",
            slug="default",
            rate_limit_rpm=120,
        )
        self._workspaces[default_ws.workspace_id] = default_ws

        admin_user = AuthUser(
            user_id="usr-admin-01",
            workspace_id="ws-default",
            email="admin@voiceagent.local",
            role=UserRole.ADMIN.value,
        )
        self._users[admin_user.user_id] = admin_user

    # Users, workspaces, API keys and sessions live in PostgreSQL (app_documents collections below)
    # whenever DATABASE_URL is set. The JSON file is only used when no database is configured
    # (local tests); an existing file is imported once into the database and then removed.
    COLLECTIONS = (("workspaces", "workspace_id"), ("users", "user_id"), ("api_keys", "key_id"), ("sessions", "token_hash"))

    def _db_backed(self) -> bool:
        try:
            from agent import storage
            return bool(storage.database_url()) and storage.available()
        except Exception:
            return False

    def _absorb(self, data: Dict[str, Any]) -> None:
        for w in data.get("workspaces", []) or []:
            self._workspaces[w["workspace_id"]] = Workspace(**w)
        for u in data.get("users", []) or []:
            u["role"] = normalize_role(u.get("role"))
            self._users[u["user_id"]] = AuthUser(**u)
        for k in data.get("api_keys", []) or []:
            key = ApiKey(**k)
            self._api_keys[key.key_id] = key
            self._key_hash_index[key.key_hash] = key.key_id
        now = time.time()
        for sdoc in data.get("sessions", []) or []:
            if float(sdoc.get("expires_at", 0)) > now:
                self._sessions[sdoc["token_hash"]] = sdoc

    def _load_store(self):
        if self._db_backed():
            from agent import storage
            self._absorb({name: storage.load_collection(name) for name, _ in self.COLLECTIONS})
            self._migrate_file_to_db()
            return
        if os.getenv("DATABASE_URL"):
            log.error("DATABASE_URL is set but PostgreSQL is unreachable; auth store starts with defaults only")
            return
        if os.path.isfile(self.store_path):
            try:
                with open(self.store_path, "r", encoding="utf-8") as f:
                    self._absorb(json.load(f))
            except Exception as e:
                log.warning("Failed to load auth store from %s: %s", self.store_path, e)

    def _migrate_file_to_db(self) -> None:
        """One-time import of a legacy config/auth_store.json into PostgreSQL, then drop the file."""
        if not os.path.isfile(self.store_path):
            return
        try:
            with open(self.store_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._absorb(data)          # file entries win for the documents they contain
            self._save_store()
            os.remove(self.store_path)
            log.info("Imported %s into PostgreSQL (%d users) and removed the file", self.store_path, len(self._users))
        except Exception as e:
            log.warning("Could not migrate %s into PostgreSQL: %s", self.store_path, e)

    def _snapshot(self) -> Dict[str, Any]:
        return {
            "workspaces": [w.to_dict() for w in self._workspaces.values()],
            "users": [u.to_dict() for u in self._users.values()],
            "api_keys": [k.to_dict(include_hash=True) for k in self._api_keys.values()],
            "sessions": [s for s in self._sessions.values() if float(s.get("expires_at", 0)) > time.time()],
            "updated_at": time.time(),
        }

    def _save_store(self):
        snap = self._snapshot()
        if os.getenv("DATABASE_URL"):
            from agent import storage
            ok = all(storage.save_collection(name, snap[name], id_field, snap["updated_at"]) for name, id_field in self.COLLECTIONS)
            if not ok:
                log.error("Auth store not saved: PostgreSQL write failed")
            return
        try:
            os.makedirs(os.path.dirname(self.store_path), exist_ok=True)
            with open(self.store_path, "w", encoding="utf-8") as f:
                json.dump(snap, f, indent=2)
        except Exception as e:
            log.warning("Failed to save auth store: %s", e)

    # ── Workspace Operations ────────────────────────────────────────────────

    def create_workspace(self, name: str, slug: Optional[str] = None, rate_limit_rpm: int = 120) -> Workspace:
        ws_id = f"ws-{secrets.token_hex(4)}"
        ws_slug = slug or re.sub(r"[^a-z0-9_-]", "-", name.lower()).strip("-")
        ws = Workspace(
            workspace_id=ws_id,
            name=name,
            slug=ws_slug,
            rate_limit_rpm=rate_limit_rpm,
        )
        self._workspaces[ws_id] = ws
        self._save_store()
        log.info("Created workspace %s (%s)", ws.name, ws_id)
        return ws

    def get_workspace(self, workspace_id: str) -> Optional[Workspace]:
        return self._workspaces.get(workspace_id)

    def list_workspaces(self) -> List[Dict[str, Any]]:
        return [w.to_dict() for w in self._workspaces.values() if w.active]

    # ── API Key Management ──────────────────────────────────────────────────

    def generate_api_key(
        self,
        workspace_id: str,
        name: str,
        scopes: Optional[List[str]] = None,
        is_test: bool = False,
        expires_in_days: Optional[int] = None,
    ) -> Tuple[ApiKey, str]:
        """Generates a cryptographically secure API key. Returns (ApiKey, raw_secret_token)."""
        if workspace_id not in self._workspaces:
            raise KeyError(f"Workspace '{workspace_id}' not found")

        prefix = "ak_test_" if is_test else "ak_live_"
        raw_secret = f"{prefix}{secrets.token_urlsafe(32)}"
        key_hash = hashlib.sha256(raw_secret.encode("utf-8")).hexdigest()
        key_id = f"key-{secrets.token_hex(6)}"

        effective_scopes = scopes if scopes is not None else [ApiScope.ALL.value]
        expires_at = (time.time() + expires_in_days * 86400) if expires_in_days else None

        key = ApiKey(
            key_id=key_id,
            workspace_id=workspace_id,
            name=name,
            key_prefix=raw_secret[:12] + "...",
            key_hash=key_hash,
            scopes=effective_scopes,
            is_test=is_test,
            expires_at=expires_at,
        )
        self._api_keys[key_id] = key
        self._key_hash_index[key_hash] = key_id
        self._save_store()
        log.info("Generated API Key '%s' (id=%s) for workspace %s", name, key_id, workspace_id)
        return key, raw_secret

    def validate_api_key(self, raw_key: str) -> Optional[ApiKey]:
        """Validates a raw API key token. Returns ApiKey if valid and not expired/revoked."""
        if not raw_key or not isinstance(raw_key, str):
            return None
        key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        key_id = self._key_hash_index.get(key_hash)
        if not key_id:
            return None

        key = self._api_keys.get(key_id)
        if not key or key.revoked:
            return None
        if key.expires_at and key.expires_at < time.time():
            return None

        key.last_used_at = time.time()
        return key

    def revoke_api_key(self, key_id: str) -> bool:
        key = self._api_keys.get(key_id)
        if not key:
            return False
        key.revoked = True
        self._save_store()
        log.info("Revoked API Key %s", key_id)
        return True

    def list_api_keys(self, workspace_id: Optional[str] = None) -> List[Dict[str, Any]]:
        keys = list(self._api_keys.values())
        if workspace_id:
            keys = [k for k in keys if k.workspace_id == workspace_id]
        return [k.to_dict(include_hash=False) for k in keys if not k.revoked]

    # ── User & RBAC Operations ──────────────────────────────────────────────

    def create_user(self, workspace_id: str, email: str, role: str) -> AuthUser:
        if workspace_id not in self._workspaces:
            raise KeyError(f"Workspace '{workspace_id}' not found")
        role = normalize_role(role)
        if role not in [r.value for r in UserRole]:
            raise ValueError(f"Invalid role '{role}'. Allowed: {[r.value for r in UserRole]}")

        user_id = f"usr-{secrets.token_hex(4)}"
        user = AuthUser(user_id=user_id, workspace_id=workspace_id, email=email, role=role)
        self._users[user_id] = user
        self._save_store()
        return user

    def get_user(self, user_id: str) -> Optional[AuthUser]:
        return self._users.get(user_id)

    def add_member(self, workspace_id: str, email: str, password: str, role: str = "user",
                   name: str = "", phone: str = "") -> AuthUser:
        """Admin creates a teammate who can sign in (password, optional phone for the SMS step)."""
        email = (email or "").strip()
        if "@" not in email or " " in email:
            raise ValueError("Enter a valid email address")
        if self._find_user_by_email(email):
            raise ValueError("A user with that email already exists")
        if len(password or "") < 8:
            raise ValueError("Password must be at least 8 characters")
        user = self.create_user(workspace_id, email, role)
        user.name = (name or "").strip()[:120]
        user.phone = (phone or "").strip()[:40]
        self.set_password(user.user_id, password)
        return user

    def deactivate_user(self, user_id: str, acting_user_id: Optional[str] = None) -> bool:
        user = self._users.get(user_id)
        if not user:
            return False
        if acting_user_id and user_id == acting_user_id:
            raise ValueError("You cannot remove your own account")
        admins = [u for u in self._users.values() if u.active and u.role == UserRole.ADMIN.value and u.workspace_id == user.workspace_id]
        if user.role == UserRole.ADMIN.value and len(admins) <= 1:
            raise ValueError("The workspace needs at least one admin")
        user.active = False
        for h in [h for h, sdoc in self._sessions.items() if sdoc.get("user_id") == user_id]:
            self._sessions.pop(h, None)
        self._save_store()
        return True

    # ── Password login & sessions ────────────────────────────────────────────
    SESSION_TTL = 12 * 3600
    SESSION_TTL_REMEMBER = 30 * 24 * 3600

    @staticmethod
    def _hash_password(password: str, salt: str) -> str:
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 200_000).hex()

    def needs_setup(self) -> bool:
        """True until at least one active user has a password (first run)."""
        return not any(u.password_hash for u in self._users.values() if u.active)

    def set_password(self, user_id: str, password: str) -> None:
        user = self._users.get(user_id)
        if not user:
            raise KeyError("User not found")
        if len(password or "") < 8:
            raise ValueError("Password must be at least 8 characters")
        salt = secrets.token_hex(16)
        user.password_salt = salt
        user.password_hash = self._hash_password(password, salt)
        self._save_store()

    def setup_admin(self, email: str, password: str, name: str = "") -> AuthUser:
        """First-run: give the workspace admin an email + password. Refused once any password exists."""
        if not self.needs_setup():
            raise PermissionError("Setup already completed; sign in instead")
        user = self._current_user()
        if not user:
            raise KeyError("No admin user in the default workspace")
        email = (email or "").strip()
        if "@" not in email or " " in email:
            raise ValueError("Enter a valid email address")
        user.email = email
        if name:
            user.name = name.strip()[:120]
        self.set_password(user.user_id, password)
        return user

    def _find_user_by_email(self, email: str) -> Optional[AuthUser]:
        email = (email or "").strip().lower()
        for u in self._users.values():
            if u.active and u.email.lower() == email:
                return u
        return None

    def check_password(self, email: str, password: str) -> AuthUser:
        """Step 1 of sign-in. Raises ValueError with a safe message when the pair is wrong."""
        user = self._find_user_by_email(email)
        # Constant-ish time: hash even when the user is unknown so timing does not reveal accounts.
        salt = user.password_salt if (user and user.password_salt) else secrets.token_hex(16)
        candidate = self._hash_password(password or "", salt)
        if not user or not user.password_hash or not secrets.compare_digest(candidate, user.password_hash):
            raise ValueError("Incorrect email or password")
        return user

    def create_session(self, user: AuthUser, remember: bool = False, user_agent: str = "", method: str = "password") -> Tuple[str, Dict[str, Any]]:
        token = secrets.token_urlsafe(32)
        ttl = self.SESSION_TTL_REMEMBER if remember else self.SESSION_TTL
        doc = {
            "token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
            "user_id": user.user_id, "workspace_id": user.workspace_id,
            "created_at": time.time(), "expires_at": time.time() + ttl, "remember": bool(remember),
            "user_agent": (user_agent or "")[:200], "method": method,
        }
        self._sessions[doc["token_hash"]] = doc
        user.last_login_at = time.time()
        self._save_store()
        return token, doc

    def login(self, email: str, password: str, remember: bool = False, user_agent: str = "") -> Tuple[str, Dict[str, Any]]:
        """Password-only sign-in (used when the account has no phone for the SMS step)."""
        user = self.check_password(email, password)
        return self.create_session(user, remember=remember, user_agent=user_agent, method="password")

    def session_user(self, token: str) -> Optional[AuthUser]:
        if not token:
            return None
        doc = self._sessions.get(hashlib.sha256(token.encode("utf-8")).hexdigest())
        if not doc or float(doc.get("expires_at", 0)) < time.time():
            return None
        user = self._users.get(doc["user_id"])
        return user if (user and user.active) else None

    def logout(self, token: str) -> bool:
        if not token:
            return False
        removed = self._sessions.pop(hashlib.sha256(token.encode("utf-8")).hexdigest(), None) is not None
        if removed:
            self._save_store()
        return removed

    def session_info(self, token: str) -> Dict[str, Any]:
        user = self.session_user(token)
        ws = self._workspaces.get(user.workspace_id) if user else None
        try:
            from agent import sms
            sms_ready = bool(sms.configured().get("ready"))
        except Exception:
            sms_ready = False
        admin = self._current_user() if self.needs_setup() else None
        return {
            "authenticated": bool(user),
            "needs_setup": self.needs_setup(),
            "setup_email": admin.email if admin else "",
            "setup_name": admin.name if admin else "",
            "sms_login": sms_ready,
            "user": {"user_id": user.user_id, "email": user.email, "name": user.name, "role": user.role,
                     "workspace": ws.name if ws else "", "workspace_id": user.workspace_id} if user else None,
        }

    # ── SMS one-time codes (step 2 after the password) ───────────────────────
    OTP_TTL = 300          # seconds a code stays valid
    OTP_RESEND_AFTER = 30  # seconds before another code may be sent for the same login
    OTP_MAX_ATTEMPTS = 5
    TICKET_TTL = 600       # seconds a password-verified login may wait for its code

    @staticmethod
    def normalize_phone(raw: str) -> str:
        from agent.telephony_manager import normalize_phone_number
        return normalize_phone_number(raw or "")

    @staticmethod
    def mask_phone(phone: str) -> str:
        p = phone or ""
        return (p[:3] + "•" * max(0, len(p) - 6) + p[-3:]) if len(p) > 6 else "•••"

    def _tickets(self) -> Dict[str, Dict[str, Any]]:
        if not hasattr(self, "_login_tickets"):
            self._login_tickets: Dict[str, Dict[str, Any]] = {}
        now = time.time()
        for k in [k for k, v in self._login_tickets.items() if now > float(v.get("expires_at", 0))]:
            self._login_tickets.pop(k, None)
        return self._login_tickets

    def begin_two_step(self, user: AuthUser, remember: bool, user_agent: str = "") -> Dict[str, Any]:
        """After a correct password: issue a pending ticket and a fresh SMS code for the user's phone."""
        phone = self.normalize_phone(user.phone)
        if not phone:
            raise ValueError("No phone number on this account")
        ticket = secrets.token_urlsafe(24)
        code = f"{secrets.randbelow(1_000_000):06d}"
        salt = secrets.token_hex(8)
        now = time.time()
        self._tickets()[hashlib.sha256(ticket.encode()).hexdigest()] = {
            "user_id": user.user_id, "phone": phone, "remember": bool(remember), "user_agent": (user_agent or "")[:200],
            "code_hash": hashlib.sha256((salt + code).encode()).hexdigest(), "salt": salt,
            "sent_at": now, "code_expires_at": now + self.OTP_TTL, "expires_at": now + self.TICKET_TTL, "attempts": 0,
        }
        return {"ticket": ticket, "code": code, "phone": phone, "phone_masked": self.mask_phone(phone)}

    def resend_code(self, ticket: str) -> Dict[str, Any]:
        rec = self._tickets().get(hashlib.sha256((ticket or "").encode()).hexdigest())
        if not rec:
            raise ValueError("This sign-in has expired. Start again with your password")
        now = time.time()
        if now - float(rec["sent_at"]) < self.OTP_RESEND_AFTER:
            raise ValueError(f"Wait {int(self.OTP_RESEND_AFTER - (now - rec['sent_at']))}s before requesting another code")
        code = f"{secrets.randbelow(1_000_000):06d}"
        salt = secrets.token_hex(8)
        rec.update(code_hash=hashlib.sha256((salt + code).encode()).hexdigest(), salt=salt, sent_at=now,
                   code_expires_at=now + self.OTP_TTL, attempts=0)
        return {"code": code, "phone": rec["phone"], "phone_masked": self.mask_phone(rec["phone"])}

    def complete_two_step(self, ticket: str, code: str) -> Tuple[str, Dict[str, Any]]:
        key = hashlib.sha256((ticket or "").encode()).hexdigest()
        rec = self._tickets().get(key)
        if not rec:
            raise ValueError("This sign-in has expired. Start again with your password")
        if time.time() > float(rec["code_expires_at"]):
            raise ValueError("That code has expired. Request a new one")
        if rec["attempts"] >= self.OTP_MAX_ATTEMPTS:
            self._tickets().pop(key, None)
            raise ValueError("Too many attempts. Start again with your password")
        rec["attempts"] += 1
        digest = hashlib.sha256((rec["salt"] + (code or "").strip()).encode()).hexdigest()
        if not secrets.compare_digest(digest, rec["code_hash"]):
            raise ValueError("Incorrect code")
        self._tickets().pop(key, None)
        user = self._users.get(rec["user_id"])
        if not user or not user.active:
            raise ValueError("Account is not active")
        return self.create_session(user, remember=rec["remember"], user_agent=rec["user_agent"], method="password+sms")

    # ── Current profile (sidebar) ────────────────────────────────────────────
    def _current_user(self) -> Optional[AuthUser]:
        """The dashboard has no login yet; it operates as the first active admin of the default workspace."""
        ws_id = "ws-default" if "ws-default" in self._workspaces else (next(iter(self._workspaces), None))
        admins = [u for u in self._users.values() if u.active and u.workspace_id == ws_id and u.role == UserRole.ADMIN.value]
        if admins:
            return sorted(admins, key=lambda u: u.created_at)[0]
        users = [u for u in self._users.values() if u.active and u.workspace_id == ws_id]
        return sorted(users, key=lambda u: u.created_at)[0] if users else None

    def get_profile(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        user = self._users.get(user_id) if user_id else None
        user = user or self._current_user()
        ws = self._workspaces.get(user.workspace_id) if user else None
        return {
            "user_id": user.user_id if user else None,
            "name": (user.name if user else "") or "",
            "email": user.email if user else "",
            "title": (user.title if user else "") or "",
            "phone": (user.phone if user else "") or "",
            "role": user.role if user else "",
            "workspace_id": ws.workspace_id if ws else "",
            "workspace": ws.name if ws else "",
            "workspace_slug": ws.slug if ws else "",
            "workspace_created_at": ws.created_at if ws else None,
            "rate_limit_rpm": ws.rate_limit_rpm if ws else None,
            "member_since": user.created_at if user else None,
            "members": [
                {"user_id": u.user_id, "email": u.email, "name": u.name or "", "role": u.role, "created_at": u.created_at,
                 "is_me": bool(user and u.user_id == user.user_id)}
                for u in sorted(self._users.values(), key=lambda x: x.created_at)
                if u.active and ws and u.workspace_id == ws.workspace_id
                and (user is None or user.role == UserRole.ADMIN.value or u.user_id == user.user_id)
            ],
            "api_keys": [
                {"key_id": k.key_id, "name": k.name, "prefix": k.key_prefix, "scopes": k.scopes, "is_test": k.is_test,
                 "created_at": k.created_at, "last_used_at": k.last_used_at, "revoked": k.revoked}
                for k in sorted(self._api_keys.values(), key=lambda x: x.created_at, reverse=True)
                if ws and k.workspace_id == ws.workspace_id and (user is None or user.role == UserRole.ADMIN.value)
            ][:10],
            "api_keys_active": sum(1 for k in self._api_keys.values() if ws and k.workspace_id == ws.workspace_id and not k.revoked),
        }

    def update_profile(self, changes: Dict[str, Any], user_id: Optional[str] = None) -> Dict[str, Any]:
        user = (self._users.get(user_id) if user_id else None) or self._current_user()
        if not user:
            raise KeyError("No user to update")
        if changes.get("new_password"):
            current = changes.get("current_password") or ""
            if user.password_hash and not secrets.compare_digest(self._hash_password(current, user.password_salt), user.password_hash):
                raise ValueError("Current password is incorrect")
            self.set_password(user.user_id, str(changes["new_password"]))
        for key in ("name", "email", "title", "phone"):
            if key in changes and changes[key] is not None:
                val = str(changes[key]).strip()
                if key == "email":
                    if val and ("@" not in val or " " in val):
                        raise ValueError("Enter a valid email address")
                    if val:
                        user.email = val
                else:
                    setattr(user, key, val[:120])
        if "workspace" in changes and changes["workspace"] is not None:
            ws = self._workspaces.get(user.workspace_id)
            if ws:
                ws.name = str(changes["workspace"]).strip()[:80] or ws.name
        self._save_store()
        return self.get_profile(user.user_id)

    def list_users(self, workspace_id: Optional[str] = None) -> List[Dict[str, Any]]:
        users = list(self._users.values())
        if workspace_id:
            users = [u for u in users if u.workspace_id == workspace_id]
        return [u.to_dict() for u in users if u.active]

    # ── Permission & Scope Checks ────────────────────────────────────────────

    def has_scope(self, api_key: ApiKey, required_scope: str) -> bool:
        """Checks if an API key has a specific scope or wildcard access."""
        if ApiScope.ALL.value in api_key.scopes:
            return True
        if required_scope in api_key.scopes:
            return True
        # Check namespace wildcard e.g. "calls:*" covers "calls:read"
        namespace = required_scope.split(":")[0] + ":*"
        return namespace in api_key.scopes

    def role_has_permission(self, role_str: str, required_scope: str) -> bool:
        """Checks if a user role possesses the required permission scope."""
        try:
            role = UserRole(normalize_role(role_str))
        except ValueError:
            return False
        allowed = ROLE_SCOPES.get(role, set())
        return ApiScope.ALL.value in allowed or required_scope in allowed

    # ── JWT / Session Token Issuance ────────────────────────────────────────

    def issue_token(
        self,
        workspace_id: str,
        subject: str,
        role: str = "user",
        scopes: Optional[List[str]] = None,
        ttl_seconds: int = 3600,
    ) -> str:
        """Issues a signed JWT-like Bearer token: base64(header).base64(payload).signature."""
        role = normalize_role(role)
        now = int(time.time())
        payload = {
            "sub": subject,
            "ws": workspace_id,
            "role": role,
            "scopes": scopes or list(ROLE_SCOPES.get(UserRole(role), set())),
            "iat": now,
            "exp": now + ttl_seconds,
        }
        header_b64 = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
        payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        to_sign = f"{header_b64}.{payload_b64}".encode()
        sig = hmac.new(JWT_SECRET.encode(), to_sign, hashlib.sha256).hexdigest()
        return f"{header_b64}.{payload_b64}.{sig}"

    def verify_token(self, token: str) -> Optional[Dict[str, Any]]:
        """Verifies a signed Bearer token. Returns payload dict if valid and not expired."""
        try:
            parts = token.strip().split(".")
            if len(parts) != 3:
                return None
            header_b64, payload_b64, sig = parts
            to_sign = f"{header_b64}.{payload_b64}".encode()
            expected_sig = hmac.new(JWT_SECRET.encode(), to_sign, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, expected_sig):
                return None

            # Add padding back to base64
            padded = payload_b64 + "=" * ((4 - len(payload_b64) % 4) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
            if payload.get("exp", 0) < time.time():
                return None  # Expired
            return payload
        except Exception:
            return None

    # ── Comprehensive Request Authenticator ─────────────────────────────────

    def authenticate_request(
        self,
        headers: Dict[str, str],
        required_scope: Optional[str] = None,
    ) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
        """Authenticates an incoming HTTP request via Bearer Token, X-API-Key, or Workspace header.

        Returns: (is_authenticated, auth_context, error_message)
        auth_context contains: {workspace_id, auth_type, identity, role, scopes}
        """
        # Case-insensitive header dictionary
        hdrs = {k.lower(): str(v).strip() for k, v in headers.items()}
        auth_hdr = hdrs.get("authorization", "")
        api_key_hdr = hdrs.get("x-api-key", "")
        ws_hdr = hdrs.get("x-workspace-id", "")

        # Case A: Bearer JWT Token
        if auth_hdr.startswith("Bearer "):
            token = auth_hdr[7:].strip()
            payload = self.verify_token(token)
            if not payload:
                return False, None, "Invalid or expired Bearer token"

            ws_id = payload.get("ws", "ws-default")
            scopes = payload.get("scopes", [])
            role = normalize_role(payload.get("role", "user"))

            # Check Rate Limit
            rate_res = self.rate_limiter.check(f"user:{payload['sub']}")
            if not rate_res.allowed:
                return False, None, f"Rate limit exceeded. Retry in {rate_res.retry_after}s"

            if required_scope and ApiScope.ALL.value not in scopes and required_scope not in scopes:
                return False, None, f"Forbidden: missing required scope '{required_scope}'"

            ctx = {
                "workspace_id": ws_id,
                "auth_type": "bearer",
                "identity": payload["sub"],
                "role": role,
                "scopes": scopes,
            }
            return True, ctx, None

        # Case B: API Key (either in X-API-Key or Authorization: ApiKey ...)
        raw_key = api_key_hdr
        if not raw_key and auth_hdr.startswith("ApiKey "):
            raw_key = auth_hdr[7:].strip()

        if raw_key:
            key_obj = self.validate_api_key(raw_key)
            if not key_obj:
                return False, None, "Invalid, expired, or revoked API key"

            # Check Rate Limit on the API key
            rate_res = self.rate_limiter.check(f"key:{key_obj.key_id}")
            if not rate_res.allowed:
                return False, None, f"Rate limit exceeded. Retry in {rate_res.retry_after}s"

            if required_scope and not self.has_scope(key_obj, required_scope):
                return False, None, f"Forbidden: API key lacks scope '{required_scope}'"

            ctx = {
                "workspace_id": key_obj.workspace_id,
                "auth_type": "api_key",
                "identity": key_obj.key_id,
                "role": UserRole.ADMIN.value,
                "scopes": key_obj.scopes,
                "is_test": key_obj.is_test,
            }
            return True, ctx, None

        # Case C: Workspace header fallback for open local endpoints
        if ws_hdr:
            ws = self.get_workspace(ws_hdr)
            if not ws or not ws.active:
                return False, None, f"Workspace '{ws_hdr}' not found or inactive"

            rate_res = self.rate_limiter.check(f"ws:{ws.workspace_id}", limit=ws.rate_limit_rpm)
            if not rate_res.allowed:
                return False, None, f"Workspace rate limit exceeded. Retry in {rate_res.retry_after}s"

            ctx = {
                "workspace_id": ws.workspace_id,
                "auth_type": "workspace_header",
                "identity": f"anon-{ws.workspace_id}",
                "role": UserRole.USER.value,
                "scopes": [ApiScope.ALL.value],
            }
            return True, ctx, None

        # If required_scope was requested and no auth was provided at all
        if required_scope:
            return False, None, f"Missing authentication (Bearer token or X-API-Key required for '{required_scope}')"

        # Default fallback for localhost internal requests
        ctx = {
            "workspace_id": "ws-default",
            "auth_type": "internal",
            "identity": "internal-admin",
            "role": UserRole.ADMIN.value,
            "scopes": [ApiScope.ALL.value],
        }
        return True, ctx, None


# Global singleton
auth_manager = AuthManager()

