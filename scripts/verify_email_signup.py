#!/usr/bin/env python3
"""scripts/verify_email_signup.py — new sign-ups confirm their email address with a code (no approval step).

1. Offline (throwaway auth store): email tickets go to the account's address, last 30 minutes, are
   masked for display, survive a resend with their channel, activate the workspace on the right code,
   and refuse wrong / reused codes.
2. HTTP on isolated server copies:
   a) MAIL_DRY_RUN=1, no SMS: sign-up returns an email code step; the code activates the account and
      signs in; signing in again before verifying sends a fresh email code; resend keeps the channel.
   b) neither email nor SMS set up: sign-up says the email can't be sent (no approval wording) and
      sign-in for that account answers 503 with the same message.
Never touches PostgreSQL, config/ or a real mailbox.
"""

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

from agent.auth_manager import AuthManager  # noqa: E402

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


SIGNUP = {"name": "Guna Prakash", "email": "gunaprakash575@example.com", "password": "correct-horse-battery",
          "phone": "555-201-3344", "firm_name": "Prakash Law",
          "firm_profile": {"website": "prakashlaw.com", "office_phone": "5551234567", "timezone": "America/Chicago", "firm_size": "solo"}}


def offline():
    print("\n1. Email codes (offline)")
    am = AuthManager(store_path=os.path.join(tempfile.mkdtemp(prefix="email-"), "auth_store.json"))
    res = am.signup(SIGNUP, source_ip="203.0.113.9")
    user = am.get_user(res["user_id"])
    check("new sign-up starts pending", am.is_signup_pending(user.workspace_id))
    step = am.begin_two_step(user, True, purpose="signup", channel="email")
    check("code goes to the account's email", step["channel"] == "email" and step["dest"] == SIGNUP["email"])
    check("email code lasts 30 minutes", step["expires_in"] == 1800)
    check("address masked for display", step["dest_masked"] == "gu••••••••••75@example.com", step["dest_masked"])
    check("short addresses masked too", am.mask_email("ab@x.com") == "a•@x.com" and am.mask_email("abcde@x.com") == "ab•de@x.com")
    try:
        am.complete_two_step(step["ticket"], "000000" if step["code"] != "000000" else "111111")
        check("wrong code refused", False)
    except ValueError as e:
        check("wrong code refused", "incorrect" in str(e).lower())
    am._tickets()[__import__("hashlib").sha256(step["ticket"].encode()).hexdigest()]["sent_at"] -= 120
    again = am.resend_code(step["ticket"])
    check("resend keeps the email channel and address", again["channel"] == "email" and again["dest"] == SIGNUP["email"] and again["expires_in"] == 1800)
    token, _ = am.complete_two_step(step["ticket"], again["code"])
    check("right code signs in", bool(token))
    ws = am.get_workspace(user.workspace_id)
    check("workspace activated, recorded as verified by email", ws.active and ws.metadata["signup"].get("resolved_via") == "email", ws.metadata.get("signup"))
    try:
        am.complete_two_step(step["ticket"], again["code"])
        check("used code can't be reused", False)
    except ValueError:
        check("used code can't be reused", True)
    try:
        am.begin_two_step(user, True, purpose="signup", channel="email")
        check("verified account can't start sign-up verification again", False)
    except ValueError:
        check("verified account can't start sign-up verification again", True)
    sms = am.begin_two_step(am.signup({**SIGNUP, "email": "sms@example.com"}) and am._find_user_by_email("sms@example.com"), True, purpose="signup")
    check("SMS stays the default channel (sign-in codes unchanged)", sms["channel"] == "sms" and sms["expires_in"] == 300)


class Client:
    def __init__(self, base):
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=20) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def server(extra_env):
    tmp = tempfile.mkdtemp(prefix="email-live-")
    for d in ("agent", "scripts", "web"):
        shutil.copytree(os.path.join(ROOT_DIR, d), os.path.join(tmp, d), ignore=shutil.ignore_patterns("__pycache__", "audio", "*.mp3", "*.wav"))
    os.makedirs(os.path.join(tmp, "config"))
    os.makedirs(os.path.join(tmp, "recordings"))
    shutil.copy(os.path.join(ROOT_DIR, "config", "agents.json"), os.path.join(tmp, "config", "agents.json"))
    port = free_port()
    env = dict(os.environ, DATABASE_URL="", AUTH_TRUST_LOOPBACK="0", SIGNUP_ENABLED="1", SMS_DRY_RUN="",
               LIVEKIT_URL="ws://127.0.0.1:1", LIVEKIT_API_KEY="d", LIVEKIT_API_SECRET="dummy-secret-dummy-secret", **extra_env)
    for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "RESEND_API_KEY", "SMTP_HOST", "MAIL_FROM"):
        env.pop(k, None)
    log = open(os.path.join(tmp, "server.log"), "w")
    proc = subprocess.Popen([sys.executable, "scripts/serve.py", str(port)], cwd=tmp, env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            urllib.request.urlopen(base + "/login", timeout=1)
            break
        except Exception:
            time.sleep(0.25)
    return proc, base, tmp


def stop(proc, tmp):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    if failed:
        print(f"  server log kept at {tmp}/server.log")
    else:
        shutil.rmtree(tmp, ignore_errors=True)


def live_dry_run():
    print("\n2a. HTTP: email in local test mode (MAIL_DRY_RUN=1), no SMS")
    proc, base, tmp = server({"MAIL_DRY_RUN": "1"})
    try:
        c = Client(base)
        code, opts = c.call("GET", "/api/v1/auth/signup/options")
        check("sign-up page told verification is by email", opts.get("verification") == "email", opts.get("verification"))
        code, body = c.call("POST", "/api/v1/auth/signup", SIGNUP)
        check("sign-up returns the email code step", code == 201 and body.get("step") == "otp" and body.get("channel") == "email", (code, body))
        check("masked address shown, code valid 30 min", body.get("dest_masked", "").endswith("@example.com") and body.get("expires_in") == 1800)
        check("no approval wording anywhere", "approv" not in json.dumps(body).lower())
        code, me = c.call("GET", "/api/v1/auth/session")
        check("not signed in before verifying", not me.get("authenticated"), me)
        code, r = c.call("POST", "/api/v1/auth/otp/verify", {"ticket": body["ticket"], "code": "000000" if body["dry_run_code"] != "000000" else "111111"})
        check("wrong code is 401", code == 401, code)
        code, r = c.call("POST", "/api/v1/auth/otp/verify", {"ticket": body["ticket"], "code": body["dry_run_code"]})
        check("right code signs in", code == 200 and r.get("step") == "done", (code, r))
        code, me = c.call("GET", "/api/v1/auth/session")
        check("session active after verifying", me.get("authenticated") is True, me)

        c2 = Client(base)
        code, body2 = c2.call("POST", "/api/v1/auth/signup", {**SIGNUP, "email": "later@example.com"})
        check("second sign-up also gets an email code", body2.get("channel") == "email")
        c3 = Client(base)
        code, login = c3.call("POST", "/api/v1/auth/login", {"email": "later@example.com", "password": SIGNUP["password"]})
        check("signing in before verifying sends a fresh email code", code == 200 and login.get("step") == "otp" and login.get("channel") == "email", (code, login))
        code, rs = c3.call("POST", "/api/v1/auth/otp/resend", {"ticket": login["ticket"]})
        check("resend too soon is refused (429)", code == 429, code)
        code, r = c3.call("POST", "/api/v1/auth/otp/verify", {"ticket": login["ticket"], "code": login["dry_run_code"]})
        check("code from the sign-in email activates the account", code == 200 and r.get("step") == "done", (code, r))
        with open(os.path.join(tmp, "server.log")) as f:
            check("dry-run code logged, nothing emailed", "MAIL_DRY_RUN: verification code" in f.read())
    finally:
        stop(proc, tmp)


def live_unavailable():
    print("\n2b. HTTP: neither email nor SMS set up")
    proc, base, tmp = server({"MAIL_DRY_RUN": ""})
    try:
        c = Client(base)
        code, opts = c.call("GET", "/api/v1/auth/signup/options")
        check("sign-up page told verification is unavailable", opts.get("verification") == "unavailable", opts.get("verification"))
        code, body = c.call("POST", "/api/v1/auth/signup", {**SIGNUP, "email": "nomail@example.com"})
        check("sign-up explains the email can't be sent", code == 201 and body.get("step") == "verification_unavailable"
              and "verification email" in body.get("message", ""), (code, body))
        check("no approval wording", "approv" not in json.dumps(body).lower())
        code, login = Client(base).call("POST", "/api/v1/auth/login", {"email": "nomail@example.com", "password": SIGNUP["password"]})
        check("sign-in for that account is 503 with the same message", code == 503 and "verification email" in login.get("error", ""), (code, login))
    finally:
        stop(proc, tmp)


if __name__ == "__main__":
    offline()
    if "--offline" not in sys.argv:
        live_dry_run()
        live_unavailable()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
