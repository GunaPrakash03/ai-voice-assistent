"""
Outbound SMS for one-time sign-in codes.

Sends through Twilio's Messages API using the account already configured for telephony
(TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN) and the workspace's SMS-capable Twilio number.

Environment:
  TWILIO_SMS_FROM   override the sender number (default: first active Twilio DID with "sms").
  SMS_DRY_RUN=1     do not call Twilio; log the message instead (local testing).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Optional

log = logging.getLogger("sms")


def sender_number() -> Optional[str]:
    override = (os.getenv("TWILIO_SMS_FROM") or "").strip()
    if override:
        return override
    try:
        from agent.telephony_manager import telephony_manager
        for rec in telephony_manager._owned_numbers.values():
            if rec.status == "active" and getattr(rec, "carrier", "") == "twilio" and "sms" in (rec.capabilities or []):
                return rec.phone_number
    except Exception as e:
        log.debug("no telephony numbers available for SMS: %s", e)
    return None


def configured() -> Dict[str, object]:
    sid = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
    tok = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
    frm = sender_number()
    return {"ready": bool(sid and tok and frm) or os.getenv("SMS_DRY_RUN") == "1", "from": frm,
            "reason": "" if (sid and tok) else "TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not configured" if not frm else ""}


def send_sms(to: str, body: str) -> Dict[str, object]:
    """Returns {"ok": bool, "sid": ..., "error": ...}. Never raises."""
    if os.getenv("SMS_DRY_RUN") == "1":
        log.info("[SMS dry run] to=%s body=%r", to, body)
        return {"ok": True, "sid": "dry-run", "dry_run": True}
    sid = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
    tok = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
    frm = sender_number()
    if not (sid and tok):
        return {"ok": False, "error": "Twilio credentials are not configured (API Keys page → Twilio)"}
    if not frm:
        return {"ok": False, "error": "No SMS-capable Twilio number in this workspace"}
    data = urllib.parse.urlencode({"From": frm, "To": to, "Body": body}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        data=data,
        headers={"Authorization": "Basic " + base64.b64encode(f"{sid}:{tok}".encode()).decode(),
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            return {"ok": True, "sid": payload.get("sid"), "status": payload.get("status")}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8", "replace")).get("message", "")
        except Exception:
            pass
        log.warning("Twilio SMS to %s failed: HTTP %s %s", to, e.code, detail)
        return {"ok": False, "error": f"Twilio error {e.code}: {detail or e.reason}"}
    except Exception as e:
        log.warning("Twilio SMS to %s failed: %s", to, e)
        return {"ok": False, "error": f"SMS could not be sent: {e}"}
