#!/usr/bin/env python3
"""scripts/test_task2_middleware.py — Verifies Task 2: Server Middleware & System REST APIs."""

import json
import os
import sys
import time
import urllib.request
import urllib.error

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from agent.auth_manager import auth_manager, UserRole

BASE_URL = f"http://localhost:{os.getenv('PORT', '8091')}"

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
        # Disable automatic 302 following so we can inspect redirection
        class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
            def http_error_302(self, req, fp, code, msg, headers):
                return fp
        opener = urllib.request.build_opener(NoRedirectHandler)
        resp = opener.open(req, timeout=5)
        raw = resp.read().decode("utf-8")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = raw
        return resp.status, resp.headers, parsed
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = raw
        return e.code, e.headers, parsed

def run_tests():
    print("=== [TASK 2 VERIFICATION] Server Middleware & System REST APIs ===")

    # 1. Setup Test Users & Tokens via dev-session on server
    # 1a. Super Admin
    su = auth_manager.get_user("usr-admin-01") or auth_manager._current_user()
    su.role = UserRole.SUPER_ADMIN.value
    auth_manager._save_store()
    _, _, d_su = make_req("/api/v1/auth/dev-session", method="POST", body={"role": "super_admin"})
    super_token = d_su["token"]

    # 1b. Product Admin in ws-default
    pa = auth_manager._find_user_by_email("prodadmin@test.local")
    if not pa:
        pa = auth_manager.create_user("ws-default", "prodadmin@test.local", UserRole.ADMIN.value)
        auth_manager.set_password(pa.user_id, "Password123!")
    pa.role = UserRole.ADMIN.value
    auth_manager._save_store()
    _, _, d_pa = make_req("/api/v1/auth/dev-session", method="POST", body={"email": "prodadmin@test.local"})
    prod_token = d_pa["token"]

    # 1c. Member Admin in ws-default
    ma = auth_manager._find_user_by_email("memberadmin@test.local")
    if not ma:
        ma = auth_manager.create_user("ws-default", "memberadmin@test.local", UserRole.MEMBER_ADMIN.value)
        auth_manager.set_password(ma.user_id, "Password123!")
    ma.role = UserRole.MEMBER_ADMIN.value
    auth_manager._save_store()
    _, _, d_ma = make_req("/api/v1/auth/dev-session", method="POST", body={"email": "memberadmin@test.local"})
    member_token = d_ma["token"]

    # 1d. A second Member Admin in ws-default (the Standard User role was folded into Member Admin)
    reg_u = auth_manager._find_user_by_email("regular@test.local")
    if not reg_u:
        reg_u = auth_manager.create_user("ws-default", "regular@test.local", UserRole.MEMBER_ADMIN.value)
        auth_manager.set_password(reg_u.user_id, "Password123!")
    reg_u.role = UserRole.MEMBER_ADMIN.value
    auth_manager._save_store()
    _, _, d_u = make_req("/api/v1/auth/dev-session", method="POST", body={"email": "regular@test.local"})
    user_token = d_u["token"]

    print(" -> Obtained server session tokens for all roles.")


    # -----------------------------------------------------------------------
    # TEST 1: Super Admin Access (Allowed: 200 OK)
    # -----------------------------------------------------------------------
    print("\n--- Testing Super Admin Permissions ---")
    
    # Page /organizations
    status, hdrs, body = make_req("/organizations", token=super_token)
    assert status == 200, f"Super Admin should access /organizations (got {status})"
    assert "<title>Overall Organizations" in body, "Expected HTML page"
    print(" -> PASSED: Super Admin accessed /organizations (200 OK).")

    # API /api/v1/system/organizations
    status, _, data = make_req("/api/v1/system/organizations", token=super_token)
    assert status == 200 and data.get("status") == "ok", f"Super Admin API failed: {status}"
    print(" -> PASSED: Super Admin accessed /api/v1/system/organizations (200 OK).")

    # API /api/v1/system/users
    status, _, data = make_req("/api/v1/system/users", token=super_token)
    assert status == 200 and data.get("status") == "ok", f"Super Admin API failed: {status}"
    print(" -> PASSED: Super Admin accessed /api/v1/system/users (200 OK).")

    # API /api/v1/system/stats
    status, _, data = make_req("/api/v1/system/stats", token=super_token)
    assert status == 200 and "total_organizations" in data, f"Super Admin API failed: {status}"
    print(" -> PASSED: Super Admin accessed /api/v1/system/stats (200 OK).")

    # POST create organization
    test_org_name = f"Test Hospital {int(time.time())}"
    status, _, data = make_req("/api/v1/system/organizations", method="POST", body={
        "action": "create",
        "name": test_org_name,
        "rate_limit_rpm": 180,
    }, token=super_token)
    assert status == 201 and data.get("status") == "ok", f"Super Admin org creation failed: {status} {data}"
    created_ws_id = data["organization"]["workspace_id"]
    print(f" -> PASSED: Super Admin created organization '{test_org_name}' ({created_ws_id}).")

    # -----------------------------------------------------------------------
    # TEST 2: Product Admin Access (Denied: 403 / Redirect)
    # -----------------------------------------------------------------------
    print("\n--- Testing Product Admin Restrictions ---")

    # Page /organizations -> Blocked
    status, hdrs, _ = make_req("/organizations", token=prod_token)
    assert status == 302, f"Product Admin must be redirected away from /organizations (got {status})"
    loc = hdrs.get("Location", "")
    assert "denied=super_admin" in loc, f"Expected redirect to ?denied=super_admin, got {loc}"
    print(" -> PASSED: Product Admin redirected with ?denied=super_admin from /organizations.")

    # API /api/v1/system/organizations -> 403 Forbidden
    status, _, data = make_req("/api/v1/system/organizations", token=prod_token)
    assert status == 403, f"Product Admin must receive 403 for /api/v1/system/organizations (got {status})"
    print(" -> PASSED: Product Admin blocked from GET /api/v1/system/organizations (403 Forbidden).")

    # API /api/v1/system/users -> 403 Forbidden
    status, _, data = make_req("/api/v1/system/users", token=prod_token)
    assert status == 403, f"Product Admin must receive 403 for /api/v1/system/users (got {status})"
    print(" -> PASSED: Product Admin blocked from GET /api/v1/system/users (403 Forbidden).")

    # API POST /api/v1/system/organizations -> 403 Forbidden
    status, _, data = make_req("/api/v1/system/organizations", method="POST", body={"action": "create", "name": "Illegal Org"}, token=prod_token)
    assert status == 403, f"Product Admin must receive 403 for POST /api/v1/system/organizations (got {status})"
    print(" -> PASSED: Product Admin blocked from POST /api/v1/system/organizations (403 Forbidden).")

    # -----------------------------------------------------------------------
    # TEST 3: User Creation Permissions via /api/v1/auth/users
    # -----------------------------------------------------------------------
    print("\n--- Testing Creation Delegation (/api/v1/auth/users) ---")

    # 3a. Product Admin creating Member Admin in own org -> 200 OK
    status, _, data = make_req("/api/v1/auth/users", method="POST", body={
        "action": "create",
        "email": f"new_member_admin_{int(time.time())}@test.local",
        "password": "Password123!",
        "role": "member_admin",
        "name": "New Team Lead"
    }, token=prod_token)
    assert status == 200 and data.get("status") == "ok", f"Product Admin should create member_admin (got {status} {data})"
    print(" -> PASSED: Product Admin successfully created Member Admin in own workspace.")

    # 3b. Product Admin creating standard User in own org -> 200 OK
    status, _, data = make_req("/api/v1/auth/users", method="POST", body={
        "action": "create",
        "email": f"new_user_{int(time.time())}@test.local",
        "password": "Password123!",
        "role": "user",
        "name": "New Team Member"
    }, token=prod_token)
    assert status == 200 and data.get("status") == "ok", f"Product Admin should create member (legacy role name) (got {status} {data})"
    print(" -> PASSED: Product Admin created member via legacy role name in own workspace.")

    # 3c. Product Admin attempting to create Product Admin -> 403 Forbidden
    status, _, data = make_req("/api/v1/auth/users", method="POST", body={
        "action": "create",
        "email": f"rogue_admin_{int(time.time())}@test.local",
        "password": "Password123!",
        "role": "admin",
        "name": "Rogue Admin"
    }, token=prod_token)
    assert status == 403, f"Product Admin cannot create Product Admin (got {status} {data})"
    print(f" -> PASSED: Product Admin blocked from creating Product Admin (403: {data.get('error')}).")

    # 3d. Product Admin attempting to create Super Admin -> 403 Forbidden
    status, _, data = make_req("/api/v1/auth/users", method="POST", body={
        "action": "create",
        "email": f"rogue_super_{int(time.time())}@test.local",
        "password": "Password123!",
        "role": "super_admin",
        "name": "Rogue Super Admin"
    }, token=prod_token)
    assert status == 403, f"Product Admin cannot create Super Admin (got {status} {data})"
    print(f" -> PASSED: Product Admin blocked from creating Super Admin (403: {data.get('error')}).")

    # 3e. Product Admin attempting to create user in another organization -> 403 Forbidden
    status, _, data = make_req("/api/v1/auth/users", method="POST", body={
        "action": "create",
        "email": f"cross_org_{int(time.time())}@test.local",
        "password": "Password123!",
        "role": "user",
        "workspace_id": created_ws_id
    }, token=prod_token)
    assert status == 403, f"Product Admin cannot create user in other org (got {status} {data})"
    print(f" -> PASSED: Product Admin blocked from cross-org user creation (403: {data.get('error')}).")

    # -----------------------------------------------------------------------
    # TEST 4: Member Admin Restrictions
    # -----------------------------------------------------------------------
    print("\n--- Testing Member Admin Restrictions ---")

    # Member Admin accessing /organizations -> Blocked
    status, hdrs, _ = make_req("/organizations", token=member_token)
    assert status == 302 and "denied=super_admin" in hdrs.get("Location", "")
    print(" -> PASSED: Member Admin redirected away from /organizations.")

    # Second Member Admin accessing /organizations -> Blocked
    status, hdrs, _ = make_req("/organizations", token=user_token)
    assert status == 302 and "denied=super_admin" in hdrs.get("Location", "")
    print(" -> PASSED: Second Member Admin redirected away from /organizations.")

    # Member Admin attempting to call /api/v1/auth/users -> 403 Forbidden
    status, _, data = make_req("/api/v1/auth/users", method="POST", body={"action": "create", "email": "fail@test.com"}, token=user_token)
    assert status == 403, f"Member Admin cannot create accounts (got {status})"
    print(" -> PASSED: Member Admin blocked from user creation endpoint (403 Forbidden).")

    print("\n=== ALL TESTS PASSED FOR TASK 2 ===")

if __name__ == "__main__":
    run_tests()
