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
import tempfile
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from agent.firm_profile import SIGNUP_REQUIRED_FIELDS, normalize_firm_profile, normalize_phone as normalize_firm_phone

log = logging.getLogger("voice-agent.auth")
if not log.handlers:
    logging.basicConfig(level=logging.INFO)


ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT_DIR, "config")
AUTH_STORE_PATH = os.path.join(CONFIG_DIR, "auth_store.json")
SYSTEM_BRANDING_PATH = os.path.join(CONFIG_DIR, "system_branding.json")

# Default system secret for signing session tokens
JWT_SECRET = os.getenv("AUTH_JWT_SECRET", "voice-agent-jwt-secret-key-prod-2026")


# ---------------------------------------------------------------------------
# Enums: Roles & Scopes
# ---------------------------------------------------------------------------

class UserRole(str, Enum):
    """The platform has exactly three roles. Member Admin is the lowest: it covers day-to-day work
    (overview, calls, softphone, agents, profile) and member management inside its workspace."""
    SUPER_ADMIN  = "super_admin"   # Overall Admin: manages all orgs and can create product/member admins
    ADMIN        = "admin"         # Product Admin: manages single workspace; can create member admins
    MEMBER_ADMIN = "member_admin"  # Member Admin: works in and manages members of a single workspace


# Aliases from earlier builds and system variations. The retired "Standard User" role and its
# synonyms map to Member Admin so old records and API clients keep working.
ROLE_ALIASES = {
    "super_admin": UserRole.SUPER_ADMIN.value,
    "superadmin": UserRole.SUPER_ADMIN.value,
    "overall_admin": UserRole.SUPER_ADMIN.value,
    "system_admin": UserRole.SUPER_ADMIN.value,
    "root": UserRole.SUPER_ADMIN.value,
    "admin": UserRole.ADMIN.value,
    "product_admin": UserRole.ADMIN.value,
    "member_admin": UserRole.MEMBER_ADMIN.value,
    "team_admin": UserRole.MEMBER_ADMIN.value,
    "operator": UserRole.MEMBER_ADMIN.value,
    "analyst": UserRole.MEMBER_ADMIN.value,
    "member": UserRole.MEMBER_ADMIN.value,
    "user": UserRole.MEMBER_ADMIN.value,
}


def normalize_role(role: Optional[str]) -> str:
    """Every role string, old or new, resolves to one of the three UserRole values."""
    r = (role or "").strip().lower()
    r = ROLE_ALIASES.get(r, r)
    return r if r in {x.value for x in UserRole} else UserRole.MEMBER_ADMIN.value


def can_create_role(creator_role: str, creator_ws_id: str, target_role: str, target_ws_id: str) -> Tuple[bool, str]:
    """Validates if creator has permission to create target_role in target_ws_id.

    Rules:
    - Super Admin can create any role (super_admin, admin/product_admin, member_admin) in ANY organization.
    - Product Admin (admin) can ONLY create member_admin in their OWN organization.
    - Member Admin (member_admin) can ONLY invite other member_admin in their OWN organization.
    """
    c_role = normalize_role(creator_role)
    t_role = normalize_role(target_role)

    if c_role == UserRole.SUPER_ADMIN.value:
        return True, "ok"

    if c_role == UserRole.ADMIN.value:
        if creator_ws_id != target_ws_id:
            return False, "Product Admins can only create users in their own organization"
        if t_role in (UserRole.SUPER_ADMIN.value, UserRole.ADMIN.value):
            return False, "Product Admins can only create Member Admins"
        if t_role == UserRole.MEMBER_ADMIN.value:
            return True, "ok"
        return False, f"Invalid role '{target_role}'"

    if c_role == UserRole.MEMBER_ADMIN.value:
        if creator_ws_id != target_ws_id:
            return False, "Member Admins can only invite members in their own organization"
        if t_role == UserRole.MEMBER_ADMIN.value:
            return True, "ok"
        return False, "Member Admins can only invite other Member Admins"

    return False, f"Unknown role '{creator_role}' cannot create or invite members"


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
    UserRole.SUPER_ADMIN: {s.value for s in ApiScope},
    # Product Admin manages workspace configuration (agents, webhooks) but does NOT have access
    # to API Keys & Provider Secrets (workspaces:admin) or Softphone WebRTC Dialer (telephony:dial),
    # which are strictly reserved for Super Admin.
    UserRole.ADMIN: {
        ApiScope.CALLS_READ.value,
        ApiScope.CALLS_WRITE.value,
        ApiScope.CALLS_DISPATCH.value,
        ApiScope.AGENTS_READ.value,
        ApiScope.AGENTS_WRITE.value,
        ApiScope.ANALYTICS_READ.value,
        ApiScope.WEBHOOKS_ADMIN.value,
    },
    UserRole.MEMBER_ADMIN: {
        ApiScope.CALLS_READ.value,
        ApiScope.CALLS_WRITE.value,
        ApiScope.CALLS_DISPATCH.value,
        ApiScope.AGENTS_READ.value,
        ApiScope.AGENTS_WRITE.value,
        ApiScope.ANALYTICS_READ.value,
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
    # Linked Google / Microsoft accounts ("google:<sub>", "microsoft:<tid>:<oid>"), see agent/oauth.py.
    identities: List[str] = field(default_factory=list)
    # Clio Manage user id this member is linked to, so case assignments can set the matter's
    # Responsible Attorney (Team page). "" = match by email when the Clio app may list users.
    clio_user_id: str = ""

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
            history = self._history.get(key) or []
            oldest = history[0] if history else now
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


class PendingVerification(ValueError):
    """Correct password, but the account's self-service sign-up has not been verified yet.
    A ValueError so callers that only show the message keep working; the login endpoint catches it
    to send a fresh verification code instead."""

    def __init__(self, message: str, user: "AuthUser"):
        super().__init__(message)
        self.user = user


# ---------------------------------------------------------------------------
# Auth & Tenant Manager
# ---------------------------------------------------------------------------

class AuthManager:
    """Manages workspaces, API keys, users, RBAC permissions, and JWT session tokens."""

    def __init__(self, store_path: str = AUTH_STORE_PATH):
        self.store_path = store_path
        self._db_load_failed: bool = False
        self._workspaces: Dict[str, Workspace] = {}
        self._api_keys: Dict[str, ApiKey] = {}         # key_id -> ApiKey
        self._key_hash_index: Dict[str, str] = {}      # key_hash -> key_id
        self._sessions: Dict[str, Dict[str, Any]] = {}  # sha256(session token) -> session doc
        self._users: Dict[str, AuthUser] = {}          # user_id -> AuthUser
        self.rate_limiter = SlidingWindowRateLimiter(default_limit=120, window_seconds=60)
        self.signup_limiter = SlidingWindowRateLimiter(default_limit=self.SIGNUP_ATTEMPTS_PER_IP_PER_HOUR, window_seconds=3600)
        self._init_defaults()
        self._load_store()
        self._fold_retired_roles()
        self._ensure_env_super_admins()


    def _fold_retired_roles(self) -> None:
        """Accounts still carrying a retired role (e.g. the old Standard User) become Member Admins,
        once, so every stored role is one of the three the platform has."""
        changed = [u for u in self._users.values() if u.role != normalize_role(u.role)]
        for u in changed:
            log.info("Role '%s' on %s folded into %s", u.role, u.email, normalize_role(u.role))
            u.role = normalize_role(u.role)
        if changed:
            self._save_store()

    def _ensure_env_super_admins(self) -> None:
        """SUPER_ADMIN_EMAIL (comma-separated) names accounts that must exist as active Super Admins:
        promoted if present, created in the default workspace otherwise. SUPER_ADMIN_PASSWORD, when set,
        gives a created account (or one with no password yet) its first password; with
        SUPER_ADMIN_RESET_PASSWORD=1 it overwrites an existing password too (a one-shot recovery: unset
        it afterwards). Lets a deployment (Railway) get its owner account from a variable instead of a
        database edit."""
        emails = [e.strip().lower() for e in os.getenv("SUPER_ADMIN_EMAIL", "").split(",") if e.strip()]
        if not emails:
            return
        password = os.getenv("SUPER_ADMIN_PASSWORD", "")
        reset = os.getenv("SUPER_ADMIN_RESET_PASSWORD", "").strip().lower() in ("1", "true", "yes")
        changed = False
        if len(emails) > 1:
            log.error("SUPER_ADMIN_EMAIL lists %d addresses; only one Super Admin is allowed, using %s", len(emails), emails[0])
            emails = emails[:1]
        for email in emails:
            user = self._find_user_by_email(email) or next(
                (u for u in self._users.values() if u.email.lower() == email), None)
            already = bool(user and user.active and user.role == UserRole.SUPER_ADMIN.value)
            holder = None if already else self.admin_slot_holder(UserRole.SUPER_ADMIN.value, "", exclude_user_id=user.user_id if user else None)
            if holder and holder.user_id == "usr-admin-01" and not holder.password_hash:
                holder.active = False       # the built-in placeholder never signed in: the named owner replaces it
                changed = True
            elif holder:
                log.error("SUPER_ADMIN_EMAIL=%s ignored: %s is already the Super Admin (only one is allowed)", email, holder.email)
                continue
            if user:
                if user.role != UserRole.SUPER_ADMIN.value or not user.active:
                    user.role = UserRole.SUPER_ADMIN.value
                    user.active = True
                    changed = True
                    log.info("SUPER_ADMIN_EMAIL: promoted %s to super_admin", email)
            else:
                user = AuthUser(user_id=f"usr-{secrets.token_hex(4)}", workspace_id="ws-default",
                                email=email, role=UserRole.SUPER_ADMIN.value, name=email.split("@")[0])
                self._users[user.user_id] = user
                changed = True
                log.info("SUPER_ADMIN_EMAIL: created super_admin %s", email)
            if password and len(password) >= 8 and (reset or not user.password_hash):
                salt = secrets.token_hex(16)
                user.password_salt = salt
                user.password_hash = self._hash_password(password, salt)
                changed = True
                log.info("SUPER_ADMIN_EMAIL: password %s for %s", "reset" if reset else "set", email)
        if changed:
            self._save_store()

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
            role=UserRole.SUPER_ADMIN.value,
            name="Super Administrator",
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
            self._db_load_failed = False
            self._migrate_file_to_db()
            return
        if os.getenv("DATABASE_URL"):
            self._db_load_failed = True
            log.error("DATABASE_URL is set but PostgreSQL is unreachable; auth store starts in protected fallback mode")
            if os.path.isfile(self.store_path):
                try:
                    with open(self.store_path, "r", encoding="utf-8") as f:
                        self._absorb(json.load(f))
                except Exception as e:
                    log.warning("Failed fallback load from %s: %s", self.store_path, e)
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
        if self._db_load_failed:
            if self._db_backed():
                log.info("PostgreSQL reconnected; loading database state before saving")
                from agent import storage
                self._absorb({name: storage.load_collection(name) for name, _ in self.COLLECTIONS})
                self._db_load_failed = False
            else:
                log.error("Auth store not saved: PostgreSQL was unreachable at startup and is still down (refusing to overwrite)")
                return False
        snap = self._snapshot()
        if os.getenv("DATABASE_URL"):
            from agent import storage
            ok = all(storage.save_collection(name, snap[name], id_field, snap["updated_at"]) for name, id_field in self.COLLECTIONS)
            if not ok:
                log.error("Auth store not saved: PostgreSQL write failed")
                return False
            return True
        try:
            store_dir = os.path.dirname(os.path.abspath(self.store_path))
            os.makedirs(store_dir, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", dir=store_dir, delete=False, encoding="utf-8") as tf:
                json.dump(snap, tf, indent=2)
                temp_path = tf.name
            os.replace(temp_path, self.store_path)
            return True
        except Exception as e:
            log.warning("Failed to save auth store: %s", e)
            if "temp_path" in locals() and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            return False

    # ── Workspace Operations ────────────────────────────────────────────────

    def create_workspace(self, name: str, slug: Optional[str] = None, rate_limit_rpm: int = 120) -> Workspace:
        rpm = max(1, int(rate_limit_rpm or 120))
        ws_id = f"ws-{secrets.token_hex(4)}"
        ws_slug = slug or re.sub(r"[^a-z0-9_-]", "-", name.lower()).strip("-")
        ws = Workspace(
            workspace_id=ws_id,
            name=name,
            slug=ws_slug,
            rate_limit_rpm=rpm,
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

        ws = self.get_workspace(key.workspace_id)
        if not ws or not ws.active:
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

    # One Super Admin for the whole platform, one Product Admin per organization. Every path that
    # creates, promotes, moves or reactivates an account checks the slot first.
    def admin_slot_holder(self, role: str, workspace_id: str, exclude_user_id: Optional[str] = None) -> Optional["AuthUser"]:
        """The active account already holding the single Super Admin / Product Admin slot, if any."""
        role = normalize_role(role)
        for u in sorted(self._users.values(), key=lambda x: x.created_at):
            if not u.active or u.user_id == exclude_user_id or u.role != role:
                continue
            if role == UserRole.SUPER_ADMIN.value:
                return u
            if role == UserRole.ADMIN.value and u.workspace_id == workspace_id:
                return u
        return None

    def require_admin_slot(self, role: str, workspace_id: str, exclude_user_id: Optional[str] = None) -> None:
        holder = self.admin_slot_holder(role, workspace_id, exclude_user_id)
        if not holder:
            return
        if normalize_role(role) == UserRole.SUPER_ADMIN.value:
            raise ValueError(f"There can be only one Super Admin, and {holder.email} already is. Transfer the role instead.")
        ws = self._workspaces.get(workspace_id)
        raise ValueError(f"{ws.name if ws else 'This organization'} already has a Product Admin ({holder.email}). "
                         "Each organization has exactly one; transfer the role instead.")

    def create_user(self, workspace_id: str, email: str, role: str) -> AuthUser:
        if workspace_id not in self._workspaces:
            raise KeyError(f"Workspace '{workspace_id}' not found")
        role = normalize_role(role)
        if role not in [r.value for r in UserRole]:
            raise ValueError(f"Invalid role '{role}'. Allowed: {[r.value for r in UserRole]}")
        self.require_admin_slot(role, workspace_id)

        user_id = f"usr-{secrets.token_hex(4)}"
        user = AuthUser(user_id=user_id, workspace_id=workspace_id, email=email, role=role)
        self._users[user_id] = user
        self._save_store()
        return user

    def get_user(self, user_id: str) -> Optional[AuthUser]:
        return self._users.get(user_id)

    def add_member(self, workspace_id: str, email: str, password: str, role: str = "member_admin",
                   name: str = "", phone: str = "", title: str = "") -> AuthUser:
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
        user.title = " ".join((title or "").split())[:80]   # e.g. Attorney, Paralegal (shown on the Team and Cases pages)
        self.set_password(user.user_id, password)
        return user

    def set_clio_user_id(self, user_id: str, clio_user_id: str) -> AuthUser:
        user = self._users.get(user_id)
        if not user or not user.active:
            raise KeyError(user_id)
        value = (clio_user_id or "").strip()
        if value and not value.isdigit():
            raise ValueError("A Clio user id is a number (Clio → Settings → Firm → Users, or ask Clio support)")
        user.clio_user_id = value[:20]
        self._save_store()
        return user

    def deactivate_user(self, user_id: str, acting_user_id: Optional[str] = None) -> bool:
        user = self._users.get(user_id)
        if not user:
            return False
        if acting_user_id and user_id == acting_user_id:
            raise ValueError("You cannot remove your own account")

        if acting_user_id:
            acting_user = self._users.get(acting_user_id)
            if acting_user:
                # Only super_admin can deactivate across workspaces or deactivate super_admins
                if acting_user.role != UserRole.SUPER_ADMIN.value:
                    if user.workspace_id != acting_user.workspace_id:
                        raise PermissionError("Cannot deactivate users in another organization")
                    if user.role == UserRole.SUPER_ADMIN.value:
                        raise PermissionError("Only Super Admins can deactivate a Super Admin")
                    if user.role == UserRole.ADMIN.value and acting_user.role != UserRole.ADMIN.value:
                        raise PermissionError("Insufficient permissions to deactivate an administrator")

        if user.role == UserRole.SUPER_ADMIN.value:
            super_admins = [u for u in self._users.values() if u.active and u.role == UserRole.SUPER_ADMIN.value and u.user_id != user.user_id]
            if not super_admins:
                raise ValueError("The system needs at least one active Super Admin")

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
        existing = self._find_user_by_email(email)
        if existing and existing.user_id != user.user_id:
            raise ValueError("An account with this email address already exists")
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
        ws = self.get_workspace(user.workspace_id)
        if ws and not ws.active:
            if ws.metadata.get("signup", {}).get("status") == self.SIGNUP_PENDING:
                raise PendingVerification("This account has not been verified yet. Finish verifying your phone number to sign in.", user)
            raise ValueError("This organization has been suspended. Please contact your administrator.")
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
        if not doc or float(doc.get("expires_at", 0)) < time.time() or doc.get("kind") == "password_reset":
            return None
        user = self._users.get(doc["user_id"])
        if not user or not user.active:
            return None
        ws = self.get_workspace(user.workspace_id)
        if ws and not ws.active:
            return None
        return user

    # ── Forgotten password (email link) ─────────────────────────────────────
    # Reset tickets are stored beside sessions (same persisted collection, same expiry pruning) but
    # carry kind="password_reset" so they can never be presented as a login session.
    RESET_TTL = 60 * 60          # a reset link is good for one hour
    RESET_RESEND_WAIT = 60       # and a new one cannot be requested more often than once a minute

    def create_password_reset(self, email: str) -> Optional[Tuple[AuthUser, str]]:
        """Issue a single-use reset token for an active account. Returns None for unknown emails so
        the caller can answer identically either way (no account enumeration); raises ValueError
        when one was requested too recently."""
        user = self._find_user_by_email(email)
        if not user:
            return None
        now = time.time()
        for h, doc in list(self._sessions.items()):
            if doc.get("kind") == "password_reset" and doc.get("user_id") == user.user_id:
                if now - float(doc.get("created_at", 0)) < self.RESET_RESEND_WAIT:
                    raise ValueError("A reset link was sent a moment ago. Check your inbox or try again in a minute.")
                self._sessions.pop(h, None)          # one outstanding link per account
        token = secrets.token_urlsafe(32)
        self._sessions[hashlib.sha256(token.encode("utf-8")).hexdigest()] = {
            "token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
            "kind": "password_reset", "user_id": user.user_id, "workspace_id": user.workspace_id,
            "created_at": now, "expires_at": now + self.RESET_TTL, "remember": False,
            "user_agent": "", "method": "password_reset",
        }
        self._save_store()
        return user, token

    def peek_password_reset(self, token: str) -> Optional[AuthUser]:
        """The account a still-valid reset token belongs to (for the reset page), else None."""
        doc = self._sessions.get(hashlib.sha256((token or "").encode("utf-8")).hexdigest())
        if not doc or doc.get("kind") != "password_reset" or float(doc.get("expires_at", 0)) < time.time():
            return None
        user = self._users.get(doc["user_id"])
        return user if (user and user.active) else None

    def consume_password_reset(self, token: str, new_password: str) -> AuthUser:
        """Set a new password from a valid token, burn the token and sign the account out everywhere."""
        user = self.peek_password_reset(token)
        if not user:
            raise ValueError("This reset link is invalid or has expired. Request a new one.")
        self.set_password(user.user_id, new_password)   # validates length; saves
        for h in [h for h, d in self._sessions.items() if d.get("user_id") == user.user_id]:
            self._sessions.pop(h, None)
        self._save_store()
        log.info("Password reset completed for %s", user.email)
        return user

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
    SIGNUP_TICKET_TTL = 1800  # a new sign-up may take longer to find its phone
    TICKET_PURPOSES = ("login", "signup")

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

    def begin_two_step(self, user: AuthUser, remember: bool, user_agent: str = "", purpose: str = "login") -> Dict[str, Any]:
        """After a correct password (or a new sign-up): issue a pending ticket and a fresh SMS code for
        the user's phone. purpose="signup" makes complete_two_step activate the pending workspace."""
        if purpose not in self.TICKET_PURPOSES:
            raise ValueError(f"Unknown verification purpose '{purpose}'")
        phone = self.normalize_phone(user.phone)
        if not phone:
            raise ValueError("No phone number on this account")
        if purpose == "signup" and not self.is_signup_pending(user.workspace_id):
            raise ValueError("This account does not need verification")
        ticket = secrets.token_urlsafe(24)
        code = f"{secrets.randbelow(1_000_000):06d}"
        salt = secrets.token_hex(8)
        now = time.time()
        self._tickets()[hashlib.sha256(ticket.encode()).hexdigest()] = {
            "user_id": user.user_id, "phone": phone, "remember": bool(remember), "user_agent": (user_agent or "")[:200],
            "code_hash": hashlib.sha256((salt + code).encode()).hexdigest(), "salt": salt, "purpose": purpose,
            "sent_at": now, "code_expires_at": now + self.OTP_TTL, "attempts": 0,
            "expires_at": now + (self.SIGNUP_TICKET_TTL if purpose == "signup" else self.TICKET_TTL),
        }
        return {"ticket": ticket, "code": code, "phone": phone, "phone_masked": self.mask_phone(phone),
                "purpose": purpose, "expires_in": self.OTP_TTL}

    @staticmethod
    def otp_message(purpose: str, code: str) -> str:
        if purpose == "signup":
            return f"Your Call Desk verification code is {code}. It expires in 5 minutes."
        return f"Your Call Desk sign-in code is {code}. It expires in 5 minutes."

    def resend_code(self, ticket: str) -> Dict[str, Any]:
        rec = self._tickets().get(hashlib.sha256((ticket or "").encode()).hexdigest())
        if not rec:
            raise ValueError("This sign-in has expired. Start again with your password")
        now = time.time()
        if now - float(rec["sent_at"]) < self.OTP_RESEND_AFTER:
            raise ValueError(f"Wait {int(self.OTP_RESEND_AFTER - (now - rec['sent_at']))}s before requesting another code")
        code = f"{secrets.randbelow(1_000_000):06d}"
        salt = secrets.token_hex(8)
        code_exp = now + self.OTP_TTL
        ticket_exp = max(float(rec.get("expires_at", 0)), code_exp)
        rec.update(code_hash=hashlib.sha256((salt + code).encode()).hexdigest(), salt=salt, sent_at=now,
                   code_expires_at=code_exp, expires_at=ticket_exp, attempts=0)
        return {"code": code, "phone": rec["phone"], "phone_masked": self.mask_phone(rec["phone"]), "expires_in": self.OTP_TTL,
                "purpose": rec.get("purpose", "login")}

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
        if rec.get("purpose") == "signup":
            if not self.is_signup_pending(user.workspace_id):
                raise ValueError("This account can no longer be verified. Contact support.")
            self.activate_signup(user.workspace_id, via="sms")
            return self.create_session(user, remember=rec["remember"], user_agent=rec["user_agent"], method="signup+sms")
        return self.create_session(user, remember=rec["remember"], user_agent=rec["user_agent"], method="password+sms")

    # ── Current profile (sidebar) ────────────────────────────────────────────
    def _current_user(self) -> Optional[AuthUser]:
        """The dashboard operates as the super admin if present, or active admin of the default workspace."""
        super_admins = [u for u in self._users.values() if u.active and u.role == UserRole.SUPER_ADMIN.value]
        if super_admins:
            return sorted(super_admins, key=lambda u: u.created_at)[0]
        ws_id = "ws-default" if "ws-default" in self._workspaces else (next(iter(self._workspaces), None))
        admins = [u for u in self._users.values() if u.active and u.workspace_id == ws_id and u.role in (UserRole.SUPER_ADMIN.value, UserRole.ADMIN.value)]
        if admins:
            return sorted(admins, key=lambda u: u.created_at)[0]
        users = [u for u in self._users.values() if u.active and u.workspace_id == ws_id]
        return sorted(users, key=lambda u: u.created_at)[0] if users else None

    def get_profile(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        user = self._users.get(user_id) if user_id else None
        user = user or self._current_user()
        ws = self._workspaces.get(user.workspace_id) if user else None
        user_role = normalize_role(user.role) if user else ""
        return {
            "user_id": user.user_id if user else None,
            "name": (user.name if user else "") or "",
            "email": user.email if user else "",
            "title": (user.title if user else "") or "",
            "phone": (user.phone if user else "") or "",
            "role": user_role,
            "is_super_admin": user_role == UserRole.SUPER_ADMIN.value,
            "is_admin": user_role in (UserRole.SUPER_ADMIN.value, UserRole.ADMIN.value),
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
                and (user is None or user.role in (UserRole.ADMIN.value, UserRole.SUPER_ADMIN.value) or u.user_id == user.user_id)
            ],
            "api_keys": [
                {"key_id": k.key_id, "name": k.name, "prefix": k.key_prefix, "scopes": k.scopes, "is_test": k.is_test,
                 "created_at": k.created_at, "last_used_at": k.last_used_at, "revoked": k.revoked}
                for k in sorted(self._api_keys.values(), key=lambda x: x.created_at, reverse=True)
                if ws and k.workspace_id == ws.workspace_id and (user is None or user.role in (UserRole.ADMIN.value, UserRole.SUPER_ADMIN.value))
            ][:10],
            "api_keys_active": sum(1 for k in self._api_keys.values() if ws and k.workspace_id == ws.workspace_id and not k.revoked),
            "system_branding": self.get_branding(),
        }

    def get_branding(self) -> Dict[str, Any]:
        if os.path.isfile(SYSTEM_BRANDING_PATH):
            try:
                with open(SYSTEM_BRANDING_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return {
                        "dashboard_name": str(data.get("dashboard_name") or "Call Desk").strip(),
                        "tagline": str(data.get("tagline") or "AI Assistant Voice").strip(),
                        "updated_at": data.get("updated_at"),
                        "updated_by": data.get("updated_by") or "",
                    }
            except Exception as e:
                log.warning("Failed to read system branding: %s", e)
        return {
            "dashboard_name": "Call Desk",
            "tagline": "AI Assistant Voice",
            "updated_at": None,
            "updated_by": "",
        }

    def set_branding(self, changes: Dict[str, Any], user_email: str = "") -> Dict[str, Any]:
        name = str(changes.get("dashboard_name") or "").strip()
        if not name:
            raise ValueError("Dashboard name cannot be empty")
        if len(name) > 60:
            raise ValueError("Dashboard name must be 60 characters or fewer")
        tagline = str(changes.get("tagline") if "tagline" in changes else "AI Assistant Voice").strip()
        if len(tagline) > 120:
            raise ValueError("Tagline must be 120 characters or fewer")
        doc = {
            "dashboard_name": name,
            "tagline": tagline,
            "updated_at": time.time(),
            "updated_by": user_email or "superadmin",
        }
        os.makedirs(os.path.dirname(SYSTEM_BRANDING_PATH), exist_ok=True)
        with open(SYSTEM_BRANDING_PATH, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
        return doc

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
                    if val and val.lower() != user.email.lower():
                        if any(u.email.lower() == val.lower() and u.user_id != user.user_id for u in self._users.values()):
                            raise ValueError("An account with this email address already exists")
                        user.email = val
                else:
                    setattr(user, key, val[:120])
        if "workspace" in changes and changes["workspace"] is not None:
            if user.role not in (UserRole.ADMIN.value, UserRole.SUPER_ADMIN.value):
                raise PermissionError("Only workspace administrators can rename the workspace")
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

    # ── Super Admin System Operations ────────────────────────────────────────

    def is_super_admin(self, user_or_role: Any) -> bool:
        """Returns True if the user or role string is Super Admin."""
        if not user_or_role:
            return False
        role = getattr(user_or_role, "role", None) or str(user_or_role)
        return normalize_role(role) == UserRole.SUPER_ADMIN.value

    def list_all_organizations(self) -> List[Dict[str, Any]]:
        """Returns all workspaces enriched with multi-tenant metrics."""
        result = []
        for ws in sorted(self._workspaces.values(), key=lambda w: w.created_at):
            ws_users = [u for u in self._users.values() if u.workspace_id == ws.workspace_id and u.active]
            super_admins = sum(1 for u in ws_users if normalize_role(u.role) == UserRole.SUPER_ADMIN.value)
            admins = sum(1 for u in ws_users if normalize_role(u.role) == UserRole.ADMIN.value)
            member_admins = sum(1 for u in ws_users if normalize_role(u.role) == UserRole.MEMBER_ADMIN.value)
            active_keys = sum(1 for k in self._api_keys.values() if k.workspace_id == ws.workspace_id and not k.revoked)

            result.append({
                "workspace_id": ws.workspace_id,
                "name": ws.name,
                "slug": ws.slug,
                "created_at": ws.created_at,
                "rate_limit_rpm": ws.rate_limit_rpm,
                "active": ws.active,
                "member_count": len(ws_users),
                "super_admin_count": super_admins,
                "admin_count": admins,
                "member_admin_count": member_admins,
                "active_api_keys": active_keys,
                "firm_profile": ws.metadata.get("firm_profile", {}),
                "signup": ws.metadata.get("signup"),
            })
        return result

    def create_organization(
        self,
        name: str,
        slug: Optional[str] = None,
        rate_limit_rpm: int = 120,
        admin_email: Optional[str] = None,
        admin_password: Optional[str] = None,
        admin_name: Optional[str] = None,
        firm_profile: Optional[Dict[str, Any]] = None,
        admin_phone: str = "",
        active: bool = True,
        signup: Optional[Dict[str, Any]] = None,
        onboarding: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Creates a new workspace/organization, optionally seeding an initial Product Admin.
        ``firm_profile`` is the law firm's details (see agent/firm_profile.py). Self-service sign-up
        passes ``active=False`` plus a ``signup`` record so the workspace is stored pending from the
        first save (never briefly active)."""
        name = (name or "").strip()
        if not name:
            raise ValueError("Organization name is required")
        profile = normalize_firm_profile(firm_profile)

        if (admin_email and not admin_password) or (admin_password and not admin_email):
            raise ValueError("Both admin_email and admin_password must be provided together")
        if admin_email and admin_password:
            admin_email = admin_email.strip()
            if "@" not in admin_email or " " in admin_email:
                raise ValueError("Enter a valid email address for admin")
            if len(admin_password or "") < 8:
                raise ValueError("Admin password must be at least 8 characters")
            if self._find_user_by_email(admin_email):
                raise ValueError("A user with that admin email already exists")

        if not slug:
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
            if not slug:
                slug = f"org-{secrets.token_hex(3)}"

        # Check slug uniqueness
        for w in self._workspaces.values():
            if w.slug.lower() == slug.lower():
                slug = f"{slug}-{secrets.token_hex(2)}"
                break

        ws_id = f"ws-{secrets.token_hex(4)}"
        ws = Workspace(
            workspace_id=ws_id,
            name=name,
            slug=slug,
            rate_limit_rpm=max(10, int(rate_limit_rpm or 120)),
            created_at=time.time(),
            active=bool(active),
            metadata={k: v for k, v in (("firm_profile", profile), ("signup", signup), ("onboarding", onboarding)) if v},
        )
        self._workspaces[ws_id] = ws

        created_admin = None
        try:
            if admin_email and admin_password:
                created_admin = self.add_member(
                    workspace_id=ws_id,
                    email=admin_email,
                    password=admin_password,
                    role=UserRole.ADMIN.value,
                    name=admin_name or f"{name} Admin",
                    phone=admin_phone,
                )
            if self._save_store() is False:
                raise RuntimeError("Could not save the new organization. Please try again.")
        except Exception:
            # Undo everything this call added, including an admin created before the failure, so the
            # email is not left claimed by a user whose workspace no longer exists.
            self._workspaces.pop(ws_id, None)
            for uid in [u.user_id for u in self._users.values() if u.workspace_id == ws_id]:
                self._users.pop(uid, None)
            raise

        res = ws.to_dict()
        res["admin"] = created_admin.to_dict() if created_admin else None
        return res

    # ── Self-service sign-up (Task 5.2) ─────────────────────────────────────
    SIGNUP_PENDING = "pending_verification"
    SIGNUP_ATTEMPTS_PER_IP_PER_HOUR = 20
    SIGNUP_FIELDS = ("name", "email", "password", "phone", "firm_name", "firm_profile", "social_token")

    def find_user_by_identity(self, identity: str) -> Optional[AuthUser]:
        for u in self._users.values():
            if u.active and identity in (u.identities or []):
                return u
        return None

    def link_identity(self, user: AuthUser, identity: str) -> None:
        other = self.find_user_by_identity(identity)
        if other and other.user_id != user.user_id:
            raise ValueError("That account is already linked to another user")
        if identity not in user.identities:
            user.identities = [*user.identities, identity]
            if self._save_store() is False:
                user.identities = [i for i in user.identities if i != identity]
                raise RuntimeError("Could not link the account. Please try again.")

    def signup(self, payload: Dict[str, Any], source_ip: str = "", social: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """A law firm creates its own workspace plus its first Product Admin.

        ``social`` is a verified Google/Microsoft identity (agent/oauth.py): the email must match it and
        no password is asked for (a random one is set; "Forgot password" can set a real one later).
        Everything is validated before anything is created. The workspace starts inactive with
        metadata.signup.status = "pending_verification"; the admin cannot sign in until the phone
        number is verified (Task 5.3) or a super admin approves it. Firm name and details are
        optional here: the admin fills them in on /onboarding after the first sign-in
        (``complete_onboarding``), until then the workspace is named "<name>'s firm".
        """
        if not isinstance(payload, dict):
            raise ValueError("Request body must be an object")
        unknown = sorted(set(payload) - set(self.SIGNUP_FIELDS))
        if unknown:
            raise ValueError(f"Unknown sign-up field: {', '.join(unknown)}")
        if social:
            payload = {**payload, "password": secrets.token_urlsafe(24)}

        def text(key: str, label: str, max_len: int) -> str:
            val = payload.get(key)
            if val is not None and not isinstance(val, str):
                raise ValueError(f"{label} must be text")
            val = re.sub(r"\s+", " ", val or "").strip()
            if not val:
                raise ValueError(f"{label} is required")
            if len(val) > max_len:
                raise ValueError(f"{label} must be at most {max_len} characters")
            return val

        name = text("name", "Your name", 120)
        # The firm's details normally come later, during onboarding; API clients may still send them.
        named = bool(payload.get("firm_name"))
        firm_name = text("firm_name", "Law firm name", 100) if named else f"{name}'s firm"[:100]
        email = text("email", "Work email", 254)
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise ValueError("Enter a valid email address")
        password = payload.get("password")
        if not isinstance(password, str) or len(password) < 8:
            raise ValueError("Password must be at least 8 characters")
        if len(password) > 256:
            raise ValueError("Password must be at most 256 characters")
        if password.strip().lower() == email.lower():
            raise ValueError("Password must not be your email address")
        phone = normalize_firm_phone(text("phone", "Mobile phone", 40), "Mobile phone")
        profile = normalize_firm_profile(payload.get("firm_profile"))
        if social:
            if email.lower() != str(social.get("email", "")).lower():
                raise ValueError("Work email must match the account you signed up with")
            if self.find_user_by_identity(social["identity"]):
                raise ValueError("An account with this email already exists. Sign in or reset your password instead.")
        if self._find_user_by_email(email):
            raise ValueError("An account with this email already exists. Sign in or reset your password instead.")

        org = self.create_organization(
            name=firm_name,
            admin_email=email,
            admin_password=password,
            admin_name=name,
            admin_phone=phone,
            firm_profile=profile,
            active=False,
            signup={"status": self.SIGNUP_PENDING, "created_at": time.time(), "source_ip": source_ip[:64],
                    "method": social["provider"] if social else "password"},
            onboarding={"status": self.ONBOARDING_PENDING, "named": named},
        )
        if social:
            try:
                self.link_identity(self._users[org["admin"]["user_id"]], social["identity"])
            except Exception:
                # Don't leave a workspace whose owner can't sign in with the account they used.
                self._workspaces.pop(org["workspace_id"], None)
                self._users.pop(org["admin"]["user_id"], None)
                self._save_store()
                raise
        log.info("Self-service sign-up: %s (%s) by %s, pending verification", firm_name, org["workspace_id"], email)
        return {
            "workspace_id": org["workspace_id"],
            "slug": org["slug"],
            "firm_name": org["name"],
            "user_id": org["admin"]["user_id"],
            "email": email,
            "phone_masked": self.mask_phone(phone),
            "status": self.SIGNUP_PENDING,
        }

    def is_signup_pending(self, workspace_id: str) -> bool:
        ws = self._workspaces.get(workspace_id)
        return bool(ws and not ws.active and ws.metadata.get("signup", {}).get("status") == self.SIGNUP_PENDING)

    def _close_signup(self, ws: Workspace, status: str, via: str) -> None:
        """Move a pending sign-up to its final state (in memory; caller saves)."""
        ws.metadata = {**ws.metadata, "signup": {**ws.metadata.get("signup", {}), "status": status,
                                                 "resolved_at": time.time(), "resolved_via": via}}

    def activate_signup(self, workspace_id: str, via: str) -> Dict[str, Any]:
        """Pending sign-up -> active workspace (phone verified, or approved by a super admin)."""
        if not self.is_signup_pending(workspace_id):
            raise ValueError("This organization is not waiting for verification")
        ws = self._workspaces[workspace_id]
        before = (ws.active, ws.metadata)
        ws.active = True
        self._close_signup(ws, "active", via)
        if self._save_store() is False:
            ws.active, ws.metadata = before
            raise RuntimeError("Could not activate the account. Please try again.")
        log.info("Sign-up %s (%s) activated via %s", ws.name, workspace_id, via)
        return ws.to_dict()

    def workspace_owner(self, workspace_id: str) -> Optional[AuthUser]:
        """The workspace's first Product Admin (the person who signed up)."""
        admins = [u for u in self._users.values() if u.workspace_id == workspace_id and u.active
                  and normalize_role(u.role) == UserRole.ADMIN.value]
        return min(admins, key=lambda u: u.created_at) if admins else None

    ONBOARDING_PENDING = "pending"
    ONBOARDING_COMPLETE = "complete"
    ONBOARDING_FIELDS = ("status", "named", "completed_at", "agent_id", "seeded_at", "logo_url", "clio_selected", "dismissed")

    def onboarding_pending(self, workspace_id: str) -> bool:
        ws = self._workspaces.get(workspace_id)
        return bool(ws and (ws.metadata.get("onboarding") or {}).get("status") == self.ONBOARDING_PENDING)

    def complete_onboarding(self, workspace_id: str, firm_name: Any, firm_profile: Any) -> Dict[str, Any]:
        """Sign-up only creates the account; the firm details arrive here, after the first sign-in.
        Names the workspace, stores the profile (sign-up's required fields enforced now) and marks
        onboarding complete. Firm details can't be changed through this afterwards."""
        ws = self._workspaces.get(workspace_id)
        if not ws:
            raise KeyError(f"Organization '{workspace_id}' not found")
        if not self.onboarding_pending(workspace_id):
            raise ValueError("Onboarding is already complete for this workspace")
        if firm_name is not None and not isinstance(firm_name, str):
            raise ValueError("Law firm name must be text")
        name = re.sub(r"\s+", " ", firm_name or "").strip()
        if not name:
            raise ValueError("Law firm name is required")
        if len(name) > 100:
            raise ValueError("Law firm name must be at most 100 characters")
        profile = normalize_firm_profile(firm_profile, ws.metadata.get("firm_profile"), required=SIGNUP_REQUIRED_FIELDS)

        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or ws.slug
        if any(w.slug.lower() == slug and w.workspace_id != workspace_id for w in self._workspaces.values()):
            slug = f"{slug}-{secrets.token_hex(2)}"
        before = (ws.name, ws.slug, ws.metadata)
        ws.name, ws.slug = name, slug
        ws.metadata = {**ws.metadata, "firm_profile": profile,
                       "onboarding": {**ws.metadata.get("onboarding", {}), "status": self.ONBOARDING_COMPLETE,
                                      "named": True, "completed_at": time.time()}}
        if self._save_store() is False:
            ws.name, ws.slug, ws.metadata = before
            raise RuntimeError("Could not save your firm details. Please try again.")
        log.info("Onboarding complete for %s (%s)", name, workspace_id)
        return ws.to_dict()

    def set_onboarding(self, workspace_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        """Merge into the workspace's onboarding record (Task 5.5 first-login checklist)."""
        ws = self._workspaces.get(workspace_id)
        if not ws:
            raise KeyError(f"Organization '{workspace_id}' not found")
        unknown = set(changes) - set(self.ONBOARDING_FIELDS)
        if unknown:
            raise ValueError(f"Unknown onboarding field: {', '.join(sorted(unknown))}")
        before = ws.metadata
        record = {**ws.metadata.get("onboarding", {}), **changes}
        ws.metadata = {**ws.metadata, "onboarding": record}
        if self._save_store() is False:
            ws.metadata = before
            raise RuntimeError("Could not save onboarding state")
        return record

    def update_organization(self, workspace_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        """Updates organization settings (name, rate_limit_rpm, active toggle, firm_profile).
        ``firm_profile`` is a partial update merged over the stored profile; None/"" clears a field."""
        ws = self._workspaces.get(workspace_id)
        if not ws:
            raise KeyError(f"Organization '{workspace_id}' not found")

        # Validate first so a bad profile leaves the other settings untouched too.
        profile = None
        if changes.get("firm_profile") is not None:
            profile = normalize_firm_profile(changes["firm_profile"], ws.metadata.get("firm_profile"))

        if "name" in changes and changes["name"] is not None:
            name = str(changes["name"]).strip()
            if name:
                ws.name = name[:100]

        if "slug" in changes and changes["slug"] is not None:
            slug = re.sub(r"[^a-z0-9-]+", "", str(changes["slug"]).lower().strip())
            if slug:
                ws.slug = slug[:50]

        if "rate_limit_rpm" in changes and changes["rate_limit_rpm"] is not None:
            ws.rate_limit_rpm = max(10, int(changes["rate_limit_rpm"]))

        if "active" in changes and changes["active"] is not None:
            # A super admin deciding on a pending sign-up closes it either way, so a rejected
            # sign-up cannot verify itself back to active later.
            if self.is_signup_pending(workspace_id):
                self._close_signup(ws, "active" if changes["active"] else "rejected", "super_admin")
            ws.active = bool(changes["active"])

        if profile is not None:
            ws.metadata = {**ws.metadata, "firm_profile": profile}
            if not profile:
                ws.metadata.pop("firm_profile")

        self._save_store()
        return ws.to_dict()

    def list_all_users_global(self, role_filter: Optional[str] = None, workspace_filter: Optional[str] = None) -> List[Dict[str, Any]]:
        """Returns multi-tenant users across all workspaces with enriched organization info."""
        result = []
        for u in sorted(self._users.values(), key=lambda x: x.created_at, reverse=True):
            ws = self._workspaces.get(u.workspace_id)
            user_role = normalize_role(u.role)
            if role_filter and user_role != normalize_role(role_filter):
                continue
            if workspace_filter and u.workspace_id != workspace_filter:
                continue

            result.append({
                "user_id": u.user_id,
                "name": u.name or (u.email.split("@")[0] if "@" in u.email else "User"),
                "email": u.email,
                "role": user_role,
                "workspace_id": u.workspace_id,
                "workspace_name": ws.name if ws else "Unknown Workspace",
                "workspace_slug": ws.slug if ws else "",
                "created_at": u.created_at,
                "active": u.active,
                "phone": u.phone or "",
                "title": u.title or "",
                "last_login_at": u.last_login_at,
                "has_password": bool(u.password_hash),
            })
        return result

    def update_user_global(self, user_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        """Allows Super Admin to update user role, reassign workspace, toggle active, or edit fields."""
        user = self._users.get(user_id)
        if not user:
            raise KeyError(f"User '{user_id}' not found")

        # Work out the account's role / workspace / active state after this change, check the single
        # Super Admin and one-Product-Admin-per-organization rules against it, then apply.
        new_role = normalize_role(str(changes["role"])) if changes.get("role") is not None else user.role
        new_ws_id = str(changes["workspace_id"]).strip() if changes.get("workspace_id") is not None else user.workspace_id
        new_active = bool(changes["active"]) if changes.get("active") is not None else user.active
        if new_ws_id not in self._workspaces:
            raise KeyError(f"Workspace '{new_ws_id}' not found")
        if user.role == UserRole.SUPER_ADMIN.value and user.active and (new_role != UserRole.SUPER_ADMIN.value or not new_active):
            raise ValueError("The platform needs its Super Admin. Make someone else Super Admin (transfer) first.")
        if user.role == UserRole.ADMIN.value and user.active and (new_role != UserRole.ADMIN.value or not new_active or new_ws_id != user.workspace_id):
            others = [u for u in self._users.values() if u.active and u.role == UserRole.ADMIN.value
                      and u.workspace_id == user.workspace_id and u.user_id != user.user_id]
            if not others:
                raise ValueError("This organization needs its Product Admin. Make someone else Product Admin (transfer) first.")
        displaced = None
        entering = new_role != user.role or new_ws_id != user.workspace_id or (new_active and not user.active)
        if entering and new_active and new_role in (UserRole.SUPER_ADMIN.value, UserRole.ADMIN.value):
            holder = self.admin_slot_holder(new_role, new_ws_id, exclude_user_id=user.user_id)
            if holder and changes.get("replace_current"):
                displaced = holder      # transfer: the current holder becomes a Member Admin
            else:
                self.require_admin_slot(new_role, new_ws_id, exclude_user_id=user.user_id)
        user.role, user.workspace_id, user.active = new_role, new_ws_id, new_active
        if displaced:
            displaced.role = UserRole.MEMBER_ADMIN.value
            log.info("%s handed over to %s; %s is now a Member Admin", new_role, user.email, displaced.email)

        for key in ("name", "email", "phone", "title"):
            if key in changes and changes[key] is not None:
                val = str(changes[key]).strip()
                if key == "email":
                    if val and ("@" not in val or " " in val):
                        raise ValueError("Enter a valid email address")
                    if val and val.lower() != user.email.lower():
                        if any(u.email.lower() == val.lower() and u.user_id != user.user_id for u in self._users.values()):
                            raise ValueError("An account with this email address already exists")
                        user.email = val
                else:
                    setattr(user, key, val[:120])

        if changes.get("password"):
            self.set_password(user.user_id, str(changes["password"]))

        self._save_store()
        ws = self._workspaces.get(user.workspace_id)
        return {
            "user_id": user.user_id,
            "name": user.name,
            "email": user.email,
            "role": user.role,
            "workspace_id": user.workspace_id,
            "workspace_name": ws.name if ws else "",
            "active": user.active,
        }


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
        role: str = "member_admin",
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
            ws_id = payload.get("ws", "ws-default")
            ws = self.get_workspace(ws_id)
            if not ws or not ws.active:
                return None
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
                return False, None, "Invalid, expired, or suspended Bearer token"

            ws_id = payload.get("ws", "ws-default")
            scopes = payload.get("scopes", [])
            role = normalize_role(payload.get("role", "member_admin"))

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

        # Case C: Workspace header fallback for open endpoints
        if ws_hdr:
            ws = self.get_workspace(ws_hdr)
            if not ws or not ws.active:
                return False, None, f"Workspace '{ws_hdr}' not found or inactive"

            rate_res = self.rate_limiter.check(f"ws:{ws.workspace_id}", limit=ws.rate_limit_rpm)
            if not rate_res.allowed:
                return False, None, f"Workspace rate limit exceeded. Retry in {rate_res.retry_after}s"

            if required_scope:
                return False, None, f"Authentication required for scope '{required_scope}'. Please provide an API Key or Bearer Token."

            ctx = {
                "workspace_id": ws.workspace_id,
                "auth_type": "workspace_header",
                "identity": f"anon-{ws.workspace_id}",
                "role": UserRole.MEMBER_ADMIN.value,
                "scopes": [],
            }
            return True, ctx, None

        # If required_scope was requested and no auth was provided at all
        if required_scope:
            return False, None, f"Missing authentication (Bearer token or X-API-Key required for '{required_scope}')"

        # Default fallback for internal unauthenticated requests
        ctx = {
            "workspace_id": "ws-default",
            "auth_type": "internal",
            "identity": "internal-user",
            "role": UserRole.MEMBER_ADMIN.value,
            "scopes": [],
        }
        return True, ctx, None


# Global singleton
auth_manager = AuthManager()

