#!/usr/bin/env python3
"""scripts/verify_super_admin.py — Comprehensive End-to-End Verification Suite for Super Admin Portal & Multi-Tier RBAC.

Verifies:
1. Role Hierarchy & Aliases in agent/auth_manager.py
2. Creation Delegation Matrix (can_create_role) across organizations
3. Server Middleware Access Control (/organizations, /users, /api/v1/system/*)
4. Role-based Creation API Enforcement (/api/v1/auth/users)
5. UI Template Integrity and Real Browser Screenshot Assets
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from agent.auth_manager import auth_manager, UserRole, normalize_role, can_create_role

BASE_URL = "http://localhost:8091"

class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):
        return fp

opener = urllib.request.build_opener(NoRedirectHandler)

def make_req(path, method="GET", body=None, token=None):
    headers = {}
    if token:
        headers["Cookie"] = f"va_session={token}"
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"{BASE_URL}{path}", data=data, headers=headers, method=method)
    try:
        resp = opener.open(req, timeout=8)
        raw = resp.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = raw
        return resp.status, resp.headers, parsed
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = raw
        return e.code, e.headers, parsed


def main():
    print("=====================================================================")
    print("  SUPER ADMIN PORTAL & MULTI-TIER RBAC: COMPREHENSIVE TEST SUITE     ")
    print("=====================================================================\n")

    passed_count = 0
    total_count = 0

    def check(name, condition, details=""):
        nonlocal passed_count, total_count
        total_count += 1
        if condition:
            passed_count += 1
            print(f"  [PASS] {name} {details}")
        else:
            print(f"  [FAIL] {name} {details}")
            raise AssertionError(f"Assertion failed for check: {name} ({details})")

    # -----------------------------------------------------------------------
    # SECTION 1: Role Definitions & Aliases in auth_manager.py
    # -----------------------------------------------------------------------
    print("--- [SECTION 1] Role Architecture & Normalization ---")
    roles = [r.value for r in UserRole]
    check("Role Enum Contains super_admin", UserRole.SUPER_ADMIN.value in roles)
    check("Role Enum Contains admin", UserRole.ADMIN.value in roles)
    check("Role Enum Contains member_admin", UserRole.MEMBER_ADMIN.value in roles)
    check("Role Enum has exactly three roles", set(roles) == {"super_admin", "admin", "member_admin"})
    
    check("Alias 'overall_admin' maps to super_admin", normalize_role("overall_admin") == "super_admin")
    check("Alias 'superadmin' maps to super_admin", normalize_role("superadmin") == "super_admin")
    check("Alias 'product_admin' maps to admin", normalize_role("product_admin") == "admin")
    check("Alias 'member_admin' maps to member_admin", normalize_role("member_admin") == "member_admin")
    check("Alias 'operator' folds into member_admin", normalize_role("operator") == "member_admin")
    check("Retired 'user' role folds into member_admin", normalize_role("user") == "member_admin")
    check("is_super_admin(super_admin) == True", auth_manager.is_super_admin("super_admin") is True)
    check("is_super_admin(admin) == False", auth_manager.is_super_admin("admin") is False)

    # -----------------------------------------------------------------------
    # SECTION 2: Creation Delegation Matrix (can_create_role)
    # -----------------------------------------------------------------------
    print("\n--- [SECTION 2] Creation Delegation Matrix (can_create_role) ---")
    ws1 = "ws-alpha"
    ws2 = "ws-beta"

    # Super Admin can create any role in any org
    for r in ("super_admin", "admin", "member_admin"):
        ok, _ = can_create_role("super_admin", ws1, r, ws2)
        check(f"Super Admin can create '{r}' in foreign org", ok is True)

    # Product Admin rights
    ok, _ = can_create_role("admin", ws1, "member_admin", ws1)
    check("Product Admin CAN create member_admin in own org", ok is True)
    ok, _ = can_create_role("admin", ws1, "user", ws1)
    check("Product Admin CAN create via legacy 'user' name (→ member_admin)", ok is True)
    ok, _ = can_create_role("admin", ws1, "admin", ws1)
    check("Product Admin CANNOT create Product Admin", ok is False)
    ok, _ = can_create_role("admin", ws1, "super_admin", ws1)
    check("Product Admin CANNOT create Super Admin", ok is False)
    ok, _ = can_create_role("admin", ws1, "member_admin", ws2)
    check("Product Admin CANNOT create member in foreign org", ok is False)

    # Member Admin rights (lowest role; may only invite peers in its own org)
    ok, _ = can_create_role("member_admin", ws1, "member_admin", ws1)
    check("Member Admin CAN invite Member Admin in own org", ok is True)
    ok, _ = can_create_role("member_admin", ws1, "admin", ws1)
    check("Member Admin CANNOT create Product Admin", ok is False)
    ok, _ = can_create_role("member_admin", ws1, "super_admin", ws1)
    check("Member Admin CANNOT create Super Admin", ok is False)
    ok, _ = can_create_role("member_admin", ws1, "member_admin", ws2)
    check("Member Admin CANNOT invite into foreign org", ok is False)

    # -----------------------------------------------------------------------
    # SECTION 3: Server Middleware Gating & Live Endpoints
    # -----------------------------------------------------------------------
    print("\n--- [SECTION 3] Server Middleware & Route Security ---")
    # Mint test tokens via /api/v1/auth/dev-session
    _, _, d_su = make_req("/api/v1/auth/dev-session", method="POST", body={"role": "super_admin"})
    super_token = d_su["token"]

    _, _, d_pa = make_req("/api/v1/auth/dev-session", method="POST", body={"email": "prodadmin@test.local"})
    prod_token = d_pa["token"]

    _, _, d_ma = make_req("/api/v1/auth/dev-session", method="POST", body={"email": "memberadmin@test.local"})
    member_token = d_ma["token"]

    _, _, d_u = make_req("/api/v1/auth/dev-session", method="POST", body={"email": "regular@test.local"})
    user_token = d_u["token"]

    # 3a. Unauthenticated access to /organizations and /users -> 302 redirect to /login
    st, hdrs, _ = make_req("/organizations")
    check("Unauthenticated /organizations redirects to login", st == 302 and "/login" in hdrs.get("Location", ""))
    st, hdrs, _ = make_req("/users")
    check("Unauthenticated /users redirects to login", st == 302 and "/login" in hdrs.get("Location", ""))

    # 3b. Super Admin Access -> 200 OK
    st, _, body = make_req("/organizations", token=super_token)
    check("Super Admin access to /organizations -> 200 OK", st == 200 and "<title>Overall Organizations" in body)
    st, _, body = make_req("/users", token=super_token)
    check("Super Admin access to /users -> 200 OK", st == 200 and "<title>Global System Users" in body)

    # 3c. Super Admin System REST APIs -> 200 OK
    st, _, data = make_req("/api/v1/system/organizations", token=super_token)
    check("Super Admin GET /api/v1/system/organizations -> 200 OK", st == 200 and data.get("status") == "ok")
    st, _, data = make_req("/api/v1/system/users", token=super_token)
    check("Super Admin GET /api/v1/system/users -> 200 OK", st == 200 and data.get("status") == "ok")
    st, _, data = make_req("/api/v1/system/stats", token=super_token)
    check("Super Admin GET /api/v1/system/stats -> 200 OK", st == 200 and data.get("total_organizations", 0) > 0)

    # 3d. Product Admin Blocked -> Redirect / 403
    st, hdrs, _ = make_req("/organizations", token=prod_token)
    check("Product Admin blocked from /organizations (302 redirect to ?denied=super_admin)", st == 302 and "denied=super_admin" in hdrs.get("Location", ""))
    st, hdrs, _ = make_req("/users", token=prod_token)
    check("Product Admin blocked from /users (302 redirect to ?denied=super_admin)", st == 302 and "denied=super_admin" in hdrs.get("Location", ""))
    st, _, _ = make_req("/api/v1/system/organizations", token=prod_token)
    check("Product Admin blocked from GET /api/v1/system/organizations (403 Forbidden)", st == 403)
    st, _, _ = make_req("/api/v1/system/users", token=prod_token)
    check("Product Admin blocked from GET /api/v1/system/users (403 Forbidden)", st == 403)
    st, _, _ = make_req("/api/v1/system/organizations", method="POST", body={"action": "create", "name": "Hack"}, token=prod_token)
    check("Product Admin blocked from POST /api/v1/system/organizations (403 Forbidden)", st == 403)

    # 3e. Member Admin Blocked
    st, hdrs, _ = make_req("/organizations", token=member_token)
    check("Member Admin blocked from /organizations (302)", st == 302 and "denied=super_admin" in hdrs.get("Location", ""))
    st, hdrs, _ = make_req("/users", token=member_token)
    check("Member Admin blocked from /users (302)", st == 302 and "denied=super_admin" in hdrs.get("Location", ""))

    # 3f. Second Member Admin (created via the legacy "user" name) blocked
    st, hdrs, _ = make_req("/organizations", token=user_token)
    check("Member Admin (legacy user) blocked from /organizations (302)", st == 302 and "denied=super_admin" in hdrs.get("Location", ""))
    st, hdrs, _ = make_req("/users", token=user_token)
    check("Member Admin (legacy user) blocked from /users (302)", st == 302 and "denied=super_admin" in hdrs.get("Location", ""))

    # -----------------------------------------------------------------------
    # SECTION 4: Role-Based Creation API Enforcement (/api/v1/auth/users)
    # -----------------------------------------------------------------------
    print("\n--- [SECTION 4] Workspace-Scoped User Creation Enforcement ---")
    ts = int(time.time())
    # Product admin creating member_admin -> Allowed
    st, _, d = make_req("/api/v1/auth/users", method="POST", body={
        "action": "create",
        "email": f"teamlead_{ts}@test.local",
        "password": "Password123!",
        "role": "member_admin",
        "name": "Team Lead"
    }, token=prod_token)
    check("Product Admin creates member_admin in own org -> 200 OK", st == 200 and d.get("status") == "ok")

    # Product admin creating user -> Allowed
    st, _, d = make_req("/api/v1/auth/users", method="POST", body={
        "action": "create",
        "email": f"teammember_{ts}@test.local",
        "password": "Password123!",
        "role": "user",
        "name": "Staff Member"
    }, token=prod_token)
    check("Product Admin creates user in own org -> 200 OK", st == 200 and d.get("status") == "ok")

    # Product admin creating admin -> Forbidden 403
    st, _, d = make_req("/api/v1/auth/users", method="POST", body={
        "action": "create",
        "email": f"illegal_admin_{ts}@test.local",
        "password": "Password123!",
        "role": "admin",
        "name": "Illegal Admin"
    }, token=prod_token)
    check("Product Admin creating admin -> 403 Forbidden", st == 403)

    # Product admin creating super_admin -> Forbidden 403
    st, _, d = make_req("/api/v1/auth/users", method="POST", body={
        "action": "create",
        "email": f"illegal_super_{ts}@test.local",
        "password": "Password123!",
        "role": "super_admin",
        "name": "Illegal Super"
    }, token=prod_token)
    check("Product Admin creating super_admin -> 403 Forbidden", st == 403)

    # -----------------------------------------------------------------------
    # SECTION 5: Frontend Template Files & Screenshot Verification
    # -----------------------------------------------------------------------
    print("\n--- [SECTION 5] Frontend Templates & Screenshot Assets ---")
    org_html = os.path.join(ROOT_DIR, "web", "organizations.html")
    users_html = os.path.join(ROOT_DIR, "web", "users.html")
    check("web/organizations.html exists", os.path.isfile(org_html))
    check("web/users.html exists", os.path.isfile(users_html))

    with open(org_html, "r", encoding="utf-8") as f:
        c = f.read()
        check("organizations.html has Super Admin branding", "Super Admin Portal" in c)
        check("organizations.html has Create Organization modal", "modalCreate" in c)
        check("organizations.html has Edit Organization modal", "modalEdit" in c)

    with open(users_html, "r", encoding="utf-8") as f:
        c = f.read()
        check("users.html has Global System Users title", "Global System Users" in c)
        check("users.html has Add User modal", "modalAddUser" in c)
        check("users.html has Role Change modal", "modalChangeRole" in c)

    screenshots = [
        "scratch/user_screenshots/12_overall_organizations_dashboard.png",
        "scratch/user_screenshots/13_create_organization_modal.png",
        "scratch/user_screenshots/14_global_users_directory.png",
        "scratch/user_screenshots/15_add_user_modal.png",
    ]
    for shot in screenshots:
        p = os.path.join(ROOT_DIR, shot)
        check(f"Screenshot asset '{os.path.basename(shot)}' exists", os.path.isfile(p) and os.path.getsize(p) > 20000)

    print("\n=====================================================================")
    print(f"  VERIFICATION COMPLETE: {passed_count}/{total_count} CHECKS PASSED (100% SUCCESS)")
    print("=====================================================================")

if __name__ == "__main__":
    main()
