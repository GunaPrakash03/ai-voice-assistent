#!/usr/bin/env python3
"""scripts/verify_trunk_validation.py — SIP trunk create validation and marketplace carrier counts.

Runs an isolated copy of the server (its own config in a temp dir, no carrier keys, no PostgreSQL):
  * POST /api/telephony/trunks/inbound refuses an empty payload, a missing name and an empty number list,
    and still accepts a proper trunk (whose DID then shows as an owned number);
  * inbound/outbound/unified trunks can be deleted;
  * the Telnyx / Twilio chip counts on the number marketplace follow the country and search filters.
Never touches the real config, .env, PostgreSQL or a carrier API.
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
import urllib.parse
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


def counts(client, carrier, country="", search=""):
    q = urllib.parse.urlencode({"carrier": carrier, "country": country, "search": search})
    code, body = client.call("GET", "/api/telephony/numbers/available?" + q)
    chips = {c["carrier"]: c["available"] for c in body.get("carriers", [])}
    return code, chips, body.get("available", [])


def main():
    print("\nSIP trunk validation + marketplace counts (isolated server copy)")
    tmp = tempfile.mkdtemp(prefix="trunk-validation-")
    for d in ("agent", "scripts", "web"):
        shutil.copytree(os.path.join(ROOT_DIR, d), os.path.join(tmp, d), ignore=shutil.ignore_patterns("__pycache__", "audio", "*.mp3", "*.wav"))
    os.makedirs(os.path.join(tmp, "config"))
    os.makedirs(os.path.join(tmp, "recordings"))
    shutil.copy(os.path.join(ROOT_DIR, "config", "agents.json"), os.path.join(tmp, "config", "agents.json"))
    pw = "Passw0rd!x"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SMTP_", "RESEND_", "MAIL_", "TWILIO_", "TELNYX_"))}
    env.update(DATABASE_URL="", AUTH_TRUST_LOOPBACK="0", SUPER_ADMIN_EMAIL="root@platform.test", SUPER_ADMIN_PASSWORD=pw,
               LIVEKIT_URL="ws://127.0.0.1:1", LIVEKIT_API_KEY="d", LIVEKIT_API_SECRET="dummy-secret-dummy-secret")
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
        S = Client(base)
        check("Super Admin signs in", S.login("root@platform.test", pw))

        # ── Inbound trunk validation
        code, body = S.call("GET", "/api/telephony/trunks")
        before = len(body.get("inbound", []))
        code, body = S.call("POST", "/api/telephony/trunks/inbound", {})
        errs = " ".join(body.get("errors", [])).lower()
        check("empty payload refused (400)", code == 400, (code, body))
        check("  ...says a name is required", "name is required" in errs, errs)
        check("  ...says a phone number is required", "at least one phone number" in errs, errs)
        code, body = S.call("GET", "/api/telephony/trunks")
        check("  ...and no trunk was created", len(body.get("inbound", [])) == before, (before, len(body.get("inbound", []))))

        code, body = S.call("POST", "/api/telephony/trunks/inbound", {"trunk_id": "in-noname", "numbers": ["+14155550123"]})
        check("missing name refused", code == 400 and "name is required" in body.get("error", ""), (code, body))
        code, body = S.call("POST", "/api/telephony/trunks/inbound", {"trunk_id": "in-nonums", "name": "No numbers", "numbers": []})
        check("empty number list refused", code == 400 and "at least one phone number" in body.get("error", ""), (code, body))
        code, body = S.call("POST", "/api/telephony/trunks/inbound", {"trunk_id": "in-bad", "name": "Bad", "numbers": ["12"]})
        check("bad number refused (named in the error)", code == 400 and "invalid phone number" in body.get("error", "") and "12" in body.get("error", ""), (code, body))

        good = {"trunk_id": "in-good", "name": "Front desk", "numbers": ["+14155550123"], "allowed_addresses": ["203.0.113.0/24"]}
        code, body = S.call("POST", "/api/telephony/trunks/inbound", good)
        check("proper inbound trunk created", code == 200 and body.get("status") == "ok", (code, body))
        check("  ...its DID is provisioned as an owned number", "+14155550123" in body.get("provisioned_numbers", []), body)
        code, body = S.call("POST", "/api/telephony/trunks/inbound", good)
        check("same id again refused (409)", code == 409, code)

        # ── Delete routes
        code, body = S.call("POST", "/api/telephony/trunks/inbound/delete", {"trunk_id": "in-good"})
        check("inbound trunk deleted", code == 200 and body.get("deleted") is True, (code, body))
        code, body = S.call("POST", "/api/telephony/trunks/inbound/delete", {"trunk_id": "in-good"})
        check("deleting it again is 404", code == 404, code)
        code, body = S.call("POST", "/api/telephony/trunks/outbound",
                            {"trunk_id": "out-t", "name": "Out", "address": "sip.telnyx.com:5060", "numbers": ["+14155550124"]})
        check("outbound trunk created", code == 200, (code, body))
        code, body = S.call("POST", "/api/telephony/trunks/outbound/delete", {"trunk_id": "out-t"})
        check("outbound trunk deleted", code == 200 and body.get("deleted") is True, (code, body))
        code, body = S.call("POST", "/api/telephony/trunks", {"trunk_id": "uni-t", "name": "Both ways", "numbers": ["+14155550125"]})
        check("unified trunk created", code == 200, (code, body))
        code, body = S.call("POST", "/api/telephony/trunks/delete", {"trunk_id": "uni-t"})
        check("unified trunk deleted", code == 200 and body.get("deleted") is True, (code, body))

        # ── Marketplace chip counts follow the filters (catalog only: no carrier keys in this copy)
        # The demo setup already owns two Telnyx toll-free DIDs, so they drop out of the counts.
        code, chips_all, _ = counts(S, "telnyx", country="ALL")
        check("all countries: Telnyx 6, Twilio 8", code == 200 and chips_all == {"telnyx": 6, "twilio": 8}, chips_all)
        code, chips_us, listed = counts(S, "telnyx", country="US")
        check("US: Telnyx 4, Twilio 5", chips_us == {"telnyx": 4, "twilio": 5}, chips_us)
        check("  ...Telnyx chip equals the Telnyx cards listed", chips_us["telnyx"] == len(listed), (chips_us, len(listed)))
        code, chips_gb, listed = counts(S, "twilio", country="GB")
        check("GB: Telnyx 1, Twilio 1", chips_gb == {"telnyx": 1, "twilio": 1}, chips_gb)
        code, chips_de, _ = counts(S, "twilio", country="DE")
        check("DE: Telnyx 1, Twilio 0", chips_de == {"telnyx": 1, "twilio": 0}, chips_de)
        code, chips_s, listed = counts(S, "twilio", country="US", search="boston")
        check("US + search 'boston': Twilio 1, Telnyx 0", chips_s == {"telnyx": 0, "twilio": 1} and len(listed) == 1, (chips_s, len(listed)))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if failed:
            print("  server log:", os.path.join(tmp, "server.log"))
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{passed}/{passed + failed} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
