"""
Task 2.1 — SIP Gateway & Telephony Integration Manager.

Components:
1. SIP Trunk Models (Inbound & Outbound Carrier Trunks: Twilio, Telnyx, Generic SIP).
2. SIP Dispatch Rules Engine (Inbound DID phone number routing to agent rooms).
3. Outbound Phone Dialer API (Programmatic outbound calling via LiveKit SIP).
4. Telephony Metadata Parser (Caller ID, dialed DID, call direction, carrier headers).
5. Phone Number Inventory & Direct Purchasing Engine (Browse, provision, re-route, and release DIDs).
6. Simulated Telephony Engine (Offline verification when external carrier credentials are pending).
"""

import asyncio
import ipaddress
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional
import requests
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("telephony-manager")

E164_REGEX = re.compile(r"^\+?[1-9]\d{1,14}$")
CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")
PHONE_NUMBERS_FILE = os.path.join(CONFIG_DIR, "phone_numbers.json")
SIP_TRUNKS_FILE = os.path.join(CONFIG_DIR, "sip_trunks.json")


def _storage_sync(path: str) -> None:
    """Write-through to PostgreSQL (no-op when DATABASE_URL is unset)."""
    try:
        from agent import storage
        storage.sync_file(path)
    except Exception as e:
        log.debug("storage sync skipped for %s: %s", path, e)
VALID_TRANSPORTS = ("udp", "tcp", "tls")


class CallDirection(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class CallStatus(str, Enum):
    INITIATED = "initiated"
    RINGING = "ringing"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class PhoneNumberRecord:
    phone_number: str
    friendly_name: str
    country: str
    region: str
    capabilities: List[str]  # ["voice", "sip", "sms", "dual_channel"]
    monthly_cost: float
    status: str = "active"  # "active" | "released"
    carrier: str = "telnyx"  # "telnyx" | "twilio"
    assigned_trunk_id: str = "trunk-inbound-primary"
    assigned_agent: str = "Intake Agent"
    purchased_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SIPInboundTrunk:
    trunk_id: str
    name: str
    numbers: List[str]  # E.164 phone numbers (e.g. ["+18005550199"])
    allowed_addresses: List[str] = field(default_factory=list)  # Carrier IP whitelists
    auth_username: Optional[str] = None
    auth_password: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def matches_number(self, phone_number: str) -> bool:
        norm = normalize_phone_number(phone_number)
        return any(normalize_phone_number(n) == norm for n in self.numbers)


@dataclass
class SIPOutboundTrunk:
    trunk_id: str
    name: str
    address: str  # Carrier SIP URI/host (e.g. "sip.telnyx.com:5060" or "sip.twilio.com")
    numbers: List[str]  # Outbound Caller IDs (e.g. ["+18005550199"])
    auth_username: Optional[str] = None
    auth_password: Optional[str] = None
    transport: str = "udp"  # "udp" | "tcp" | "tls"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SIPDispatchRule:
    rule_id: str
    name: str
    trunk_ids: List[str]  # Trunks this rule applies to
    room_prefix: str = "sip-"
    agent_name: str = "VoiceAssistantAgent"
    pin: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def generate_room_name(self, caller_number: str) -> str:
        clean = re.sub(r"[^\w-]", "", caller_number)
        return f"{self.room_prefix}{clean}-{int(time.time())}"


@dataclass
class TelephonyCallRecord:
    call_id: str
    direction: CallDirection
    from_number: str
    to_number: str
    room_name: str
    participant_identity: str
    status: CallStatus
    created_at: float = field(default_factory=time.time)
    answered_at: Optional[float] = None
    ended_at: Optional[float] = None
    duration_seconds: float = 0.0
    sip_trunk_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


def normalize_phone_number(number: str) -> str:
    """Normalize phone number to E.164-compatible format."""
    if not number:
        return ""
    cleaned = re.sub(r"[^\d+]", "", number)
    if not cleaned.startswith("+"):
        if len(cleaned) == 10:
            cleaned = "+1" + cleaned
        elif len(cleaned) == 11 and cleaned.startswith("1"):
            cleaned = "+" + cleaned
        else:
            cleaned = "+" + cleaned
    return cleaned


def format_friendly_phone(number: str) -> str:
    norm = normalize_phone_number(number)
    if norm.startswith("+1") and len(norm) == 12:
        return f"+1 ({norm[2:5]}) {norm[5:8]}-{norm[8:]}"
    elif norm.startswith("+44") and len(norm) >= 12:
        return f"+44 {norm[3:7]} {norm[7:]}"
    return norm or number


def is_valid_phone_number(number: str) -> bool:
    norm = normalize_phone_number(number)
    return bool(E164_REGEX.match(norm))


def _mask_secret(val: Optional[str]) -> Optional[str]:
    if not val:
        return None
    return "••••••••"


def validate_inbound_trunk_payload(trunk_id: str, name: str, numbers: List[str], allowed_addresses: List[str], transport: str = "udp") -> List[str]:
    """Returns a list of human-readable validation errors (empty = valid)."""
    errors: List[str] = []
    if not trunk_id or not re.match(r"^[A-Za-z0-9_.-]{3,64}$", trunk_id):
        errors.append("trunk_id must be 3-64 chars of letters, digits, '-', '_' or '.'")
    if not name or not name.strip():
        errors.append("name is required")
    if not isinstance(numbers, list):
        errors.append("numbers must be a list of E.164 phone numbers")
    else:
        bad = [n for n in numbers if not is_valid_phone_number(str(n))]
        if bad:
            errors.append(f"invalid phone number(s): {', '.join(str(b) for b in bad)}")
    for addr in (allowed_addresses or []):
        try:
            ipaddress.ip_network(str(addr), strict=False)
        except ValueError:
            errors.append(f"allowed_addresses entry '{addr}' is not an IP address or CIDR block")
    if transport and transport.lower() not in VALID_TRANSPORTS:
        errors.append(f"transport must be one of {', '.join(VALID_TRANSPORTS)}")
    return errors


def validate_outbound_trunk_payload(trunk_id: str, name: str, address: str, numbers: List[str], transport: str) -> List[str]:
    errors = validate_inbound_trunk_payload(trunk_id, name, numbers, [], transport)
    if not address or not address.strip():
        errors.append("address (carrier SIP host, e.g. sip.telnyx.com:5060) is required")
    elif not re.match(r"^(sip:)?[A-Za-z0-9.-]+(:\d{1,5})?$", address.strip()):
        errors.append(f"address '{address}' is not a valid SIP host[:port]")
    if isinstance(numbers, list) and not numbers:
        errors.append("outbound trunk needs at least one caller-ID number")
    return errors


CARRIERS = {
    "telnyx": {"name": "Telnyx", "sip_host": "sip.telnyx.com", "docs_url": "https://portal.telnyx.com/#/app/numbers/search-numbers"},
    "twilio": {"name": "Twilio", "sip_host": "sip.twilio.com", "docs_url": "https://console.twilio.com/us1/develop/phone-numbers/manage/search"},
}

# Inventory is kept per carrier so the marketplace never mixes Telnyx and Twilio DIDs in one list.
AVAILABLE_NUMBERS_CATALOG = [
    # ── Telnyx inventory
    {"phone_number": "+14158000129", "carrier": "telnyx", "friendly_name": "+1 (415) 800-0129", "country": "US", "region": "San Francisco, CA", "capabilities": ["voice", "sip", "sms", "dual_channel"], "monthly_cost": 1.50},
    {"phone_number": "+12125003491", "carrier": "telnyx", "friendly_name": "+1 (212) 500-3491", "country": "US", "region": "New York, NY", "capabilities": ["voice", "sip", "sms", "dual_channel"], "monthly_cost": 1.50},
    {"phone_number": "+13127658920", "carrier": "telnyx", "friendly_name": "+1 (312) 765-8920", "country": "US", "region": "Chicago, IL", "capabilities": ["voice", "sip", "sms", "dual_channel"], "monthly_cost": 1.50},
    {"phone_number": "+15129930415", "carrier": "telnyx", "friendly_name": "+1 (512) 993-0415", "country": "US", "region": "Austin, TX", "capabilities": ["voice", "sip", "sms", "dual_channel"], "monthly_cost": 1.50},
    {"phone_number": "+18005550199", "carrier": "telnyx", "friendly_name": "+1 (800) 555-0199 (Toll-Free)", "country": "US", "region": "Toll-Free North America", "capabilities": ["voice", "sip", "sms", "toll_free"], "monthly_cost": 2.00},
    {"phone_number": "+18885550142", "carrier": "telnyx", "friendly_name": "+1 (888) 555-0142 (Toll-Free)", "country": "US", "region": "Toll-Free North America", "capabilities": ["voice", "sip", "sms", "toll_free"], "monthly_cost": 2.00},
    {"phone_number": "+442079460912", "carrier": "telnyx", "friendly_name": "+44 20 7946 0912", "country": "GB", "region": "London, UK", "capabilities": ["voice", "sip", "dual_channel"], "monthly_cost": 2.50},
    {"phone_number": "+493023125990", "carrier": "telnyx", "friendly_name": "+49 30 2312 5990", "country": "DE", "region": "Berlin, Germany", "capabilities": ["voice", "sip", "dual_channel"], "monthly_cost": 2.50},
    # ── Twilio inventory
    {"phone_number": "+12064129983", "carrier": "twilio", "friendly_name": "+1 (206) 412-9983", "country": "US", "region": "Seattle, WA", "capabilities": ["voice", "sip", "sms", "mms"], "monthly_cost": 1.15},
    {"phone_number": "+16175508821", "carrier": "twilio", "friendly_name": "+1 (617) 550-8821", "country": "US", "region": "Boston, MA", "capabilities": ["voice", "sip", "sms", "mms"], "monthly_cost": 1.15},
    {"phone_number": "+13235550164", "carrier": "twilio", "friendly_name": "+1 (323) 555-0164", "country": "US", "region": "Los Angeles, CA", "capabilities": ["voice", "sip", "sms", "mms"], "monthly_cost": 1.15},
    {"phone_number": "+17865550138", "carrier": "twilio", "friendly_name": "+1 (786) 555-0138", "country": "US", "region": "Miami, FL", "capabilities": ["voice", "sip", "sms", "mms"], "monthly_cost": 1.15},
    {"phone_number": "+18335550177", "carrier": "twilio", "friendly_name": "+1 (833) 555-0177 (Toll-Free)", "country": "US", "region": "Toll-Free North America", "capabilities": ["voice", "sip", "sms", "toll_free"], "monthly_cost": 2.15},
    {"phone_number": "+14165507812", "carrier": "twilio", "friendly_name": "+1 (416) 550-7812", "country": "CA", "region": "Toronto, ON", "capabilities": ["voice", "sip", "sms", "mms"], "monthly_cost": 1.15},
    {"phone_number": "+61291001844", "carrier": "twilio", "friendly_name": "+61 2 9100 1844", "country": "AU", "region": "Sydney, Australia", "capabilities": ["voice", "sip"], "monthly_cost": 3.15},
    {"phone_number": "+442038078115", "carrier": "twilio", "friendly_name": "+44 20 3807 8115", "country": "GB", "region": "London, UK", "capabilities": ["voice", "sip"], "monthly_cost": 1.15},
]


class TelephonyManager:
    """
    Coordinates inbound & outbound SIP trunks, phone number purchasing, routing rules, and calls.
    """

    def __init__(
        self,
        livekit_url: Optional[str] = None,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
    ):
        self.livekit_url = livekit_url or os.getenv("LIVEKIT_URL", "ws://127.0.0.1:7880")
        self.api_key = api_key or os.getenv("LIVEKIT_API_KEY", "")
        self.api_secret = api_secret or os.getenv("LIVEKIT_API_SECRET", "")

        self._inbound_trunks: Dict[str, SIPInboundTrunk] = {}
        self._outbound_trunks: Dict[str, SIPOutboundTrunk] = {}
        self._dispatch_rules: Dict[str, SIPDispatchRule] = {}
        self._calls: Dict[str, TelephonyCallRecord] = {}
        self._owned_numbers: Dict[str, PhoneNumberRecord] = {}

        if not self._load_trunks():
            self._init_default_demo_trunks()
        self._load_phone_numbers()
        self._rebind_numbers_to_trunks()

    # ── Trunk persistence ────────────────────────────────────────────────────
    def _load_trunks(self) -> bool:
        """Load inbound/outbound trunks and dispatch rules from config/sip_trunks.json."""
        if not os.path.isfile(SIP_TRUNKS_FILE):
            return False
        try:
            with open(SIP_TRUNKS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data.get("inbound", []):
                t = SIPInboundTrunk(**item)
                self._inbound_trunks[t.trunk_id] = t
            for item in data.get("outbound", []):
                t = SIPOutboundTrunk(**item)
                self._outbound_trunks[t.trunk_id] = t
            for item in data.get("rules", []):
                r = SIPDispatchRule(**item)
                self._dispatch_rules[r.rule_id] = r
            return bool(self._inbound_trunks or self._outbound_trunks)
        except Exception as e:
            log.warning("Failed to load SIP trunks from disk: %s", e)
            return False

    def _save_trunks(self) -> None:
        try:
            os.makedirs(os.path.dirname(SIP_TRUNKS_FILE), exist_ok=True)
            with open(SIP_TRUNKS_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "inbound": [asdict(t) for t in self._inbound_trunks.values()],
                    "outbound": [asdict(t) for t in self._outbound_trunks.values()],
                    "rules": [asdict(r) for r in self._dispatch_rules.values()],
                    "updated_at": time.time(),
                }, f, indent=2)
            _storage_sync(SIP_TRUNKS_FILE)
        except Exception as e:
            log.warning("Failed to save SIP trunks: %s", e)

    def _number_rule_id(self, norm_number: str) -> str:
        return f"rule-{norm_number.replace('+', '')}"

    def _ensure_number_rule(self, rec: "PhoneNumberRecord") -> SIPDispatchRule:
        """Each owned number gets its own dispatch rule so re-routing one DID never affects another."""
        rule_id = self._number_rule_id(rec.phone_number)
        rule = self._dispatch_rules.get(rule_id)
        if rule is None:
            rule = SIPDispatchRule(
                rule_id=rule_id,
                name=f"DID Routing for {rec.friendly_name or rec.phone_number}",
                trunk_ids=[rec.assigned_trunk_id],
                room_prefix="call-",
                agent_name=rec.assigned_agent,
            )
            self._dispatch_rules[rule_id] = rule
        else:
            rule.trunk_ids = [rec.assigned_trunk_id]
            rule.agent_name = rec.assigned_agent
        return rule

    def _rebind_numbers_to_trunks(self) -> None:
        """After a restart, make sure every active owned DID is on its trunk and has a rule."""
        changed = False
        for rec in self._owned_numbers.values():
            if rec.status != "active":
                continue
            trunk = self._inbound_trunks.get(rec.assigned_trunk_id)
            if trunk is None:
                log.warning("Owned number %s references unknown trunk %s; keeping record, routing falls back to default rule",
                            rec.phone_number, rec.assigned_trunk_id)
            elif rec.phone_number not in trunk.numbers:
                trunk.numbers.append(rec.phone_number)
                changed = True
            if self._number_rule_id(rec.phone_number) not in self._dispatch_rules:
                self._ensure_number_rule(rec)
                changed = True
        if changed:
            self._save_trunks()

    def _init_default_demo_trunks(self):
        """Pre-populate reference carrier trunks and dispatch rules, in memory only. Never persisted:
        a missing/unreadable config/sip_trunks.json (e.g. an image built without config/ baked in)
        must not push these demo placeholders over real trunk data another instance already wrote
        to a shared database."""
        inbound = SIPInboundTrunk(
            trunk_id="trunk-inbound-primary",
            name="Primary Inbound Carrier Trunk",
            numbers=["+18005550199", "+18885550142"],
            allowed_addresses=["192.0.2.0/24", "198.51.100.0/24"],
            metadata={"carrier": "telnyx", "region": "us-east"},
        )
        self.register_inbound_trunk(inbound, persist=False)

        outbound = SIPOutboundTrunk(
            trunk_id="trunk-outbound-primary",
            name="Primary Outbound Carrier Trunk",
            address="sip.telnyx.com:5060",
            numbers=["+18005550199"],
            auth_username=os.getenv("SIP_OUTBOUND_USER", "voice_outbound_user"),
            auth_password=os.getenv("SIP_OUTBOUND_PASS", "secret_sip_pass"),
            metadata={"carrier": "telnyx"},
        )
        self.register_outbound_trunk(outbound, persist=False)

        rule = SIPDispatchRule(
            rule_id="rule-inbound-default",
            name="Default Customer Support Routing",
            trunk_ids=["trunk-inbound-primary"],
            room_prefix="call-",
            agent_name="VoiceAssistantAgent",
        )
        self.register_dispatch_rule(rule, persist=False)

    def _load_phone_numbers(self):
        """Seed or load persistent phone numbers."""
        if os.path.isfile(PHONE_NUMBERS_FILE):
            try:
                with open(PHONE_NUMBERS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for item in data.get("numbers", []):
                        rec = PhoneNumberRecord(**item)
                        self._owned_numbers[rec.phone_number] = rec
            except Exception as e:
                log.warning("Failed to load phone numbers from disk: %s", e)

        if not self._owned_numbers:
            # Seed initial default numbers
            self._owned_numbers["+18005550199"] = PhoneNumberRecord(
                phone_number="+18005550199",
                friendly_name="+1 (800) 555-0199 (Toll-Free)",
                country="US",
                region="Toll-Free North America",
                capabilities=["voice", "sip", "sms", "toll_free"],
                monthly_cost=2.00,
                assigned_trunk_id="trunk-inbound-primary",
                assigned_agent="Intake Agent",
            )
            self._owned_numbers["+18885550142"] = PhoneNumberRecord(
                phone_number="+18885550142",
                friendly_name="+1 (888) 555-0142 (Toll-Free)",
                country="US",
                region="Toll-Free North America",
                capabilities=["voice", "sip", "sms", "toll_free"],
                monthly_cost=2.00,
                assigned_trunk_id="trunk-inbound-primary",
                assigned_agent="Support Agent",
            )
            # Local-file write only — NOT pushed to PostgreSQL. A missing config/phone_numbers.json
            # (e.g. a container image built without config/ baked in) must never overwrite real
            # provisioned numbers that another instance already wrote to a shared database; only an
            # explicit action (purchase/edit/release, via _save_phone_numbers) should push state.
            self._write_phone_numbers_file(sync=False)

    def _save_phone_numbers(self):
        """Save phone numbers to disk and write through to PostgreSQL."""
        self._write_phone_numbers_file(sync=True)

    def _write_phone_numbers_file(self, sync: bool):
        try:
            os.makedirs(os.path.dirname(PHONE_NUMBERS_FILE), exist_ok=True)
            with open(PHONE_NUMBERS_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "numbers": [asdict(n) for n in self._owned_numbers.values()],
                    "updated_at": time.time(),
                }, f, indent=2)
            if sync:
                _storage_sync(PHONE_NUMBERS_FILE)
        except Exception as e:
            log.warning("Failed to save phone numbers: %s", e)

    def has_trunk(self, trunk_id: str) -> bool:
        return trunk_id in self._inbound_trunks or trunk_id in self._outbound_trunks

    def register_inbound_trunk(self, trunk: SIPInboundTrunk, persist: bool = True) -> None:
        trunk.numbers = [normalize_phone_number(n) for n in trunk.numbers]
        self._inbound_trunks[trunk.trunk_id] = trunk
        log.info("Registered Inbound SIP Trunk: %s (%s numbers)", trunk.trunk_id, len(trunk.numbers))
        if persist:
            self._save_trunks()

    def register_outbound_trunk(self, trunk: SIPOutboundTrunk, persist: bool = True) -> None:
        trunk.numbers = [normalize_phone_number(n) for n in trunk.numbers]
        trunk.transport = (trunk.transport or "udp").lower()
        self._outbound_trunks[trunk.trunk_id] = trunk
        log.info("Registered Outbound SIP Trunk: %s -> %s", trunk.trunk_id, trunk.address)
        if persist:
            self._save_trunks()

    def register_dispatch_rule(self, rule: SIPDispatchRule, persist: bool = True) -> None:
        self._dispatch_rules[rule.rule_id] = rule
        log.info("Registered Dispatch Rule: %s -> agent %s", rule.rule_id, rule.agent_name)
        if persist:
            self._save_trunks()

    @staticmethod
    def _public_trunk(trunk) -> dict:
        d = asdict(trunk)
        d["has_auth"] = bool(trunk.auth_username or trunk.auth_password)
        d["auth_password"] = _mask_secret(trunk.auth_password)
        return d

    def delete_inbound_trunk(self, trunk_id: str) -> bool:
        trunk = self._inbound_trunks.pop(trunk_id, None)
        if not trunk:
            return False
        # Remove any dispatch rules associated only with this trunk
        to_del = [rid for rid, r in self._dispatch_rules.items() if r.trunk_ids == [trunk_id]]
        for rid in to_del:
            self._dispatch_rules.pop(rid, None)
        # Update any numbers referencing this trunk
        for rec in self._owned_numbers.values():
            if rec.assigned_trunk_id == trunk_id:
                rec.assigned_trunk_id = "trunk-inbound-primary"
        self._save_trunks()
        self._save_phone_numbers()
        log.info("Deleted Inbound SIP Trunk: %s", trunk_id)
        return True

    def delete_outbound_trunk(self, trunk_id: str) -> bool:
        trunk = self._outbound_trunks.pop(trunk_id, None)
        if not trunk:
            return False
        self._save_trunks()
        log.info("Deleted Outbound SIP Trunk: %s", trunk_id)
        return True

    def delete_dispatch_rule(self, rule_id: str) -> bool:
        rule = self._dispatch_rules.pop(rule_id, None)
        if not rule:
            return False
        self._save_trunks()
        log.info("Deleted Dispatch Rule: %s", rule_id)
        return True

    def list_inbound_trunks(self) -> List[dict]:
        return [self._public_trunk(t) for t in self._inbound_trunks.values()]

    def list_outbound_trunks(self) -> List[dict]:
        return [self._public_trunk(t) for t in self._outbound_trunks.values()]

    def list_dispatch_rules(self) -> List[dict]:
        return [asdict(r) for r in self._dispatch_rules.values()]

    def list_calls(self) -> List[dict]:
        now = time.time()
        for record in self._calls.values():
            if record.status in (CallStatus.ACTIVE, CallStatus.RINGING, CallStatus.INITIATED):
                # Stale test / simulated calls older than 120s without live session auto-complete
                if now - record.created_at > 120:
                    record.status = CallStatus.COMPLETED
                    record.ended_at = now
                    if record.answered_at:
                        record.duration_seconds = round(record.ended_at - record.answered_at, 2)
        return [asdict(c) for c in self._calls.values()]

    def end_all_calls(self) -> List[TelephonyCallRecord]:
        ended = []
        now = time.time()
        for record in self._calls.values():
            if record.status in (CallStatus.ACTIVE, CallStatus.RINGING, CallStatus.INITIATED):
                record.status = CallStatus.COMPLETED
                record.ended_at = now
                if record.answered_at:
                    record.duration_seconds = round(record.ended_at - record.answered_at, 2)
                ended.append(record)
        return ended

    # ── Phone Number Management ──────────────────────────────────────────────
    @staticmethod
    def _get_twilio_creds() -> Optional[tuple]:
        sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
        token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
        if sid and token:
            return (sid, token)
        return None

    @staticmethod
    def _get_telnyx_creds() -> Optional[str]:
        key = os.getenv("TELNYX_API_KEY", "").strip()
        return key if key else None

    def _release_twilio_number(self, rec: PhoneNumberRecord) -> None:
        if rec.carrier == "twilio" or "twilio_sid" in rec.metadata:
            creds = self._get_twilio_creds()
            if creds:
                sid, token = creds
                twilio_sid = rec.metadata.get("twilio_sid")
                if not twilio_sid:
                    try:
                        r = requests.get(
                            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/IncomingPhoneNumbers.json?PhoneNumber={requests.utils.quote(rec.phone_number)}",
                            auth=(sid, token),
                            timeout=5,
                        )
                        if r.status_code == 200:
                            data = r.json().get("incoming_phone_numbers", [])
                            if data:
                                twilio_sid = data[0].get("sid")
                    except Exception as e:
                        log.warning("Failed to lookup Twilio SID for %s: %s", rec.phone_number, e)

                if twilio_sid:
                    try:
                        del_r = requests.delete(
                            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/IncomingPhoneNumbers/{twilio_sid}.json",
                            auth=(sid, token),
                            timeout=5,
                        )
                        log.info("Twilio API delete number %s (%s) response status: %s", rec.phone_number, twilio_sid, del_r.status_code)
                    except Exception as e:
                        log.warning("Failed to delete Twilio number %s via API: %s", rec.phone_number, e)

    def _release_telnyx_number(self, rec: PhoneNumberRecord) -> None:
        if rec.carrier == "telnyx" or "telnyx_id" in rec.metadata:
            key = self._get_telnyx_creds()
            if key:
                headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
                telnyx_id = rec.metadata.get("telnyx_id")
                if not telnyx_id:
                    try:
                        r = requests.get(
                            f"https://api.telnyx.com/v2/phone_numbers?filter[phone_number]={requests.utils.quote(rec.phone_number)}",
                            headers=headers,
                            timeout=5,
                        )
                        if r.status_code == 200:
                            data = r.json().get("data", [])
                            if data:
                                telnyx_id = data[0].get("id")
                    except Exception as e:
                        log.warning("Failed to lookup Telnyx ID for %s: %s", rec.phone_number, e)

                if telnyx_id:
                    try:
                        del_r = requests.delete(
                            f"https://api.telnyx.com/v2/phone_numbers/{telnyx_id}",
                            headers=headers,
                            timeout=5,
                        )
                        log.info("Telnyx API delete number %s (%s) response status: %s", rec.phone_number, telnyx_id, del_r.status_code)
                    except Exception as e:
                        log.warning("Failed to delete Telnyx number %s via API: %s", rec.phone_number, e)

    def list_carriers(self) -> List[dict]:
        out = []
        for cid, meta in CARRIERS.items():
            active = {k for k, v in self._owned_numbers.items() if v.status == "active"}
            owned = sum(1 for n in self._owned_numbers.values() if n.status == "active" and n.carrier == cid)
            avail = sum(1 for i in AVAILABLE_NUMBERS_CATALOG if i["carrier"] == cid and i["phone_number"] not in active)
            out.append({"carrier": cid, "name": meta["name"], "sip_host": meta["sip_host"], "docs_url": meta["docs_url"], "owned": owned, "available": avail})
        return out

    def list_available_numbers(self, country: Optional[str] = None, search: Optional[str] = None, carrier: Optional[str] = None) -> List[dict]:
        owned = {k for k, v in self._owned_numbers.items() if v.status == "active"}
        target_carrier = (carrier or "").lower().strip()
        target_country = (country or "").upper().strip()

        live_results: List[dict] = []

        # 1. Twilio live query
        if target_carrier in ("twilio", "all", ""):
            tw_creds = self._get_twilio_creds()
            if tw_creds:
                sid, token = tw_creds
                cntry = target_country if (target_country and target_country != "ALL") else "US"
                try:
                    params: Dict[str, Any] = {"PageSize": 20}
                    if search:
                        q = search.strip()
                        if q.isdigit() and len(q) == 3:
                            params["AreaCode"] = q
                        elif any(c.isdigit() for c in q):
                            params["Contains"] = re.sub(r"[^\d]", "", q)
                    r = requests.get(
                        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/AvailablePhoneNumbers/{cntry}/Local.json",
                        params=params,
                        auth=(sid, token),
                        timeout=5,
                    )
                    if r.status_code == 200:
                        data = r.json()
                        for item in data.get("available_phone_numbers", []):
                            p_num = item.get("phone_number")
                            if not p_num or p_num in owned:
                                continue
                            locality = item.get("locality") or ""
                            region_code = item.get("region") or cntry
                            region_str = f"{locality}, {region_code}".strip(", ") or "Twilio Direct"
                            caps = []
                            cap_dict = item.get("capabilities", {})
                            if cap_dict.get("voice"):
                                caps.append("voice")
                            caps.append("sip")
                            if cap_dict.get("sms"):
                                caps.append("sms")
                            if cap_dict.get("mms"):
                                caps.append("mms")
                            live_results.append({
                                "phone_number": p_num,
                                "carrier": "twilio",
                                "friendly_name": item.get("friendly_name") or p_num,
                                "country": item.get("iso_country") or cntry,
                                "region": region_str,
                                "capabilities": caps,
                                "monthly_cost": 1.15,
                            })
                except Exception as e:
                    log.warning("Twilio Live AvailablePhoneNumbers API query failed: %s", e)

        # 2. Telnyx live query
        if target_carrier in ("telnyx", "all", ""):
            tx_key = self._get_telnyx_creds()
            if tx_key:
                cntry = target_country if (target_country and len(target_country) == 2 and target_country != "ALL") else "US"
                try:
                    tx_headers = {"Authorization": f"Bearer {tx_key}", "Content-Type": "application/json"}
                    tx_params: Dict[str, Any] = {
                        "filter[country_code]": cntry,
                        "filter[phone_number_type]": "local",
                        "filter[limit]": 24,
                    }
                    if search:
                        q_digits = re.sub(r"[^\d]", "", search.strip())
                        if q_digits:
                            tx_params["filter[phone_number][contains]"] = q_digits
                    r = requests.get(
                        "https://api.telnyx.com/v2/available_phone_numbers",
                        params=tx_params,
                        headers=tx_headers,
                        timeout=5,
                    )
                    if r.status_code == 200:
                        data = r.json().get("data", [])
                        for item in data:
                            p_num = item.get("phone_number", "")
                            # Skip invalid, unassigned, or masked wildcard numbers (e.g. +18334------)
                            if not p_num or "-" in p_num or "*" in p_num or p_num in owned:
                                continue
                            # Parse region
                            reg_info = item.get("region_information", [])
                            city = next((r.get("region_name") for r in reg_info if r.get("region_type") in ("rate_center", "location")), "")
                            state = next((r.get("region_name") for r in reg_info if r.get("region_type") == "state"), cntry)
                            region_str = f"{city.title()}, {state}".strip(", ") if city else f"{state} (Telnyx)"

                            cost_info = item.get("cost_information", {})
                            try:
                                m_cost = float(cost_info.get("monthly_cost", 1.00))
                                if m_cost <= 0:
                                    m_cost = 1.00
                            except (TypeError, ValueError):
                                m_cost = 1.00

                            features_raw = item.get("features", [])
                            features_list = [f.get("name") for f in features_raw if isinstance(f, dict)] if isinstance(features_raw, list) else []
                            caps = ["voice", "sip", "sms"]
                            if "mms" in features_list:
                                caps.append("mms")

                            live_results.append({
                                "phone_number": p_num,
                                "carrier": "telnyx",
                                "friendly_name": format_friendly_phone(p_num),
                                "country": cntry,
                                "region": region_str,
                                "capabilities": caps,
                                "monthly_cost": round(m_cost, 2),
                            })
                except Exception as e:
                    log.warning("Telnyx Live AvailablePhoneNumbers API query failed: %s", e)

        if target_carrier in ("twilio", "telnyx") and live_results:
            return live_results

        results = list(live_results) if target_carrier in ("all", "") else []
        for item in AVAILABLE_NUMBERS_CATALOG:
            if item["phone_number"] in owned:
                continue
            if target_carrier and target_carrier != "all" and item["carrier"] != target_carrier:
                continue
            if target_country and target_country != "ALL" and item["country"] != target_country:
                continue
            if search:
                q = search.lower().strip()
                if (q not in item["phone_number"].lower() and
                    q not in item["friendly_name"].lower() and
                    q not in item["region"].lower()):
                    continue
            if not any(r["phone_number"] == item["phone_number"] for r in results):
                results.append(item)
        return results

    def sync_carrier_numbers(self) -> List[dict]:
        """Fetch and sync active numbers directly from live carrier accounts (Twilio & Telnyx)."""
        changed = False

        # 1. Sync from Twilio
        tw_creds = self._get_twilio_creds()
        if tw_creds:
            sid, token = tw_creds
            try:
                r = requests.get(
                    f"https://api.twilio.com/2010-04-01/Accounts/{sid}/IncomingPhoneNumbers.json",
                    auth=(sid, token),
                    timeout=5,
                )
                if r.status_code == 200:
                    data = r.json().get("incoming_phone_numbers", [])
                    for item in data:
                        p_num = normalize_phone_number(item.get("phone_number", ""))
                        if not p_num:
                            continue
                        rec = self._owned_numbers.get(p_num)
                        if not rec or rec.status != "active":
                            trunk_sid = item.get("trunk_sid") or "trunk-inbound-primary"
                            rec = PhoneNumberRecord(
                                phone_number=p_num,
                                friendly_name=item.get("friendly_name") or format_friendly_phone(p_num),
                                country="US",
                                region="Twilio Primary",
                                capabilities=["voice", "sip", "sms", "mms"],
                                monthly_cost=1.15,
                                status="active",
                                carrier="twilio",
                                assigned_trunk_id=trunk_sid,
                                assigned_agent="Maya - Bottini & Bottini",
                                purchased_at=time.time(),
                                metadata={"twilio_sid": item.get("sid"), "trunk_sid": item.get("trunk_sid")},
                            )
                            self._owned_numbers[p_num] = rec
                            self._ensure_number_rule(rec)
                            changed = True
            except Exception as e:
                log.warning("Twilio carrier sync failed: %s", e)

        # 2. Sync from Telnyx
        tx_key = self._get_telnyx_creds()
        if tx_key:
            try:
                headers = {"Authorization": f"Bearer {tx_key}", "Content-Type": "application/json"}
                r = requests.get("https://api.telnyx.com/v2/phone_numbers", headers=headers, timeout=5)
                if r.status_code == 200:
                    data = r.json().get("data", [])
                    for item in data:
                        p_num = normalize_phone_number(item.get("phone_number", ""))
                        if not p_num or item.get("status") != "active":
                            continue
                        rec = self._owned_numbers.get(p_num)
                        if not rec or rec.status != "active":
                            rec = PhoneNumberRecord(
                                phone_number=p_num,
                                friendly_name=format_friendly_phone(p_num),
                                country="US",
                                region="Telnyx Primary",
                                capabilities=["voice", "sip", "sms", "mms"],
                                monthly_cost=1.00,
                                status="active",
                                carrier="telnyx",
                                assigned_trunk_id="trunk-inbound-primary",
                                assigned_agent="Maya - Bottini & Bottini",
                                purchased_at=time.time(),
                                metadata={"telnyx_id": item.get("id"), "connection_id": item.get("connection_id")},
                            )
                            self._owned_numbers[p_num] = rec
                            self._ensure_number_rule(rec)
                            changed = True
            except Exception as e:
                log.warning("Telnyx carrier sync failed: %s", e)

        if changed:
            self._save_trunks()
            self._save_phone_numbers()

        return [asdict(n) for n in self._owned_numbers.values() if n.status == "active"]

    def list_owned_numbers(self) -> List[dict]:
        return [asdict(n) for n in self._owned_numbers.values() if n.status == "active"]

    def purchase_number(
        self,
        phone_number: str,
        friendly_name: str = "",
        assigned_trunk_id: str = "trunk-inbound-primary",
        assigned_agent: str = "Intake Agent",
        carrier: Optional[str] = None,
    ) -> dict:
        norm = normalize_phone_number(phone_number)
        if not is_valid_phone_number(norm):
            raise ValueError(f"Invalid phone number '{phone_number}'")
        existing = self._owned_numbers.get(norm)
        if existing and existing.status == "active":
            raise ValueError(f"{norm} is already provisioned and assigned to {existing.assigned_agent}. Use re-route instead.")
        if assigned_trunk_id not in self._inbound_trunks:
            raise ValueError(f"Unknown inbound trunk '{assigned_trunk_id}'. Create the trunk first.")

        catalog_entry = next((item for item in AVAILABLE_NUMBERS_CATALOG if item["phone_number"] == norm), None)
        country = catalog_entry["country"] if catalog_entry else "US"
        region = catalog_entry["region"] if catalog_entry else "Direct Inward Dialing"
        capabilities = catalog_entry["capabilities"] if catalog_entry else ["voice", "sip", "sms", "dual_channel"]
        monthly_cost = catalog_entry["monthly_cost"] if catalog_entry else 1.15
        fname = friendly_name or (catalog_entry["friendly_name"] if catalog_entry else norm)
        carrier_id = (carrier or (catalog_entry["carrier"] if catalog_entry else "twilio")).lower()
        if carrier_id not in CARRIERS:
            raise ValueError(f"Unknown carrier '{carrier_id}'. Choose one of: {', '.join(CARRIERS)}")

        metadata: Dict[str, Any] = {}

        # If Twilio carrier, execute REAL LIVE Twilio API purchase
        if carrier_id == "twilio":
            creds = self._get_twilio_creds()
            if creds:
                sid, token = creds
                buy_data: Dict[str, str] = {
                    "PhoneNumber": norm,
                    "FriendlyName": fname,
                }
                if assigned_trunk_id.startswith("TK"):
                    buy_data["TrunkSid"] = assigned_trunk_id

                try:
                    r = requests.post(
                        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/IncomingPhoneNumbers.json",
                        data=buy_data,
                        auth=(sid, token),
                        timeout=10,
                    )
                    if r.status_code not in (200, 201):
                        err_json = {}
                        try:
                            err_json = r.json()
                        except Exception:
                            pass
                        err_msg = err_json.get("message") or f"HTTP {r.status_code}: {r.text}"
                        err_code = err_json.get("code")
                        code_str = f" [Code {err_code}]" if err_code else ""
                        raise ValueError(f"Twilio Carrier Purchase Failed{code_str}: {err_msg}")

                    res_json = r.json()
                    metadata["twilio_sid"] = res_json.get("sid")
                    metadata["trunk_sid"] = res_json.get("trunk_sid")
                    if res_json.get("friendly_name"):
                        fname = res_json.get("friendly_name")
                except requests.RequestException as req_err:
                    raise ValueError(f"Twilio Carrier Connection Error: {req_err}")

        # If Telnyx carrier, execute REAL LIVE Telnyx API purchase
        elif carrier_id == "telnyx":
            key = self._get_telnyx_creds()
            if key:
                headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
                order_payload = {"phone_numbers": [{"phone_number": norm}]}
                try:
                    r = requests.post(
                        "https://api.telnyx.com/v2/number_orders",
                        json=order_payload,
                        headers=headers,
                        timeout=10,
                    )
                    if r.status_code not in (200, 201):
                        err_json = {}
                        try:
                            err_json = r.json()
                        except Exception:
                            pass
                        errors = err_json.get("errors", [])
                        err_detail = errors[0].get("detail") if errors else (err_json.get("message") or f"HTTP {r.status_code}: {r.text}")
                        err_code = errors[0].get("code") if errors else ""
                        code_str = f" [Code {err_code}]" if err_code else ""
                        raise ValueError(f"Telnyx Carrier Purchase Failed{code_str}: {err_detail}")

                    res_json = r.json().get("data", {})
                    metadata["telnyx_order_id"] = res_json.get("id")
                    monthly_cost = 1.00
                except requests.RequestException as req_err:
                    raise ValueError(f"Telnyx Carrier Connection Error: {req_err}")

        record = PhoneNumberRecord(
            phone_number=norm,
            friendly_name=fname,
            country=country,
            region=region,
            capabilities=capabilities,
            monthly_cost=monthly_cost,
            status="active",
            carrier=carrier_id,
            assigned_trunk_id=assigned_trunk_id,
            assigned_agent=assigned_agent,
            purchased_at=time.time(),
            metadata=metadata,
        )
        self._owned_numbers[norm] = record

        # Bind to trunk
        trunk = self._inbound_trunks.get(assigned_trunk_id)
        if trunk and norm not in trunk.numbers:
            trunk.numbers.append(norm)

        # One dispatch rule per DID
        self._ensure_number_rule(record)
        self._save_trunks()
        self._save_phone_numbers()

        log.info("Purchased phone number %s -> assigned to %s on %s (carrier=%s)", norm, assigned_agent, assigned_trunk_id, carrier_id)
        return asdict(record)

    def release_number(self, phone_number: str) -> bool:
        norm = normalize_phone_number(phone_number)
        rec = self._owned_numbers.get(norm)
        if not rec or rec.status != "active":
            return False

        self._release_twilio_number(rec)
        self._release_telnyx_number(rec)

        rec.status = "released"
        # Unbind from trunk and drop its dispatch rule
        trunk = self._inbound_trunks.get(rec.assigned_trunk_id)
        if trunk and norm in trunk.numbers:
            trunk.numbers.remove(norm)
        self._dispatch_rules.pop(self._number_rule_id(norm), None)

        self._save_trunks()
        self._save_phone_numbers()
        log.info("Released phone number %s", norm)
        return True

    def update_number_routing(self, phone_number: str, agent_name: str, trunk_id: Optional[str] = None) -> Optional[dict]:
        norm = normalize_phone_number(phone_number)
        rec = self._owned_numbers.get(norm)
        if not rec or rec.status != "active":
            return None
        if not agent_name or not agent_name.strip():
            raise ValueError("agent_name is required")
        if trunk_id and trunk_id not in self._inbound_trunks:
            raise ValueError(f"Unknown inbound trunk '{trunk_id}'")

        rec.assigned_agent = agent_name.strip()
        if trunk_id and trunk_id in self._inbound_trunks:
            if trunk_id != rec.assigned_trunk_id:
                old_trunk = self._inbound_trunks.get(rec.assigned_trunk_id)
                if old_trunk and norm in old_trunk.numbers:
                    old_trunk.numbers.remove(norm)
                new_trunk = self._inbound_trunks.get(trunk_id)
                if new_trunk and norm not in new_trunk.numbers:
                    new_trunk.numbers.append(norm)
                rec.assigned_trunk_id = trunk_id

        # Update only this DID's own dispatch rule
        self._ensure_number_rule(rec)
        self._save_trunks()
        self._save_phone_numbers()
        log.info("Updated routing for %s -> agent=%s trunk=%s", norm, agent_name, rec.assigned_trunk_id)
        return asdict(rec)

    def update_phone_number(
        self,
        phone_number: str,
        friendly_name: Optional[str] = None,
        agent_name: Optional[str] = None,
        trunk_id: Optional[str] = None,
        carrier: Optional[str] = None,
    ) -> Optional[dict]:
        norm = normalize_phone_number(phone_number)
        rec = self._owned_numbers.get(norm)
        if not rec:
            return None

        if friendly_name is not None and friendly_name.strip():
            rec.friendly_name = friendly_name.strip()
        if agent_name is not None and agent_name.strip():
            rec.assigned_agent = agent_name.strip()
        if carrier is not None and carrier.strip():
            c_id = carrier.strip().lower()
            if c_id in CARRIERS:
                rec.carrier = c_id
        if trunk_id is not None and trunk_id.strip():
            t_id = trunk_id.strip()
            if t_id in self._inbound_trunks and t_id != rec.assigned_trunk_id:
                old_trunk = self._inbound_trunks.get(rec.assigned_trunk_id)
                if old_trunk and norm in old_trunk.numbers:
                    old_trunk.numbers.remove(norm)
                new_trunk = self._inbound_trunks.get(t_id)
                if new_trunk and norm not in new_trunk.numbers:
                    new_trunk.numbers.append(norm)
                rec.assigned_trunk_id = t_id

        self._ensure_number_rule(rec)
        self._save_trunks()
        self._save_phone_numbers()
        log.info("Updated phone number %s details (friendly_name='%s', agent='%s', trunk='%s')",
                 norm, rec.friendly_name, rec.assigned_agent, rec.assigned_trunk_id)
        return asdict(rec)

    def delete_phone_number(self, phone_number: str, purge: bool = False) -> bool:
        norm = normalize_phone_number(phone_number)
        rec = self._owned_numbers.get(norm)
        if not rec:
            return False

        if rec.status == "active":
            self._release_twilio_number(rec)
            self._release_telnyx_number(rec)

        # Unbind from trunk and drop its dispatch rule
        trunk = self._inbound_trunks.get(rec.assigned_trunk_id)
        if trunk and norm in trunk.numbers:
            trunk.numbers.remove(norm)
        self._dispatch_rules.pop(self._number_rule_id(norm), None)

        if purge:
            self._owned_numbers.pop(norm, None)
        else:
            rec.status = "released"

        self._save_trunks()
        self._save_phone_numbers()
        log.info("Deleted/released phone number %s (purged=%s)", norm, purge)
        return True

    def route_inbound_call(self, dialed_number: str, caller_number: str) -> Optional[dict]:
        norm_dialed = normalize_phone_number(dialed_number)
        norm_caller = normalize_phone_number(caller_number)

        matched_trunk = None
        for trunk in self._inbound_trunks.values():
            if trunk.matches_number(norm_dialed):
                matched_trunk = trunk
                break

        matched_rule = None
        # Check if number has explicit assigned agent
        owned = self._owned_numbers.get(norm_dialed)
        if owned and owned.status == "active":
            agent_name = owned.assigned_agent
            matched_rule = self._dispatch_rules.get(self._number_rule_id(norm_dialed))
        elif matched_trunk:
            for rule in self._dispatch_rules.values():
                if matched_trunk.trunk_id in rule.trunk_ids:
                    matched_rule = rule
                    break
            agent_name = matched_rule.agent_name if matched_rule else "VoiceAssistantAgent"
        else:
            agent_name = "VoiceAssistantAgent"

        if not matched_rule:
            matched_rule = next(iter(self._dispatch_rules.values()), None)

        if not matched_rule:
            log.warning("No dispatch rule found for dialed number: %s", dialed_number)
            return None

        room_name = matched_rule.generate_room_name(norm_caller)
        call_id = f"sip-in-{int(time.time())}-{norm_caller[-4:]}"

        record = TelephonyCallRecord(
            call_id=call_id,
            direction=CallDirection.INBOUND,
            from_number=norm_caller,
            to_number=norm_dialed,
            room_name=room_name,
            participant_identity=f"sip-{norm_caller}",
            status=CallStatus.ACTIVE,
            answered_at=time.time(),
            sip_trunk_id=matched_trunk.trunk_id if matched_trunk else None,
            metadata={"agent_name": agent_name},
        )
        self._calls[call_id] = record

        return {
            "call_id": call_id,
            "room_name": room_name,
            "participant_identity": record.participant_identity,
            "rule": asdict(matched_rule),
            "trunk": asdict(matched_trunk) if matched_trunk else None,
        }

    async def dial_phone_number(
        self,
        destination_number: str,
        caller_id: Optional[str] = None,
        room_name: Optional[str] = None,
        outbound_trunk_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TelephonyCallRecord:
        norm_dest = normalize_phone_number(destination_number)
        if not is_valid_phone_number(norm_dest):
            raise ValueError(f"Invalid E.164 destination phone number: '{destination_number}'")

        trunk = None
        if outbound_trunk_id and outbound_trunk_id in self._outbound_trunks:
            trunk = self._outbound_trunks[outbound_trunk_id]
        elif self._outbound_trunks:
            trunk = next(iter(self._outbound_trunks.values()))

        if not trunk:
            raise RuntimeError("No outbound SIP trunk configured")

        if not caller_id and not trunk.numbers:
            raise ValueError(f"Outbound trunk '{trunk.trunk_id}' has no caller-ID numbers configured")
        selected_caller_id = normalize_phone_number(caller_id or trunk.numbers[0])
        target_room = room_name or f"outbound-{int(time.time())}-{norm_dest[-4:]}"
        call_id = f"sip-out-{int(time.time())}-{norm_dest[-4:]}"
        participant_identity = f"phone-{norm_dest}"

        record = TelephonyCallRecord(
            call_id=call_id,
            direction=CallDirection.OUTBOUND,
            from_number=selected_caller_id,
            to_number=norm_dest,
            room_name=target_room,
            participant_identity=participant_identity,
            status=CallStatus.INITIATED,
            sip_trunk_id=trunk.trunk_id,
            metadata=metadata or {},
        )
        self._calls[call_id] = record

        log.info(
            "Initiating outbound call: from=%s to=%s (room=%s, trunk=%s)",
            selected_caller_id,
            norm_dest,
            target_room,
            trunk.trunk_id,
        )

        try:
            if self.api_key and self.api_secret and "127.0.0.1" not in self.livekit_url:
                from livekit import api
                lk_api = api.LiveKitAPI(self.livekit_url, self.api_key, self.api_secret)
                try:
                    sip_call_req = api.CreateSIPParticipantRequest(
                        sip_trunk_id=trunk.trunk_id,
                        sip_call_to=norm_dest,
                        room_name=target_room,
                        participant_identity=participant_identity,
                        participant_name=f"Customer ({norm_dest})",
                    )
                    await lk_api.sip.create_sip_participant(sip_call_req)
                    record.status = CallStatus.ACTIVE
                    record.answered_at = time.time()
                finally:
                    await lk_api.aclose()
            else:
                record.status = CallStatus.RINGING
                await asyncio.sleep(0.05)
                record.status = CallStatus.ACTIVE
                record.answered_at = time.time()
                log.info("Simulated outbound call connected successfully: %s", call_id)
        except Exception as err:
            log.warning("LiveKit SIP API unavailable or failed (%s); marked call active in simulated mode", err)
            record.status = CallStatus.ACTIVE
            record.answered_at = time.time()

        return record

    def end_call(self, call_id: str) -> Optional[TelephonyCallRecord]:
        record = self._calls.get(call_id)
        if not record:
            return None
        record.status = CallStatus.COMPLETED
        record.ended_at = time.time()
        if record.answered_at:
            record.duration_seconds = round(record.ended_at - record.answered_at, 2)
        log.info("Call ended: %s (duration=%.2fs)", call_id, record.duration_seconds)
        return record


# Global singleton instance
telephony_manager = TelephonyManager()
