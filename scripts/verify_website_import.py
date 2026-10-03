#!/usr/bin/env python3
"""scripts/verify_website_import.py — firm details from the website (docs/FIRM_WEBSITE_IMPORT_PLAN.md).

1. Website reader against a local test site: page picking, broken / slow / redirecting / robots-blocked
   pages, size cap, non-HTML.
2. Address safety: private, loopback, link-local, IPv4-mapped and mixed DNS answers refused; IP literals,
   odd ports, credentials and non-http schemes refused.
3. Turning pages into details: Gemini stand-in (capping, de-duplication, area mapping), failure and
   empty answers fall back to headings.
4. Firm profile fields: validation and limits.
5. Receptionist script: firm block built, capped, and rewritten in place without touching other edits.
6. HTTP on an isolated server copy: who may call the draft / firm-details endpoints, bad input, the
   per-firm limit, saving details.

Offline except section 6, which starts its own copy of the server; never touches PostgreSQL, config/ or
the network beyond 127.0.0.1.
"""

import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

os.environ["DATABASE_URL"] = ""
os.environ["SUPER_ADMIN_EMAIL"] = ""
os.environ["GEMINI_API_KEY"] = ""

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

import agent.website_scraper as ws  # noqa: E402
import agent.website_details as wd  # noqa: E402
from agent.firm_profile import normalize_firm_profile  # noqa: E402

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


def raises(label, fn, exc, needle=""):
    try:
        result = fn()
    except exc as e:
        check(label, needle.lower() in str(e).lower(), f"(error was: {e})")
        return
    check(label, False, f"(accepted: {result!r})")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── Local test website ───────────────────────────────────────────────────────
HOME = """<html><head><title>Roe &amp; Park LLP</title><meta name="description" content="Family and injury lawyers in Austin."></head>
<body><nav><a href="/about-us">About Us</a> <a href="/attorneys">Our Attorneys</a> <a href="/practice-areas">Practice Areas</a>
<a href="/blog">Blog</a> <a href="https://other.test/attorneys">Partner site</a> <a href="/brochure.pdf">Brochure</a>
<a href="mailto:x@roe.test">Email</a> <a href="/team-bios">Team bios</a> <a href="/lawyers">Lawyers</a>
<a href="/services">Services</a> <a href="/our-story">Our story</a> <a href="#top">Top</a></nav>
<script>var hidden = "Do not read me";</script><h1>Roe &amp; Park</h1><p>Welcome.</p></body></html>"""
PAGES = {
    "/about-us": "<h1>About us</h1><p>Roe &amp; Park LLP has represented families and injured workers across Central Texas since 1998, from our office on Congress Avenue in Austin.</p>",
    "/attorneys": "<h1>Our Attorneys</h1><h3>Jane Roe</h3><p>Founding Partner</p><p>Jane handles divorce.</p><h3>Sam Park, Esq.</h3><p>Associate Attorney</p><h3>Contact Us Today</h3>",
    "/practice-areas": "<h1>Practice Areas</h1><h2>Divorce &amp; Custody</h2><p>We guide parents through custody.</p><h2>Car Accidents</h2><p>Injured in a crash?</p>",
}


class Site:
    def __init__(self, robots="User-agent: *\nDisallow: /team-bios\n"):
        site = self
        self.robots = robots
        self.hits = []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
                raw = body.encode() if isinstance(body, str) else body
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(raw)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):
                site.hits.append(self.path)
                p = self.path.split("?")[0]
                if p == "/robots.txt":
                    return self._send(200, site.robots, "text/plain") if site.robots is not None else self._send(404, "")
                if p == "/":
                    return self._send(200, HOME)
                if p in PAGES:
                    return self._send(200, PAGES[p])
                if p == "/lawyers":
                    return self._send(500, "oops")
                if p == "/our-story":
                    time.sleep(3)
                    return self._send(200, "<h1>Late</h1>")
                if p == "/services":
                    return self._send(302, "", headers={"Location": "http://127.0.0.1/admin"})
                if p == "/huge":
                    return self._send(200, "<p>" + "x" * (ws.MAX_BYTES + 50_000) + "</p>")
                if p == "/data":
                    return self._send(200, "{}", "application/json")
                if p == "/loop":
                    return self._send(302, "", headers={"Location": "/loop"})
                self._send(404, "nope")

        self.port = free_port()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://roe.test:{self.port}/"


def point_test_hosts_at_localhost(port):
    """The reader refuses 127.0.0.1 by design; for the local site, *.test names resolve to it here."""
    real = ws._public_ip
    ws.ALLOWED_PORTS = ws.ALLOWED_PORTS + (port,)

    def fake(host, p):
        if host.endswith(".test"):
            return "127.0.0.1"
        return real(host, p)
    ws._public_ip = fake
    return real


def section_reader():
    print("\n1. Website reader (local test site)")
    site = Site()
    real_ip = point_test_hosts_at_localhost(site.port)
    old_timeout = ws.PAGE_TIMEOUT
    ws.PAGE_TIMEOUT = 1.5
    try:
        t0 = time.monotonic()
        r = ws.read_site(site.url)
        took = time.monotonic() - t0
        kinds = {p["url"].rsplit("/", 1)[-1]: p["kind"] for p in r["pages"]}
        check("homepage plus About, Attorneys and Practice pages read",
              {"about-us": "about", "attorneys": "attorneys", "practice-areas": "practice"}.items() <= kinds.items(), kinds)
        visited = set(site.hits)
        check("blog, PDF, mailto, #anchor and other sites are not opened",
              not ({"/blog", "/brochure.pdf"} & visited) and not any("other.test" in p["url"] for p in r["pages"]), visited)
        reasons = {s["url"].rsplit("/", 1)[-1]: s["reason"] for s in r["skipped"]}
        check("robots.txt-disallowed page skipped and never requested", reasons.get("team-bios") == "robots.txt" and "/team-bios" not in visited, reasons)
        check("server error page skipped with a reason", "error (500)" in reasons.get("lawyers", ""), reasons)
        check("slow page skipped as too slow", "too long" in reasons.get("our-story", ""), reasons)
        check("redirect to a private address skipped", "ip address" in reasons.get("services", "").lower(), reasons)
        check("whole read stays within the time budget", took < ws.TOTAL_SECONDS, f"{took:.1f}s")
        home = r["pages"][0]
        check("script contents never reach the text", "Do not read me" not in home["text"])
        check("title and meta description captured", home["title"] == "Roe & Park LLP" and "Austin" in home["description"])
        check("headings keep their level marks for the fallback", "# Roe & Park" in home["text"])

        deadline = time.monotonic() + 10
        _, big = ws.fetch(f"http://roe.test:{site.port}/huge", deadline)
        check("pages are cut at the size cap", len(big) <= ws.MAX_BYTES, len(big))
        raises("non-HTML address refused", lambda: ws.fetch(f"http://roe.test:{site.port}/data", deadline), ws.WebsiteReadError, "isn't a web page")
        raises("redirect loop stopped", lambda: ws.fetch(f"http://roe.test:{site.port}/loop", deadline), ws.WebsiteReadError, "too many times")
        raises("missing page reported", lambda: ws.fetch(f"http://roe.test:{site.port}/missing", deadline), ws.WebsiteReadError, "404")

        blocked = Site(robots="User-agent: *\nDisallow: /\n")
        ws.ALLOWED_PORTS = ws.ALLOWED_PORTS + (blocked.port,)
        raises("a site that blocks all readers is not read", lambda: ws.read_site(blocked.url), ws.WebsiteReadError, "robots.txt")
        no_robots = Site(robots=None)
        ws.ALLOWED_PORTS = ws.ALLOWED_PORTS + (no_robots.port,)
        check("no robots.txt means reading is allowed", len(ws.read_site(no_robots.url)["pages"]) >= 3)
        for s in (blocked, no_robots):
            s.server.shutdown()
    finally:
        ws._public_ip = real_ip
        ws.PAGE_TIMEOUT = old_timeout
        site.server.shutdown()


def section_safety():
    print("\n2. Address safety")
    for url, needle in (("ftp://roe.com/", "http"), ("http://u:p@roe.com/", "username"), ("http://127.0.0.1/", "ip address"),
                        ("http://[::1]/", "ip address"), ("http://169.254.169.254/latest/", "ip address"),
                        ("http://roe.com:22/", "unusual port"), ("http://roe.com:99999/", "valid website")):
        raises(f"refused: {url}", lambda u=url: ws._check_url(u), ws.WebsiteReadError, needle)
    for host in ("localhost", "printer.local", "db.internal"):
        raises(f"refused name: {host}", lambda h=host: ws._public_ip(h, 80), ws.WebsiteReadError, "private network")
    real = socket.getaddrinfo
    answers = {
        "private.example": [("10.0.0.7",)],
        "mixed.example": [("93.184.216.34",), ("192.168.1.10",)],
        "mapped.example": [("::ffff:127.0.0.1",)],
        "metadata.example": [("169.254.169.254",)],
        "public.example": [("93.184.216.34",)],
    }

    def fake(host, port, *a, **kw):
        if host in answers:
            fam = socket.AF_INET6 if ":" in answers[host][0][0] else socket.AF_INET
            return [(fam, socket.SOCK_STREAM, 6, "", (ip[0], port)) for ip in answers[host]]
        return real(host, port, *a, **kw)
    socket.getaddrinfo = fake
    try:
        for host in ("private.example", "mixed.example", "mapped.example", "metadata.example"):
            raises(f"DNS answer refused: {host}", lambda h=host: ws._public_ip(h, 443), ws.WebsiteReadError, "private network")
        check("public DNS answer accepted", ws._public_ip("public.example", 443) == "93.184.216.34")
    finally:
        socket.getaddrinfo = real
    raises("unknown domain reported plainly", lambda: ws._public_ip("no-such-firm.invalid", 443), ws.WebsiteReadError, "couldn't find")


def section_details():
    print("\n3. Turning pages into firm details")
    pages = []
    for url, kind, html in (("https://roe.test/", "home", HOME), ("https://roe.test/about-us", "about", PAGES["/about-us"]),
                            ("https://roe.test/attorneys", "attorneys", PAGES["/attorneys"]),
                            ("https://roe.test/practice-areas", "practice", PAGES["/practice-areas"])):
        p = ws.parse_page(html)
        pages.append({"url": url, "kind": kind, "title": p["title"], "description": p["description"], "text": p["text"]})
    site = {"pages": pages, "skipped": [{"url": "https://roe.test/lawyers", "reason": "error"}]}

    d = wd.extract_firm_details(site)
    check("without Gemini the heading reader is used", d["engine"] == "headings")
    check("fallback: about text from the About page", d["about"].startswith("Roe & Park LLP has represented"), d["about"])
    check("fallback: attorneys with titles, section headings left out",
          [(a["name"], a["title"]) for a in d["attorneys"]] == [("Jane Roe", "Founding Partner"), ("Sam Park, Esq.", "Associate Attorney")], d["attorneys"])
    check("fallback: practice areas mapped to onboarding options", d["areas"] == ["family", "personal_injury"], d["areas"])
    check("pages read and skipped are reported", len(d["pages_read"]) == 4 and d["skipped"][0]["reason"] == "error")

    for name, want in (("Family-based Immigration", "immigration"), ("U Visas for Victims of Criminal Activity", "immigration"),
                       ("Guardianship & Elder Law", "estate_planning"), ("DUI Defense", "criminal_defense"), ("Premarital Agreements", "family")):
        check(f"practice '{name}' maps to {want}", wd.area_key(name) == want, wd.area_key(name))
    check("the name decides before the description", wd.area_key("Asylum", "We help families stay together") == "immigration")

    import agent.schema_extractor as se
    real = se.gemini_json
    try:
        se.gemini_json = lambda *a, **k: {
            "about": "A" * 3000,
            "practice_areas": ["Immigration", {"name": "immigration", "description": "dup"}, {"name": "Estate Planning", "description": "Wills and trusts."}],
            "attorneys": [{"name": "Ana Ruiz", "title": "Partner", "practice_areas": "Immigration, Asylum", "bio": "B" * 900},
                          {"name": "ana ruiz"}, {"name": ""}, {"name": "This Is Clearly Not A Real Person Name"}]}
        d = wd.extract_firm_details(site)
        check("Gemini answer used", d["engine"] == "gemini")
        check("about capped", len(d["about"]) == wd.MAX_ABOUT)
        check("practice areas de-duplicated and mapped", [(p["name"], p["area"]) for p in d["practice_areas"]] == [("Immigration", "immigration"), ("Estate Planning", "estate_planning")], d["practice_areas"])
        check("attorneys de-duplicated, blanks and over-long names dropped, bio capped",
              [a["name"] for a in d["attorneys"]] == ["Ana Ruiz"] and len(d["attorneys"][0]["bio"]) == wd.MAX_BIO
              and d["attorneys"][0]["practice_areas"] == ["Immigration", "Asylum"], d["attorneys"])

        def boom(*a, **k):
            raise RuntimeError("Gemini HTTP 429")
        se.gemini_json = boom
        check("Gemini failure falls back to headings", wd.extract_firm_details(site)["engine"] == "headings")
        se.gemini_json = lambda *a, **k: {"about": "", "practice_areas": [], "attorneys": []}
        check("empty Gemini answer falls back to headings", wd.extract_firm_details(site)["engine"] == "headings")
        seen = {}
        se.gemini_json = lambda s, u, **k: seen.update(prompt=u) or {"about": "x"}
        big = {"pages": [{"url": f"https://roe.test/{i}", "kind": "about", "title": "", "description": "", "text": "y" * 12000} for i in range(8)]}
        wd.extract_firm_details(big)
        check("text sent to Gemini is capped", len(seen["prompt"]) <= wd.PROMPT_CHARS + 2000, len(seen["prompt"]))
    finally:
        se.gemini_json = real


def section_profile():
    print("\n4. Firm profile fields")
    p = normalize_firm_profile({"about_firm": "One.\n\n\n\nTwo  words.", "details_source": "website", "details_read_at": 1790000000,
                                "practice_details": [{"name": "Divorce", "description": "Custody."}, {"name": "divorce", "description": "dup"}],
                                "attorneys": [{"name": " Jane  Roe ", "title": "Partner", "practice_areas": "Divorce, Custody"}]})
    check("paragraph breaks kept, extra blank lines dropped", p["about_firm"] == "One.\n\nTwo words.", repr(p["about_firm"]))
    check("practice areas de-duplicated", p["practice_details"] == [{"name": "Divorce", "description": "Custody."}])
    check("attorney cleaned, areas split", p["attorneys"][0] == {"name": "Jane Roe", "title": "Partner", "practice_areas": ["Divorce", "Custody"], "bio": ""})
    raises("same attorney twice refused", lambda: normalize_firm_profile({"attorneys": [{"name": "A B"}, {"name": "a b"}]}), ValueError, "listed twice")
    raises("too many attorneys refused", lambda: normalize_firm_profile({"attorneys": [{"name": f"Person {i}"} for i in range(41)]}), ValueError, "at most 40")
    raises("unknown source refused", lambda: normalize_firm_profile({"details_source": "robot"}), ValueError, "website, manual")
    raises("bad timestamp refused", lambda: normalize_firm_profile({"details_read_at": "yesterday"}), ValueError, "timestamp")
    check("clearing a field removes it", "about_firm" not in normalize_firm_profile({"about_firm": None}, p))


def section_agent():
    print("\n5. Receptionist script")
    from agent import onboarding as ob
    prof = {"about_firm": "Roe & Park helps families.", "practice_details": [{"name": "Divorce", "description": "Custody."}],
            "attorneys": [{"name": "Jane Roe", "title": "Partner", "practice_areas": ["Divorce"], "bio": "x " * 200}]}
    block = ob.firm_knowledge(prof)
    check("block starts and ends with markers", block[1].startswith(ob.KNOWLEDGE_START) and block[-1] == ob.KNOWLEDGE_END)
    check("long bios shortened", all(len(l) < 300 for l in block))
    check("no details, no block", ob.firm_knowledge({}) == [])
    many = {"attorneys": [{"name": f"Person Number{i}", "bio": "b " * 70} for i in range(40)]}
    check("block capped", sum(len(l) for l in ob.firm_knowledge(many)) <= ob.FIRM_KNOWLEDGE_CHARS + 200)
    cfg = ob.build_agent_config("Roe & Park", prof, None)
    check("seeded agent's stored prompt has no firm block (added at call time)", ob.KNOWLEDGE_START not in cfg["system_prompt"] and "Jane Roe" not in cfg["system_prompt"])
    check("seeded agent's stored prompt has no legal-advice line (global rule)", "legal advice" not in cfg["system_prompt"].lower())
    stored = f"You are Maya.\n\n{ob.KNOWLEDGE_START} x\nOld details\n{ob.KNOWLEDGE_END}\nAdmin rule: mention parking."
    clean = ob.strip_firm_block(stored)
    check("strip removes a stored block, keeps the admin's lines", ob.KNOWLEDGE_START not in clean and "Old details" not in clean and "mention parking" in clean and clean.startswith("You are Maya."))
    check("strip leaves prompts without a block alone", ob.strip_firm_block("You are Maya.") == "You are Maya.")

    class FakeBuilder:
        def __init__(self):
            self.agents = [{"agent_id": "a1", "system_prompt": stored}, {"agent_id": "a2", "system_prompt": "Plain."}]
            self.updates = []

        def list_agents(self):
            return self.agents

        def update_agent(self, agent_id, changes, note=""):
            self.updates.append((agent_id, changes["system_prompt"]))

    fb = FakeBuilder()
    check("one-time cleanup touches only agents with a block", ob.strip_firm_blocks(fb) == 1 and fb.updates[0][0] == "a1" and "Old details" not in fb.updates[0][1])

    print("\n5b. Call-time instructions (global rules first, firm details from the database)")
    import agent.firm_context as fc
    real_profile = fc.firm_profile
    try:
        fc.firm_profile = lambda w: {"about_firm": "NOW: also serving Dallas.", "attorneys": [{"name": "Jane Roe", "title": "Partner"}]} if w == "ws-a" else {}
        stored = f"You are Maya.\n\n{ob.KNOWLEDGE_START} old\nOLD about\n{ob.KNOWLEDGE_END}\nAdmin rule: mention parking."
        r = fc.call_instructions(stored, "ws-a")
        check("global instructions come first", r.startswith(fc.GLOBAL_MARK))
        check("then the agent prompt, then the firm details", r.index(fc.GLOBAL_MARK) < r.index("You are Maya") < r.index(ob.KNOWLEDGE_START))
        check("no-legal-advice rule present", fc.NO_LEGAL_ADVICE_MARK in r)
        check("stored firm block replaced by the saved details", "OLD about" not in r and "NOW: also serving Dallas." in r and r.count(ob.KNOWLEDGE_START) == 1)
        check("admin's own prompt lines kept", "mention parking" in r)
        check("agent without a firm block gets the saved details", "NOW: also serving Dallas." in fc.call_instructions("You are Maya.", "ws-a"))
        r0 = fc.call_instructions("You are Maya.", "ws-none")
        check("no saved details: global rules + prompt only", fc.GLOBAL_MARK in r0 and ob.KNOWLEDGE_START not in r0)
        check("never added twice", fc.call_instructions(r, "ws-a").count(fc.GLOBAL_MARK) == 1)
    finally:
        fc.firm_profile = real_profile
    with tempfile.TemporaryDirectory() as td:
        store = os.path.join(td, "auth_store.json")
        json.dump({"workspaces": [{"workspace_id": "ws-f", "metadata": {"firm_profile": {"about_firm": "From the store."}}}]}, open(store, "w"))
        old_path, fc.AUTH_STORE_PATH = fc.AUTH_STORE_PATH, store
        fc.forget()
        try:
            check("firm details read from the saved store", fc.firm_profile("ws-f").get("about_firm") == "From the store.")
            json.dump({"workspaces": [{"workspace_id": "ws-f", "metadata": {"firm_profile": {"about_firm": "Changed."}}}]}, open(store, "w"))
            check("cached for a short while", fc.firm_profile("ws-f").get("about_firm") == "From the store.")
            fc.forget("ws-f")
            check("forget() picks up a save at once", fc.firm_profile("ws-f").get("about_firm") == "Changed.")
            check("unknown workspace: no details", fc.firm_profile("ws-missing") == {})
        finally:
            fc.AUTH_STORE_PATH = old_path
            fc.forget()


# ── 6. HTTP ───────────────────────────────────────────────────────────────────
class Client:
    def __init__(self, base):
        import http.cookiejar
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=40) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}

    def login(self, email, pw):
        code, body = self.call("POST", "/api/v1/auth/login", {"email": email, "password": pw})
        return code == 200 and body.get("step") == "done"


def section_http():
    print("\n6. HTTP (isolated server copy)")
    tmp = tempfile.mkdtemp(prefix="website-live-")
    for d in ("agent", "scripts", "web"):
        shutil.copytree(os.path.join(ROOT_DIR, d), os.path.join(tmp, d), ignore=shutil.ignore_patterns("__pycache__", "audio", "*.mp3", "*.wav"))
    os.makedirs(os.path.join(tmp, "config"))
    os.makedirs(os.path.join(tmp, "recordings"))
    shutil.copy(os.path.join(ROOT_DIR, "config", "agents.json"), os.path.join(tmp, "config", "agents.json"))
    sys.path.insert(0, tmp)
    from agent.auth_manager import AuthManager
    am = AuthManager(store_path=os.path.join(tmp, "config", "auth_store.json"))
    pw = "Passw0rd!x"
    org = am.create_organization("Roe Park", admin_email="admin@roe.test", admin_password=pw)
    wid = org["workspace_id"] if isinstance(org, dict) else org.workspace_id
    am.add_member(wid, "staff@roe.test", pw, role="member_admin")
    am._save_store()
    port = free_port()
    env = dict(os.environ, DATABASE_URL="", AUTH_TRUST_LOOPBACK="0", SIGNUP_ENABLED="0", GEMINI_API_KEY="",
               LIVEKIT_URL="ws://127.0.0.1:1", LIVEKIT_API_KEY="dummy", LIVEKIT_API_SECRET="dummy-secret-dummy-secret")
    log_path = os.path.join(tmp, "server.log")
    proc = subprocess.Popen([sys.executable, "scripts/serve.py", str(port)], cwd=tmp, env=env, stdout=open(log_path, "w"), stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(base + "/login", timeout=1)
                break
            except Exception:
                time.sleep(0.25)
        A, S = Client(base), Client(base)
        check("admin and staff sign in", A.login("admin@roe.test", pw) and S.login("staff@roe.test", pw))
        code, _ = S.call("POST", "/api/v1/onboarding/website-draft", {"website": "roe.test"})
        check("staff cannot read websites (403)", code == 403, code)
        code, body = A.call("POST", "/api/v1/onboarding/website-draft", {})
        check("missing website is 400", code == 400, (code, body))
        code, body = A.call("POST", "/api/v1/onboarding/website-draft", {"website": "localhost"})
        check("non-public website name is 400", code == 400, (code, body))
        code, body = A.call("POST", "/api/v1/onboarding/website-draft", {"website": "https://no-such-firm-xyz.invalid"})
        check("unreachable website is 422 with a plain message", code == 422 and body.get("unreadable") and "find" in body.get("error", "").lower(), (code, body))
        codes = [A.call("POST", "/api/v1/onboarding/website-draft", {"website": "https://no-such-firm-xyz.invalid"})[0] for _ in range(4)]
        check("per-firm limit kicks in after 5 tries (429)", codes[-1] == 429 and 429 not in codes[:2], codes)

        code, _ = S.call("GET", "/api/v1/firm/details")
        check("staff cannot read firm details API (403)", code == 403, code)
        code, body = A.call("POST", "/api/v1/firm/details", {"about_firm": "We help families.", "attorneys": [{"name": "Jane Roe", "title": "Partner"}],
                                                              "details_source": "manual"})
        check("admin saves firm details", code == 200 and body["details"]["attorneys"][0]["name"] == "Jane Roe", (code, body))
        code, body = A.call("GET", "/api/v1/firm/details")
        check("details read back", body.get("details", {}).get("about_firm") == "We help families.", body)
        code, body = A.call("POST", "/api/v1/firm/details", {"website": "evil.test"})
        check("only firm-detail fields accepted (400)", code == 400, (code, body))
        code, body = A.call("POST", "/api/v1/firm/details", {"attorneys": [{"name": "A B"}, {"name": "a b"}]})
        check("validation errors are 400", code == 400 and "twice" in body.get("error", ""), (code, body))
        code, _ = S.call("POST", "/api/v1/firm/details", {"about_firm": "x"})
        check("staff cannot save firm details (403)", code == 403, code)
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
    section_reader()
    section_safety()
    section_details()
    section_profile()
    section_agent()
    if "--offline" not in sys.argv:
        section_http()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
