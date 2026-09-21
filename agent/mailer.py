"""Outbound email for account flows (password reset). Two transports, picked by what is configured:

- Resend HTTP API when RESEND_API_KEY is set (no SMTP port needed; works from Railway).
- SMTP when SMTP_HOST is set (SMTP_PORT 587 STARTTLS by default, 465 for implicit TLS,
  SMTP_USER / SMTP_PASSWORD optional).

MAIL_FROM is the sender on either transport, e.g. "Voice Agent <no-reply@yourdomain.com>".
Nothing is configured → configured()["ready"] is False and send_email() reports why, so callers
can show a clear message instead of failing silently.
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
import urllib.request
from email.message import EmailMessage
from typing import Dict

log = logging.getLogger("voice-agent.mailer")


def _from_address() -> str:
    addr = (os.getenv("MAIL_FROM") or os.getenv("SMTP_FROM") or "").strip()
    if not addr:
        u = (os.getenv("SMTP_USER") or "").strip()
        if "@" in u:
            addr = u
    return addr


def configured() -> Dict[str, object]:
    """Which transport will be used and whether it is usable."""
    sender = _from_address()
    if os.getenv("RESEND_API_KEY", "").strip():
        return {"ready": bool(sender), "transport": "resend", "from": sender,
                "reason": "" if sender else "MAIL_FROM is not set"}
    if os.getenv("SMTP_HOST", "").strip():
        return {"ready": bool(sender), "transport": "smtp", "from": sender,
                "reason": "" if sender else "MAIL_FROM (or SMTP_USER containing '@') is not set"}
    return {"ready": False, "transport": "", "from": sender,
            "reason": "No email transport: set RESEND_API_KEY or SMTP_HOST (+ MAIL_FROM)"}


def send_email(to: str, subject: str, text: str, html: str = "") -> Dict[str, object]:
    """Send one message. Returns {"ok": bool, "transport": str, "error": str}."""
    cfg = configured()
    if not cfg["ready"]:
        return {"ok": False, "transport": cfg["transport"], "error": str(cfg["reason"])}
    try:
        if cfg["transport"] == "resend":
            return _send_resend(to, subject, text, html)
        return _send_smtp(to, subject, text, html)
    except Exception as e:  # network / auth failures surface as a message, never a crash
        log.warning("Email to %s failed via %s: %s", to, cfg["transport"], e)
        return {"ok": False, "transport": cfg["transport"], "error": str(e)}


def _send_resend(to: str, subject: str, text: str, html: str) -> Dict[str, object]:
    payload = {"from": _from_address(), "to": [to], "subject": subject, "text": text}
    if html:
        payload["html"] = html
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {os.getenv('RESEND_API_KEY', '').strip()}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read() or b"{}")
        log.info("Email sent to %s via Resend (id=%s)", to, body.get("id"))
        return {"ok": True, "transport": "resend", "error": ""}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        return {"ok": False, "transport": "resend", "error": f"Resend HTTP {e.code}: {detail}"}


def _send_smtp(to: str, subject: str, text: str, html: str) -> Dict[str, object]:
    host = os.getenv("SMTP_HOST", "").strip()
    port = int(os.getenv("SMTP_PORT", "587") or 587)
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    tls_mode = (os.getenv("SMTP_TLS") or "").strip().lower()
    use_ssl = tls_mode in ("ssl", "tls") or (port == 465 and tls_mode != "starttls")
    allow_insecure = os.getenv("SMTP_ALLOW_INSECURE") == "1"

    msg = EmailMessage()
    msg["From"] = _from_address()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    ctx = ssl.create_default_context()
    if use_ssl:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=20) as s:
            if user:
                s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.ehlo()
            if s.has_extn("starttls"):
                s.starttls(context=ctx)
                s.ehlo()
            elif not allow_insecure and (user or password):
                raise RuntimeError(
                    f"SMTP server {host}:{port} does not advertise STARTTLS. "
                    "Refusing to transmit credentials in plaintext (set SMTP_ALLOW_INSECURE=1 to bypass)."
                )
            if user:
                s.login(user, password)
            s.send_message(msg)
    log.info("Email sent to %s via SMTP %s:%s", to, host, port)
    return {"ok": True, "transport": "smtp", "error": ""}
