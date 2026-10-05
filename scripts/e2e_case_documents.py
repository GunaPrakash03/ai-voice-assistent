#!/usr/bin/env python3
"""scripts/e2e_case_documents.py — Case Documents end to end, like a real caller (web/documents-plan.html).

On an isolated server copy (own config, no database, a local test mail server, SMS dry run) with the real
Gemini key from .env:
  1. a real Gemini conversation with the firm's agent: the caller has documents and gives an email;
  2. the server's after-call pipeline (the same path as a phone call): reads the answer and the caller's
     basics, creates the case, emails the private upload link;
  3. the caller opens the emailed link on a phone-size browser and uploads two files;
  4. the firm is emailed, staff see 'New documents' on Cases, open them, download the PDF intact.
Uses about 8 Gemini calls. Never touches the real config, .env, database, a mailbox or a phone.

    python3 scripts/e2e_case_documents.py [screenshot-dir]
"""
import email as email_lib, email.header, json, os, shutil, subprocess, sys, tempfile, time, urllib.request, urllib.parse
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); SP = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="e2e-shots-")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import verify_case_documents as v
from verify_email_settings import FakeSMTP
PW = v.PW
key = next((l.split("=", 1)[1].strip().strip("'\"") for l in open(os.path.join(ROOT, ".env")) if l.startswith("GEMINI_API_KEY=")), "")
smtp = FakeSMTP()
tmp = tempfile.mkdtemp(prefix="e2e-docs-"); port = v.free_port(); base = f"http://127.0.0.1:{port}"
for d in ("agent", "scripts", "web"):
    shutil.copytree(os.path.join(ROOT, d), os.path.join(tmp, d), ignore=shutil.ignore_patterns("__pycache__", "audio", "*.mp3", "*.wav"))
for d in ("config", "recordings"): os.makedirs(os.path.join(tmp, d))
open(os.path.join(tmp, ".env"), "w").write(f"GEMINI_API_KEY={key}\n")
extra = {"PUBLIC_BASE_URL": base, "SMTP_HOST": "127.0.0.1", "SMTP_PORT": str(smtp.port), "SMTP_TLS": "auto",
         "MAIL_FROM": "Bottini Law <desk@bottini.test>", "SMS_DRY_RUN": "1", "DOCS_NOTIFY_DELAY": "2"}
env = v.server_env(extra)
seed = f"""
import sys, json; sys.path.insert(0, '.')
from agent.auth_manager import AuthManager
am = AuthManager(store_path='config/auth_store.json')
o = am.create_organization('Bottini Law', admin_email='admin@bottini.test', admin_password='{PW}')
ws = o['workspace_id'] if isinstance(o, dict) else o.workspace_id
u = am.create_user(ws, 'dana@bottini.test', 'member_admin'); am.set_password(u.user_id, '{PW}'); am._save_store()
from agent.agent_builder import agent_builder
a = agent_builder.create_agent('Maya', 'Thanks for calling Bottini Law, this is Maya. How can I help?',
    "You are Maya, the intake receptionist at Bottini Law. Take the caller's name, phone and what happened.",
    llm_model='gemini-3.5-flash-lite', workspace_id=ws, lead_emails=['intake@bottini.test'])
print(json.dumps({{'ws': ws, 'staff': u.user_id, 'agent_id': a.agent_id}}))
"""
out = subprocess.run([sys.executable, "-c", seed], cwd=tmp, env=env, capture_output=True, text=True)
info = json.loads(out.stdout.strip().splitlines()[-1])
proc = subprocess.Popen([sys.executable, "scripts/serve.py", str(port)], cwd=tmp, env=env, stdout=open(os.path.join(tmp, "server.log"), "w"), stderr=subprocess.STDOUT)
ok = []
def step(label, cond, detail=""):
    ok.append(bool(cond)); print(("  ✔ " if cond else "  ✘ ") + label + ("" if cond else f"   [{detail}]"))
try:
    for _ in range(80):
        try: urllib.request.urlopen(base + "/login", timeout=1); break
        except Exception: time.sleep(0.25)
    # 1. The call (real Gemini, the agent as configured in this firm)
    print("\n1. The call (real Gemini)")
    sys.path.insert(0, tmp); os.environ.update(DATABASE_URL="", GEMINI_API_KEY=key)
    os.chdir(tmp)
    from agent.agent_builder import AgentConfig, agent_builder, looks_like_closing
    cfg = AgentConfig(agent_id=info["agent_id"], name="Maya", first_message="Thanks for calling Bottini Law, this is Maya. How can I help?",
                      system_prompt="You are Maya, the intake receptionist at Bottini Law. Take the caller's name, phone and what happened.",
                      llm_model="gemini-3.5-flash-lite", workspace_id=info["ws"])
    hist = [{"speaker": "agent", "text": cfg.first_message}]
    print("     Maya:   " + cfg.first_message)
    for said in ("Hi, I slipped on a wet floor at a grocery store two days ago and hurt my wrist.",
                 "Jordan Reyes, my number is 951 555 0188.", "No, that's everything.",
                 "Yes, I have photos of the floor and my ER discharge papers.",
                 "jordan dot reyes at example dot com", "Yes, that's right."):
        hist.append({"speaker": "caller", "text": said}); print("     Caller: " + said)
        reply = agent_builder._preview_reply(cfg, said, None, history=hist)
        hist.append({"speaker": "agent", "text": reply}); print("     Maya:   " + reply)
        if looks_like_closing(reply): print("     (call ends: the agent said goodbye)"); break
        time.sleep(1.5)
    step("agent asked about documents", any("document" in t["text"].lower() for t in hist if t["speaker"] == "agent"))
    # 2. After the call: the server's real pipeline
    print("\n2. After the call (the server's pipeline)")
    A = v.Client(base); A.login("admin@bottini.test")
    code, body = A.call("POST", "/api/pipeline/enqueue", {"call_id": f"e2e-call-{int(time.time())}", "transcript_turns": hist,
                        "metadata": {"agent_id": info["agent_id"], "agent_name": "Maya", "from_number": "+19515550188"}})
    md = (body.get("job") or {}).get("metadata") or {}
    print("     read from the transcript:", md.get("documents_request"))
    code, cs = A.call("GET", "/api/v1/cases")
    case = next((c for c in cs.get("cases", []) if c["case_id"] == md.get("case_id")), {})
    print(f"     case: {case.get('client_name')} · {case.get('phone')} · {case.get('email')} · {case.get('case_type')} · {case.get('summary')}")
    step("case has the caller's name", case.get("client_name") == "Jordan Reyes", case.get("client_name"))
    print("     link:", md.get("documents_link"))
    step("pipeline read 'has documents' and the email", (md.get("documents_request") or {}).get("wanted") and md["documents_request"].get("email") == "jordan.reyes@example.com", md.get("documents_request"))
    step("case created", bool(md.get("case_id")), body.get("error"))
    step("link sent by email", (md.get("documents_link") or {}).get("ok") and md["documents_link"]["channel"] == "email", md.get("documents_link"))
    cid = md.get("case_id", "")
    A.call("POST", "/api/v1/cases/assign", {"case_id": cid, "user_ids": [info["staff"]], "mode": "set"})
    time.sleep(0.5)
    mails = [m for m in smtp.messages if "jordan.reyes@example.com" in m["to"]]
    msg = email_lib.message_from_string(mails[0]["data"]) if mails else None
    subject = str(email.header.make_header(email.header.decode_header(msg["Subject"]))) if msg else ""
    text = next((p.get_payload(decode=True).decode() for p in msg.walk() if p.get_content_type() == "text/plain"), "") if msg else ""
    url = next((w for w in text.split() if "/upload/" in w), "")
    step(f"caller's email arrived: \"{subject}\"", bool(url), text[:200])
    print("     ---- email to the caller ----"); print("\n".join("     " + l for l in text.strip().splitlines())); print("     -----------------------------")
    # 3. The caller uploads from a phone
    print("\n3. The caller opens the link on a phone and uploads")
    from playwright.sync_api import sync_playwright
    files = []
    for n, data in (("ER discharge.pdf", v.pdf("Emergency room discharge - Jordan Reyes")), ("wet floor.png", v.png("wet floor"))):
        p = os.path.join(tmp, n); open(p, "wb").write(data); files.append(p)
    with sync_playwright() as p:
        b = p.chromium.launch()
        ph = b.new_context(viewport={"width": 390, "height": 844}); pg = ph.new_page(); errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(url, wait_until="load"); pg.wait_for_timeout(800)
        step(f"page greets: \"{pg.text_content('h1')}\" ({pg.text_content('#firm')})", "Jordan" in pg.text_content("h1"))
        pg.screenshot(path=os.path.join(SP, "e2e-1-caller-page.png"))
        pg.set_input_files("#pick", files); pg.wait_for_timeout(2500)
        step("both files sent, thank-you shown", pg.text_content("#list").count("Sent ✓") == 2 and pg.is_visible("#done"), pg.text_content("#list"))
        pg.screenshot(path=os.path.join(SP, "e2e-2-caller-done.png"))
        step("no page errors", not errs, errs)
        # 4. The firm
        print("\n4. The firm")
        time.sleep(3.5)
        subj = lambda m: str(email.header.make_header(email.header.decode_header(email_lib.message_from_string(m["data"])["Subject"])))
        firm = [m for m in smtp.messages if "new document" in subj(m)]
        step(f"firm emailed ({', '.join(sorted({t for m in firm for t in m['to']}))}): \"{subj(firm[0]) if firm else ''}\"", len(firm) == 2, [(m["to"], subj(m)) for m in smtp.messages])
        st = b.new_context(viewport={"width": 1280, "height": 860})
        st.request.post(base + "/api/v1/auth/login", data={"email": "dana@bottini.test", "password": PW})
        sp_ = st.new_page(); sp_.goto(base + "/cases", wait_until="load"); sp_.wait_for_timeout(1200)
        row = sp_.inner_text("tr:has-text('Jordan Reyes')")
        step("staff's Cases page: 'New documents · 2 from the caller'", "New documents · 2 from the caller" in row, row)
        sp_.screenshot(path=os.path.join(SP, "e2e-3-staff-cases.png"))
        sp_.click("tr:has-text('Jordan Reyes') [data-docs]"); sp_.wait_for_timeout(1000)
        lst = sp_.inner_text("#docList")
        step("Documents window: both files, marked Caller", "ER discharge.pdf" in lst and "wet floor.png" in lst and sp_.locator(".tag-caller").count() == 2, lst)
        sp_.screenshot(path=os.path.join(SP, "e2e-4-staff-documents.png"))
        with sp_.expect_download() as dl:
            sp_.click("#docList a.nm:has-text('ER discharge.pdf')")
        step("staff download the caller's PDF intact", open(dl.value.path(), "rb").read() == v.pdf("Emergency room discharge - Jordan Reyes"))
        sp_.keyboard.press("Escape"); sp_.wait_for_timeout(400)
        step("badge cleared after opening: " + sp_.inner_text("tr:has-text('Jordan Reyes') .docreq"), "Documents received · 2 files" in sp_.inner_text("tr:has-text('Jordan Reyes')"))
        b.close()
finally:
    proc.terminate(); proc.wait()
    print(f"\n{sum(ok)}/{len(ok)} steps passed" + ("" if all(ok) else f"  (server log: {tmp}/server.log)"))
    if all(ok): shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(0 if ok and all(ok) else 1)
