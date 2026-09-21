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
    assert "user" not in roles, "the Standard User role was retired; only three roles exist"
    assert len(roles) == 3, f"expected exactly three roles, got {roles}"
    print(" -> PASSED: Exactly 3 roles configured.")

    # 2. Verify Role Aliases
    assert normalize_role("super_admin") == "super_admin"
    assert normalize_role("overall_admin") == "super_admin"
    assert normalize_role("product_admin") == "admin"
    assert normalize_role("member_admin") == "member_admin"
    assert normalize_role("operator") == "member_admin"   # retired synonyms fold into Member Admin
    assert normalize_role("user") == "member_admin"
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
    for target in ("super_admin", "admin", "member_admin"):
        ok, reason = can_create_role("super_admin", ws_a, target, ws_b)
        assert ok is True, f"Super Admin should be able to create {target} in any org"
    print(" -> PASSED: Super Admin global delegation verified.")

    # 4b. Product Admin:
    # - can create member_admin in own workspace ("user" is an alias of member_admin now)
    ok, _ = can_create_role("admin", ws_a, "member_admin", ws_a)
    assert ok is True, "Product Admin should be able to create member_admin in own org"
    ok, _ = can_create_role("admin", ws_a, "user", ws_a)
    assert ok is True, "Legacy 'user' requests resolve to member_admin"
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
    # - can ONLY invite other member_admin in own workspace
    ok, _ = can_create_role("member_admin", ws_a, "member_admin", ws_a)
    assert ok is True, "Member Admin should be able to invite member_admin in own org"
    ok, reason = can_create_role("member_admin", ws_a, "admin", ws_a)
    assert ok is False, "Member Admin cannot create Product Admin"
    ok, reason = can_create_role("member_admin", ws_a, "super_admin", ws_a)
    assert ok is False, "Member Admin cannot create Super Admin"
    ok, reason = can_create_role("member_admin", ws_a, "member_admin", ws_b)
    assert ok is False, "Member Admin cannot invite into another org"
    print(" -> PASSED: Member Admin workspace-scoped delegation verified.")

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
