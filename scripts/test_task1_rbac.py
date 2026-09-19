#!/usr/bin/env python3
"""scripts/test_task1_rbac.py — Verifies Task 1: RBAC Engine & Role Hierarchy in agent/auth_manager.py."""

import os
import sys
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from agent.auth_manager import auth_manager, UserRole, normalize_role, can_create_role

def run_tests():
    print("=== [TASK 1 VERIFICATION] RBAC Engine & Role Hierarchy ===")
    
    # 1. Verify Enum Roles
    roles = [r.value for r in UserRole]
    print(f"[TEST 1] Registered User Roles: {roles}")
    assert "super_admin" in roles, "super_admin missing from UserRole"
    assert "admin" in roles, "admin missing from UserRole"
    assert "member_admin" in roles, "member_admin missing from UserRole"
    assert "user" in roles, "user missing from UserRole"
    print(" -> PASSED: All 4 roles correctly configured.")

    # 2. Verify Role Aliases
    assert normalize_role("super_admin") == "super_admin"
    assert normalize_role("overall_admin") == "super_admin"
    assert normalize_role("product_admin") == "admin"
    assert normalize_role("member_admin") == "member_admin"
    assert normalize_role("operator") == "user"
    print(" -> PASSED: Role alias normalization verified.")

    # 3. Verify Super Admin check
    assert auth_manager.is_super_admin("super_admin") is True
    assert auth_manager.is_super_admin("overall_admin") is True
    assert auth_manager.is_super_admin("admin") is False
    assert auth_manager.is_super_admin("member_admin") is False
    assert auth_manager.is_super_admin("user") is False
    print(" -> PASSED: is_super_admin helper checks verified.")

    # 4. Verify Creation Hierarchy (can_create_role)
    ws_a = "ws-alpha"
    ws_b = "ws-beta"

    # 4a. Super Admin can create ANY role in ANY workspace
    for target in ("super_admin", "admin", "member_admin", "user"):
        ok, reason = can_create_role("super_admin", ws_a, target, ws_b)
        assert ok is True, f"Super Admin should be able to create {target} in any org"
    print(" -> PASSED: Super Admin global delegation verified.")

    # 4b. Product Admin:
    # - can create member_admin and user in own workspace
    ok, _ = can_create_role("admin", ws_a, "member_admin", ws_a)
    assert ok is True, "Product Admin should be able to create member_admin in own org"
    ok, _ = can_create_role("admin", ws_a, "user", ws_a)
    assert ok is True, "Product Admin should be able to create user in own org"
    # - CANNOT create admin or super_admin
    ok, reason = can_create_role("admin", ws_a, "admin", ws_a)
    assert ok is False, "Product Admin cannot create Product Admin"
    ok, reason = can_create_role("admin", ws_a, "super_admin", ws_a)
    assert ok is False, "Product Admin cannot create Super Admin"
    # - CANNOT create across workspaces
    ok, reason = can_create_role("admin", ws_a, "member_admin", ws_b)
    assert ok is False, "Product Admin cannot create user in other org"
    print(" -> PASSED: Product Admin workspace-scoped delegation verified.")

    # 4c. Member Admin:
    # - can ONLY create user in own workspace
    ok, _ = can_create_role("member_admin", ws_a, "user", ws_a)
    assert ok is True, "Member Admin should be able to create user in own org"
    ok, reason = can_create_role("member_admin", ws_a, "member_admin", ws_a)
    assert ok is False, "Member Admin cannot create Member Admin"
    ok, reason = can_create_role("member_admin", ws_a, "admin", ws_a)
    assert ok is False, "Member Admin cannot create Product Admin"
    ok, reason = can_create_role("member_admin", ws_a, "user", ws_b)
    assert ok is False, "Member Admin cannot create user in other org"
    print(" -> PASSED: Member Admin workspace-scoped delegation verified.")

    # 4d. Standard User cannot create any accounts
    ok, reason = can_create_role("user", ws_a, "user", ws_a)
    assert ok is False, "Regular User cannot create accounts"
    print(" -> PASSED: Standard User restrictions verified.")

    # 5. Multi-Tenant Organization & User Querying
    orgs = auth_manager.list_all_organizations()
    users = auth_manager.list_all_users_global()
    assert len(orgs) > 0, "Expected at least 1 organization"
    assert len(users) > 0, "Expected at least 1 user"
    first_org = orgs[0]
    assert "workspace_id" in first_org and "member_count" in first_org
    first_user = users[0]
    assert "user_id" in first_user and "role" in first_user and "workspace_name" in first_user
    print(f" -> PASSED: Organization ({len(orgs)}) & Global User ({len(users)}) enriched listings verified.")

    print("\n=== ALL 5 CHECKS PASSED FOR TASK 1 ===")

if __name__ == "__main__":
    run_tests()
