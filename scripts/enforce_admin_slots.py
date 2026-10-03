#!/usr/bin/env python3
"""scripts/enforce_admin_slots.py — bring existing accounts in line with the admin rule:
one Super Admin for the whole platform, one Product Admin per organization.

Extra admins are demoted to Member Admin, or with --deactivate switched off (signed out, hidden from
the Team page, unable to sign in). Nothing is deleted; either can be undone from the Users Registry. You choose which account stays Super Admin; in each organization the oldest
active Product Admin stays unless --keep-admin names another.

Dry run by default: prints the plan. Add --apply to write it.
  python3 scripts/enforce_admin_slots.py --keep-super you@firm.com
  python3 scripts/enforce_admin_slots.py --keep-super you@firm.com --keep-admin owner@alpha.com --apply

Reads DATABASE_URL from the environment (set -a; . ./.env; set +a) like the server does.
"""

import argparse
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)
os.environ.setdefault("SUPER_ADMIN_EMAIL", "")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--keep-super", required=True, help="email of the account that stays Super Admin")
    ap.add_argument("--keep-admin", action="append", default=[], help="email of a Product Admin to keep in their organization (repeatable)")
    ap.add_argument("--deactivate", action="store_true", help="deactivate extra admins instead of demoting them")
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    args = ap.parse_args()
    os.environ["SUPER_ADMIN_EMAIL"] = ""           # the env owner rule must not fire while we reorganise

    from agent.auth_manager import AuthManager
    am = AuthManager()
    if getattr(am, "_db_load_failed", False):
        sys.exit("PostgreSQL is configured but unreachable; refusing to run.")
    active = sorted((u for u in am._users.values() if u.active), key=lambda u: u.created_at)
    ws_name = lambda wid: (am._workspaces.get(wid).name if am._workspaces.get(wid) else wid)
    plan = []   # (user, from_role, reason)

    keep_email = args.keep_super.strip().lower()
    supers = [u for u in active if u.role == "super_admin"]
    keepers = [u for u in supers if u.email.lower() == keep_email]
    if not keepers:
        candidates = ", ".join(sorted({u.email for u in supers})) or "none"
        sys.exit(f"{args.keep_super} is not an active Super Admin. Active Super Admin emails: {candidates}")
    keep = keepers[0]                               # oldest account with that email
    for u in supers:
        if u.user_id != keep.user_id:
            plan.append((u, "super_admin", "extra Super Admin" + (" (duplicate account of the one kept)" if u.email.lower() == keep_email else "")))

    keep_admins = {e.strip().lower() for e in args.keep_admin}
    by_ws = {}
    for u in active:
        if u.role == "admin":
            by_ws.setdefault(u.workspace_id, []).append(u)
    for wid, group in by_ws.items():
        if len(group) < 2:
            continue
        chosen = next((u for u in group if u.email.lower() in keep_admins), group[0])
        for u in group:
            if u.user_id != chosen.user_id:
                plan.append((u, "admin", f"extra Product Admin in {ws_name(wid)} (keeping {chosen.email})"))

    no_admin = [w for w in am._workspaces.values() if w.active and w.workspace_id not in by_ws]

    print(f"Super Admin kept: {keep.email} ({ws_name(keep.workspace_id)}, {keep.user_id})")
    verb = "deactivate" if args.deactivate else "demote to Member Admin"
    print(f"\n{len(plan)} account(s) to {verb}:")
    for u, role, why in plan:
        print(f"  {u.email:<45} {ws_name(u.workspace_id)[:28]:<28} {u.user_id}  {why}")
    if no_admin:
        print(f"\n{len(no_admin)} active organization(s) have no Product Admin (not changed; assign one in Users Registry):")
        for w in no_admin:
            print(f"  {w.name[:40]:<40} {w.workspace_id}")

    if not args.apply:
        print(f"\nDry run: nothing changed. Add --apply to {verb} the accounts above.")
        return
    gone = {u.user_id for u, _, _ in plan}
    for u, _, _ in plan:
        u.role = "member_admin"
        if args.deactivate:
            u.active = False
    if args.deactivate:
        for h in [h for h, sdoc in am._sessions.items() if sdoc.get("user_id") in gone]:
            am._sessions.pop(h, None)
    if am._save_store() is False:
        sys.exit("Save failed; no changes were written.")
    print(f"\nDone: {len(plan)} account(s) " + ("deactivated and signed out." if args.deactivate else "are now Member Admins."))


if __name__ == "__main__":
    main()
