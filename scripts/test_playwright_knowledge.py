#!/usr/bin/env python3
"""scripts/test_playwright_knowledge.py — the Knowledge base page (/knowledge) in a real (headless) Chromium.

Starts its own isolated copy of the server (agent/, scripts/, web/ in a temp dir; no database, no carrier
or mail keys) with one firm, a Product Admin and a Member Admin, then drives the page like a person:

  Admin   empty state · sidebar link · upload every format through the file picker (an old .doc is
          refused before upload) · statuses settle (an image is Ready when a Gemini key is available,
          else Failed with the reason) · stats · preview · Try a question finds the right file ·
          no-match wording · download by file name · delete with in-place confirm (and Keep) ·
          Read again · no JS or server errors
  Phone   375 px wide, dark scheme: no sideways scroll
  Member  read-only notice, no upload/delete/read-again, can preview and search, no JS errors

The only key it uses is GEMINI_API_KEY from .env (pass --no-gemini to leave it out; images then fail
with the Gemini reason). Never touches the real config, .env or database.

    python3 scripts/test_playwright_knowledge.py [--no-gemini]
"""

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT_DIR, "scripts"))
PW = "Passw0rd!x"
results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(("  [PASS] " if ok else "  [FAIL] ") + label + ("" if ok else f"  {detail}"))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def gemini_key():
    path = os.path.join(ROOT_DIR, ".env")
    if not os.path.isfile(path):
        return ""
    for line in open(path, encoding="utf-8"):
        if line.startswith("GEMINI_API_KEY="):
            return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def build_copy(tmp, use_gemini):
    for d in ("agent", "scripts", "web"):
        shutil.copytree(os.path.join(ROOT_DIR, d), os.path.join(tmp, d), ignore=shutil.ignore_patterns("__pycache__", "audio", "*.mp3", "*.wav"))
    for d in ("config", "recordings", "samples"):
        os.makedirs(os.path.join(tmp, d))
    shutil.copy(os.path.join(ROOT_DIR, "config", "agents.json"), os.path.join(tmp, "config", "agents.json"))
    key = gemini_key() if use_gemini else ""
    if key:
        with open(os.path.join(tmp, ".env"), "w") as f:
            f.write(f"GEMINI_API_KEY={key}\n")
    os.environ["DATABASE_URL"] = ""
    import verify_knowledge as vk
    for name, (make, kind) in vk.SAMPLES.items():
        if kind != "image":
            with open(os.path.join(tmp, "samples", name), "wb") as f:
                f.write(make())
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGB", (900, 300), "white")
    d = ImageDraw.Draw(im)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 34)
    except OSError:
        font = None
    for i, line in enumerate(["OFFICE HOLIDAY HOURS", "Closed December 24-26", "Open 9 am - 1 pm on December 31"]):
        d.text((30, 30 + i * 80), line, fill="black", font=font)
    im.save(os.path.join(tmp, "samples", "holiday-hours.png"))
    with open(os.path.join(tmp, "samples", "old.doc"), "wb") as f:
        f.write(b"\xd0\xcf\x11\xe0" + b"\x00" * 200)
    seed = (
        "import sys; sys.path.insert(0, '.')\n"
        "from agent.auth_manager import AuthManager\n"
        "am = AuthManager(store_path='config/auth_store.json')\n"
        f"a = am.create_organization('Bottini Law', admin_email='admin@a.test', admin_password='{PW}')\n"
        "ws = a['workspace_id'] if isinstance(a, dict) else a.workspace_id\n"
        f"u = am.create_user(ws, 'member@a.test', 'member_admin'); am.set_password(u.user_id, '{PW}')\n"
        "am._save_store()\n"
    )
    env = server_env()
    subprocess.run([sys.executable, "-c", seed], cwd=tmp, env=env, check=True, capture_output=True)
    return bool(key)


def server_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SMTP_", "RESEND_", "MAIL_", "TWILIO_", "TELNYX_", "GEMINI_", "GOOGLE_API", "CLIO_"))}
    env.update(DATABASE_URL="", AUTH_TRUST_LOOPBACK="0", SUPER_ADMIN_EMAIL="root@platform.test", SUPER_ADMIN_PASSWORD=PW,
               LIVEKIT_URL="ws://127.0.0.1:1", LIVEKIT_API_KEY="d", LIVEKIT_API_SECRET="dummy-secret-dummy-secret")
    return env


async def drive(base, tmp, has_gemini):
    from playwright.async_api import async_playwright

    async def login(ctx, email):
        r = await ctx.request.post(base + "/api/v1/auth/login", data={"email": email, "password": PW})
        return (await r.json()).get("step") == "done"

    async with async_playwright() as p:
        b = await p.chromium.launch()
        print("\nAdmin")
        ctx = await b.new_context(viewport={"width": 1280, "height": 900})
        check("admin signs in", await login(ctx, "admin@a.test"))
        pg = await ctx.new_page()
        errs, bad = [], []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        # The firm picker asks for the organization list, which only Super Admins get (403 by design).
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" and "403" not in m.text else None)
        pg.on("response", lambda r: bad.append(f"{r.status} {r.url}") if r.status >= 500 else None)
        await pg.goto(base + "/knowledge", wait_until="load")
        await pg.wait_for_selector("#rows .empty")
        check("empty state", "No files yet" in await pg.inner_text("#rows"))
        check("upload area shown", await pg.is_visible("#drop"))
        check("sidebar: Knowledge base, active", await pg.locator(".nav a.active", has_text="Knowledge base").count() == 1)

        samples = os.path.join(tmp, "samples")
        await pg.set_input_files("#fileInput", [os.path.join(samples, n) for n in sorted(os.listdir(samples))])
        await pg.wait_for_timeout(500)
        check("old .doc refused before upload, with the reason", "isn't supported" in await pg.inner_text("#queue"))
        t0 = time.time()
        rows = ""
        while time.time() - t0 < 150:
            rows = await pg.inner_text("#rows")
            if rows.count("Ready") + rows.count("Failed") >= 7 and "Reading" not in rows:
                break
            await pg.wait_for_timeout(1000)
        if has_gemini:
            check("all 7 files read", rows.count("Ready") == 7, rows[:400])
            check("image read by Gemini", "Read by Gemini" in rows)
        else:
            check("6 files read, the image failed", rows.count("Ready") == 6 and rows.count("Failed") == 1, rows[:400])
            check("image failure names the missing Gemini key", "Gemini API key" in rows)
        check("PDF notes its scanned page", "scanned page" in rows)
        check("stats count the files", "of 300 allowed" in await pg.inner_text(".stats"))

        await pg.click("tr:has-text('office.docx') button[data-act=preview]")
        await pg.wait_for_timeout(800)
        check("preview shows the extracted text", "Elm Street" in await pg.inner_text("#previewText"))
        await pg.keyboard.press("Escape")
        check("preview closes with Escape", not await pg.is_visible("#previewModal .modal"))

        asks = [("where can I park", "office.docx"), ("how much is a will", "fees.pdf"), ("phone number for Riverside", "locations.xlsx")]
        if has_gemini:
            asks.append(("Are you open on New Year's Eve?", "holiday-hours.png"))
        for question, want in asks:
            await pg.fill("#askInput", question)
            await pg.click("#btnAsk")
            await pg.wait_for_timeout(700)
            hits = await pg.inner_text("#hits")
            check(f"ask '{question}' -> {want}", hits.split("\n")[0].startswith(want), hits[:120])
        await pg.fill("#askInput", "zzzz qqqq")
        await pg.click("#btnAsk")
        await pg.wait_for_timeout(600)
        check("no match explains the call-back", "offer a call back" in await pg.inner_text("#hits"))

        async with pg.expect_download() as dl:
            await pg.click("tr:has-text('notes.txt') a.dl")
        path = await (await dl.value).path()
        check("download by file name returns the original", open(path, "rb").read().startswith(b"\xef\xbb\xbfThe firm"))

        await pg.click("tr:has-text('faq.csv') button[data-act=confirm]")
        check("delete asks to confirm in place", await pg.locator("tr:has-text('faq.csv') button:has-text('Delete for good')").count() == 1)
        await pg.click("tr:has-text('faq.csv') button:has-text('Keep')")
        check("Keep cancels", await pg.locator("tr:has-text('faq.csv') button:has-text('Delete for good')").count() == 0)
        await pg.click("tr:has-text('faq.csv') button[data-act=confirm]")
        await pg.click("tr:has-text('faq.csv') button:has-text('Delete for good')")
        await pg.wait_for_timeout(900)
        check("file deleted", "faq.csv" not in await pg.inner_text("#rows"))
        await pg.click("tr:has-text('intake.md') button[data-act=reprocess]")
        await pg.wait_for_timeout(2500)
        check("Read again ends Ready", "Ready" in await pg.inner_text("tr:has-text('intake.md')"))
        check("no JS errors or server errors", not errs and not bad, (errs, bad))

        print("\nPhone, dark")
        m = await b.new_context(viewport={"width": 375, "height": 812}, color_scheme="dark")
        await login(m, "admin@a.test")
        mp = await m.new_page()
        await mp.goto(base + "/knowledge", wait_until="load")
        await mp.wait_for_timeout(1200)
        overflow = await mp.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        check("no sideways page scroll at 375 px", overflow <= 0, overflow)

        print("\nMember Admin")
        mc = await b.new_context(viewport={"width": 1280, "height": 900})
        check("member signs in", await login(mc, "member@a.test"))
        mpg = await mc.new_page()
        merr = []
        mpg.on("pageerror", lambda e: merr.append(str(e)))
        await mpg.goto(base + "/knowledge", wait_until="load")
        await mpg.wait_for_timeout(1200)
        check("view-only notice, no upload area", await mpg.is_visible("#viewOnly") and not await mpg.is_visible("#drop") and not await mpg.is_visible("#btnUpload"))
        check("no Delete or Read again", await mpg.locator("button[data-act=confirm], button[data-act=reprocess]").count() == 0)
        check("can preview and search", await mpg.locator("button[data-act=preview]").count() > 0 and await mpg.is_visible("#askInput"))
        check("sidebar shows Knowledge base", "Knowledge base" in await mpg.inner_text(".nav"))
        check("no JS errors", not merr, merr)
        await b.close()


def main():
    use_gemini = "--no-gemini" not in sys.argv
    tmp = tempfile.mkdtemp(prefix="knowledge-ui-")
    print("Knowledge base page (isolated server copy, headless Chromium)")
    has_gemini = build_copy(tmp, use_gemini)
    print("  Gemini key:", "used for images" if has_gemini else "not used (images should fail with the reason)")
    port = free_port()
    proc = subprocess.Popen([sys.executable, "scripts/serve.py", str(port)], cwd=tmp, env=server_env(),
                            stdout=open(os.path.join(tmp, "server.log"), "w"), stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(base + "/login", timeout=1)
                break
            except Exception:
                time.sleep(0.25)
        asyncio.run(drive(base, tmp, has_gemini))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if all(results):
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print("  server log:", os.path.join(tmp, "server.log"))
    print(f"\n{sum(results)}/{len(results)} passed")
    sys.exit(0 if results and all(results) else 1)


if __name__ == "__main__":
    main()
