"""Task 3.3 — Custom Schema Data Extractor.

Provides dynamic JSON Schema-driven structured extraction of typed business
entities (string, number, boolean, date, phone, email, enum) from voice call
transcripts. Outputs confidence-scored field-level results suitable for
CRM/ERP ingestion.

Features:
- Dynamic schema registration & validation (JSON Schema draft-7 subset)
- Type-safe extraction per field: str, int, float, bool, date, phone, email, enum
- Per-field confidence scoring (0.0–1.0) based on pattern match strength
- Fallback chain: regex patterns → keyword proximity → default values
- Business entity schemas bundled: legal_intake, billing, scheduling, support
- Extraction result serialization to dict/JSON for CRM payload dispatch
"""

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("voice-agent.schema_extractor")
if not log.handlers:
    logging.basicConfig(level=logging.INFO)


# ---------------------------------------------------------------------------
# Field Type Registry
# ---------------------------------------------------------------------------

class FieldType(str, Enum):
    STRING  = "string"
    NUMBER  = "number"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    DATE    = "date"
    PHONE   = "phone"
    EMAIL   = "email"
    ENUM    = "enum"


# ---------------------------------------------------------------------------
# Extraction Patterns (compiled once at module load)
# ---------------------------------------------------------------------------

_PHONE_RE = re.compile(
    r"(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)?\d{3}[-.\s]?\d{4}"
)
_EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}")
_DATE_RE = re.compile(
    r"(?:"
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?(?:[,\s]+\d{4})?"
    r"|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\b(?:today|tomorrow|yesterday|next\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b"
    r")",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"-?\b\d+(?:\.\d+)?\b")
_BOOL_TRUE_RE  = re.compile(r"\b(yes|yep|yeah|correct|sure|confirm|confirmed|affirmative|absolutely|indeed)\b", re.I)
_BOOL_FALSE_RE = re.compile(r"\b(no|nope|not|incorrect|deny|denied|negative|never)\b", re.I)

# Cues that mark what the caller is asking for, rather than what they are describing.
_INTENT_CUE_RE = re.compile(
    r"\b(want|wanted|need|needed|request|requesting|requested|asking for|ask for|"
    r"looking for|seeking|would like|demand|prefer|book|booking|schedule|scheduling)"
    r"\b[^.?!]{0,40}$",
    re.I,
)


def _enum_pattern(value: str) -> "re.Pattern":
    """Whole-word matcher for an enum value, tolerant of _ / - / space separators."""
    parts = [re.escape(p) for p in re.split(r"[_\s-]+", value) if p]
    return re.compile(r"\b" + r"[\s_-]+".join(parts) + r"\b", re.I)

# Proximity search window (characters around a keyword hit)
PROXIMITY_WINDOW = 120


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class FieldResult:
    field_name: str
    field_type: str
    value: Any           # extracted typed value or None
    raw_match: str       # raw text that triggered extraction
    confidence: float    # 0.0 – 1.0
    source: str          # "pattern", "proximity", "default", "missing"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ExtractionResult:
    schema_id: str
    call_id: str
    fields: List[FieldResult] = field(default_factory=list)
    overall_confidence: float = 0.0
    extraction_coverage: float = 0.0   # fraction of required fields extracted
    missing_required: List[str] = field(default_factory=list)
    extracted_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["fields"] = [f.to_dict() for f in self.fields]
        return d

    def to_crm_payload(self) -> Dict[str, Any]:
        """Returns flat key→value dict suitable for CRM/ERP ingestion."""
        return {f.field_name: f.value for f in self.fields if f.value is not None}


# ---------------------------------------------------------------------------
# Schema Registry
# ---------------------------------------------------------------------------

_SCHEMA_REGISTRY: Dict[str, Dict[str, Any]] = {}


def register_schema(schema_id: str, schema: Dict[str, Any]) -> None:
    """Register a JSON Schema (draft-7 subset) by ID."""
    if "properties" not in schema:
        raise ValueError(f"Schema '{schema_id}' must have 'properties'")
    _SCHEMA_REGISTRY[schema_id] = schema
    log.debug("Registered extraction schema: %s (%d fields)", schema_id, len(schema["properties"]))


def get_schema(schema_id: str) -> Optional[Dict[str, Any]]:
    return _SCHEMA_REGISTRY.get(schema_id)


def list_schemas() -> List[str]:
    return list(_SCHEMA_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Core Extractor
# ---------------------------------------------------------------------------

class SchemaExtractor:
    """Dynamic JSON Schema-driven entity extractor for voice call transcripts."""

    def _full_text(self, transcript_turns: List[Dict[str, Any]]) -> str:
        return " ".join(
            str(t.get("text", t.get("content", ""))) for t in transcript_turns
        )

    def _extract_field(
        self,
        field_name: str,
        field_schema: Dict[str, Any],
        full_text: str,
        transcript_turns: List[Dict[str, Any]],
    ) -> FieldResult:
        """Extracts a single field from transcript text using the field schema."""
        ftype_raw = field_schema.get("type", "string")
        ftype = FieldType(ftype_raw) if ftype_raw in [e.value for e in FieldType] else FieldType.STRING

        keywords: List[str] = field_schema.get("x-keywords", [field_name.replace("_", " ")])
        enum_values: List[str] = field_schema.get("enum", [])
        patterns: List[str] = field_schema.get("x-patterns", [])
        default = field_schema.get("default", None)

        text_lower = full_text.lower()

        # 1. Custom regex patterns (highest confidence: 0.95)
        for pat in patterns:
            m = re.search(pat, full_text, re.IGNORECASE)
            if m:
                # Use capture group 1 if defined, else full match
                raw = m.group(1) if m.lastindex and m.lastindex >= 1 else m.group(0)
                value = self._coerce(raw.strip(), ftype, enum_values)
                if value is not None:
                    return FieldResult(field_name, ftype.value, value, raw, 0.95, "pattern")


        # 2. Enum — scored across the whole transcript, not first-keyword-wins
        if ftype == FieldType.ENUM and enum_values:
            enum_result = self._extract_enum(field_name, field_schema, enum_values, full_text)
            if enum_result is not None:
                return enum_result

        # 3. Type-native extraction with keyword proximity
        for kw in keywords:
            kw_lower = kw.lower()
            idx = text_lower.find(kw_lower)
            if idx != -1:
                window = full_text[max(0, idx - 20): idx + PROXIMITY_WINDOW]

                # Phone
                if ftype == FieldType.PHONE:
                    m = _PHONE_RE.search(window)
                    if m:
                        value = self._coerce(m.group(0), ftype, enum_values)
                        return FieldResult(field_name, ftype.value, value, m.group(0), 0.90, "proximity")

                # Email
                elif ftype == FieldType.EMAIL:
                    m = _EMAIL_RE.search(window)
                    if m:
                        return FieldResult(field_name, ftype.value, m.group(0).lower(), m.group(0), 0.92, "proximity")

                # Date
                elif ftype == FieldType.DATE:
                    m = _DATE_RE.search(window)
                    if m:
                        return FieldResult(field_name, ftype.value, m.group(0), m.group(0), 0.88, "proximity")

                # Number / Integer
                elif ftype in (FieldType.NUMBER, FieldType.INTEGER):
                    m = _NUMBER_RE.search(window)
                    if m:
                        value = self._coerce(m.group(0), ftype, enum_values)
                        return FieldResult(field_name, ftype.value, value, m.group(0), 0.85, "proximity")

                # Boolean
                elif ftype == FieldType.BOOLEAN:
                    if _BOOL_TRUE_RE.search(window):
                        return FieldResult(field_name, ftype.value, True, "yes", 0.80, "proximity")
                    if _BOOL_FALSE_RE.search(window):
                        return FieldResult(field_name, ftype.value, False, "no", 0.80, "proximity")

                # String — extract next meaningful token after keyword (stop at punctuation)
                else:
                    kw_end = idx + len(kw_lower)
                    snippet = full_text[kw_end: kw_end + 60].strip()
                    # Strip leading articles/connectors
                    snippet = re.sub(r"^[\s,.:;!?]*(is|are|was|a|an|the)\s+", "", snippet, flags=re.I)
                    # Stop at sentence boundary
                    stop_match = re.search(r"[.!?]|\bI\s+", snippet)
                    if stop_match:
                        snippet = snippet[:stop_match.start()].strip()
                    words = snippet.split()
                    if words:
                        # For name-like fields, take max 2-3 words; others max 5
                        max_words = 3 if "name" in field_name.lower() else 5
                        value_str = " ".join(words[:min(max_words, len(words))])
                        value_str = re.sub(r"[,.:;!?]+$", "", value_str).strip()
                        if value_str:
                            return FieldResult(field_name, ftype.value, value_str, value_str, 0.70, "proximity")


        # 4. Global type-native scan (no keyword required, lower confidence)
        if ftype == FieldType.PHONE:
            m = _PHONE_RE.search(full_text)
            if m:
                return FieldResult(field_name, ftype.value, m.group(0), m.group(0), 0.55, "pattern")

        if ftype == FieldType.EMAIL:
            m = _EMAIL_RE.search(full_text)
            if m:
                return FieldResult(field_name, ftype.value, m.group(0).lower(), m.group(0), 0.60, "pattern")

        if ftype == FieldType.DATE:
            m = _DATE_RE.search(full_text)
            if m:
                return FieldResult(field_name, ftype.value, m.group(0), m.group(0), 0.50, "pattern")

        # 5. Default value
        if default is not None:
            return FieldResult(field_name, ftype.value, default, "", 0.30, "default")

        # 6. Missing
        return FieldResult(field_name, ftype.value, None, "", 0.0, "missing")

    def _extract_enum(
        self,
        field_name: str,
        field_schema: Dict[str, Any],
        enum_values: List[str],
        full_text: str,
    ) -> Optional[FieldResult]:
        """Resolves an enum field by scoring every candidate mention in the transcript.

        A caller names the problem before naming what they want ("a $149.99 overcharge
        ... I want a refund"), so an option introduced by an intent cue outranks an
        incidental mention, and a later mention edges out an earlier one.
        """
        best: Optional[Tuple[float, str, str]] = None
        span = max(len(full_text) - 1, 1)

        for ev in enum_values:
            for m in _enum_pattern(ev).finditer(full_text):
                score = 1.0
                lead = full_text[max(0, m.start() - 48): m.start()]
                if _INTENT_CUE_RE.search(lead):
                    score += 1.5
                score += 0.4 * (m.start() / span)
                if best is None or score > best[0]:
                    best = (score, ev, m.group(0))

        if best is None:
            return None

        _, value, raw = best
        keywords = [k.lower() for k in field_schema.get("x-keywords", [])]
        in_context = any(k in full_text.lower() for k in keywords)
        confidence, source = (0.87, "proximity") if in_context else (0.65, "pattern")
        return FieldResult(field_name, FieldType.ENUM.value, value, raw, confidence, source)

    def _coerce(self, raw: str, ftype: FieldType, enum_values: List[str]) -> Any:
        """Coerces raw text to the target field type."""
        raw = raw.strip()
        try:
            if ftype == FieldType.INTEGER:
                return int(float(raw.replace(",", "")))
            if ftype == FieldType.NUMBER:
                return float(raw.replace(",", ""))
            if ftype == FieldType.BOOLEAN:
                return raw.lower() in ("yes", "true", "1", "confirmed")
            if ftype == FieldType.ENUM and enum_values:
                for ev in enum_values:
                    if ev.lower() == raw.lower():
                        return ev
                return None
            if ftype == FieldType.PHONE:
                # Normalize to digits-only representation
                digits = re.sub(r"\D", "", raw)
                if len(digits) >= 10:
                    return raw  # return original formatted
            return raw
        except Exception:
            return raw

    def extract(
        self,
        schema_id: str,
        call_id: str,
        transcript_turns: List[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ExtractionResult:
        """Extracts entities from a call transcript using the named schema."""
        schema = get_schema(schema_id)
        if not schema:
            raise KeyError(f"Schema '{schema_id}' not registered. "
                           f"Available: {list_schemas()}")

        properties: Dict[str, Any] = schema.get("properties", {})
        required_fields: List[str] = schema.get("required", [])
        full_text = self._full_text(transcript_turns)

        field_results: List[FieldResult] = []
        for fname, fschema in properties.items():
            result = self._extract_field(fname, fschema, full_text, transcript_turns)
            field_results.append(result)

        # Metrics
        extracted = [f for f in field_results if f.value is not None]
        missing_required = [
            f for f in required_fields
            if not any(r.field_name == f and r.value is not None for r in field_results)
        ]
        overall_conf = (
            sum(f.confidence for f in field_results) / max(1, len(field_results))
        )
        coverage = len(extracted) / max(1, len(field_results))

        result = ExtractionResult(
            schema_id=schema_id,
            call_id=call_id,
            fields=field_results,
            overall_confidence=round(overall_conf, 3),
            extraction_coverage=round(coverage, 3),
            missing_required=missing_required,
        )
        log.info(
            "Extraction [%s] call=%s: %d/%d fields, coverage=%.1f%%, conf=%.2f",
            schema_id, call_id, len(extracted), len(field_results),
            coverage * 100, overall_conf,
        )
        return result

    def extract_multi(
        self,
        schema_ids: List[str],
        call_id: str,
        transcript_turns: List[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, ExtractionResult]:
        """Extracts entities using multiple schemas and returns results keyed by schema_id."""
        return {
            sid: self.extract(sid, call_id, transcript_turns, metadata)
            for sid in schema_ids
        }


# ---------------------------------------------------------------------------
# Built-in Business Schemas
# ---------------------------------------------------------------------------

LEGAL_INTAKE_SCHEMA = {
    "title": "Legal Intake",
    "description": "Structured intake form for new legal clients",
    "required": ["client_name", "case_type", "contact_phone"],
    "properties": {
        "client_name": {
            "type": "string",
            "x-keywords": ["my name is", "name is", "i am", "this is", "speaking with"],
            "description": "Full name of the prospective client",
        },
        "case_type": {
            "type": "enum",
            "enum": ["personal_injury", "family_law", "criminal", "immigration", "estate", "contract", "employment"],
            "x-keywords": ["case", "matter", "about", "regarding", "help with"],
            "description": "Category of the legal matter",
        },
        "contact_phone": {
            "type": "phone",
            "x-keywords": ["phone", "number", "call me", "reach me", "contact"],
            "description": "Caller's contact phone number",
        },
        "contact_email": {
            "type": "email",
            "x-keywords": ["email", "e-mail", "send to", "address"],
            "description": "Caller's email address",
        },
        "injury_date": {
            "type": "date",
            "x-keywords": ["incident", "accident", "happened", "occurred", "date of", "when"],
            "description": "Date of the incident or injury",
        },
        "needs_urgent_consultation": {
            "type": "boolean",
            "x-keywords": ["urgent", "emergency", "asap", "right away", "today", "immediately"],
            "description": "Whether caller requires urgent consultation",
            "default": False,
        },
        "retainer_discussed": {
            "type": "boolean",
            "x-keywords": ["retainer", "fee", "payment", "cost", "charge"],
            "description": "Whether retainer fees were discussed",
            "default": False,
        },
    },
}

BILLING_SCHEMA = {
    "title": "Billing Inquiry",
    "description": "Billing and payment entity extraction",
    "required": ["account_id", "issue_type"],
    "properties": {
        "account_id": {
            "type": "string",
            "x-patterns": [r"\b(?:account|acct)[:\s#]*(?:number\s*)?([A-Z0-9][A-Z0-9-]{3,15})\b"],
            "x-keywords": ["account number", "account id", "my account", "acct"],
            "description": "Customer account identifier",
        },

        "issue_type": {
            "type": "enum",
            "enum": ["overcharge", "refund", "statement", "payment_method", "dispute", "general"],
            "x-keywords": ["charged", "charge", "refund", "statement", "dispute", "payment"],
            "description": "Nature of the billing issue",
        },
        "amount": {
            "type": "number",
            "x-patterns": [r"\$\s*(\d+(?:\.\d{2})?)"],
            "x-keywords": ["amount", "charged", "charge of", "invoice", "total", "balance", "dollars"],
            "description": "Dollar amount mentioned",
        },
        "invoice_number": {
            "type": "string",
            "x-patterns": [r"(?:invoice|inv)[.:\s#]*([A-Z0-9-]{4,16})\b"],
            "x-keywords": ["invoice number", "invoice #", "invoice id"],
            "description": "Invoice or statement number",
        },
        "callback_number": {
            "type": "phone",
            "x-keywords": ["call me", "call back", "reach me", "phone", "number"],
            "description": "Phone number for billing callback",
        },
    },
}

SCHEDULING_SCHEMA = {
    "title": "Appointment Scheduling",
    "description": "Extracts appointment scheduling intent and details",
    "required": ["appointment_date", "service_type"],
    "properties": {
        "appointment_date": {
            "type": "date",
            "x-keywords": ["schedule", "appointment", "book", "date", "when", "available"],
            "description": "Requested appointment date",
        },
        "service_type": {
            "type": "enum",
            "enum": ["consultation", "follow_up", "initial_intake", "deposition", "hearing", "mediation"],
            "x-keywords": ["for", "type", "regarding", "need", "want"],
            "description": "Type of appointment being requested",
        },
        "preferred_time": {
            "type": "string",
            "x-patterns": [r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b"],
            "x-keywords": ["time", "at", "o'clock", "prefer", "morning", "afternoon"],
            "description": "Preferred appointment time",
        },
        "attendee_name": {
            "type": "string",
            "x-keywords": ["my name is", "name is", "i am", "for", "appointment for"],
            "description": "Name of the person booking",
        },
        "contact_phone": {
            "type": "phone",
            "x-keywords": ["phone", "call", "number", "reach"],
            "description": "Contact number for appointment confirmation",
        },
        "reminder_requested": {
            "type": "boolean",
            "x-keywords": ["remind", "reminder", "notification", "alert"],
            "description": "Whether caller requested a reminder",
            "default": False,
        },
    },
}

SUPPORT_SCHEMA = {
    "title": "Technical Support",
    "description": "Technical support ticket entity extraction",
    "required": ["issue_description", "severity"],
    "properties": {
        "issue_description": {
            "type": "string",
            "x-keywords": ["problem", "issue", "error", "broken", "not working", "help with"],
            "description": "Summary of the technical issue",
        },
        "severity": {
            "type": "enum",
            "enum": ["critical", "high", "medium", "low"],
            "x-keywords": ["severity", "priority", "urgent", "important", "blocking"],
            "default": "medium",
            "description": "Issue severity level",
        },
        "error_code": {
            "type": "string",
            "x-patterns": [r"\b(?:error|err|code)[:\s#]*([A-Z0-9_-]{3,12})\b"],
            "x-keywords": ["error code", "error number", "code"],
            "description": "Error code or reference number",
        },
        "customer_id": {
            "type": "string",
            "x-patterns": [r"\b(?:customer|client|user)[:\s#]*([A-Z0-9]{4,12})\b"],
            "x-keywords": ["customer id", "user id", "my id"],
            "description": "Customer or user identifier",
        },
        "product": {
            "type": "string",
            "x-keywords": ["product", "software", "platform", "system", "app", "application"],
            "description": "Product or system affected",
        },
        "contact_email": {
            "type": "email",
            "x-keywords": ["email", "send", "contact"],
            "description": "Email for support ticket updates",
        },
    },
}


# ---------------------------------------------------------------------------
# Register built-in schemas & create singleton
# ---------------------------------------------------------------------------

register_schema("legal_intake", LEGAL_INTAKE_SCHEMA)
register_schema("billing", BILLING_SCHEMA)
register_schema("scheduling", SCHEDULING_SCHEMA)
register_schema("technical_support", SUPPORT_SCHEMA)

schema_extractor = SchemaExtractor()
