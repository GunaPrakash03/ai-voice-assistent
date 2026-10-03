#!/usr/bin/env python3
"""scripts/verify_admin_slots.py — one Super Admin for the platform, one Product Admin per organization.

Runs offline against throwaway JSON auth stores (DATABASE_URL is cleared), so it never touches
PostgreSQL or config/auth_store.json.
"""

import os
import sys
import tempfile

os.environ["DATABASE_URL"] = ""
os.environ["SUPER_ADMIN_EMAIL"] = ""

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from agent.auth_manager import AuthManager  # noqa: E402

passed = 0
failed = 0
PW = "Passw0rd!x"


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  [PASS] {label}")
    else:
        failed += 1
        print(f"  [FAIL] {label} {detail}")


def rejects(label, fn, needle):
    try:
        result = fn()
    except ValueError as e:
        check(label, needle.lower() in str(e).lower(), f"(error was: {e})")
        return
    check(label, False, f"(accepted: {result!r})")


def fresh():
    return AuthManager(store_path=os.path.join(tempfile.mkdtemp(prefix="slots-"), "auth_store.json"))


def supers(am):
    return [u for u in am._users.values() if u.active and u.role == "super_admin"]


def admins(am, ws):
    return [u for u in am._users.values() if u.active and u.role == "admin" and u.workspace_id == ws]


print("\n1. Creating accounts")
am = fresh()
check("a fresh platform has exactly one Super Admin", len(supers(am)) == 1)
rejects("a second Super Admin is refused", lambda: am.add_member("ws-default", "two@x.test", PW, role="super_admin"), "only one super admin")
org = am.create_organization("Alpha Law", admin_email="alice@alpha.test", admin_password=PW, admin_name="Alice")
ws = org["workspace_id"] if isinstance(org, dict) else org.workspace_id
check("a new organization gets its Product Admin", len(admins(am, ws)) == 1)
rejects("a second Product Admin in the same organization is refused",
        lambda: am.add_member(ws, "bob@alpha.test", PW, role="admin"), "already has a product admin")
sam = am.add_member(ws, "sam@alpha.test", PW, role="member_admin", name="Sam")
tia = am.add_member(ws, "tia@alpha.test", PW, role="member_admin", name="Tia")
check("Member Admins are unlimited", len([u for u in am._users.values() if u.workspace_id == ws]) == 3)
beta = am.create_organization("Beta Legal", admin_email="bea@beta.test", admin_password=PW)
ws_b = beta["workspace_id"] if isinstance(beta, dict) else beta.workspace_id
check("another organization has its own Product Admin", len(admins(am, ws_b)) == 1)

print("\n2. Changing roles")
alice = admins(am, ws)[0]
rejects("promoting a member while the organization has a Product Admin is refused",
        lambda: am.update_user_global(sam.user_id, {"role": "admin"}), "already has a product admin")
am.update_user_global(sam.user_id, {"role": "admin", "replace_current": True})
check("hand-over: Sam becomes Product Admin", am.get_user(sam.user_id).role == "admin")
check("hand-over: Alice becomes a Member Admin", am.get_user(alice.user_id).role == "member_admin")
check("still exactly one Product Admin", len(admins(am, ws)) == 1)
rejects("demoting the only Product Admin is refused", lambda: am.update_user_global(sam.user_id, {"role": "member_admin"}), "needs its product admin")
rejects("deactivating the only Product Admin is refused", lambda: am.update_user_global(sam.user_id, {"active": False}), "needs its product admin")
rejects("moving the Product Admin into an organization that has one is refused",
        lambda: am.update_user_global(tia.user_id, {"workspace_id": ws_b, "role": "admin"}), "already has a product admin")
rejects("moving the only Product Admin out of the organization is refused",
        lambda: am.update_user_global(sam.user_id, {"workspace_id": ws_b}), "needs its product admin")
am.update_user_global(tia.user_id, {"active": False})
rejects("reactivating someone as Product Admin when the slot is taken is refused",
        lambda: am.update_user_global(tia.user_id, {"active": True, "role": "admin"}), "already has a product admin")
check("editing a name does not trip the rule", am.update_user_global(sam.user_id, {"name": "Sam R"})["name"] == "Sam R")

boss = supers(am)[0]
rejects("promoting to Super Admin without hand-over is refused",
        lambda: am.update_user_global(alice.user_id, {"role": "super_admin"}), "only one super admin")
rejects("demoting the Super Admin directly is refused", lambda: am.update_user_global(boss.user_id, {"role": "admin"}), "needs its super admin")
rejects("deactivating the Super Admin is refused", lambda: am.update_user_global(boss.user_id, {"active": False}), "needs its super admin")
am.update_user_global(alice.user_id, {"role": "super_admin", "replace_current": True})
check("Super Admin hand-over: Alice is now Super Admin", am.get_user(alice.user_id).role == "super_admin")
check("Super Admin hand-over: the previous one is a Member Admin", am.get_user(boss.user_id).role == "member_admin")
check("still exactly one Super Admin", len(supers(am)) == 1)

print("\n3. Organizations that already break the rule (old data)")
am._users[tia.user_id].active = True
am._users[tia.user_id].role = "admin"          # simulate a legacy duplicate Product Admin
check("legacy duplicate: other fields can still be edited", am.update_user_global(tia.user_id, {"title": "Partner"}) is not None)
check("legacy duplicate: demoting one of two Product Admins is allowed",
      am.update_user_global(tia.user_id, {"role": "member_admin"})["role"] == "member_admin")

print("\n4. SUPER_ADMIN_EMAIL (deployment owner)")
os.environ["SUPER_ADMIN_EMAIL"] = "owner@firm.test"
os.environ["SUPER_ADMIN_PASSWORD"] = PW
am2 = fresh()
check("on a fresh platform the named owner replaces the built-in placeholder",
      [u.email for u in supers(am2)] == ["owner@firm.test"], [u.email for u in supers(am2)])
os.environ["SUPER_ADMIN_EMAIL"] = "owner@firm.test,second@firm.test"
am3 = fresh()
check("a second address in SUPER_ADMIN_EMAIL is ignored", [u.email for u in supers(am3)] == ["owner@firm.test"], [u.email for u in supers(am3)])
store = am2.store_path
am2._save_store()
os.environ["SUPER_ADMIN_EMAIL"] = "intruder@firm.test"
am4 = AuthManager(store_path=store)
check("a different address cannot add a second Super Admin to an existing platform",
      [u.email for u in supers(am4)] == ["owner@firm.test"], [u.email for u in supers(am4)])
# Password recovery for the existing Super Admin still works while old duplicate Super Admins remain.
legacy = am4._users[next(u.user_id for u in am4._users.values() if u.email == "owner@firm.test")]
dup = am4.add_member("ws-default", "dup@firm.test", PW, role="member_admin")
am4._users[dup.user_id].role = "super_admin"     # simulate an old duplicate Super Admin
am4._save_store()
old_hash = legacy.password_hash
os.environ["SUPER_ADMIN_EMAIL"] = "owner@firm.test"
os.environ["SUPER_ADMIN_PASSWORD"] = "N3w-Passw0rd!"
os.environ["SUPER_ADMIN_RESET_PASSWORD"] = "1"
am5 = AuthManager(store_path=store)
check("one-shot password reset works for the existing Super Admin despite old duplicates",
      am5._find_user_by_email("owner@firm.test").password_hash != old_hash)
check("and the new password signs in", am5.check_password("owner@firm.test", "N3w-Passw0rd!") is not None)
os.environ["SUPER_ADMIN_EMAIL"] = ""
os.environ["SUPER_ADMIN_PASSWORD"] = ""
os.environ["SUPER_ADMIN_RESET_PASSWORD"] = ""

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
