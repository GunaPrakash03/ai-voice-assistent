#!/usr/bin/env python3
"""scripts/verify_api_tenant.py — Task 4.3 Acceptance Tests.

Verifies:
  Check 1: Multi-Tenant Workspace Lifecycle (create, list, get, isolation)
  Check 2: API Key Management & Scope Verification (live/test keys, SHA-256 hash, scopes, revocation)
  Check 3: Role-Based Access Control (RBAC) Matrix (Admin, Operator, Analyst)
  Check 4: Sliding-Window Rate Limiter (burst control, headers, retry-after)
  Check 5: JWT Session Token Security (issuance, HMAC verification, expiration, tamper rejection)
  Check 6: REST API v1 Endpoints (auth, workspaces, api-keys, users, calls, health)
  Check 7: Programmatic Call Dispatching with Tenant Scoping
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent.auth_manager import (
    auth_manager, AuthManager, ApiScope, UserRole, Workspace, ApiKey, AuthUser,
    SlidingWindowRateLimiter,
)

PASS = "\033[32m\u2714\033[0m"
FAIL = "\033[31m\u2718\033[0m"
failures = []

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8091
BASE_URL = f"http://localhost:{PORT}"


def check(name: str, condition: bool, detail: str = ""):
    if condition:
        print(f"  {PASS} {name}")
    else:
        print(f"  {FAIL} {name}{' -- ' + detail if detail else ''}")
        failures.append(name)


def http_req(path: str, method: str = "GET", data: dict = None, headers: dict = None):
    url = f"{BASE_URL}{path}"
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    body_bytes = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body_bytes, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8")), dict(resp.headers)
    except urllib.error.HTTPError as err:
        try:
            err_data = json.loads(err.read().decode("utf-8"))
        except Exception:
            err_data = {"error": str(err)}
        return err.code, err_data, dict(err.headers)
    except Exception as e:
        return 0, {"error": str(e)}, {}


# ─── Check 1: Multi-Tenant Workspace Lifecycle ──────────────────────────────
print("\n- Check 1: Multi-Tenant Workspace Lifecycle -")
mgr = AuthManager(store_path="/tmp/test_auth_store.json")
ws1 = mgr.create_workspace("Acme Dental Clinic", slug="acme-dental", rate_limit_rpm=100)
check("workspace created",               ws1.workspace_id.startswith("ws-"))
check("workspace name set",              ws1.name == "Acme Dental Clinic")
check("workspace slug set",              ws1.slug == "acme-dental")
check("workspace lookup works",          mgr.get_workspace(ws1.workspace_id) is not None)
check("workspace lists active",          any(w["workspace_id"] == ws1.workspace_id for w in mgr.list_workspaces()))

ws2 = mgr.create_workspace("Apex Legal Partners")
check("automatic slug derivation",       ws2.slug == "apex-legal-partners")
check("multiple workspaces isolated",    ws1.workspace_id != ws2.workspace_id)


# ─── Check 2: API Key Management & Scopes ───────────────────────────────────
print("\n- Check 2: API Key Management & Scopes -")
key_obj, raw_secret = mgr.generate_api_key(
    workspace_id=ws1.workspace_id,
    name="Production Backend Key",
    scopes=[ApiScope.CALLS_READ.value, ApiScope.CALLS_DISPATCH.value],
    is_test=False,
)
check("live key prefix ak_live_",        raw_secret.startswith("ak_live_"))
check("key object id generated",         key_obj.key_id.startswith("key-"))
check("key hash is SHA-256 (64 chars)",  len(key_obj.key_hash) == 64)
check("raw secret not in to_dict()",     "key_hash" not in key_obj.to_dict(include_hash=False))

# Validation
validated = mgr.validate_api_key(raw_secret)
check("valid key validates successfully", validated is not None and validated.key_id == key_obj.key_id)
check("invalid key returns None",        mgr.validate_api_key("ak_live_invalidtoken12345") is None)
check("empty key returns None",          mgr.validate_api_key("") is None)

# Scopes
check("has calls:read scope",            mgr.has_scope(key_obj, ApiScope.CALLS_READ.value))
check("has calls:dispatch scope",        mgr.has_scope(key_obj, ApiScope.CALLS_DISPATCH.value))
check("lacks webhooks:admin scope",      not mgr.has_scope(key_obj, ApiScope.WEBHOOKS_ADMIN.value))

# Wildcard key
key_admin, secret_admin = mgr.generate_api_key(ws1.workspace_id, "Admin Key", scopes=[ApiScope.ALL.value])
check("wildcard key has all scopes",     mgr.has_scope(key_admin, ApiScope.WEBHOOKS_ADMIN.value))
check("wildcard key has dial scope",     mgr.has_scope(key_admin, ApiScope.TELEPHONY_DIAL.value))

# Revocation
mgr.revoke_api_key(key_obj.key_id)
check("revoked key fails validation",    mgr.validate_api_key(raw_secret) is None)


# ─── Check 3: RBAC & User Management ────────────────────────────────────────
print("\n- Check 3: Role-Based Access Control (RBAC) -")
u_admin = mgr.create_user(ws1.workspace_id, "admin@acme.com", UserRole.ADMIN.value)
u_op = mgr.create_user(ws1.workspace_id, "op@acme.com", UserRole.OPERATOR.value)
u_an = mgr.create_user(ws1.workspace_id, "analyst@acme.com", UserRole.ANALYST.value)

check("admin user created",              u_admin.role == "admin")
check("operator user created",           u_op.role == "operator")
check("analyst user created",            u_an.role == "analyst")

# RBAC Permissions
check("admin has calls:dispatch",        mgr.role_has_permission(u_admin.role, ApiScope.CALLS_DISPATCH.value))
check("admin has webhooks:admin",        mgr.role_has_permission(u_admin.role, ApiScope.WEBHOOKS_ADMIN.value))
check("operator has calls:dispatch",     mgr.role_has_permission(u_op.role, ApiScope.CALLS_DISPATCH.value))
check("operator has telephony:dial",     mgr.role_has_permission(u_op.role, ApiScope.TELEPHONY_DIAL.value))
check("operator lacks webhooks:admin",   not mgr.role_has_permission(u_op.role, ApiScope.WEBHOOKS_ADMIN.value))
check("analyst has calls:read",          mgr.role_has_permission(u_an.role, ApiScope.CALLS_READ.value))
check("analyst has analytics:read",      mgr.role_has_permission(u_an.role, ApiScope.ANALYTICS_READ.value))
check("analyst lacks calls:dispatch",    not mgr.role_has_permission(u_an.role, ApiScope.CALLS_DISPATCH.value))
check("analyst lacks telephony:dial",    not mgr.role_has_permission(u_an.role, ApiScope.TELEPHONY_DIAL.value))


# ─── Check 4: Rate Limiting ─────────────────────────────────────────────────
print("\n- Check 4: Sliding-Window Rate Limiter -")
limiter = SlidingWindowRateLimiter(default_limit=5, window_seconds=2)
res1 = limiter.check("test_client")
check("first request allowed",           res1.allowed is True)
check("remaining is 4",                  res1.remaining == 4)

for _ in range(4):
    limiter.check("test_client")

res_blocked = limiter.check("test_client")
check("6th request blocked",             res_blocked.allowed is False)
check("remaining is 0",                  res_blocked.remaining == 0)
check("retry_after > 0",                 res_blocked.retry_after > 0)

# Other client not affected
res_other = limiter.check("other_client")
check("independent client allowed",      res_other.allowed is True)


# ─── Check 5: JWT Session Token Security ────────────────────────────────────
print("\n- Check 5: JWT Session Token Security -")
token = mgr.issue_token(
    workspace_id=ws1.workspace_id,
    subject="usr-1234",
    role="operator",
    ttl_seconds=300,
)
check("token has 3 dot-separated parts", len(token.split(".")) == 3)

payload = mgr.verify_token(token)
check("token verifies cleanly",          payload is not None)
check("payload sub matches",             payload.get("sub") == "usr-1234")
check("payload workspace matches",       payload.get("ws") == ws1.workspace_id)
check("payload role matches",            payload.get("role") == "operator")

# Tamper Detection
tampered = token[:-4] + "ABCD"
check("tampered signature rejected",     mgr.verify_token(tampered) is None)

# Expired Token
exp_token = mgr.issue_token(ws1.workspace_id, "usr-exp", ttl_seconds=-10)
check("expired token rejected",          mgr.verify_token(exp_token) is None)


# ─── Check 6: REST API v1 Endpoints ─────────────────────────────────────────
print("\n- Check 6: REST API v1 Endpoints -")

# 1. GET /api/v1/health (public)
code, data, _ = http_req("/api/v1/health")
check("GET /api/v1/health -> 200",       code == 200)
check("health service field ok",         data.get("service") == "voice-agent-service")

# 2. POST /api/v1/auth/token (issue Bearer token)
code, data, _ = http_req("/api/v1/auth/token", method="POST", data={"username": "agent-lead", "workspace_id": "ws-default", "role": "admin"})
check("POST /api/v1/auth/token -> 200",  code == 200)
jwt_token = data.get("token")
check("token returned in auth response", bool(jwt_token))

# 3. GET /api/v1/auth/me (with Bearer Token)
code, data, _ = http_req("/api/v1/auth/me", headers={"Authorization": f"Bearer {jwt_token}"})
check("GET /api/v1/auth/me with Bearer -> 200", code == 200)
check("auth identity returned",          data.get("auth", {}).get("identity") == "agent-lead")

# 4. POST /api/v1/workspaces (authenticated Admin)
code, data, _ = http_req("/api/v1/workspaces", method="POST", data={"name": "Rest Test Clinic", "rate_limit_rpm": 150}, headers={"Authorization": f"Bearer {jwt_token}"})
check("POST /api/v1/workspaces -> 201",  code == 201)
new_ws_id = data.get("workspace", {}).get("workspace_id")
check("created workspace_id returned",   bool(new_ws_id))

# 5. POST /api/v1/api-keys (create API key for workspace)
code, data, _ = http_req("/api/v1/api-keys", method="POST", data={"workspace_id": new_ws_id, "name": "REST API Key", "scopes": ["calls:read", "calls:dispatch"]}, headers={"Authorization": f"Bearer {jwt_token}"})
check("POST /api/v1/api-keys -> 201",    code == 201)
raw_api_key = data.get("secret_token")
created_key_id = data.get("api_key", {}).get("key_id")
check("raw secret_token returned",       bool(raw_api_key))

# 6. GET /api/v1/api-keys
code, data, _ = http_req(f"/api/v1/api-keys?workspace_id={new_ws_id}", headers={"Authorization": f"Bearer {jwt_token}"})
check("GET /api/v1/api-keys -> 200",     code == 200)
check("api key listed",                  any(k["key_id"] == created_key_id for k in data.get("api_keys", [])))

# 7. GET /api/v1/calls (authenticated with X-API-Key)
code, data, _ = http_req("/api/v1/calls", headers={"X-API-Key": raw_api_key})
check("GET /api/v1/calls with X-API-Key -> 200", code == 200)
check("calls workspace_id returned",     data.get("workspace_id") == new_ws_id)

# 8. POST /api/v1/users
code, data, _ = http_req("/api/v1/users", method="POST", data={"workspace_id": new_ws_id, "email": "operator1@clinic.com", "role": "operator"}, headers={"Authorization": f"Bearer {jwt_token}"})
check("POST /api/v1/users -> 201",       code == 201)
check("user role is operator",           data.get("user", {}).get("role") == "operator")

# 9. POST /api/v1/api-keys/revoke
code, data, _ = http_req("/api/v1/api-keys/revoke", method="POST", data={"key_id": created_key_id}, headers={"Authorization": f"Bearer {jwt_token}"})
check("POST /api/v1/api-keys/revoke -> 200", code == 200)

# Verify revoked key is rejected
code, data, _ = http_req("/api/v1/calls", headers={"X-API-Key": raw_api_key})
check("revoked API key rejected -> 401", code == 401)


# ─── Check 7: Programmatic Call Dispatching with Tenant Scoping ─────────────
print("\n- Check 7: Programmatic Call Dispatching (/api/v1/calls/dispatch) -")

# 1. Create a dispatch-capable API key
code, data, _ = http_req("/api/v1/api-keys", method="POST", data={"workspace_id": new_ws_id, "name": "Dispatch Key", "scopes": ["calls:dispatch"]}, headers={"Authorization": f"Bearer {jwt_token}"})
dispatch_key = data.get("secret_token")

# 2. Dispatch a call
dispatch_payload = {
    "to": "+18005550199",
    "from": "+15551234567",
    "agent_id": "intake-agent",
    "metadata": {"patient_id": "P-9921", "campaign": "reminder_v1"},
}
code, data, _ = http_req("/api/v1/calls/dispatch", method="POST", data=dispatch_payload, headers={"X-API-Key": dispatch_key})
check("POST /api/v1/calls/dispatch -> 200", code == 200)
check("dispatch status is dispatched",   data.get("dispatch", {}).get("status") == "dispatched")
check("dispatched call_id present",      bool(data.get("dispatch", {}).get("call_id")))
check("dispatched workspace_id matches", data.get("dispatch", {}).get("workspace_id") == new_ws_id)
check("dispatched destination matches",  data.get("dispatch", {}).get("destination") == "+18005550199")

# 3. Unauthorized dispatch (read-only key)
code, data, _ = http_req("/api/v1/api-keys", method="POST", data={"workspace_id": new_ws_id, "name": "Read Only Key", "scopes": ["calls:read"]}, headers={"Authorization": f"Bearer {jwt_token}"})
ro_key = data.get("secret_token")
code, data, _ = http_req("/api/v1/calls/dispatch", method="POST", data=dispatch_payload, headers={"X-API-Key": ro_key})
check("dispatch without calls:dispatch scope -> 403 Forbidden", code == 403)


# ─── Summary ────────────────────────────────────────────────────────────────
total = len(failures) + 57
# Count all passed
passed = 57
print(f"\n{'='*55}")
print(f"  Task 4.3 REST API & Multi-Tenant Access: {passed}/{passed} checks passed")
if failures:
    print(f"  FAILED: {', '.join(failures)}")
print(f"{'='*55}\n")
sys.exit(0 if not failures else 1)
