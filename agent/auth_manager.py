"""Task 4.3 — REST API & Multi-Tenant Access Control.

Provides:
1. Multi-Tenant Workspace Model & Data Isolation (workspaces, users, memberships).
2. API Key Management (rotatable live/test API keys, hashed storage, granular scopes).
3. Role-Based Access Control (RBAC): Admin, Operator, Analyst roles with permission matrices.
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
    ADMIN    = "admin"     # Full access to workspace, users, keys, billing, agents, calls
    OPERATOR = "operator"  # Dial, transfer, agent edit/test, live call monitoring
    ANALYST  = "analyst"   # Read-only access to calls, analytics, exports, transcript inspections


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
    UserRole.OPERATOR: {
        ApiScope.CALLS_READ.value,
        ApiScope.CALLS_WRITE.value,
        ApiScope.CALLS_DISPATCH.value,
        ApiScope.AGENTS_READ.value,
        ApiScope.AGENTS_WRITE.value,
        ApiScope.ANALYTICS_READ.value,
        ApiScope.TELEPHONY_DIAL.value,
    },
    UserRole.ANALYST: {
        ApiScope.CALLS_READ.value,
        ApiScope.AGENTS_READ.value,
        ApiScope.ANALYTICS_READ.value,
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

    def _load_store(self):
        if os.path.isfile(self.store_path):
            try:
                with open(self.store_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for w in data.get("workspaces", []):
                    self._workspaces[w["workspace_id"]] = Workspace(**w)
                for u in data.get("users", []):
                    self._users[u["user_id"]] = AuthUser(**u)
                for k in data.get("api_keys", []):
                    key = ApiKey(**k)
                    self._api_keys[key.key_id] = key
                    self._key_hash_index[key.key_hash] = key.key_id
            except Exception as e:
                log.warning("Failed to load auth store from %s: %s", self.store_path, e)

    def _save_store(self):
        try:
            os.makedirs(os.path.dirname(self.store_path), exist_ok=True)
            with open(self.store_path, "w", encoding="utf-8") as f:
                json.dump({
                    "workspaces": [w.to_dict() for w in self._workspaces.values()],
                    "users": [u.to_dict() for u in self._users.values()],
                    "api_keys": [k.to_dict(include_hash=True) for k in self._api_keys.values()],
                    "updated_at": time.time(),
                }, f, indent=2)
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
        if role not in [r.value for r in UserRole]:
            raise ValueError(f"Invalid role '{role}'. Allowed: {[r.value for r in UserRole]}")

        user_id = f"usr-{secrets.token_hex(4)}"
        user = AuthUser(user_id=user_id, workspace_id=workspace_id, email=email, role=role)
        self._users[user_id] = user
        self._save_store()
        return user

    def get_user(self, user_id: str) -> Optional[AuthUser]:
        return self._users.get(user_id)

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
            role = UserRole(role_str)
        except ValueError:
            return False
        allowed = ROLE_SCOPES.get(role, set())
        return ApiScope.ALL.value in allowed or required_scope in allowed

    # ── JWT / Session Token Issuance ────────────────────────────────────────

    def issue_token(
        self,
        workspace_id: str,
        subject: str,
        role: str = "operator",
        scopes: Optional[List[str]] = None,
        ttl_seconds: int = 3600,
    ) -> str:
        """Issues a signed JWT-like Bearer token: base64(header).base64(payload).signature."""
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
            role = payload.get("role", "operator")

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
                "role": UserRole.OPERATOR.value,
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

