#!/usr/bin/env python3
"""scripts/verify_email_settings.py — the Super Admin Email delivery page (/email-settings).

Runs an isolated copy of the server (its own .env, auth store and config in a temp dir) plus a tiny local
SMTP server that records what it receives:
  * only the Super Admin can read or change the settings or send a test;
  * bad input is refused with a plain message (transport, sender, host, port, security, Resend key);
  * SMTP settings are saved to the copy's .env, used at once, and the password is never sent back;
  * a test email arrives at the local SMTP server; a dead server reports the failure;
  * switching to Resend clears SMTP (and back), Off disables sending, and sign-up then reports
    verification as unavailable / email accordingly.
Never touches the real .env, PostgreSQL or a real mail server.
"""

import http.cookiejar
import json
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeSMTP:
    """Just enough SMTP (no auth, no TLS) to accept one message per connection and keep it."""
    def __init__(self):
        box = self.messages = []

        class H(socketserver.StreamRequestHandler):
            def handle(self):
                w = lambda line: (self.wfile.write((line + "\r\n").encode()), self.wfile.flush())
                w("220 fake ESMTP")
                rcpt, data_mode, lines = [], False, []
                while True:
                    raw = self.rfile.readline()
                    if not raw:
                        return
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if data_mode:
                        if line == ".":
                            box.append({"to": rcpt, "data": "\n".join(lines)})
                            data_mode, lines = False, []
                            w("250 queued")
                        else:
                            lines.append(line)
                        continue
                    cmd = line[:4].upper()
                    if cmd in ("EHLO", "HELO"):
                        w("250 fake")
                    elif cmd == "MAIL":
                        w("250 ok")
                    elif cmd == "RCPT":
                        rcpt.append(line.split(":", 1)[1].strip(" <>"))
                        w("250 ok")
                    elif cmd == "DATA":
                        data_mode = True
                        w("354 go")
                    elif cmd == "QUIT":
                        w("221 bye")
                        return
                    else:
                        w("250 ok")

        self.port = free_port()
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", self.port), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


class Client:
    def __init__(self, base):
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}

    def login(self, email, pw):
        code, body = self.call("POST", "/api/v1/auth/login", {"email": email, "password": pw})
        return code == 200 and body.get("step") == "done"


def main():
    print("\nEmail delivery page (isolated server copy + local SMTP)")
    tmp = tempfile.mkdtemp(prefix="email-settings-")
    for d in ("agent", "scripts", "web"):
        shutil.copytree(os.path.join(ROOT_DIR, d), os.path.join(tmp, d), ignore=shutil.ignore_patterns("__pycache__", "audio", "*.mp3", "*.wav"))
    os.makedirs(os.path.join(tmp, "config"))
    os.makedirs(os.path.join(tmp, "recordings"))
    shutil.copy(os.path.join(ROOT_DIR, "config", "agents.json"), os.path.join(tmp, "config", "agents.json"))
    pw = "Passw0rd!x"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SMTP_", "RESEND_", "MAIL_", "TWILIO_"))}
    env.update(DATABASE_URL="", AUTH_TRUST_LOOPBACK="0", SIGNUP_ENABLED="1", SUPER_ADMIN_EMAIL="root@platform.test",
               SUPER_ADMIN_PASSWORD=pw, LIVEKIT_URL="ws://127.0.0.1:1", LIVEKIT_API_KEY="d", LIVEKIT_API_SECRET="dummy-secret-dummy-secret")
    sys.path.insert(0, tmp)
    os.environ.update(DATABASE_URL="", SUPER_ADMIN_EMAIL="root@platform.test", SUPER_ADMIN_PASSWORD=pw)
    from agent.auth_manager import AuthManager
    am = AuthManager(store_path=os.path.join(tmp, "config", "auth_store.json"))
    am.create_organization("Firm", admin_email="admin@firm.test", admin_password=pw)
    am._save_store()
    smtp = FakeSMTP()
    port = free_port()
    proc = subprocess.Popen([sys.executable, "scripts/serve.py", str(port)], cwd=tmp, env=env,
                            stdout=open(os.path.join(tmp, "server.log"), "w"), stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(base + "/login", timeout=1)
                break
            except Exception:
                time.sleep(0.25)
        S, A = Client(base), Client(base)
        check("Super Admin and Product Admin sign in", S.login("root@platform.test", pw) and A.login("admin@firm.test", pw))
        for method, path in (("GET", "/api/v1/system/email"), ("POST", "/api/v1/system/email"), ("POST", "/api/v1/system/email/test")):
            code, _ = A.call(method, path, {} if method == "POST" else None)
            check(f"Product Admin refused: {method} {path}", code == 403, code)
        try:
            page = A.opener.open(urllib.request.Request(base + "/email-settings"), timeout=5)
            check("Product Admin is sent away from the page", "denied=super_admin" in page.geturl(), page.geturl())
        except urllib.error.HTTPError as e:
            check("Product Admin is sent away from the page", e.code in (302, 403), e.code)

        code, s = S.call("GET", "/api/v1/system/email")
        check("starts not set up", code == 200 and s["ready"] is False and s["transport"] == "off", s)
        code, opts = S.call("GET", "/api/v1/auth/signup/options")
        check("sign-up verification unavailable while email is off", opts.get("verification") == "unavailable", opts.get("verification"))

        good = {"transport": "smtp", "mail_from": "Call Desk <desk@firm.test>", "smtp_host": "127.0.0.1", "smtp_port": str(smtp.port), "smtp_tls": "auto"}
        for bad, needle in ((dict(good, transport="pigeon"), "choose"), (dict(good, mail_from="not an address"), "sender"),
                            (dict(good, smtp_host="no spaces allowed"), "smtp server"), (dict(good, smtp_port="99999"), "port"),
                            (dict(good, smtp_tls="weird"), "security"), ({"transport": "resend", "mail_from": "a@b.co", "resend_api_key": "sk_x"}, "re_"),
                            ({"transport": "resend", "mail_from": "a@b.co"}, "resend api key")):
            code, body = S.call("POST", "/api/v1/system/email", bad)
            check(f"refused: {needle}", code == 400 and needle in body.get("error", "").lower(), (code, body))

        code, s = S.call("POST", "/api/v1/system/email", dict(good, smtp_user="desk@firm.test", smtp_password="s3cret-app-pass"))
        check("SMTP settings saved and email is ready", code == 200 and s["ready"] is True and s["transport"] == "smtp", s)
        check("password reported as saved, never sent back", s["settings"]["smtp_password_set"] is True and "s3cret" not in json.dumps(s))
        envfile = open(os.path.join(tmp, ".env")).read()
        check("written to the server's .env", f"SMTP_HOST=127.0.0.1" in envfile and f"SMTP_PORT={smtp.port}" in envfile and "MAIL_FROM=Call Desk <desk@firm.test>" in envfile)
        code, s = S.call("POST", "/api/v1/system/email", dict(good, smtp_user="desk@firm.test"))
        check("saving without a password keeps the saved one", s["settings"]["smtp_password_set"] is True)
        code, opts = S.call("GET", "/api/v1/auth/signup/options")
        check("sign-up now verifies by email", opts.get("verification") == "email", opts.get("verification"))

        # The fake server offers no STARTTLS and credentials over plaintext are refused by design, so the
        # test runs without a login; dropping the username also drops the saved password.
        code, s = S.call("POST", "/api/v1/system/email", dict(good, smtp_user=""))
        check("no username clears the saved password", s["settings"]["smtp_password_set"] is False, s["settings"])
        code, r = S.call("POST", "/api/v1/system/email/test", {"to": "someone@example.com"})
        check("test email sent", code == 200 and r.get("transport") == "smtp", (code, r))
        time.sleep(0.3)
        check("test email arrived at the SMTP server", any("someone@example.com" in m["to"] and "Call Desk test email" in m["data"] for m in smtp.messages), smtp.messages)
        code, r = S.call("POST", "/api/v1/system/email/test", {"to": "nope"})
        check("test needs a valid address", code == 400, code)

        dead = free_port()
        S.call("POST", "/api/v1/system/email", dict(good, smtp_port=str(dead)))
        code, r = S.call("POST", "/api/v1/system/email/test", {"to": "someone@example.com"})
        check("dead SMTP server reported as not sent (502)", code == 502 and r.get("error"), (code, r))

        code, s = S.call("POST", "/api/v1/system/email", {"transport": "resend", "mail_from": "desk@firm.test", "resend_api_key": "re_abcdefghijklmnop"})
        check("switch to Resend: ready, SMTP cleared, key masked", code == 200 and s["transport"] == "resend" and s["settings"]["smtp_host"] == ""
              and s["settings"]["resend_key_set"] and "abcdefghijklmnop" not in json.dumps(s), s)
        code, s = S.call("POST", "/api/v1/system/email", dict(good))
        check("switch back to SMTP clears Resend", s["transport"] == "smtp" and not s["settings"]["resend_key_set"], s)
        code, s = S.call("POST", "/api/v1/system/email", {"transport": "off"})
        check("Off: not ready, nothing set", s["ready"] is False and s["transport"] == "off", s)
        code, r = S.call("POST", "/api/v1/system/email/test", {"to": "someone@example.com"})
        check("test while off explains why", code == 502 and "no email transport" in r.get("error", "").lower(), (code, r))
        codes = [S.call("POST", "/api/v1/system/email/test", {"to": "x@example.com"})[0] for _ in range(4)]
        check("test emails are rate-limited", 429 in codes, codes)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        smtp.server.shutdown()
        if failed:
            print(f"  server log kept at {tmp}/server.log")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
