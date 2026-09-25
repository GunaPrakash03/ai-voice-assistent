"""Clio CRM & Legal Practice Management Connector.

Integrates Voice Agent Service with:
1. Clio Grow Public Inbox API: Asynchronous post-call lead creation, custom fields, and transcript ingestion.
2. Clio Manage REST API v4: Live conflict checks and calendar appointment booking.
"""

import logging
import os
import re
import urllib.request
import urllib.error
import json
from typing import Any, Dict, List, Optional

log = logging.getLogger("voice-agent.clio")
if not log.handlers:
    logging.basicConfig(level=logging.INFO)

CLIO_GROW_API_URL = "https://grow.clio.com/api/v1/inbox_leads"
CLIO_MANAGE_BASE_URL = "https://app.clio.com/api/v4"


class ClioConnector:
    """Production connector for Clio Grow & Clio Manage."""

    def __init__(
        self,
        grow_inbox_token: Optional[str] = None,
        manage_access_token: Optional[str] = None,
        enabled: Optional[bool] = None,
    ):
        self.grow_token = grow_inbox_token or os.getenv("CLIO_GROW_INBOX_TOKEN", "")
        self.manage_token = manage_access_token or os.getenv("CLIO_MANAGE_ACCESS_TOKEN", "")
        if enabled is not None:
            self.enabled = enabled
        else:
            self.enabled = os.getenv("CLIO_ENABLED", "true").lower() in ("true", "1", "yes")

    def format_grow_payload(
        self,
        extracted_fields: Dict[str, Any],
        transcript_text: str = "",
        recording_url: Optional[str] = None,
        lead_source: str = "AI Voice Agent - Inbound Call",
    ) -> Dict[str, Any]:
        """Formats extracted entities into the Clio Grow /inbox_leads JSON schema."""
        raw_name = extracted_fields.get("client_name") or "New Caller"
        parts = raw_name.strip().split()
        first_name = parts[0] if parts else "New"
        last_name = " ".join(parts[1:]) if len(parts) > 1 else "Lead"

        phone = extracted_fields.get("contact_phone") or ""
        email = extracted_fields.get("contact_email") or ""
        case_type = extracted_fields.get("case_type") or "Legal Intake"
        incident_date = extracted_fields.get("injury_date") or ""

        # Construct legal intake message body
        msg_lines = [
            f"=== AI VOICE AGENT INTAKE SUMMARY ===",
            f"Case Type: {case_type}",
        ]
        if incident_date:
            msg_lines.append(f"Incident / Event Date: {incident_date}")
        if extracted_fields.get("needs_urgent_consultation"):
            msg_lines.append("Urgency: HIGH - Immediate consultation requested")
        if recording_url:
            msg_lines.append(f"Call Recording: {recording_url}")
        
        if transcript_text:
            msg_lines.append("\n=== CONVERSATION TRANSCRIPT SNIPPET ===")
            msg_lines.append(transcript_text[:1200] + ("..." if len(transcript_text) > 1200 else ""))

        payload = {
            "inbox_lead": {
                "first_name": first_name,
                "last_name": last_name,
                "phone": phone,
                "email": email,
                "message": "\n".join(msg_lines),
                "source": lead_source,
                "referrer": "Voice Agent Telephony Gateway",
            }
        }
        return payload

    def send_lead_to_clio_grow(
        self,
        extracted_fields: Dict[str, Any],
        transcript_text: str = "",
        recording_url: Optional[str] = None,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """Posts a new lead directly to Clio Grow Inbox API."""
        if not self.enabled:
            log.info("Clio integration disabled (CLIO_ENABLED=false), skipping.")
            return {"status": "skipped", "reason": "disabled"}

        if not self.grow_token:
            log.warning("CLIO_GROW_INBOX_TOKEN not configured. Lead was not sent to Clio Grow.")
            return {"status": "skipped", "reason": "missing_token"}

        payload = self.format_grow_payload(extracted_fields, transcript_text, recording_url)
        body_bytes = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            CLIO_GROW_API_URL,
            data=body_bytes,
            headers={
                "Authorization": f"Bearer {self.grow_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Voice-Agent-Service/1.0",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp_data = resp.read().decode("utf-8")
                code = resp.status
                log.info("Successfully pushed lead to Clio Grow (HTTP %d)", code)
                return {"status": "success", "code": code, "response": resp_data}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            log.error("Clio Grow API HTTP error %d: %s", e.code, err_body)
            return {"status": "error", "code": e.code, "error": err_body}
        except Exception as e:
            log.error("Clio Grow connection error: %s", e)
            return {"status": "error", "error": str(e)}

    def check_manage_conflict(self, party_name: str, timeout: float = 5.0) -> Dict[str, Any]:
        """Queries Clio Manage v4 /conflicts.json to check adverse party conflicts."""
        if not self.manage_token:
            return {"status": "error", "error": "CLIO_MANAGE_ACCESS_TOKEN not set"}

        query_str = urllib.parse.quote(party_name)
        url = f"{CLIO_MANAGE_BASE_URL}/conflicts.json?query={query_str}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.manage_token}",
                "Accept": "application/json",
                "User-Agent": "Voice-Agent-Service/1.0",
            },
            method="GET",
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                matches = data.get("data", [])
                return {
                    "status": "success",
                    "conflict_detected": len(matches) > 0,
                    "matches": matches,
                    "count": len(matches),
                }
        except Exception as e:
            log.error("Clio Manage conflict check error: %s", e)
            return {"status": "error", "error": str(e)}

    def create_manage_contact(
        self,
        first_name: str,
        last_name: str,
        phone: Optional[str] = None,
        email: Optional[str] = None,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """Creates a Person contact in Clio Manage via API v4."""
        token = self.manage_token or os.getenv("CLIO_MANAGE_ACCESS_TOKEN")
        if not token:
            return {"status": "error", "error": "CLIO_MANAGE_ACCESS_TOKEN not configured"}

        payload: Dict[str, Any] = {
            "data": {
                "first_name": first_name,
                "last_name": last_name or "Client",
                "type": "Person",
            }
        }
        if phone:
            payload["data"]["phone_numbers"] = [{"name": "Mobile", "number": phone, "default_number": True}]
        if email:
            payload["data"]["email_addresses"] = [{"name": "Work", "address": email, "default_email": True}]

        req = urllib.request.Request(
            f"{CLIO_MANAGE_BASE_URL}/contacts.json",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Voice-Agent-Service/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                contact_info = data.get("data", {})
                log.info("Created Clio Manage contact: %s (ID: %s)", contact_info.get("name"), contact_info.get("id"))
                return {"status": "success", "contact_id": contact_info.get("id"), "contact": contact_info}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            log.error("Clio Manage create contact error %d: %s", e.code, err_body)
            return {"status": "error", "code": e.code, "error": err_body}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def create_manage_matter(
        self,
        client_id: int,
        description: str,
        status: str = "Open",
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """Creates a Matter in Clio Manage linked to a client contact."""
        token = self.manage_token or os.getenv("CLIO_MANAGE_ACCESS_TOKEN")
        if not token:
            return {"status": "error", "error": "CLIO_MANAGE_ACCESS_TOKEN not configured"}

        payload = {
            "data": {
                "client": {"id": client_id},
                "description": description,
                "status": status,
            }
        }
        req = urllib.request.Request(
            f"{CLIO_MANAGE_BASE_URL}/matters.json",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Voice-Agent-Service/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                matter_info = data.get("data", {})
                log.info("Created Clio Manage matter: %s (ID: %s)", matter_info.get("display_number"), matter_info.get("id"))
                return {"status": "success", "matter_id": matter_info.get("id"), "matter": matter_info}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            log.error("Clio Manage create matter error %d: %s", e.code, err_body)
            return {"status": "error", "code": e.code, "error": err_body}
        except Exception as e:
            return {"status": "error", "error": str(e)}


clio_connector = ClioConnector()

