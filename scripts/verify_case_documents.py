#!/usr/bin/env python3
"""scripts/verify_case_documents.py — documents on cases (web/documents-plan.html).

1. Offline (throwaway folder, DATABASE_URL cleared): accepted types decided from the bytes (PDF, Word,
   Excel, CSV, text, PNG/JPEG/WebP and iPhone HEIC); refusals (old .doc, zip, empty, oversize, duplicate on
   the same case, per-case limits); the same file is fine on another case; list order, counts, delete,
   reload from disk.
2. PostgreSQL (only with VERIFY_PG_URL, a throwaway database): records in app_documents, bytes in
   case_document_blobs, a second process sees them, delete removes both.
3. HTTP on an isolated server copy (own config, no .env, no database): two firms; a Product Admin, a
   Member Admin assigned to the case and one who isn't. Admin uploads, lists, downloads (byte for byte,
   attachment + nosniff, images inline only when asked), deletes; assigned staff upload and delete only
   their own; unassigned staff, the other firm and signed-out requests are refused; the case list carries
   the document count; a Super Admin reaches any firm's case.
4. Browser (headless Chromium, same copy): New case with files attached, the Documents window lists them,
   add more by file picker, delete with an in-place confirm, assigned staff see their case's documents.
Never touches the real config, .env or database.
"""

import http.cookiejar
import io
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
import zipfile

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PW = "Passw0rd!x"
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


def png(text="x"):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (120, 40), "white")
    ImageDraw.Draw(im).text((5, 12), text, fill="black")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


_PDF_CACHE = {}


def pdf(text="Police report"):
    """Same bytes for the same text (PyMuPDF stamps each new file with a fresh id)."""
    if text not in _PDF_CACHE:
        _PDF_CACHE[text] = _make_pdf(text)
    return _PDF_CACHE[text]


def _make_pdf(text):
    import fitz
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), text)
    out = doc.tobytes()
    doc.close()
    return out


HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64


# ── 1. Offline ───────────────────────────────────────────────────────────────────────────────────────
def offline():
    print("\n1. Offline: types, limits, store")
    os.environ["DATABASE_URL"] = ""
    sys.path.insert(0, ROOT_DIR)
    import agent.case_documents as cdm
    tmp = tempfile.mkdtemp(prefix="casedocs-")
    try:
        check("PDF", cdm.sniff(pdf(), "a.pdf")[0] == "pdf")
        check("PNG", cdm.sniff(png(), "a.png") == ("image", "image/png"))
        check("iPhone HEIC", cdm.sniff(HEIC, "IMG_0001.HEIC") == ("image", "image/heic"))
        check("text", cdm.sniff(b"Insurance claim number 12345", "notes.txt")[0] == "text")
        for label, data in (("old .doc", b"\xd0\xcf\x11\xe0" + b"\x00" * 64), ("empty", b"")):
            try:
                cdm.sniff(data, "x")
                check(f"{label} refused", False)
            except cdm.DocumentError:
                check(f"{label} refused", True)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("a.txt", "x")
        try:
            cdm.sniff(buf.getvalue(), "a.zip")
            check("zip refused", False)
        except cdm.DocumentError:
            check("zip refused", True)

        s = cdm.CaseDocumentStore(local_dir=tmp)
        a = s.add("case-1", "ws-a", "police.pdf", pdf(), source="staff", uploaded_by="Dana", uploaded_by_id="u1")
        time.sleep(0.01)
        b = s.add("case-1", "ws-a", "photo.png", png(), source="caller", uploaded_by="Maria Lopez")
        check("added with source and uploader", a.source == "staff" and b.source == "caller" and b.uploaded_by == "Maria Lopez")
        check("listed newest first", [d.name for d in s.list_for_case("case-1")] == ["photo.png", "police.pdf"])
        check("public view hides the uploader's user id", "uploaded_by_id" not in a.public() and a.public()["kind_label"] == "PDF")
        for label, call, status in (("duplicate on the same case (409)", lambda: s.add("case-1", "ws-a", "again.pdf", pdf()), 409),
                                    ("oversize (413)", lambda: s.add("case-1", "ws-a", "big.txt", b"a" * (cdm.MAX_FILE_BYTES + 1)), 413),
                                    ("no case", lambda: s.add("", "ws-a", "x.txt", b"x"), 400),
                                    ("unknown source", lambda: s.add("case-1", "ws-a", "x.txt", b"hello", source="robot"), 400)):
            try:
                call()
                check(label, False, "accepted")
            except cdm.DocumentError as e:
                check(label, e.status == status, (str(e), e.status))
        check("the same file is fine on another case", s.add("case-2", "ws-a", "police.pdf", pdf()).case_id == "case-2")
        orig = cdm.MAX_CASE_FILES
        cdm.MAX_CASE_FILES = 2
        try:
            s.add("case-1", "ws-a", "third.txt", b"third file")
            check("per-case file limit", False, "accepted")
        except cdm.DocumentError as e:
            check("per-case file limit", e.status == 413)
        finally:
            cdm.MAX_CASE_FILES = orig
        check("counts per case", s.counts(["case-1", "case-2", "case-3"]) == {"case-1": 2, "case-2": 1})
        check("bytes kept", s.load_blob(a.doc_id) == pdf())
        s2 = cdm.CaseDocumentStore(local_dir=tmp)
        check("reload from disk", len(s2.list_for_case("case-1")) == 2 and s2.load_blob(b.doc_id) == png())
        check("delete", s.delete(a.doc_id) and s.get(a.doc_id) is None and s.load_blob(a.doc_id) is None)
        check("delete unknown -> False", s.delete("cd-nope") is False)
        check("delete_for_case", s.delete_for_case("case-2") == 1 and not s.list_for_case("case-2"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── 2. PostgreSQL ────────────────────────────────────────────────────────────────────────────────────
def postgres():
    url = os.getenv("VERIFY_PG_URL", "")
    if not url:
        print("\n2. PostgreSQL: [SKIP] set VERIFY_PG_URL to a throwaway database to include these checks")
        return
    print("\n2. PostgreSQL (throwaway database)")
    os.environ["DATABASE_URL"] = url
    import agent.case_documents as cdm
    from agent import storage
    storage._state.update(url=None, ok=False, checked=0.0, schema=False)
    s = cdm.CaseDocumentStore(local_dir=tempfile.mkdtemp())
    d = s.add("case-pg", "ws-pg", "report.pdf", pdf("pg"), source="caller", uploaded_by="Caller")
    check("bytes in case_document_blobs", storage.load_blob(d.doc_id, table="case_document_blobs") == pdf("pg"))
    check("not in knowledge_blobs", storage.load_blob(d.doc_id) is None)
    other = cdm.CaseDocumentStore(local_dir=tempfile.mkdtemp())
    check("a second process sees it", other.get(d.doc_id) is not None)
    s.delete(d.doc_id)
    check("delete removes record and bytes", storage.load_document(cdm.COLLECTION, d.doc_id) is None and storage.load_blob(d.doc_id, table="case_document_blobs") is None)
    try:
        storage._blob_table("users; DROP TABLE x")
        check("blob table name is whitelisted", False)
    except ValueError:
        check("blob table name is whitelisted", True)
    os.environ["DATABASE_URL"] = ""


# ── 3. HTTP ──────────────────────────────────────────────────────────────────────────────────────────
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

    def raw(self, method, path, data=None, headers=None):
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        try:
            with self.opener.open(req, timeout=60) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def call(self, method, path, body=None):
        code, raw, _ = self.raw(method, path, json.dumps(body).encode() if body is not None else None, {"Content-Type": "application/json"})
        try:
            return code, json.loads(raw or b"{}")
        except ValueError:
            return code, {}

    def upload(self, case_id, name, data):
        q = urllib.parse.urlencode({"case_id": case_id, "name": name})
        code, raw, _ = self.raw("POST", "/api/v1/case-documents/upload?" + q, data, {"Content-Type": "application/octet-stream"})
        try:
            return code, json.loads(raw or b"{}")
        except ValueError:
            return code, {}

    def login(self, email):
        code, body = self.call("POST", "/api/v1/auth/login", {"email": email, "password": PW})
        return code == 200 and body.get("step") == "done"


def server_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SMTP_", "RESEND_", "MAIL_", "TWILIO_", "TELNYX_", "GEMINI_", "GOOGLE_API", "CLIO_"))}
    env.update(DATABASE_URL="", AUTH_TRUST_LOOPBACK="0", SUPER_ADMIN_EMAIL="root@platform.test", SUPER_ADMIN_PASSWORD=PW,
               LIVEKIT_URL="ws://127.0.0.1:1", LIVEKIT_API_KEY="d", LIVEKIT_API_SECRET="dummy-secret-dummy-secret")
    return env


def start_copy():
    tmp = tempfile.mkdtemp(prefix="casedocs-http-")
    for d in ("agent", "scripts", "web"):
        shutil.copytree(os.path.join(ROOT_DIR, d), os.path.join(tmp, d), ignore=shutil.ignore_patterns("__pycache__", "audio", "*.mp3", "*.wav"))
    for d in ("config", "recordings"):
        os.makedirs(os.path.join(tmp, d))
    shutil.copy(os.path.join(ROOT_DIR, "config", "agents.json"), os.path.join(tmp, "config", "agents.json"))
    seed = (
        "import sys, json; sys.path.insert(0, '.')\n"
        "from agent.auth_manager import AuthManager\n"
        "am = AuthManager(store_path='config/auth_store.json')\n"
        f"a = am.create_organization('Firm A', admin_email='admin@a.test', admin_password='{PW}')\n"
        f"am.create_organization('Firm B', admin_email='admin@b.test', admin_password='{PW}')\n"
        "ws = a['workspace_id'] if isinstance(a, dict) else a.workspace_id\n"
        "ids = {}\n"
        "for e in ('staff@a.test', 'other@a.test'):\n"
        f"    u = am.create_user(ws, e, 'member_admin'); am.set_password(u.user_id, '{PW}'); ids[e] = u.user_id\n"
        "am._save_store(); print(json.dumps({'ws': ws, **ids}))\n"
    )
    out = subprocess.run([sys.executable, "-c", seed], cwd=tmp, env=server_env(), capture_output=True, text=True)
    info = json.loads(out.stdout.strip().splitlines()[-1])
    port = free_port()
    proc = subprocess.Popen([sys.executable, "scripts/serve.py", str(port)], cwd=tmp, env=server_env(),
                            stdout=open(os.path.join(tmp, "server.log"), "w"), stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(80):
        try:
            urllib.request.urlopen(base + "/login", timeout=1)
            break
        except Exception:
            time.sleep(0.25)
    return tmp, proc, base, info


def http_suite(base, info):
    print("\n3. HTTP on an isolated server copy")
    A, B, ST, OT, S, anon = (Client(base) for _ in range(6))
    check("everyone signs in", all(c.login(e) for c, e in ((A, "admin@a.test"), (B, "admin@b.test"), (ST, "staff@a.test"), (OT, "other@a.test"), (S, "root@platform.test"))))
    code, body = A.call("POST", "/api/v1/cases", {"client_name": "Maria Lopez", "case_type": "Personal Injury"})
    case = body.get("case", {})
    cid = case.get("case_id", "")
    check("case created with 0 documents", code == 201 and case.get("documents") == 0, (code, body))
    A.call("POST", "/api/v1/cases/assign", {"case_id": cid, "user_ids": [info["staff@a.test"]], "mode": "set"})

    report = pdf("Police report 2026-001")
    code, body = A.upload(cid, "police report.pdf", report)
    doc = body.get("document", {})
    check("admin uploads (201)", code == 201 and doc.get("source") == "staff" and doc.get("kind") == "pdf", (code, body))
    code, body = A.upload(cid, "photo.png", png("dent"))
    photo = body.get("document", {})
    check("admin uploads a photo", code == 201)
    code, body = A.upload(cid, "again.pdf", report)
    check("duplicate refused (409)", code == 409 and "already on the case" in body.get("error", ""))
    code, body = A.upload(cid, "a.doc", b"\xd0\xcf\x11\xe0" + b"\x00" * 64)
    check("old .doc refused (400)", code == 400)
    code, body = A.upload(cid, "big.txt", b"a" * (20 * 1024 * 1024 + 1))
    check("oversize refused (413)", code == 413 and "20 MB" in body.get("error", ""))
    code, body = A.upload("case-nope", "x.txt", b"x")
    check("unknown case (404)", code == 404)

    code, body = A.call("GET", "/api/v1/case-documents?case_id=" + cid)
    check("list: both documents, admin may delete", code == 200 and len(body["documents"]) == 2 and all(d["can_delete"] for d in body["documents"]))
    check("list hides uploader user ids", all("uploaded_by_id" not in d for d in body["documents"]))
    code, body = A.call("GET", "/api/v1/cases")
    check("case list carries the document count", next(c for c in body["cases"] if c["case_id"] == cid)["documents"] == 2)
    code, raw, hdrs = A.raw("GET", f"/api/v1/case-documents/download?case_id={cid}&doc_id={doc['doc_id']}")
    check("download byte for byte, as attachment, nosniff", code == 200 and raw == report and hdrs.get("Content-Disposition", "").startswith("attachment") and hdrs.get("X-Content-Type-Options") == "nosniff")
    code, raw, hdrs = A.raw("GET", f"/api/v1/case-documents/download?case_id={cid}&doc_id={photo['doc_id']}&inline=1")
    check("image opens inline when asked, sandboxed", hdrs.get("Content-Disposition", "").startswith("inline") and "sandbox" in hdrs.get("Content-Security-Policy", ""))
    code, raw, hdrs = A.raw("GET", f"/api/v1/case-documents/download?case_id={cid}&doc_id={doc['doc_id']}&inline=1")
    check("PDF never inline", hdrs.get("Content-Disposition", "").startswith("attachment"))

    code, body = ST.call("GET", "/api/v1/case-documents?case_id=" + cid)
    check("assigned staff list the documents", code == 200 and len(body["documents"]) == 2)
    check("assigned staff can't delete what admins added", not any(d["can_delete"] for d in body["documents"]))
    code, body = ST.upload(cid, "medical bill.png", png("bill"))
    mine = body.get("document", {})
    check("assigned staff upload (201)", code == 201 and mine.get("source") == "staff")
    code, _ = ST.call("POST", "/api/v1/case-documents/delete", {"case_id": cid, "doc_id": doc["doc_id"]})
    check("assigned staff can't delete an admin's file (403)", code == 403, code)
    code, _ = ST.call("POST", "/api/v1/case-documents/delete", {"case_id": cid, "doc_id": mine["doc_id"]})
    check("assigned staff delete their own file", code == 200)

    for who, c in (("unassigned staff", OT), ("other firm's admin", B)):
        code, _ = c.call("GET", "/api/v1/case-documents?case_id=" + cid)
        check(f"{who} can't list (404)", code == 404, code)
        code, _ = c.upload(cid, "x.txt", b"sneaky")
        check(f"{who} can't upload (404)", code == 404, code)
        code, _, _ = c.raw("GET", f"/api/v1/case-documents/download?case_id={cid}&doc_id={doc['doc_id']}")
        check(f"{who} can't download (404)", code == 404, code)
        code, _ = c.call("POST", "/api/v1/case-documents/delete", {"case_id": cid, "doc_id": doc["doc_id"]})
        check(f"{who} can't delete (404)", code == 404, code)
    code, _ = anon.call("GET", "/api/v1/case-documents?case_id=" + cid)
    check("signed out refused", code in (401, 302))
    code, _ = anon.upload(cid, "x.txt", b"x")
    check("signed-out upload refused", code in (401, 302))
    code, _, _ = A.raw("GET", f"/api/v1/case-documents/download?case_id={cid}&doc_id={doc['doc_id']}x")
    check("unknown document (404)", code == 404)

    code, body = S.call("GET", "/api/v1/case-documents?case_id=" + cid)
    check("Super Admin reaches any firm's case", code == 200 and len(body["documents"]) == 2)
    code, _ = A.call("POST", "/api/v1/case-documents/delete", {"case_id": cid, "doc_id": photo["doc_id"]})
    code2, body = A.call("GET", "/api/v1/case-documents?case_id=" + cid)
    check("admin deletes", code == 200 and len(body["documents"]) == 1)
    return cid


def browser_suite(base):
    print("\n4. Browser: Cases page")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [SKIP] playwright not installed")
        return
    tmp = tempfile.mkdtemp(prefix="casedocs-files-")
    paths = {}
    for name, data in (("intake form.pdf", pdf("Intake form")), ("car.png", png("car")), ("bill.png", png("bill")), ("old.doc", b"\xd0\xcf\x11\xe0" + b"\x00" * 64)):
        paths[name] = os.path.join(tmp, name)
        open(paths[name], "wb").write(data)
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 1280, "height": 900})
        r = ctx.request.post(base + "/api/v1/auth/login", data={"email": "admin@a.test", "password": PW})
        check("admin signs in", r.json().get("step") == "done")
        pg = ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(base + "/cases", wait_until="load")
        pg.wait_for_timeout(1000)
        pg.click("#btnNew")
        pg.fill("#nName", "Jamal Carter")
        pg.fill("#nType", "Car accident")
        pg.set_input_files("#nFiles", [paths["intake form.pdf"], paths["car.png"]])
        pg.click("#formNew button[type=submit]")
        pg.wait_for_timeout(2500)
        rowtxt = pg.inner_text("tr:has-text('Jamal Carter')")
        check("New case with 2 files shows '2 documents'", "2 documents" in rowtxt, rowtxt)
        pg.click("tr:has-text('Jamal Carter') [data-docs]")
        pg.wait_for_timeout(800)
        lst = pg.inner_text("#docList")
        check("Documents window lists both", "intake form.pdf" in lst and "car.png" in lst, lst)
        check("View offered for the photo", pg.locator("#docList li:has-text('car.png') a:has-text('View')").count() == 1)
        pg.set_input_files("#docInput", [paths["bill.png"], paths["old.doc"]])
        pg.wait_for_timeout(2000)
        check("added by file picker", "bill.png" in pg.inner_text("#docList"))
        check("old .doc refused with the reason", "isn't supported" in pg.inner_text("#docQueue"), pg.inner_text("#docQueue"))
        pg.click("#docList li:has-text('bill.png') [data-doc-confirm]")
        check("delete asks in place", pg.locator("#docList li:has-text('bill.png') [data-doc-del]").count() == 1)
        pg.click("#docList li:has-text('bill.png') [data-doc-del]")
        pg.wait_for_timeout(900)
        check("deleted", "bill.png" not in pg.inner_text("#docList"))
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(300)
        check("count on the row follows", "2 documents" in pg.inner_text("tr:has-text('Jamal Carter')"))
        check("no JS errors (admin)", not errs, errs)

        m = b.new_context(viewport={"width": 1280, "height": 900})
        m.request.post(base + "/api/v1/auth/login", data={"email": "staff@a.test", "password": PW})
        mp = m.new_page()
        merr = []
        mp.on("pageerror", lambda e: merr.append(str(e)))
        mp.goto(base + "/cases", wait_until="load")
        mp.wait_for_timeout(1000)
        check("assigned staff see their case with its documents", "1 document" in mp.inner_text("tr:has-text('Maria Lopez')"))
        mp.click("tr:has-text('Maria Lopez') [data-docs]")
        mp.wait_for_timeout(800)
        check("staff can open the Documents window, no Delete on others' files", "police report.pdf" in mp.inner_text("#docList") and mp.locator("[data-doc-confirm]").count() == 0)
        check("no JS errors (staff)", not merr, merr)
        ph = b.new_context(viewport={"width": 375, "height": 812})
        ph.request.post(base + "/api/v1/auth/login", data={"email": "admin@a.test", "password": PW})
        pp = ph.new_page()
        pp.goto(base + "/cases", wait_until="load")
        pp.wait_for_timeout(1000)
        pp.click("tr:has-text('Jamal Carter') [data-docs]")
        pp.wait_for_timeout(800)
        ov = pp.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        check("phone width: Documents window fits", ov <= 0 and pp.is_visible("#docDrop"), ov)
        b.close()
    shutil.rmtree(tmp, ignore_errors=True)


def main():
    offline()
    postgres()
    if "--offline" in sys.argv:
        print(f"\n{passed}/{passed + failed} passed")
        sys.exit(1 if failed else 0)
    tmp, proc, base, info = start_copy()
    try:
        http_suite(base, info)
        if "--no-browser" not in sys.argv:
            browser_suite(base)
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
