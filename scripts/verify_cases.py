#!/usr/bin/env python3
"""scripts/verify_cases.py — Case Desk: case records, staff assignment and the case APIs.

1. CaseManager (offline, throwaway JSON file): create/validate, idempotent per call, assign
   add/remove/set, status, list filters (workspace, staff, status, registration dates), workload,
   unassign on member removal, reload from disk.
2. Live HTTP against an isolated copy of the server (agent/, scripts/, web/ copied to a temp dir,
   DATABASE_URL empty, no .env, loopback trust off, dummy LiveKit credentials). Two firms with a
   Product Admin and two Member Admins each sign in with passwords and exercise
   GET/POST /api/v1/cases*: workspace isolation, Member Admin sees only assigned cases and cannot
   write, staff from another firm cannot be assigned, date/status validation, removal unassigns.

Nothing here touches PostgreSQL, config/ or recordings/ of the real checkout.
Run: python3 scripts/verify_cases.py            (both sections)
     python3 scripts/verify_cases.py --offline  (section 1 only)
"""

import datetime
import http.cookiejar
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

os.environ["DATABASE_URL"] = ""
os.environ["SUPER_ADMIN_EMAIL"] = ""

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from agent.case_manager import CaseManager  # noqa: E402

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


def raises(label, fn, exc=ValueError, needle=""):
    try:
        result = fn()
    except exc as e:
        check(label, needle.lower() in str(e).lower(), f"(error was: {e})")
        return
    check(label, False, f"(accepted: {result!r})")


def day(s):
    return time.mktime(datetime.date.fromisoformat(s).timetuple())


# ── 1. CaseManager ───────────────────────────────────────────────────────────
def offline():
    print("\n1. CaseManager (offline)")
    tmp = tempfile.mkdtemp(prefix="cases-")
    path = os.path.join(tmp, "cases.json")
    cm = CaseManager(path)

    raises("client name required", lambda: cm.create("ws-a", {"client_name": "   "}), needle="client name")
    raises("unknown status rejected", lambda: cm.create("ws-a", {"client_name": "X", "status": "done"}), needle="status")

    a1 = cm.create("ws-a", {"client_name": "  Maria   Lopez ", "case_type": "Personal Injury", "phone": "+15551230001",
                            "registered_at": day("2026-09-01") + 3600}, created_by="u-admin")
    check("whitespace in client name collapsed", a1.client_name == "Maria Lopez", a1.client_name)
    check("defaults: status new, manual source, no staff", (a1.status, a1.source, a1.assigned_staff) == ("new", "manual", []))
    a2 = cm.create("ws-a", {"client_name": "John Park", "call_id": "call-77", "registered_at": day("2026-10-02") + 60}, source="call")
    again = cm.create("ws-a", {"client_name": "John Park (dup)", "call_id": "call-77"}, source="call")
    check("same call_id returns the existing case", again.case_id == a2.case_id and again.client_name == "John Park")
    b1 = cm.create("ws-b", {"client_name": "Other Firm Client"})

    check("list is per workspace", {c["case_id"] for c in cm.list_cases("ws-a")} == {a1.case_id, a2.case_id})
    check("newest registration first", [c["case_id"] for c in cm.list_cases("ws-a")] == [a2.case_id, a1.case_id])
    check("registered_from filter", [c["case_id"] for c in cm.list_cases("ws-a", registered_from=day("2026-10-01"))] == [a2.case_id])
    check("registered_to is exclusive", [c["case_id"] for c in cm.list_cases("ws-a", registered_to=day("2026-10-02"))] == [a1.case_id])

    cm.assign(a1.case_id, ["u-1", "u-2", "u-1"], "add")
    check("add de-duplicates", cm.get(a1.case_id).assigned_staff == ["u-1", "u-2"])
    cm.assign(a1.case_id, ["u-3"], "add")
    cm.assign(a1.case_id, ["u-2"], "remove")
    check("remove keeps the others in order", cm.get(a1.case_id).assigned_staff == ["u-1", "u-3"])
    cm.assign(a2.case_id, ["u-3"], "set")
    check("set replaces the list", cm.get(a2.case_id).assigned_staff == ["u-3"])
    raises("bad mode rejected", lambda: cm.assign(a1.case_id, [], "swap"), needle="mode")
    raises("unknown case is KeyError", lambda: cm.assign("case-nope", ["u-1"]), exc=KeyError)
    raises("staff cap enforced", lambda: cm.assign(a1.case_id, [f"x{i}" for i in range(25)], "set"), needle="at most")

    check("filter by assigned staff", {c["case_id"] for c in cm.list_cases("ws-a", assigned_to="u-3")} == {a1.case_id, a2.case_id})
    check("assigned_to with nobody matches nothing", cm.list_cases("ws-a", assigned_to="") == [])
    cm.set_status(a2.case_id, "closed")
    raises("set_status validates", lambda: cm.set_status(a1.case_id, "archived"), needle="status")
    check("filter by status", [c["case_id"] for c in cm.list_cases("ws-a", status="closed")] == [a2.case_id])
    check("workload counts open cases only", cm.staff_workload("ws-a") == {"u-1": 1, "u-3": 1}, cm.staff_workload("ws-a"))

    check("unassign_everywhere reports changed cases", cm.unassign_everywhere("u-3") == 2)
    check("removed member gone from all cases", all("u-3" not in c["assigned_staff"] for c in cm.list_cases("ws-a")))

    print("\n1b. Registering cases from finished calls")
    gem = lambda **v: {"engine": "gemini", "values": v}
    c = cm.register_from_call("call-ai", {
        "agent_extraction": gem(full_name="Guna Prakash", callback_phone="+919345996500", email_address="guna@example.com",
                                matter_summary="Bought a phone that broke within a week."),
        "crm_payloads": {"legal_intake": {"client_name": "Maya, an AI", "case_type": "with us, or is this"}},
        "summary": {"executive_summary": "Caller asks about a refund."}, "ended_at": day("2026-09-15") + 100})
    check("AI fields win over the regex extractor", c and (c.client_name, c.phone, c.email) == ("Guna Prakash", "+919345996500", "guna@example.com"),
          c and c.to_dict())
    check("AI matter summary used", c and c.summary.startswith("Bought a phone"))
    check("phrase captured as case type is dropped", c and c.case_type == "")
    check("source is call, registered at call end, default workspace",
          c and (c.source, c.registered_at, c.workspace_id) == ("call", day("2026-09-15") + 100, "ws-default"))
    check("second run for the same call returns the same case",
          cm.register_from_call("call-ai", {"agent_extraction": gem(full_name="Someone Else")}).case_id == c.case_id)

    c = cm.register_from_call("call-rx", {"crm_payloads": {"legal_intake": {"client_name": "Arthur Dent and", "case_type": "contract"}},
                                          "summary": {"executive_summary": "Contract dispute with a supplier."}})
    check("regex name trimmed to the person", c and c.client_name == "Arthur Dent", c and c.client_name)
    check("regex enum case type labelled", c and c.case_type == "Contract")
    check("call summary used when no matter summary", c and c.summary == "Contract dispute with a supplier.")
    for label, raw in (("agent greeting", "Maya, an AI"), ("phrase", "not an employee"), ("single word", "Priya"),
                       ("spelled-out", "spelled G-u-n-a")):
        check(f"regex {label} is not a caller name",
              cm.register_from_call(f"call-bad-{label}", {"agent_name": "Maya", "crm_payloads": {"legal_intake": {"client_name": raw}}}) is None)
    check("regex extractor (not gemini) agent values ignored",
          cm.register_from_call("call-regex-agent", {"agent_extraction": {"engine": "regex", "values": {"full_name": "Maya"}}}) is None)
    c = cm.register_from_call("call-phone", {"agent_extraction": gem(callback_phone="+15550001111"), "from_number": "+15559998888"})
    check("phone alone opens an 'Unknown caller' case", c and (c.client_name, c.phone) == ("Unknown caller", "+15550001111"))
    c = cm.register_from_call("call-from", {"agent_extraction": gem(full_name="Ana Ruiz"), "from_number": "+15552223333"})
    check("caller ID fills a missing phone", c and c.phone == "+15552223333")
    check("caller ID alone does not open a case", cm.register_from_call("call-wrong-number", {"from_number": "+15554445555"}) is None)
    c = cm.register_from_call("call-clio", {"agent_extraction": gem(full_name="Peter Parker"),
                                            "clio_manage_sync": {"matter": {"status": "success", "matter_id": 9911}}})
    check("Clio matter id kept", c and c.clio_matter_id == "9911")

    print("\n1c. Two processes sharing one store")
    other = CaseManager(path)
    made = other.create("ws-c", {"client_name": "Made Elsewhere"})
    check("a case written by another process is visible", cm.get(made.case_id) is not None)
    cm.set_status(made.case_id, "on_hold")
    check("and a write here does not drop it", CaseManager(path).get(made.case_id).status == "on_hold")

    reloaded = CaseManager(path)
    check("reload restores every case", {c["case_id"] for c in reloaded.list_cases("ws-a")} == {a1.case_id, a2.case_id}
          and reloaded.get(b1.case_id) is not None)
    check("reload keeps staff and status", reloaded.get(a1.case_id).assigned_staff == ["u-1"] and reloaded.get(a2.case_id).status == "closed")
    shutil.rmtree(tmp, ignore_errors=True)


# ── 2. Live HTTP on an isolated copy ─────────────────────────────────────────
class Client:
    def __init__(self, base):
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw or b"{}")
            except ValueError:
                return e.code, {"raw": raw[:200].decode("utf-8", "replace")}

    def login(self, email, password):
        code, body = self.call("POST", "/api/v1/auth/login", {"email": email, "password": password})
        return code == 200 and body.get("step") == "done"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def live():
    print("\n2. Case APIs over HTTP (isolated server copy)")
    tmp = tempfile.mkdtemp(prefix="cases-live-")
    for d in ("agent", "scripts", "web"):
        shutil.copytree(os.path.join(ROOT_DIR, d), os.path.join(tmp, d),
                        ignore=shutil.ignore_patterns("__pycache__", "audio", "*.mp3", "*.wav"))
    os.makedirs(os.path.join(tmp, "config"))
    os.makedirs(os.path.join(tmp, "recordings"))
    shutil.copy(os.path.join(ROOT_DIR, "config", "agents.json"), os.path.join(tmp, "config", "agents.json"))

    # Seed two firms straight into the copy's auth store before the server reads it.
    sys.path.insert(0, tmp)
    from agent.auth_manager import AuthManager
    am = AuthManager(store_path=os.path.join(tmp, "config", "auth_store.json"))
    pw = "Passw0rd!x"
    wa = am.create_workspace("Alpha Law")
    wb = am.create_workspace("Beta Legal")
    admin_a = am.add_member(wa.workspace_id, "admin@alpha.test", pw, role="admin", name="Alice Admin")
    staff_a1 = am.add_member(wa.workspace_id, "sam@alpha.test", pw, role="member_admin", name="Sam Staff")
    staff_a2 = am.add_member(wa.workspace_id, "tia@alpha.test", pw, role="member_admin", name="Tia Staff")
    admin_b = am.add_member(wb.workspace_id, "admin@beta.test", pw, role="admin", name="Bob Admin")
    staff_b1 = am.add_member(wb.workspace_id, "ben@beta.test", pw, role="member_admin", name="Ben Staff")
    am._save_store()

    port = free_port()
    env = dict(os.environ, DATABASE_URL="", AUTH_TRUST_LOOPBACK="0", SIGNUP_ENABLED="0", SMS_DRY_RUN="1",
               LIVEKIT_URL="ws://127.0.0.1:1", LIVEKIT_API_KEY="dummy", LIVEKIT_API_SECRET="dummy-secret-dummy-secret",
               SUPER_ADMIN_EMAIL="", PYTHONDONTWRITEBYTECODE="1")
    for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"):
        env.pop(k, None)
    log_path = os.path.join(tmp, "server.log")
    proc = subprocess.Popen([sys.executable, "scripts/serve.py", str(port)], cwd=tmp, env=env,
                            stdout=open(log_path, "w"), stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(base + "/api/v1/auth/session", timeout=1)
                break
            except urllib.error.HTTPError:
                break
            except Exception:
                time.sleep(0.25)
        else:
            check("server started", False, open(log_path).read()[-800:])
            return

        anon = Client(base)
        code, _ = anon.call("GET", "/api/v1/cases")
        check("signed-out request refused", code in (401, 302, 303), code)

        A, S1, S2, B, SB = (Client(base) for _ in range(5))
        check("all five users sign in", all([A.login("admin@alpha.test", pw), S1.login("sam@alpha.test", pw),
                                             S2.login("tia@alpha.test", pw), B.login("admin@beta.test", pw),
                                             SB.login("ben@beta.test", pw)]))

        code, body = A.call("POST", "/api/v1/cases", {"client_name": "Maria Lopez", "case_type": "Personal Injury",
                                                      "phone": "+15551230001"})
        check("admin creates a case (201)", code == 201 and body.get("case", {}).get("workspace_id") == wa.workspace_id, (code, body))
        c1 = body.get("case", {}).get("case_id", "")
        code, body = A.call("POST", "/api/v1/cases", {"client_name": "Old Matter", "registered_at": day("2026-01-15") + 60})
        c2 = body.get("case", {}).get("case_id", "")
        code, body = B.call("POST", "/api/v1/cases", {"client_name": "Beta Client", "workspace_id": wa.workspace_id})
        cb = body.get("case", {}).get("case_id", "")
        check("product admin cannot create in another firm (workspace_id ignored)",
              body.get("case", {}).get("workspace_id") == wb.workspace_id, body)
        code, body = A.call("POST", "/api/v1/cases", {"client_name": ""})
        check("missing client name is 400", code == 400, (code, body))

        code, body = S1.call("POST", "/api/v1/cases", {"client_name": "Sneaky"})
        check("member admin cannot create (403)", code == 403, code)

        code, body = A.call("GET", "/api/v1/cases")
        ids = [c["case_id"] for c in body.get("cases", [])]
        check("admin sees own firm's cases only", set(ids) == {c1, c2}, ids)
        staff_ids = {s["user_id"] for s in body.get("staff", [])}
        check("staff list is the admin's firm", {staff_a1.user_id, staff_a2.user_id, admin_a.user_id} <= staff_ids
              and staff_b1.user_id not in staff_ids, staff_ids)

        code, body = A.call("GET", f"/api/v1/cases/{cb}")
        check("other firm's case is 404 by id", code == 404, code)
        code, body = A.call("GET", f"/api/v1/cases?workspace_id={wb.workspace_id}")
        check("product admin cannot switch workspace by query", {c["case_id"] for c in body.get("cases", [])} == {c1, c2})

        code, body = A.call("POST", "/api/v1/cases/assign", {"case_id": c1, "user_ids": [staff_a1.user_id, staff_a2.user_id]})
        check("assign two staff", code == 200 and [s["name"] for s in body["case"]["staff"]] == ["Sam Staff", "Tia Staff"], (code, body))
        code, body = A.call("POST", "/api/v1/cases/assign", {"case_id": c1, "user_ids": [staff_b1.user_id]})
        check("staff from another firm rejected (400)", code == 400 and "not a member" in body.get("error", ""), (code, body))
        code, body = A.call("POST", "/api/v1/cases/assign", {"case_id": c1, "user_ids": "nope"})
        check("user_ids must be a list", code == 400, code)
        code, body = B.call("POST", "/api/v1/cases/assign", {"case_id": c1, "user_ids": [staff_b1.user_id]})
        check("other firm's admin cannot assign on our case (404)", code == 404, code)
        code, body = S1.call("POST", "/api/v1/cases/assign", {"case_id": c1, "user_ids": [staff_a1.user_id]})
        check("member admin cannot assign (403)", code == 403, code)
        code, body = S1.call("POST", "/api/v1/cases/status", {"case_id": c1, "status": "closed"})
        check("member admin cannot change status (403)", code == 403, code)

        code, body = A.call("POST", "/api/v1/cases/status", {"case_id": c1, "status": "in_progress"})
        check("admin changes status", code == 200 and body["case"]["status"] == "in_progress", (code, body))
        code, body = A.call("POST", "/api/v1/cases/status", {"case_id": c1, "status": "archived"})
        check("unknown status is 400", code == 400, code)

        code, body = S1.call("GET", "/api/v1/cases")
        check("member admin sees only assigned cases", [c["case_id"] for c in body.get("cases", [])] == [c1], body)
        check("member admin gets no staff list", "staff" not in body)
        code, body = S1.call("GET", f"/api/v1/cases?assigned_to={staff_a2.user_id}")
        check("member admin cannot widen with assigned_to", [c["case_id"] for c in body.get("cases", [])] == [c1])
        code, _ = S1.call("GET", f"/api/v1/cases/{c2}")
        check("unassigned case by id is 404 for member admin", code == 404, code)
        code, _ = S1.call("GET", f"/api/v1/cases/{c1}")
        check("assigned case by id is 200 for member admin", code == 200, code)

        code, body = A.call("GET", "/api/v1/cases?from=2026-01-15&to=2026-01-15")
        check("date range is inclusive of the end day", [c["case_id"] for c in body.get("cases", [])] == [c2], body)
        code, body = A.call("GET", "/api/v1/cases?from=15/01/2026")
        check("bad date is 400", code == 400, code)
        code, body = A.call("GET", "/api/v1/cases?status=in_progress")
        check("status filter", [c["case_id"] for c in body.get("cases", [])] == [c1])
        code, body = A.call("GET", "/api/v1/cases")
        load = {s["user_id"]: s["open_cases"] for s in body.get("staff", [])}
        check("workload in staff list", load.get(staff_a1.user_id) == 1 and load.get(staff_a2.user_id) == 1, load)

        code, body = A.call("POST", "/api/v1/auth/users", {"action": "remove", "user_id": staff_a2.user_id})
        check("admin removes a member", code == 200 and body.get("removed"), (code, body))
        code, body = A.call("GET", f"/api/v1/cases/{c1}")
        check("removed member unassigned from cases", [s["user_id"] for s in body["case"]["staff"]] == [staff_a1.user_id], body)

        print("\n2b. Team page API")
        code, body = A.call("GET", "/api/v1/team")
        emails = {m["email"] for m in body.get("members", [])}
        check("admin team list is own firm, removed member gone", code == 200 and emails == {"admin@alpha.test", "sam@alpha.test"}, (code, emails))
        sam = next((m for m in body.get("members", []) if m["email"] == "sam@alpha.test"), {})
        check("case load per member", (sam.get("open_cases"), sam.get("total_cases")) == (1, 1), sam)
        me = next((m for m in body.get("members", []) if m["email"] == "admin@alpha.test"), {})
        check("viewer marked is_me and cannot remove self", me.get("is_me") and not me.get("can_remove"), me)
        check("Product Admin may add Member Admins only", body.get("can_add_roles") == ["member_admin"], body.get("can_add_roles"))
        code, _ = S1.call("GET", "/api/v1/team")
        check("member admin cannot read the team list (403)", code == 403, code)
        req = urllib.request.Request(base + "/team")
        try:
            S1.opener.open(req, timeout=5)
            landed = ""
        except urllib.error.HTTPError as e:
            landed = str(e.code)
        page = S1.opener.open(urllib.request.Request(base + "/team"), timeout=5)
        check("member admin is sent away from /team", "denied=admin" in page.geturl() or landed in ("302", "403"), page.geturl())

        code, body = A.call("POST", "/api/v1/auth/users", {"name": "Lena Paralegal", "email": "lena@alpha.test", "password": pw,
                                                           "title": "  Senior   Paralegal ", "role": "member_admin"})
        check("admin adds staff with a job title", code == 200 and body.get("user", {}).get("title") == "Senior Paralegal", (code, body))
        lena = body.get("user", {}).get("user_id", "")
        code, body = A.call("POST", "/api/v1/auth/users", {"name": "X", "email": "x@alpha.test", "password": pw, "role": "admin"})
        check("Product Admin cannot add a Product Admin (403)", code == 403, code)
        code, body = A.call("POST", "/api/v1/cases/assign", {"case_id": c1, "user_ids": [lena], "mode": "add"})
        check("job title shown on case staff", any(s.get("title") == "Senior Paralegal" for s in body.get("case", {}).get("staff", [])), body)
        check("new staff member can sign in", Client(base).login("lena@alpha.test", pw))
        code, body = B.call("POST", "/api/v1/auth/users", {"action": "remove", "user_id": lena})
        check("other firm's admin removing our staff is 403, not a server error", code == 403, (code, body))

        check("cases saved to the copy's config/cases.json", os.path.isfile(os.path.join(tmp, "config", "cases.json")))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        if failed or sys.exc_info()[0]:
            print(f"  server log kept at {log_path}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    offline()
    if "--offline" not in sys.argv:
        live()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
