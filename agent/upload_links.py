"""
Private upload links for callers (web/documents-plan.html, Phase 3).

A link lets someone add files to ONE case, and remove the ones they sent through that same link while it is
valid (a wrong upload); it can't list, open or delete anything else on the case. It is a random token (only its SHA-256 is stored), valid for LINK_DAYS, good for MAX_FILES files.
Sending a new link for a case retires the older ones.

``send_link`` makes a link and delivers it: by email when there's a usable address and email is set up,
otherwise by text message to the caller's phone. A link is never sent to a caller when the server has no
public address (PUBLIC_BASE_URL), because it would only open on this computer; the case records why.

Storage follows agent/case_documents.py: ``app_documents`` collection ``upload_links`` with DATABASE_URL,
else config/upload_links.json. Never raises from send_link: the outcome is returned and written to the case.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from agent.case_documents import DocumentError

log = logging.getLogger("upload-links")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_PATH = os.path.join(ROOT_DIR, "config", "upload_links.json")
COLLECTION = "upload_links"
LINK_DAYS = 7
MAX_FILES = 20
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class UploadLink:
    link_id: str
    token_hash: str
    case_id: str
    workspace_id: str
    expires_at: float
    max_files: int = MAX_FILES
    files_used: int = 0
    uploaded: List[str] = field(default_factory=list)     # names of the files sent through this link
    uploaded_ids: List[str] = field(default_factory=list) # their case-document ids (same order): what the caller may remove
    revoked: bool = False
    channel: str = ""                                      # email | sms | copy
    sent_to: str = ""
    created_by: str = ""                                   # "call" or the staff member's name
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def state(self) -> str:
        if self.revoked:
            return "replaced"
        if time.time() >= self.expires_at:
            return "expired"
        if self.files_used >= self.max_files:
            return "used_up"
        return "active"


class UploadLinkStore:
    def __init__(self, path: str = LOCAL_PATH):
        self.path = path
        self._lock = threading.RLock()
        self._links: Dict[str, UploadLink] = {}
        self._mtime = 0.0
        self._load()

    def _load(self) -> None:
        docs: List[Dict[str, Any]] = []
        if os.getenv("DATABASE_URL"):
            from agent import storage
            if not storage.available():
                return
            docs = storage.load_collection(COLLECTION)
        elif os.path.isfile(self.path):
            try:
                self._mtime = os.path.getmtime(self.path)
                with open(self.path, "r", encoding="utf-8") as f:
                    docs = json.load(f).get("links") or []
            except Exception as e:
                log.warning("Failed to load upload links: %s", e)
                return
        known = set(UploadLink.__dataclass_fields__)
        links = {}
        for d in docs:
            try:
                link = UploadLink(**{k: v for k, v in d.items() if k in known})
                links[link.link_id] = link
            except TypeError as e:
                log.warning("Skipping malformed upload link: %s", e)
        with self._lock:
            self._links = links

    def _fresh(self, link_id: str) -> Optional[UploadLink]:
        """The link as stored now (another process may have used it)."""
        if os.getenv("DATABASE_URL"):
            from agent import storage
            d = storage.load_document(COLLECTION, link_id)
            if d:
                known = set(UploadLink.__dataclass_fields__)
                self._links[link_id] = UploadLink(**{k: v for k, v in d.items() if k in known})
        elif os.path.isfile(self.path) and os.path.getmtime(self.path) != self._mtime:
            self._load()
        return self._links.get(link_id)

    def _save(self, link: UploadLink) -> None:
        link.updated_at = time.time()
        if os.getenv("DATABASE_URL"):
            from agent import storage
            if not storage.save_document(COLLECTION, link.link_id, link.to_dict(), link.updated_at):
                raise DocumentError("The link couldn't be saved: the database is unavailable.", 503)
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"links": [l.to_dict() for l in self._links.values()], "updated_at": time.time()}, f, indent=2)
        os.replace(tmp, self.path)
        self._mtime = os.path.getmtime(self.path)

    def create(self, case_id: str, workspace_id: str, created_by: str = "call", days: int = LINK_DAYS) -> Tuple[UploadLink, str]:
        """A new link for the case (older ones are retired). Returns (link, token); the token is shown once."""
        token = secrets.token_urlsafe(32)
        with self._lock:
            if not os.getenv("DATABASE_URL"):
                self._load()
            for old in [l for l in self._links.values() if l.case_id == case_id and not l.revoked]:
                old.revoked = True
                self._save(old)
            link = UploadLink(link_id="ul-" + secrets.token_hex(6), token_hash=token_hash(token), case_id=case_id,
                              workspace_id=workspace_id, expires_at=time.time() + days * 86400, created_by=created_by)
            self._links[link.link_id] = link
            self._save(link)
        return link, token

    def resolve(self, token: str) -> UploadLink:
        """The usable link behind a token, or DocumentError saying why not (404 unknown, 410 no longer valid)."""
        if not token or not TOKEN_RE.match(token):
            raise DocumentError("This upload link isn't valid. Check you copied the whole link.", 404)
        h = token_hash(token)
        with self._lock:
            if os.getenv("DATABASE_URL"):
                from agent import storage
                docs = [d for d in storage.load_collection(COLLECTION) if d.get("token_hash") == h]
                link = None
                if docs:
                    known = set(UploadLink.__dataclass_fields__)
                    link = UploadLink(**{k: v for k, v in docs[0].items() if k in known})
                    self._links[link.link_id] = link
            else:
                self._fresh("")
                link = next((l for l in self._links.values() if secrets.compare_digest(l.token_hash, h)), None)
        if not link:
            raise DocumentError("This upload link isn't valid. Check you copied the whole link.", 404)
        state = link.state()
        if state == "replaced":
            raise DocumentError("This link was replaced by a newer one. Please use the most recent link we sent you.", 410)
        if state == "expired":
            raise DocumentError("This link has expired. Call the firm and ask for a new one.", 410)
        if state == "used_up":
            raise DocumentError(f"This link has already been used for {link.max_files} files. Call the firm if you need to send more.", 410)
        return link

    def record_upload(self, link_id: str, name: str, doc_id: str = "") -> UploadLink:
        with self._lock:
            link = self._fresh(link_id)
            if not link:
                raise DocumentError("This upload link isn't valid.", 404)
            link.files_used += 1
            link.uploaded.append(name)
            link.uploaded_ids.append(doc_id)
            self._save(link)
            return link

    def sent_files(self, link: UploadLink) -> List[Dict[str, str]]:
        """What the caller sent through this link: [{"id", "name"}] (older links have names only)."""
        ids = list(link.uploaded_ids) + [""] * (len(link.uploaded) - len(link.uploaded_ids))
        return [{"id": i, "name": n} for i, n in zip(ids, link.uploaded)]

    def record_removal(self, link_id: str, doc_id: str) -> UploadLink:
        """Forgets a file the caller removed and gives its slot back."""
        with self._lock:
            link = self._fresh(link_id)
            if not link or doc_id not in link.uploaded_ids:
                raise DocumentError("That file wasn't sent with this link.", 404)
            i = link.uploaded_ids.index(doc_id)
            link.uploaded_ids.pop(i)
            if i < len(link.uploaded):
                link.uploaded.pop(i)
            link.files_used = max(0, link.files_used - 1)
            self._save(link)
            return link

    def mark_sent(self, link_id: str, channel: str, to: str) -> None:
        with self._lock:
            link = self._links.get(link_id)
            if link:
                link.channel, link.sent_to = channel, to
                self._save(link)

    def revoke(self, link_id: str) -> None:
        with self._lock:
            link = self._links.get(link_id)
            if link and not link.revoked:
                link.revoked = True
                self._save(link)

    def latest_for_case(self, case_id: str) -> Optional[UploadLink]:
        if not os.getenv("DATABASE_URL"):
            self._fresh("")
        links = [l for l in self._links.values() if l.case_id == case_id]
        return max(links, key=lambda l: l.created_at) if links else None


upload_links = UploadLinkStore()


# ── Sending ──────────────────────────────────────────────────────────────────────────────────────────
def link_url(base: str, token: str) -> str:
    return f"{base.rstrip('/')}/upload/{token}"


def _first_name(name: str) -> str:
    first = (name or "").strip().split(" ")[0]
    return first if first and first.lower() not in ("unknown", "caller") else ""


def email_body(firm: str, first: str, url: str, days: int) -> Tuple[str, str, str]:
    firm = firm or "the law firm"
    hello = f"Hi {first}," if first else "Hello,"
    subject = f"Upload your documents for {firm}"
    text = (f"{hello}\n\nThank you for calling {firm}. Use this private link to send us documents about your case, "
            f"such as photos, a police report or medical bills:\n\n{url}\n\n"
            f"The link works for {days} days and only lets you add files to your own case. Up to {MAX_FILES} files, "
            f"20 MB each (PDF, Word, photos).\n\nIf you didn't call {firm}, you can ignore this email.\n")
    from html import escape
    html = (f"<p>{escape(hello)}</p><p>Thank you for calling {escape(firm)}. Use this private link to send us documents "
            f"about your case, such as photos, a police report or medical bills:</p>"
            f"<p><a href=\"{escape(url)}\" style=\"display:inline-block;padding:10px 18px;background:#C2560F;color:#fff;"
            f"border-radius:6px;text-decoration:none;font-weight:600\">Upload documents</a></p>"
            f"<p style=\"color:#555;font-size:13px\">Or open: {escape(url)}<br>The link works for {days} days and only lets "
            f"you add files to your own case. Up to {MAX_FILES} files, 20 MB each.</p>"
            f"<p style=\"color:#888;font-size:12px\">If you didn't call {escape(firm)}, you can ignore this email.</p>")
    return subject, text, html


def sms_body(firm: str, url: str, days: int) -> str:
    return f"{firm or 'Your law firm'}: upload documents for your case here (private link, valid {days} days): {url}"


def _phone_ok(phone: str) -> str:
    digits = re.sub(r"[^\d+]", "", phone or "")
    plain = digits.lstrip("+")
    if 10 <= len(plain) <= 15:
        return ("+" if digits.startswith("+") else ("+1" if len(plain) == 10 else "+")) + plain
    return ""


def send_link(case, channel: str = "auto", email: str = "", phone: str = "", base: str = "",
              created_by: str = "call", firm: str = "") -> Dict[str, Any]:
    """Makes a link for ``case`` and delivers it. channel: auto | email | sms | copy.
    Returns {"ok", "channel", "to", "error", "url" (copy only), "expires_at"} and records it on the case."""
    from agent import mailer, sms
    from agent.case_manager import case_manager
    from agent.telephony_manager import public_base_url
    req = case.documents_request or {}
    email = (email or req.get("email") or case.email or "").strip()
    phone = _phone_ok(phone or req.get("phone") or case.phone or "")
    base = (base or public_base_url()).rstrip("/")
    result: Dict[str, Any] = {"ok": False, "channel": "", "to": "", "error": ""}

    if channel == "copy":
        if not base:
            base = "http://localhost:8091"
        link, token = upload_links.create(case.case_id, case.workspace_id, created_by=created_by)
        upload_links.mark_sent(link.link_id, "copy", "")
        result.update(ok=True, channel="copy", url=link_url(base, token), expires_at=link.expires_at)
        _record(case_manager, case, result, link)
        return result

    if not base:
        result["error"] = ("No public web address is set (PUBLIC_BASE_URL), so a link would only open on this "
                           "computer. Set it, or use Copy link from the case.")
        _record(case_manager, case, result, None)
        return result

    mail_ready = mailer.configured().get("ready")
    use = channel
    if channel == "auto":
        use = "email" if (EMAIL_RE.match(email) and mail_ready) else ("sms" if phone else "email")
    if use == "email":
        if not EMAIL_RE.match(email):
            result["error"] = "No valid email address for this caller."
        elif not mail_ready:
            result["error"] = "Email isn't set up on this server (Email delivery page)."
    elif use == "sms":
        if not phone:
            result["error"] = "No mobile number for this caller."
    else:
        result["error"] = f"Unknown channel '{channel}'."
    if result["error"]:
        _record(case_manager, case, result, None)
        return result

    firm = firm or _firm(case.workspace_id)
    link, token = upload_links.create(case.case_id, case.workspace_id, created_by=created_by)
    url = link_url(base, token)
    if use == "email":
        subject, text, html = email_body(firm, _first_name(case.client_name), url, LINK_DAYS)
        sent = mailer.send_email(email, subject, text, html)
        to = email
    else:
        sent = sms.send_sms(phone, sms_body(firm, url, LINK_DAYS))
        to = phone
    if not sent.get("ok"):
        upload_links.revoke(link.link_id)
        result.update(channel=use, to=to, error=f"Sending failed: {sent.get('error') or 'unknown error'}")
        _record(case_manager, case, result, None)
        return result
    upload_links.mark_sent(link.link_id, use, to)
    result.update(ok=True, channel=use, to=to, expires_at=link.expires_at)
    if sent.get("dry_run"):
        result["dry_run"] = True
    _record(case_manager, case, result, link)
    log.info("Upload link for case %s sent by %s to %s", case.case_id, use, to)
    return result


def _firm(workspace_id: str) -> str:
    try:
        from agent.firm_context import firm_name
        return firm_name(workspace_id)
    except Exception:
        return ""


def _record(case_manager, case, result: Dict[str, Any], link: Optional[UploadLink]) -> None:
    changes: Dict[str, Any] = {"status": "link_sent" if result["ok"] else "not_sent",
                               "channel": result.get("channel", ""), "to": result.get("to", ""),
                               "error": result.get("error", ""), "updated_at": time.time()}
    if result["ok"] and link:
        changes.update(sent_at=time.time(), expires_at=link.expires_at, link_id=link.link_id)
    if not (case.documents_request or {}).get("at"):
        changes["at"] = time.time()
    try:
        case_manager.update_documents_request(case.case_id, changes)
    except Exception as e:
        log.warning("Could not record the upload link on case %s: %s", case.case_id, e)


# ── Telling the firm (Phase 4) ───────────────────────────────────────────────────────────────────────
NOTIFY_EVERY = 600      # one email per case per 10 minutes: a caller usually sends several files in a row


def lead_emails_for_workspace(workspace_id: str) -> List[str]:
    """The lead-email addresses of the firm's agents (server-wide agents count for the default firm)."""
    try:
        from agent.agent_builder import STATE_FILE
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            agents = json.load(f).get("agents", [])
    except Exception:
        return []
    out: List[str] = []
    for a in agents:
        ws = a.get("workspace_id") or "ws-default"
        if ws == (workspace_id or "ws-default"):
            out += [e for e in (a.get("lead_emails") or []) if e not in out]
    return out


def notify_body(firm: str, client: str, count: int, url: str) -> Tuple[str, str]:
    who = client or "A caller"
    files = f"{count} new document{'s' if count != 1 else ''}"
    subject = f"{who} sent {files}" + (f" · {firm}" if firm else "")
    text = (f"{who} sent {files} for their case through the upload link.\n\n"
            f"Open the case to see them: {url}\n\n"
            "The files themselves are not attached to this email; they are only on the case.\n")
    return subject, text


def notify_new_documents(case, recipients: List[str], base: str, count: int) -> Dict[str, Any]:
    """Emails the firm that a caller added files. Never raises; returns {"ok", "sent_to", "error"}."""
    from agent import mailer
    recipients = [r for r in dict.fromkeys(recipients) if EMAIL_RE.match(r or "")]
    if not recipients:
        return {"ok": False, "sent_to": [], "error": "no recipients"}
    if not mailer.configured().get("ready"):
        return {"ok": False, "sent_to": [], "error": "email isn't set up"}
    subject, text = notify_body(_firm(case.workspace_id), case.client_name, count, f"{base.rstrip('/')}/cases")
    sent_to, errors = [], []
    for r in recipients:
        res = mailer.send_email(r, subject, text)
        (sent_to if res.get("ok") else errors).append(r if res.get("ok") else f"{r}: {res.get('error')}")
    return {"ok": bool(sent_to), "sent_to": sent_to, "error": "; ".join(errors)}
