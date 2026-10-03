"""Task 5.1 — Law firm profile collected at sign-up.

A firm profile lives on the workspace as ``Workspace.metadata["firm_profile"]`` (the firm name is the
workspace name itself, so it is not repeated here). ``normalize_firm_profile`` is the single place
that validates and cleans the data; sign-up, the super-admin organization API and the profile page
all go through it so stored profiles always have the same shape.

Every field is optional at this layer. Sign-up enforces SIGNUP_REQUIRED_FIELDS on top.
"""

import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

FIRM_SIZE_LABELS = {"solo": "Solo", "2-10": "2–10", "11-50": "11–50", "50+": "50+"}
FIRM_SIZES = tuple(FIRM_SIZE_LABELS)

PRACTICE_AREAS = {
    "personal_injury": "Personal Injury",
    "family": "Family Law",
    "criminal_defense": "Criminal Defense",
    "immigration": "Immigration",
    "estate_planning": "Estate Planning",
    "business": "Business / Corporate",
    "employment": "Employment",
    "real_estate": "Real Estate",
    "other": "Other",
}

AFTER_HOURS_LABELS = {"take_message": "Take a message", "book_callback": "Book a callback",
                      "transfer": "Transfer to an on-call number"}
AFTER_HOURS_ACTIONS = tuple(AFTER_HOURS_LABELS)

CALL_VOLUME_LABELS = {"under_100": "Under 100", "100-500": "100–500", "500-2000": "500–2,000", "2000+": "2,000+"}
CALL_VOLUMES = tuple(CALL_VOLUME_LABELS)   # calls per month

PRACTICE_SOFTWARE = {
    "clio": "Clio",
    "mycase": "MyCase",
    "practicepanther": "PracticePanther",
    "filevine": "Filevine",
    "other": "Other",
    "none": "None",
}

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

ADDRESS_FIELDS = ("line1", "line2", "city", "state", "postal_code", "country")

SIGNUP_REQUIRED_FIELDS = ("website", "office_phone", "timezone", "firm_size")

MAX_LEAD_EMAILS = 5
MAX_LANGUAGES = 10

FIELDS = (
    "website", "office_phone", "address", "timezone", "firm_size",
    "practice_areas", "practice_areas_other", "business_hours", "after_hours",
    "after_hours_transfer_number", "languages", "monthly_call_volume",
    "practice_software", "practice_software_other", "lead_emails", "referral_source",
)

_HOSTNAME_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_LANG_RE = re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")
_TZ_SHAPE_RE = re.compile(r"^[A-Za-z]+(/[A-Za-z0-9_+-]+)+$|^UTC$")


def _text(value: Any, field: str, max_len: int) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise ValueError(f"{field} must be text")
    return re.sub(r"\s+", " ", str(value)).strip()[:max_len]


def normalize_website(raw: Any) -> str:
    """'smithlaw.com' -> 'https://smithlaw.com'. Only http(s) URLs with a real public-looking host."""
    url = _text(raw, "Website", 300)
    if not url:
        return ""
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
        url = "https://" + url
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        parts.port  # raises ValueError on a malformed port
    except ValueError:
        raise ValueError("Enter a valid website address, e.g. https://yourfirm.com")
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError("Website must start with http:// or https://")
    if parts.username or parts.password or not _HOSTNAME_RE.match(host):
        raise ValueError("Enter a valid website address, e.g. https://yourfirm.com")
    path = parts.path.rstrip("/")
    query = f"?{parts.query}" if parts.query else ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme.lower()}://{host}{port}{path}{query}"


def normalize_phone(raw: Any, field: str = "Office phone") -> str:
    """E.164 (+15551234567) with a plausible digit count."""
    text = _text(raw, field, 40)
    if not text:
        return ""
    from agent.telephony_manager import normalize_phone_number
    phone = normalize_phone_number(text)
    if not re.fullmatch(r"\+\d{8,15}", phone):
        raise ValueError(f"{field} must be a valid phone number, e.g. +1 555 123 4567")
    return phone


def _known_timezones() -> set:
    try:
        from zoneinfo import available_timezones
        return available_timezones()
    except Exception:
        return set()


# Old IANA names that browsers still report (Chrome's Intl uses ICU's legacy IDs, e.g. Asia/Calcutta)
# but that tz installs without the "backward" file do not know. Stored under the current name.
TIMEZONE_ALIASES = {
    "Asia/Calcutta": "Asia/Kolkata", "Asia/Saigon": "Asia/Ho_Chi_Minh", "Asia/Katmandu": "Asia/Kathmandu",
    "Asia/Rangoon": "Asia/Yangon", "Asia/Dacca": "Asia/Dhaka", "Asia/Thimbu": "Asia/Thimphu",
    "Asia/Ujung_Pandang": "Asia/Makassar", "Asia/Ulan_Bator": "Asia/Ulaanbaatar", "Asia/Macao": "Asia/Macau",
    "Asia/Chongqing": "Asia/Shanghai", "Asia/Chungking": "Asia/Shanghai", "Asia/Harbin": "Asia/Shanghai",
    "Asia/Kashgar": "Asia/Urumqi", "Asia/Tel_Aviv": "Asia/Jerusalem", "Asia/Ashkhabad": "Asia/Ashgabat",
    "Europe/Kiev": "Europe/Kyiv", "Europe/Uzhgorod": "Europe/Kyiv", "Europe/Zaporozhye": "Europe/Kyiv",
    "Atlantic/Faeroe": "Atlantic/Faroe", "Africa/Asmera": "Africa/Asmara",
    "America/Godthab": "America/Nuuk", "America/Buenos_Aires": "America/Argentina/Buenos_Aires",
    "America/Catamarca": "America/Argentina/Catamarca", "America/Cordoba": "America/Argentina/Cordoba",
    "America/Jujuy": "America/Argentina/Jujuy", "America/Mendoza": "America/Argentina/Mendoza",
    "America/Indianapolis": "America/Indiana/Indianapolis", "America/Fort_Wayne": "America/Indiana/Indianapolis",
    "America/Louisville": "America/Kentucky/Louisville", "America/Coral_Harbour": "America/Panama",
    "America/Montreal": "America/Toronto", "Pacific/Truk": "Pacific/Chuuk", "Pacific/Ponape": "Pacific/Pohnpei",
    "Pacific/Enderbury": "Pacific/Kanton", "Pacific/Samoa": "Pacific/Pago_Pago",
    "US/Eastern": "America/New_York", "US/Central": "America/Chicago", "US/Mountain": "America/Denver",
    "US/Pacific": "America/Los_Angeles", "US/Alaska": "America/Anchorage", "US/Hawaii": "Pacific/Honolulu",
    "US/Arizona": "America/Phoenix",
}


def normalize_timezone(raw: Any) -> str:
    tz = _text(raw, "Time zone", 64)
    if not tz:
        return ""
    known = _known_timezones()
    if tz not in known and tz in TIMEZONE_ALIASES:
        tz = TIMEZONE_ALIASES[tz]
    # Slim images may ship without tz data; then fall back to checking the Area/City shape.
    if (known and tz not in known) or (not known and not _TZ_SHAPE_RE.match(tz)):
        raise ValueError(f"Unknown time zone '{tz}' (use an IANA name such as America/New_York)")
    return tz


def _choice(raw: Any, field: str, allowed) -> str:
    value = _text(raw, field, 40).lower()
    if value and value not in allowed:
        raise ValueError(f"{field} must be one of: {', '.join(allowed)}")
    return value


def _list(raw: Any, field: str) -> List[Any]:
    if raw is None or raw == "":
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{field} must be a list")
    return raw


def _address(raw: Any) -> Dict[str, str]:
    if raw is None or raw == "":
        return {}
    if not isinstance(raw, dict):
        raise ValueError("Address must be an object")
    unknown = set(raw) - set(ADDRESS_FIELDS)
    if unknown:
        raise ValueError(f"Unknown address field: {', '.join(sorted(unknown))}")
    out = {}
    for key in ADDRESS_FIELDS:
        if raw.get(key) not in (None, ""):
            val = _text(raw[key], f"Address {key}", 120)
            if val:
                out[key] = val
    return out


def _business_hours(raw: Any) -> Dict[str, Optional[Dict[str, str]]]:
    """{"mon": {"open": "09:00", "close": "17:00"}, "sat": None, ...}; a missing or None day is closed."""
    if raw is None or raw == "":
        return {}
    if not isinstance(raw, dict):
        raise ValueError("Business hours must be an object keyed by weekday")
    unknown = set(raw) - set(WEEKDAYS)
    if unknown:
        raise ValueError(f"Unknown weekday in business hours: {', '.join(sorted(unknown))}")
    out: Dict[str, Optional[Dict[str, str]]] = {}
    for day in WEEKDAYS:
        if day not in raw:
            continue
        slot = raw[day]
        if slot is None:
            out[day] = None
            continue
        if not isinstance(slot, dict) or set(slot) != {"open", "close"}:
            raise ValueError(f"Business hours for {day} need 'open' and 'close'")
        opens, closes = str(slot["open"]).strip(), str(slot["close"]).strip()
        if not _TIME_RE.match(opens) or not _TIME_RE.match(closes):
            raise ValueError(f"Business hours for {day} must use 24-hour HH:MM times")
        if opens >= closes:
            raise ValueError(f"Business hours for {day}: closing time must be after opening time")
        out[day] = {"open": opens, "close": closes}
    return out


def _clean_value(field: str, raw: Any) -> Any:
    if field == "website":
        return normalize_website(raw)
    if field == "office_phone":
        return normalize_phone(raw, "Office phone")
    if field == "after_hours_transfer_number":
        return normalize_phone(raw, "After-hours transfer number")
    if field == "address":
        return _address(raw)
    if field == "timezone":
        return normalize_timezone(raw)
    if field == "firm_size":
        return _choice(raw, "Firm size", FIRM_SIZES)
    if field == "practice_areas":
        areas = []
        for item in _list(raw, "Practice areas"):
            area = _choice(item, "Practice area", tuple(PRACTICE_AREAS))
            if area and area not in areas:
                areas.append(area)
        return areas
    if field == "business_hours":
        return _business_hours(raw)
    if field == "after_hours":
        return _choice(raw, "After-hours handling", AFTER_HOURS_ACTIONS)
    if field == "languages":
        langs = []
        for item in _list(raw, "Languages"):
            code = _text(item, "Language", 8)
            code = code[:2].lower() + code[2:].upper()
            if not _LANG_RE.match(code):
                raise ValueError(f"Language '{item}' must be a language code such as en, es or pt-BR")
            if code not in langs:
                langs.append(code)
        if len(langs) > MAX_LANGUAGES:
            raise ValueError(f"At most {MAX_LANGUAGES} languages")
        return langs
    if field == "monthly_call_volume":
        return _choice(raw, "Monthly call volume", CALL_VOLUMES)
    if field == "practice_software":
        return _choice(raw, "Practice management software", tuple(PRACTICE_SOFTWARE))
    if field == "lead_emails":
        emails = []
        for item in _list(raw, "Lead notification emails"):
            email = _text(item, "Lead notification email", 254).lower()
            if not _EMAIL_RE.match(email):
                raise ValueError(f"'{item}' is not a valid email address")
            if email not in emails:
                emails.append(email)
        if len(emails) > MAX_LEAD_EMAILS:
            raise ValueError(f"At most {MAX_LEAD_EMAILS} lead notification emails")
        return emails
    if field in ("practice_areas_other", "practice_software_other"):
        return _text(raw, field.replace("_", " ").capitalize(), 120)
    if field == "referral_source":
        return _text(raw, "Referral source", 200)
    raise ValueError(f"Unknown firm profile field: {field}")


def _is_empty(value: Any) -> bool:
    return value in ("", None) or value == [] or value == {}


def normalize_firm_profile(
    changes: Optional[Dict[str, Any]],
    existing: Optional[Dict[str, Any]] = None,
    required: tuple = (),
) -> Dict[str, Any]:
    """Validate ``changes`` and merge them over ``existing``; returns a new, clean profile.

    A field set to None or "" is removed. Unknown fields are rejected rather than silently dropped,
    so a typo in an API call surfaces as an error. ``required`` lists fields that must end up set.
    """
    if changes is None:
        changes = {}
    if not isinstance(changes, dict):
        raise ValueError("Firm profile must be an object")
    unknown = sorted(set(changes) - set(FIELDS))
    if unknown:
        raise ValueError(f"Unknown firm profile field: {', '.join(unknown)}")

    profile = {k: v for k, v in (existing or {}).items() if k in FIELDS}
    for field in FIELDS:
        if field not in changes:
            continue
        value = None if changes[field] is None else _clean_value(field, changes[field])
        if _is_empty(value):
            profile.pop(field, None)
        else:
            profile[field] = value

    # Dependent fields only make sense alongside the choice that needs them.
    if profile.get("after_hours") == "transfer":
        if not profile.get("after_hours_transfer_number"):
            raise ValueError("Add the number to transfer after-hours calls to")
    else:
        profile.pop("after_hours_transfer_number", None)
    if "other" not in profile.get("practice_areas", []):
        profile.pop("practice_areas_other", None)
    if profile.get("practice_software") != "other":
        profile.pop("practice_software_other", None)

    missing = [f for f in required if _is_empty(profile.get(f))]
    if missing:
        raise ValueError(f"Missing required firm details: {', '.join(missing)}")
    return profile
