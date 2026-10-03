#!/usr/bin/env python3
"""scripts/verify_signup.py — Phase 5: Client Self-Service Sign-Up verification suite.

Task 5.1 (Firm Profile Data Model):
1. Website URL normalisation and rejection of bad URLs
2. Phone, time zone, enum, list, address and business-hours validation
3. Merge semantics (partial update, clearing fields, dependent fields)
4. AuthManager.create_organization / update_organization / list_all_organizations persistence

Task 5.2 (Self-Service Sign-Up API):
5. AuthManager.signup: validation-before-create, pending workspace, blocked sign-in, rollback
6. Per-IP attempt limiter

Task 5.3 (Phone Verification):
7. Sign-up SMS ticket: wrong/expired/locked-out codes, activation + session, single use,
   re-issue on password sign-in, super-admin approve/reject, save-failure rollback, persistence

Task 5.2b (Google / Microsoft sign-in):
9. ID-token checks (state, browser binding, aud, exp, nonce, issuer, verified email, Microsoft
   tenant ids) and sign-up with a verified identity (email must match, one account per identity)

Task 5.5 (Onboarding hand-off to agent):
10. Agent built from the firm profile (greeting, hours, after-hours, languages, practice questions,
    data fields, lead emails), seeded once when onboarding is complete, never live;
    sign-up asks only for the account; firm details are completed during onboarding, which names the
    workspace and builds the agent; lead-notification email content and recipients

8. Live HTTP (only when a server is running at VERIFY_BASE_URL): disabled/validation paths that
   create nothing

Sections 1-7 run offline against a throwaway JSON store and never touch PostgreSQL or
config/auth_store.json. Section 7 never sends a request that could create an account.
"""

import json
import os
import sys
import tempfile
import urllib.error
import urllib.request

# Keep the module-level auth_manager away from the real database and env-driven admin seeding.
os.environ["DATABASE_URL"] = ""
os.environ["SUPER_ADMIN_EMAIL"] = ""

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from agent.auth_manager import AuthManager, PendingVerification  # noqa: E402
from agent.firm_profile import (  # noqa: E402
    SIGNUP_REQUIRED_FIELDS, normalize_firm_profile, normalize_website,
)

passed = 0
failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  [PASS] {label}")
    else:
        failed += 1
        print(f"  [FAIL] {label} {detail}")


def rejects(label, fn, needle=""):
    try:
        result = fn()
    except ValueError as e:
        check(label, needle.lower() in str(e).lower(), f"(error was: {e})")
        return
    check(label, False, f"(accepted: {result!r})")


FULL = {
    "website": "SmithLee-Law.com/",
    "office_phone": "(555) 123-4567",
    "address": {"line1": " 100  Main St ", "city": "Austin", "state": "TX", "postal_code": "78701", "country": "US"},
    "timezone": "America/Chicago",
    "firm_size": "2-10",
    "practice_areas": ["personal_injury", "family", "family", "other"],
    "practice_areas_other": "Maritime",
    "business_hours": {"mon": {"open": "09:00", "close": "17:30"}, "sat": None},
    "after_hours": "transfer",
    "after_hours_transfer_number": "+44 20 7946 0958",
    "languages": ["EN", "es", "pt-br"],
    "monthly_call_volume": "100-500",
    "practice_software": "clio",
    "lead_emails": ["Intake@SmithLee.com", "intake@smithlee.com", "partner@smithlee.com"],
    "referral_source": "Google",
}


def test_website():
    print("\n1. Website normalisation")
    cases = {
        "smithlaw.com": "https://smithlaw.com",
        "  https://WWW.SmithLaw.com/  ": "https://www.smithlaw.com",
        "http://smithlaw.com/contact?x=1": "http://smithlaw.com/contact?x=1",
        "smithlaw.co.uk:8443/a/": "https://smithlaw.co.uk:8443/a",
        "": "",
    }
    for raw, want in cases.items():
        got = normalize_website(raw)
        check(f"{raw!r} -> {want!r}", got == want, f"(got {got!r})")
    for bad in ("not a url", "ftp://smithlaw.com", "https://localhost", "https://user:pw@smithlaw.com",
                "javascript:alert(1)", "https://192.168.0.1", "https://smithlaw"):
        rejects(f"rejects {bad!r}", lambda b=bad: normalize_website(b), "website")


def test_fields():
    print("\n2. Field validation")
    p = normalize_firm_profile(FULL)
    check("office phone -> E.164", p["office_phone"] == "+15551234567", p.get("office_phone"))
    check("transfer number kept (international)", p["after_hours_transfer_number"] == "+442079460958")
    check("address whitespace collapsed", p["address"]["line1"] == "100 Main St")
    check("practice areas de-duplicated, order kept", p["practice_areas"] == ["personal_injury", "family", "other"])
    check("languages normalised", p["languages"] == ["en", "es", "pt-BR"], p["languages"])
    check("lead emails lower-cased + de-duplicated", p["lead_emails"] == ["intake@smithlee.com", "partner@smithlee.com"])
    check("closed day stored as None", p["business_hours"] == {"mon": {"open": "09:00", "close": "17:30"}, "sat": None})
    check("all required sign-up fields present", all(p.get(f) for f in SIGNUP_REQUIRED_FIELDS))

    rejects("unknown top-level field", lambda: normalize_firm_profile({"firmname": "x"}), "unknown firm profile field")
    rejects("unknown time zone", lambda: normalize_firm_profile({"timezone": "Mars/Base"}), "time zone")
    check("legacy browser zone Asia/Calcutta -> Asia/Kolkata", normalize_firm_profile({"timezone": "Asia/Calcutta"})["timezone"] == "Asia/Kolkata")
    check("legacy US/Eastern -> America/New_York", normalize_firm_profile({"timezone": "US/Eastern"})["timezone"] == "America/New_York")
    from agent.firm_profile import TIMEZONE_ALIASES, _known_timezones
    known = _known_timezones()
    bad_targets = [t for t in TIMEZONE_ALIASES.values() if known and t not in known]
    check("every alias points at a known zone", not bad_targets, bad_targets)
    rejects("bad firm size", lambda: normalize_firm_profile({"firm_size": "huge"}), "firm size")
    rejects("bad practice area", lambda: normalize_firm_profile({"practice_areas": ["tax"]}), "practice area")
    rejects("practice areas not a list", lambda: normalize_firm_profile({"practice_areas": "family"}), "list")
    rejects("short phone", lambda: normalize_firm_profile({"office_phone": "12345"}), "phone")
    rejects("bad email", lambda: normalize_firm_profile({"lead_emails": ["nope"]}), "email")
    rejects("too many lead emails", lambda: normalize_firm_profile({"lead_emails": [f"a{i}@x.com" for i in range(6)]}), "at most")
    rejects("bad language code", lambda: normalize_firm_profile({"languages": ["english"]}), "language")
    rejects("unknown weekday", lambda: normalize_firm_profile({"business_hours": {"monday": None}}), "weekday")
    rejects("bad time format", lambda: normalize_firm_profile({"business_hours": {"mon": {"open": "9am", "close": "5pm"}}}), "hh:mm")
    rejects("close before open", lambda: normalize_firm_profile({"business_hours": {"mon": {"open": "17:00", "close": "09:00"}}}), "after opening")
    rejects("unknown address field", lambda: normalize_firm_profile({"address": {"street": "x"}}), "address")
    rejects("non-text value", lambda: normalize_firm_profile({"referral_source": {"a": 1}}), "text")
    rejects("transfer without number", lambda: normalize_firm_profile({"after_hours": "transfer"}), "transfer")
    rejects("required fields enforced", lambda: normalize_firm_profile({"website": "a.com"}, required=SIGNUP_REQUIRED_FIELDS), "office_phone")


def test_merge():
    print("\n3. Merge semantics")
    base = normalize_firm_profile(FULL)
    p = normalize_firm_profile({"firm_size": "11-50"}, base)
    check("partial update changes one field", p["firm_size"] == "11-50" and p["website"] == base["website"])
    check("existing profile not mutated", base["firm_size"] == "2-10")
    p = normalize_firm_profile({"referral_source": None, "languages": []}, base)
    check("None / [] clears a field", "referral_source" not in p and "languages" not in p)
    p = normalize_firm_profile({"after_hours": "take_message"}, base)
    check("transfer number dropped when not transferring", "after_hours_transfer_number" not in p)
    p = normalize_firm_profile({"practice_areas": ["family"]}, base)
    check("'other' text dropped when 'other' unselected", "practice_areas_other" not in p)
    p = normalize_firm_profile({"practice_software": "other", "practice_software_other": "Smokeball"}, base)
    check("'other' software text kept", p.get("practice_software_other") == "Smokeball")
    check("empty input -> empty profile", normalize_firm_profile(None) == {})


def test_auth_manager():
    print("\n4. Organization persistence")
    with tempfile.TemporaryDirectory() as tmp:
        store = os.path.join(tmp, "auth_store.json")
        am = AuthManager(store_path=store)
        org = am.create_organization("Smith & Lee LLP", firm_profile=FULL,
                                     admin_email="owner@smithlee.com", admin_password="correct-horse")
        ws_id = org["workspace_id"]
        check("create returns normalised profile", org["metadata"]["firm_profile"]["website"] == "https://smithlee-law.com")

        before = set(am._workspaces)
        rejects("create rejects bad profile", lambda: am.create_organization("Bad Firm", firm_profile={"firm_size": "x"}), "firm size")
        check("bad profile leaves no orphan workspace", set(am._workspaces) == before)

        plain = am.create_organization("No Profile Firm")
        check("profile is optional on create", plain["metadata"] == {})

        reloaded = AuthManager(store_path=store)
        check("profile survives reload", reloaded.get_workspace(ws_id).metadata.get("firm_profile") == org["metadata"]["firm_profile"])

        listed = next(o for o in am.list_all_organizations() if o["workspace_id"] == ws_id)
        check("list_all_organizations includes profile", listed["firm_profile"].get("timezone") == "America/Chicago")

        upd = am.update_organization(ws_id, {"name": "Smith Lee", "firm_profile": {"firm_size": "50+"}})
        check("update merges profile", upd["metadata"]["firm_profile"]["firm_size"] == "50+"
              and upd["metadata"]["firm_profile"]["office_phone"] == "+15551234567")

        rejects("update rejects bad profile", lambda: am.update_organization(ws_id, {"name": "Renamed", "firm_profile": {"timezone": "Nowhere"}}), "time zone")
        check("rejected update leaves name untouched", am.get_workspace(ws_id).name == "Smith Lee")

        am.update_organization(ws_id, {"active": False})
        check("update without firm_profile keeps it", am.get_workspace(ws_id).metadata["firm_profile"]["firm_size"] == "50+")

        cleared = am.update_organization(ws_id, {"firm_profile": {f: None for f in normalize_firm_profile(FULL)}})
        check("clearing every field removes the profile key", "firm_profile" not in cleared["metadata"])


SIGNUP = {
    "name": "Dana Smith",
    "email": "dana@smithlee.com",
    "password": "correct-horse-battery",
    "phone": "555-201-3344",
    "firm_name": "Smith & Lee LLP",
    "firm_profile": {"website": "smithlee.com", "office_phone": "5551234567",
                     "timezone": "America/Chicago", "firm_size": "solo"},
}


def test_signup():
    print("\n5. Self-service sign-up")
    with tempfile.TemporaryDirectory() as tmp:
        store = os.path.join(tmp, "auth_store.json")
        am = AuthManager(store_path=store)
        res = am.signup(SIGNUP, source_ip="203.0.113.7")
        ws = am.get_workspace(res["workspace_id"])
        user = am.get_user(res["user_id"])
        check("returns pending status", res["status"] == "pending_verification")
        check("workspace created inactive", ws is not None and ws.active is False)
        check("signup record stored", ws.metadata["signup"]["status"] == "pending_verification"
              and ws.metadata["signup"]["source_ip"] == "203.0.113.7")
        check("firm profile stored normalised", ws.metadata["firm_profile"]["website"] == "https://smithlee.com")
        check("firm name is workspace name", ws.name == "Smith & Lee LLP" and res["slug"] == "smith-lee-llp")
        check("admin is Product Admin in the new workspace", user.role == "admin" and user.workspace_id == ws.workspace_id)
        check("admin phone stored E.164", user.phone == "+15552013344")
        check("phone masked in response", res["phone_masked"].startswith("+15") and "2013344" not in res["phone_masked"])
        check("response carries no secrets", not any(k in res for k in ("password", "password_hash", "token")))
        check("pending account persisted", AuthManager(store_path=store).get_workspace(ws.workspace_id).active is False)

        rejects("pending account cannot sign in", lambda: am.check_password(SIGNUP["email"], SIGNUP["password"]), "not been verified")
        rejects("wrong password still says incorrect", lambda: am.check_password(SIGNUP["email"], "wrong-password"), "incorrect")
        token, _ = am.create_session(user)
        check("pending account has no usable session", am.session_user(token) is None)

        second = am.signup({**SIGNUP, "email": "other@smithlee.com"})
        check("same firm name gets a unique slug", second["slug"] != res["slug"] and second["slug"].startswith("smith-lee-llp"))

        count = (len(am._workspaces), len(am._users))
        bad_cases = [
            ("duplicate email (any case)", {**SIGNUP, "email": "DANA@smithlee.com"}, "already exists"),
            ("missing name", {**SIGNUP, "name": "  "}, "your name is required"),
            ("bad email", {**SIGNUP, "email": "dana@smithlee"}, "valid email"),
            ("short password", {**SIGNUP, "password": "short"}, "at least 8"),
            ("password equals email", {**SIGNUP, "email": "x@firm.com", "password": "X@firm.com"}, "email address"),
            ("bad mobile phone", {**SIGNUP, "phone": "123"}, "mobile phone"),
            ("non-text name", {**SIGNUP, "name": ["Dana"]}, "must be text"),
            ("unknown field", {**SIGNUP, "role": "super_admin"}, "unknown sign-up field"),
            ("bad firm profile value", {**SIGNUP, "firm_profile": {**SIGNUP["firm_profile"], "firm_size": "x"}}, "firm size"),
            ("body not an object", [], "object"),
        ]
        for label, body, needle in bad_cases:
            rejects(f"rejects {label}", lambda b=body: am.signup(b), needle)
        check("rejected sign-ups create nothing", (len(am._workspaces), len(am._users)) == count)

        print("   -- account-only sign-up (firm details come in onboarding)")
        bare = am.signup({k: SIGNUP[k] for k in ("name", "email", "password", "phone")} | {"email": "bare@smithlee.com"})
        bws = am.get_workspace(bare["workspace_id"])
        check("name/email/password/phone is enough", bare["status"] == "pending_verification")
        check("workspace named after the person until onboarding", bws.name == "Dana Smith's firm", bws.name)
        check("onboarding pending, not named", bws.metadata.get("onboarding") == {"status": "pending", "named": False}, bws.metadata.get("onboarding"))
        check("no firm profile stored yet", not bws.metadata.get("firm_profile"))
        check("full sign-up is also onboarding-pending (named)", ws.metadata.get("onboarding") == {"status": "pending", "named": True})
        partial = am.signup({**SIGNUP, "email": "partial@smithlee.com", "firm_profile": {"website": "a-firm.com"}})
        check("partial firm details accepted at sign-up", am.get_workspace(partial["workspace_id"]).metadata["firm_profile"] == {"website": "https://a-firm.com"})

        orig_save = am._save_store
        before = (set(am._workspaces), set(am._users))

        def save_raises():
            raise RuntimeError("disk full")
        for style, broken in (("raises", save_raises), ("returns False", lambda: False)):
            am._save_store = broken
            try:
                am.signup({**SIGNUP, "email": "rollback@smithlee.com"})
                check(f"failed save ({style}) raises", False)
            except RuntimeError:
                check(f"failed save ({style}) raises", True)
            am._save_store = orig_save
            check(f"failed save ({style}) leaves no orphan workspace or user", (set(am._workspaces), set(am._users)) == before)
        retry = am.signup({**SIGNUP, "email": "rollback@smithlee.com"})
        check("same email can sign up after a failed save", retry["status"] == "pending_verification")

        listed = next(o for o in am.list_all_organizations() if o["workspace_id"] == res["workspace_id"])
        check("super-admin list shows signup status", (listed.get("signup") or {}).get("status") == "pending_verification")

        rejects("suspended (non-signup) org keeps suspended message",
                lambda: (am.update_organization("ws-default", {"active": False}),
                         am.add_member("ws-default", "suspended@x.com", "password123"),
                         am.check_password("suspended@x.com", "password123")), "suspended")


def test_limiter():
    print("\n6. Per-IP attempt limiter")
    with tempfile.TemporaryDirectory() as tmp:
        am = AuthManager(store_path=os.path.join(tmp, "s.json"))
        limit = am.SIGNUP_ATTEMPTS_PER_IP_PER_HOUR
        results = [am.signup_limiter.check("signup:198.51.100.1").allowed for _ in range(limit + 1)]
        check(f"first {limit} attempts allowed", all(results[:limit]))
        check("next attempt blocked", results[limit] is False)
        check("other IPs unaffected", am.signup_limiter.check("signup:198.51.100.2").allowed)
        blocked = am.signup_limiter.check("signup:198.51.100.1")
        check("blocked result has retry-after", blocked.retry_after > 0 and blocked.retry_after <= 3600)


def _pending(am, email):
    res = am.signup({**SIGNUP, "email": email})
    return res, am.get_user(res["user_id"])


def test_verification():
    print("\n7. Phone verification")
    with tempfile.TemporaryDirectory() as tmp:
        store = os.path.join(tmp, "auth_store.json")
        am = AuthManager(store_path=store)
        res, user = _pending(am, "verify@smithlee.com")
        ws_id = res["workspace_id"]

        try:
            am.check_password("verify@smithlee.com", SIGNUP["password"])
            check("pending sign-in raises PendingVerification", False)
        except PendingVerification as e:
            check("pending sign-in raises PendingVerification", e.user.user_id == user.user_id)
        check("PendingVerification is still a ValueError", issubclass(PendingVerification, ValueError))

        step = am.begin_two_step(user, True, "test-agent", purpose="signup")
        check("signup ticket issued", step["purpose"] == "signup" and len(step["code"]) == 6)
        check("signup ticket lives 30 min", round(next(iter(am._tickets().values()))["expires_at"] - __import__("time").time()) in range(1795, 1801))
        check("signup SMS text", "verification code is " + step["code"] in am.otp_message("signup", step["code"]))
        check("login SMS text unchanged", "sign-in code is 123456" in am.otp_message("login", "123456"))
        rejects("unknown purpose rejected", lambda: am.begin_two_step(user, True, purpose="reset"), "purpose")

        wrong = "000000" if step["code"] != "000000" else "111111"
        rejects("wrong code rejected", lambda: am.complete_two_step(step["ticket"], wrong), "incorrect")
        check("wrong code leaves workspace pending", am.is_signup_pending(ws_id))

        token, _ = am.complete_two_step(step["ticket"], step["code"])
        ws = am.get_workspace(ws_id)
        check("correct code activates workspace", ws.active is True)
        check("signup marked active via sms", ws.metadata["signup"]["status"] == "active" and ws.metadata["signup"]["resolved_via"] == "sms")
        check("verification signs the user in", am.session_user(token) is not None and am.session_user(token).user_id == user.user_id)
        check("password sign-in now works", am.check_password("verify@smithlee.com", SIGNUP["password"]).user_id == user.user_id)
        rejects("ticket is single-use", lambda: am.complete_two_step(step["ticket"], step["code"]), "expired")
        rejects("verified account cannot get a signup ticket", lambda: am.begin_two_step(user, True, purpose="signup"), "does not need")
        check("activation persisted", AuthManager(store_path=store).get_workspace(ws_id).active is True)

        print("   -- expiry, lockout, resend, restart")
        _, u2 = _pending(am, "expire@smithlee.com")
        st = am.begin_two_step(u2, True, purpose="signup")
        am._tickets()[__import__("hashlib").sha256(st["ticket"].encode()).hexdigest()]["code_expires_at"] = 0
        rejects("expired code rejected", lambda: am.complete_two_step(st["ticket"], st["code"]), "expired")
        am._tickets()[__import__("hashlib").sha256(st["ticket"].encode()).hexdigest()]["sent_at"] = 0
        again = am.resend_code(st["ticket"])
        check("resend keeps signup purpose", again["purpose"] == "signup")
        wrong = "000000" if again["code"] != "000000" else "111111"
        for _ in range(am.OTP_MAX_ATTEMPTS):
            try:
                am.complete_two_step(st["ticket"], wrong)
            except ValueError:
                pass
        rejects("locked out after max attempts", lambda: am.complete_two_step(st["ticket"], again["code"]), "too many")
        check("lockout leaves workspace pending", am.is_signup_pending(u2.workspace_id))
        fresh = AuthManager(store_path=store)   # tickets are in memory only, like a server restart
        rejects("ticket lost on restart", lambda: fresh.complete_two_step(again["code"], again["code"]), "expired")
        try:
            fresh.check_password("expire@smithlee.com", SIGNUP["password"])
            check("password sign-in re-issues code that activates", False, "(no PendingVerification raised)")
        except PendingVerification as e:
            st2 = fresh.begin_two_step(e.user, True, purpose="signup")
            fresh.complete_two_step(st2["ticket"], st2["code"])
            check("password sign-in re-issues code that activates", fresh.get_workspace(u2.workspace_id).active is True)

        print("   -- super admin decisions")
        r3, u3 = _pending(am, "approve@smithlee.com")
        am.update_organization(r3["workspace_id"], {"active": True})
        meta = am.get_workspace(r3["workspace_id"]).metadata["signup"]
        check("super-admin activation closes signup", meta["status"] == "active" and meta["resolved_via"] == "super_admin")

        r4, u4 = _pending(am, "reject@smithlee.com")
        st4 = am.begin_two_step(u4, True, purpose="signup")
        am.update_organization(r4["workspace_id"], {"active": False})
        check("super-admin deactivation rejects signup", am.get_workspace(r4["workspace_id"]).metadata["signup"]["status"] == "rejected")
        rejects("rejected signup cannot verify with an earlier ticket", lambda: am.complete_two_step(st4["ticket"], st4["code"]), "no longer")
        check("rejected signup stays inactive", am.get_workspace(r4["workspace_id"]).active is False)
        rejects("rejected signup cannot get a new ticket", lambda: am.begin_two_step(u4, True, purpose="signup"), "does not need")
        try:
            am.check_password("reject@smithlee.com", SIGNUP["password"])
            check("rejected signup sees suspended message", False)
        except PendingVerification:
            check("rejected signup sees suspended message", False)
        except ValueError as e:
            check("rejected signup sees suspended message", "suspended" in str(e))

        print("   -- activation save failure")
        r5, u5 = _pending(am, "savefail@smithlee.com")
        st5 = am.begin_two_step(u5, True, purpose="signup")
        orig = am._save_store
        am._save_store = lambda: False
        try:
            am.complete_two_step(st5["ticket"], st5["code"])
            check("failed activation save raises", False)
        except RuntimeError:
            check("failed activation save raises", True)
        am._save_store = orig
        check("failed activation leaves signup pending", am.is_signup_pending(r5["workspace_id"]))
        rejects("activate_signup refuses non-pending org", lambda: am.activate_signup("ws-default", via="sms"), "not waiting")


def test_oauth():
    print("\n9. Google / Microsoft sign-in")
    import base64 as b64m, time as t
    from urllib.parse import urlparse, parse_qs
    os.environ.update({"GOOGLE_CLIENT_ID": "gid", "GOOGLE_CLIENT_SECRET": "gs", "MICROSOFT_CLIENT_ID": "mid", "MICROSOFT_CLIENT_SECRET": "ms"})
    from agent.oauth import OAuthManager, OAuthError, configured_providers
    check("both providers configured from env", configured_providers() == ["google", "microsoft"])
    tid = "11111111-2222-3333-4444-555555555555"

    def run(provider, claims, browser=None, state=None, code="c"):
        om = OAuthManager()
        started = om.begin(provider, "signup", "https://app.example/cb")
        q = parse_qs(urlparse(started["url"]).query)
        base = {"aud": "gid" if provider == "google" else "mid", "exp": t.time() + 300, "nonce": q["nonce"][0]}
        if provider == "google":
            base.update(iss="https://accounts.google.com", sub="s1", email="A@Firm.com", email_verified=True, name="A")
        else:
            base.update(iss=f"https://login.microsoftonline.com/{tid}/v2.0", tid=tid, oid="o1", preferred_username="a@firm.com", name="A")
        body = {**base, **claims}
        body = {k: v for k, v in body.items() if v is not None}
        om._exchange = lambda cfg, code, verifier, uri: {"id_token": "e30." + b64m.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=") + ".x"}
        return om.finish(provider, code, state or q["state"][0], started["browser_key"] if browser is None else browser), q

    ident, q = run("google", {})
    check("google identity + lower-cased verified email", ident["identity"] == "google:s1" and ident["email"] == "a@firm.com" and ident["email_verified"])
    check("PKCE S256 + nonce + state sent", q["code_challenge_method"] == ["S256"] and q["nonce"][0] and q["state"][0])
    ident, _ = run("microsoft", {})
    check("microsoft identity is tid:oid, email not trusted", ident["identity"] == f"microsoft:{tid}:o1" and ident["email_verified"] is False)
    for label, provider, claims, kw, needle in [
        ("forged state", "google", {}, {"state": "nope"}, "expired"),
        ("other browser", "google", {}, {"browser": "wrong"}, "different browser"),
        ("cancelled (no code)", "google", {}, {"code": ""}, "cancelled"),
        ("wrong audience", "google", {"aud": "someone-else"}, {}, "different app"),
        ("expired token", "google", {"exp": t.time() - 3600}, {}, "expired"),
        ("nonce mismatch", "google", {"nonce": "x"}, {}, "could not be matched"),
        ("wrong google issuer", "google", {"iss": "https://evil.example"}, {}, "unexpected issuer"),
        ("unverified google email", "google", {"email_verified": False}, {}, "isn't verified"),
        ("microsoft issuer for another tenant", "microsoft", {"iss": "https://login.microsoftonline.com/99999999-2222-3333-4444-555555555555/v2.0"}, {}, "unexpected issuer"),
        ("microsoft missing oid", "microsoft", {"oid": None}, {}, "incomplete"),
        ("microsoft no email", "microsoft", {"preferred_username": "no-at-sign"}, {}, "email"),
    ]:
        try:
            run(provider, claims, **kw)
            check(f"rejects {label}", False, "(accepted)")
        except OAuthError as e:
            check(f"rejects {label}", needle in str(e).lower(), f"(error was: {e})")
    om = OAuthManager()
    started = om.begin("google", "login", "https://app.example/cb")
    st = parse_qs(urlparse(started["url"]).query)["state"][0]
    try:
        om.finish("google", "c", st, "wrong")
    except OAuthError:
        pass
    try:
        om.finish("google", "c", st, started["browser_key"])
        check("state is single-use even after a failed attempt", False)
    except OAuthError as e:
        check("state is single-use even after a failed attempt", "expired" in str(e))

    with tempfile.TemporaryDirectory() as tmp:
        am = AuthManager(store_path=os.path.join(tmp, "s.json"))
        social = {"provider": "google", "identity": "google:s9", "email": "g@firm.com", "email_verified": True, "name": "G"}
        body = {k: v for k, v in SIGNUP.items() if k != "password"}
        rejects("social sign-up email must match the Google account", lambda: am.signup({**body, "email": "other@firm.com"}, social=social), "must match")
        res = am.signup({**body, "email": "G@firm.com", "social_token": "t"}, social=social)
        user = am.get_user(res["user_id"])
        check("social sign-up links the identity, no password needed", user.identities == ["google:s9"] and user.password_hash)
        check("signup record notes the method", am.get_workspace(res["workspace_id"]).metadata["signup"]["method"] == "google")
        check("find_user_by_identity", am.find_user_by_identity("google:s9").user_id == user.user_id)
        rejects("same Google account cannot sign up twice", lambda: am.signup({**body, "email": "g@firm.com"}, social=social), "already exists")
        other = am.signup({**SIGNUP, "email": "p@firm.com"})
        rejects("identity cannot be linked to a second user", lambda: am.link_identity(am.get_user(other["user_id"]), "google:s9"), "already linked")
        check("identities persisted", AuthManager(store_path=os.path.join(tmp, "s.json")).find_user_by_identity("google:s9") is not None)
        before = (set(am._workspaces), set(am._users))
        orig = am.link_identity
        def boom(u, i): raise RuntimeError("disk full")
        am.link_identity = boom
        try:
            am.signup({**body, "email": "h@firm.com"}, social={**social, "identity": "google:h", "email": "h@firm.com"})
        except RuntimeError:
            pass
        am.link_identity = orig
        check("failed identity link rolls the sign-up back", (set(am._workspaces), set(am._users)) == before)


def _post(base, path, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_onboarding():
    print("\n10. Onboarding hand-off to agent (Task 5.5)")
    import agent.agent_builder as ab
    from agent import onboarding
    profile = normalize_firm_profile({**FULL, "business_hours": {
        **{d: {"open": "09:00", "close": "17:00"} for d in ("mon", "tue", "wed", "thu")},
        "fri": {"open": "09:00", "close": "16:00"}, "sat": None}})
    tools = ["check_availability", "book_appointment", "transfer_call"]
    cfg = onboarding.build_agent_config("Smith & Lee LLP", profile, tools)
    prompt = cfg["system_prompt"]
    check("greeting names the firm and says it is an AI", cfg["first_message"].startswith("Thanks for calling Smith & Lee LLP.") and "AI assistant" in cfg["first_message"])
    check("prompt names firm and practice areas", "Smith & Lee LLP, a law firm practicing personal injury, family law and maritime" in prompt, prompt[:160])
    check("hours grouped with time zone", "Office hours (America/Chicago time): Monday to Thursday 9 AM to 5 PM; Friday 9 AM to 4 PM; closed Saturday and Sunday." in prompt)
    check("after hours transfers to on-call number", "transfer them to +442079460958 using transfer_call" in prompt)
    check("daytime escalation to office phone", "transfer them to +15551234567 using transfer_call" in prompt)
    check("languages listed", "You can speak English, Spanish and Portuguese." in prompt)
    check("practice-area questions added", "For an injury" in prompt and "For a family matter" in prompt and "For a criminal" not in prompt)
    from agent.firm_context import call_instructions, GLOBAL_MARK
    check("legal-advice rule kept out of the stored prompt (backend global rule)", "legal advice" not in prompt.lower())
    check("legal-advice rule added first at call time", call_instructions(prompt).startswith(GLOBAL_MARK)
          and "Never give legal advice" in call_instructions(prompt))
    names = [f["name"] for f in cfg["extraction_fields"]]
    check("intake fields + practice fields", names[:4] == ["caller_name", "callback_number", "email", "case_summary"]
          and "incident_date" in names and "family_matter_type" in names and "charge" not in names, names)
    area = next(f for f in cfg["extraction_fields"] if f["name"] == "practice_area")
    check("practice_area enum uses the firm's areas", area["options"] == ["Personal Injury", "Family Law", "Maritime"], area)
    check("lead emails carried over", cfg["lead_emails"] == ["intake@smithlee.com", "partner@smithlee.com"])
    check("webhook off (endpoints are server-wide)", cfg["webhook_enabled"] is False)
    check("language from first listed", cfg["language"] == "en-US" and onboarding.agent_language(["es"]) == "es-US")
    check("unregistered tools left out", onboarding.build_agent_config("X", profile, ["check_availability"])["tools"] == ["check_availability"])

    take_msg = onboarding.build_agent_config("X", {"after_hours": "take_message"}, tools)["system_prompt"]
    check("take-message after hours", "take a message" in take_msg and "transfer them to +" not in take_msg)
    cb = onboarding.build_agent_config("X", {"after_hours": "book_callback"}, tools)["system_prompt"]
    check("callback after hours uses booking tools", "check_availability, then book_appointment" in cb)
    cb2 = onboarding.build_agent_config("X", {"after_hours": "book_callback"}, ["transfer_call"])["system_prompt"]
    check("callback falls back to message without booking tools", "take a message" in cb2)
    bare = onboarding.build_agent_config("Solo Law", {}, tools)
    check("minimal profile still builds", bare["system_prompt"].startswith("You are the AI intake assistant answering calls for Solo Law, a law firm."))
    check("logo is the site's favicon", onboarding.logo_url("https://smithlee.com/about") == "https://smithlee.com/favicon.ico" and onboarding.logo_url("") == "")

    with tempfile.TemporaryDirectory() as tmp:
        orig_state = ab.STATE_FILE
        ab.STATE_FILE = os.path.join(tmp, "agents.json")
        try:
            builder = ab.AgentBuilder()
            live_before = builder.get_active_agent().agent_id
            ok, errors = builder.validate(dict(cfg))
            check("generated config passes agent validation", ok, errors)
            check("generated prompt has no lint warnings", builder.lint_prompt(prompt) == [], builder.lint_prompt(prompt))
            rejects("bad lead email rejected by builder", lambda: builder.create_agent(name="Bad", first_message="Hi", system_prompt="You are x.", lead_emails=["nope"]), "lead email")

            store = os.path.join(tmp, "auth_store.json")
            am = AuthManager(store_path=store)
            seed = lambda ws_id: onboarding.seed_firm_agent(am, builder, ws_id)
            res = am.signup({k: SIGNUP[k] for k in ("name", "email", "password", "phone")} | {"email": "seed@smithlee.com"})
            user = am.get_user(res["user_id"])
            ws_id = res["workspace_id"]
            step = am.begin_two_step(user, True, purpose="signup")
            am.complete_two_step(step["ticket"], step["code"])
            check("verified account is onboarding-pending", am.onboarding_pending(ws_id))
            check("no agent before onboarding", seed(ws_id) is None and len(builder.list_agents()) == 1)

            details = {**SIGNUP["firm_profile"], "lead_emails": ["intake@smithlee.com"]}
            rejects("onboarding needs a firm name", lambda: am.complete_onboarding(ws_id, " ", details), "law firm name is required")
            rejects("onboarding needs the required details", lambda: am.complete_onboarding(ws_id, "Smith & Lee LLP", {"website": "a.com"}), "office_phone")
            rejects("onboarding rejects bad values", lambda: am.complete_onboarding(ws_id, "Smith & Lee LLP", {**details, "firm_size": "x"}), "firm size")
            rejects("onboarding rejects non-text name", lambda: am.complete_onboarding(ws_id, ["x"], details), "must be text")
            check("failed onboarding changes nothing", am.onboarding_pending(ws_id) and am.get_workspace(ws_id).name == "Dana Smith's firm")
            orig_save = am._save_store
            am._save_store = lambda: False
            try:
                am.complete_onboarding(ws_id, "Smith & Lee LLP", details)
                check("failed save raises", False)
            except RuntimeError:
                check("failed save raises", True)
            am._save_store = orig_save
            check("failed save rolls back", am.onboarding_pending(ws_id) and am.get_workspace(ws_id).name == "Dana Smith's firm")

            am.complete_onboarding(ws_id, "  Smith  & Lee LLP ", details)
            ws = am.get_workspace(ws_id)
            check("onboarding names the workspace", ws.name == "Smith & Lee LLP" and ws.slug.startswith("smith-lee-llp"), (ws.name, ws.slug))
            check("onboarding stores the profile", ws.metadata["firm_profile"]["website"] == "https://smithlee.com")
            check("onboarding marked complete", ws.metadata["onboarding"]["status"] == "complete" and not am.onboarding_pending(ws_id))
            rejects("onboarding can't be repeated", lambda: am.complete_onboarding(ws_id, "Other", details), "already complete")

            rec = seed(ws_id) or {}
            agent = builder.get_agent(rec.get("agent_id", ""))
            check("agent built after onboarding", agent is not None, rec)
            check("seeded agent belongs to firm admin + workspace", agent and agent.owner_id == user.user_id and agent.workspace_id == ws_id)
            check("seeded agent is NOT live", agent and agent.active is False and builder.get_active_agent().agent_id == live_before)
            check("agent named after firm", agent and agent.name == "Smith & Lee LLP Intake")
            check("agent has the lead emails", agent and agent.lead_emails == ["intake@smithlee.com"])
            rec = am.get_workspace(ws_id).metadata["onboarding"]
            check("onboarding record: logo + clio flag + status kept", rec.get("logo_url") == "https://smithlee.com/favicon.ico"
                  and rec.get("clio_selected") is False and rec.get("dismissed") is False and rec.get("status") == "complete", rec)
            check("onboarding record persisted", AuthManager(store_path=store).get_workspace(ws_id).metadata["onboarding"]["agent_id"] == rec["agent_id"])
            check("seeding is idempotent", seed(ws_id) is None and len(builder.list_agents()) == 2)
            builder.update_agent(agent.agent_id, {"workspace_id": "ws-other", "owner_id": "u-x"})
            check("workspace_id/owner_id not editable", builder.get_agent(agent.agent_id).workspace_id == ws_id and builder.get_agent(agent.agent_id).owner_id == user.user_id)

            res2, _ = _pending(am, "approve@smithlee.com")
            am.update_organization(res2["workspace_id"], {"active": True})
            check("super-admin approval leads to onboarding, not an agent", am.onboarding_pending(res2["workspace_id"]) and seed(res2["workspace_id"]) is None)
            org = am.create_organization(name="Manual Org")
            check("super-admin-created org has no onboarding", not am.onboarding_pending(org["workspace_id"]) and seed(org["workspace_id"]) is None)
            rejects("super-admin-created org can't run onboarding", lambda: am.complete_onboarding(org["workspace_id"], "X", details), "already complete")
            rejects("unknown onboarding field rejected", lambda: am.set_onboarding(ws_id, {"agent": "x"}), "unknown")

            print("   -- lead email")
            check("recipients read from agents.json", onboarding._lead_emails_for(agent.agent_id, "") == ["intake@smithlee.com"],
                  onboarding._lead_emails_for(agent.agent_id, ""))
            check("recipients by agent name", onboarding._lead_emails_for("", agent.name) == ["intake@smithlee.com"])
            check("unknown agent -> nobody", onboarding._lead_emails_for("nope", "nope") == [])
            body = onboarding.lead_email_body("Smith & Lee LLP Intake",
                                              {"from_number": "+15550001111", "duration_seconds": 93.4,
                                               "summary": {"executive_summary": "Rear-ended on I-35."}},
                                              {"caller_name": "Jo Park", "incident_date": "2026-09-30", "email": ""})
            check("email body has caller details + summary", "Caller number: +15550001111" in body and "Duration: 93 seconds" in body
                  and "Caller name: Jo Park" in body and "Rear-ended on I-35." in body and "Email:" not in body, body)

            import types
            from agent import mailer
            sent = []
            orig_cfg, orig_send = mailer.configured, mailer.send_email
            mailer.configured = lambda: {"ready": True}
            mailer.send_email = lambda to, subj, text, html="": sent.append((to, subj)) or {"ok": True}
            try:
                job = types.SimpleNamespace(call_id="c1", metadata={"agent_id": agent.agent_id,
                                            "crm_payloads": {"legal_intake": {"client_name": "Jo Park"}}})
                onboarding.notify_leads(job)
                check("lead email sent to each address", sent == [("intake@smithlee.com", "New lead: Jo Park")], sent)
                sent.clear()
                onboarding.notify_leads(types.SimpleNamespace(call_id="c2", metadata={"agent_id": live_before}))
                check("agent without lead emails sends nothing", sent == [])
                mailer.configured = lambda: {"ready": False, "reason": "no transport"}
                onboarding.notify_leads(job)
                check("no mail transport -> skipped quietly", sent == [])
            finally:
                mailer.configured, mailer.send_email = orig_cfg, orig_send
        finally:
            ab.STATE_FILE = orig_state


def test_http():
    base = os.getenv("VERIFY_BASE_URL", "http://localhost:8091").rstrip("/")
    print(f"\n8. Live HTTP ({base})")
    try:
        urllib.request.urlopen(base + "/api/v1/health", timeout=3)
    except Exception:
        print("  [SKIP] no server running; start scripts/serve.py to include these checks")
        return
    def _get(path):
        try:
            with urllib.request.urlopen(base + path, timeout=8) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
    status, raw = _get("/api/v1/auth/signup/options")
    opts = json.loads(raw or b"{}")
    from agent import firm_profile as fp
    check("options endpoint public", status == 200 and "enabled" in opts, f"(status {status})")
    check("options mirror firm_profile choices",
          [o["value"] for o in opts.get("options", {}).get("practice_areas", [])] == list(fp.PRACTICE_AREAS)
          and [o["value"] for o in opts["options"]["firm_sizes"]] == list(fp.FIRM_SIZES)
          and opts.get("required") == list(fp.SIGNUP_REQUIRED_FIELDS))
    status, raw = _get("/signup")
    check("/signup page public", status == 200 and b"Create your account" in raw, f"(status {status})")
    status, raw = _get("/api/v1/system/branding")
    brand = json.loads(raw or b"{}").get("branding", {})
    check("branding readable without signing in", status == 200 and "dashboard_name" in brand)
    # An invalid body: never creates anything, whether sign-up is on or off.
    status, body = _post(base, "/api/v1/auth/signup", {**SIGNUP, "email": "not-an-email", "firm_profile": {}})
    if status == 404:
        check("disabled by default (404 + message)", "not enabled" in body.get("error", ""), body)
        print("  [INFO] SIGNUP_ENABLED is off; restart the server with SIGNUP_ENABLED=1 to test validation over HTTP")
        return
    check("reachable without a session", status != 401, f"(status {status})")
    check("validation error -> 400 with message", status == 400 and body.get("status") == "error" and body.get("error"), body)
    status, body = _post(base, "/api/v1/auth/signup", {"role": "super_admin"})
    check("unknown field rejected over HTTP", status == 400 and "unknown" in body.get("error", "").lower(), body)


def main():
    print("=" * 69)
    print("  PHASE 5: CLIENT SIGN-UP — VERIFICATION SUITE")
    print("=" * 69)
    test_website()
    test_fields()
    test_merge()
    test_auth_manager()
    test_signup()
    test_limiter()
    test_verification()
    test_oauth()
    test_onboarding()
    test_http()
    print("\n" + "=" * 69)
    print(f"  RESULT: {passed}/{passed + failed} checks passed")
    print("=" * 69)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
