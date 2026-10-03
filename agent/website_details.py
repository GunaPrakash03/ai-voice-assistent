"""Turn a firm's website pages (agent/website_scraper.read_site) into firm profile details:
an About text, practice areas with short descriptions, and the attorney roster.

Gemini does the reading when GEMINI_API_KEY is set (one call, JSON out, told to use only what the pages
say). Without it, or if the call fails, a heading-based fallback fills what it can. Either way the
result is a draft the admin reviews on the onboarding screen; nothing here saves anything.
"""

import logging
import re
from typing import Any, Dict, List, Optional

from agent.firm_profile import PRACTICE_AREAS

log = logging.getLogger("voice-agent.website")

MAX_ABOUT = 1500
MAX_ATTORNEYS = 40
MAX_PRACTICES = 20
MAX_BIO = 600
PROMPT_CHARS = 36_000

# Words that place a practice description under one of the onboarding practice-area options.
AREA_WORDS = {
    "personal_injury": ("injury", "accident", "wrongful death", "malpractice", "slip and fall", "workers comp", "workers' comp"),
    "family": ("family", "divorce", "custody", "child support", "alimony", "adoption", "prenup"),
    "criminal_defense": ("criminal", "dui", "dwi", "felony", "misdemeanor", "defense of charges"),
    "immigration": ("immigration", "visa", "green card", "citizenship", "asylum", "deportation"),
    "estate_planning": ("estate", "probate", "wills", "trusts", "elder law", "guardianship"),
    "business": ("business", "corporate", "commercial", "contract", "mergers", "startup", "intellectual property"),
    "employment": ("employment", "workplace", "wage", "discrimination", "harassment", "wrongful termination"),
    "real_estate": ("real estate", "property", "landlord", "tenant", "closing", "zoning", "title"),
}
# Heading words that mark a section title rather than a person's name.
NOT_NAMES = {"our", "the", "meet", "team", "attorneys", "lawyers", "contact", "us", "today", "call", "about", "practice",
             "areas", "people", "staff", "free", "consultation", "results", "reviews", "news", "blog", "home", "office", "why", "we"}
TITLE_WORDS = ("partner", "associate", "attorney", "lawyer", "counsel", "founder", "principal", "shareholder", "paralegal", "esq")


def area_key(text: str) -> str:
    t = (text or "").lower()
    for key, words in AREA_WORDS.items():
        if any(w in t for w in words):
            return key
    return "other"


def _clean(text: Any, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def _tidy(data: Dict[str, Any]) -> Dict[str, Any]:
    """Shape and cap whatever came back (model or fallback) into the draft the screen shows."""
    practices, seen = [], set()
    for p in data.get("practice_areas") or []:
        if isinstance(p, str):
            p = {"name": p}
        name = _clean((p or {}).get("name"), 80)
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        desc = _clean(p.get("description"), 400)
        practices.append({"name": name, "description": desc, "area": area_key(name + " " + desc)})
    attorneys, seen = [], set()
    for a in data.get("attorneys") or []:
        name = _clean((a or {}).get("name"), 80)
        if not name or name.lower() in seen or len(name.split()) > 6:
            continue
        seen.add(name.lower())
        areas = a.get("practice_areas") or []
        if isinstance(areas, str):
            areas = [x.strip() for x in areas.split(",")]
        attorneys.append({"name": name, "title": _clean(a.get("title"), 80),
                          "practice_areas": [_clean(x, 60) for x in areas if _clean(x, 60)][:6],
                          "bio": _clean(a.get("bio"), MAX_BIO)})
    return {"about": _clean(data.get("about"), MAX_ABOUT),
            "practice_areas": practices[:MAX_PRACTICES],
            "attorneys": attorneys[:MAX_ATTORNEYS]}


def _prompt_text(site: Dict[str, Any]) -> str:
    chunks, used = [], 0
    for p in site.get("pages", []):
        block = f"=== PAGE ({p['kind']}) {p['url']}\nTITLE: {p.get('title', '')}\n" \
                f"{('DESCRIPTION: ' + p['description'] + chr(10)) if p.get('description') else ''}{p.get('text', '')}\n"
        if used + len(block) > PROMPT_CHARS:
            block = block[:max(0, PROMPT_CHARS - used)]
        chunks.append(block)
        used += len(block)
        if used >= PROMPT_CHARS:
            break
    return "\n".join(chunks)


SYSTEM = (
    "You read a law firm's website and fill in its profile for an AI phone receptionist. Use ONLY facts stated "
    "on the pages; never invent names, titles, areas or credentials. Answer with JSON exactly like:\n"
    '{"about": "2-4 plain sentences on who the firm is, where it is and who it helps",\n'
    ' "practice_areas": [{"name": "Divorce", "description": "one sentence from the site"}],\n'
    ' "attorneys": [{"name": "Jane Roe", "title": "Partner", "practice_areas": ["Divorce"], "bio": "1-2 sentences"}]}\n'
    "Leave a list empty if the pages don't say. Skip non-lawyer staff unless listed with the attorneys."
)


def _with_gemini(site: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        from agent.schema_extractor import gemini_json
        data = gemini_json(SYSTEM, _prompt_text(site), max_tokens=4000, timeout=40.0)
        return data if isinstance(data, dict) else None
    except Exception as e:
        log.info("Website details via Gemini unavailable (%s); using the page headings", e)
        return None


_PERSON = re.compile(r"^([A-Z][a-zA-Z'’.-]+(?: [A-Z][a-zA-Z'’.-]*\.?){1,3})(?:,? (?:Esq\.?|J\.?D\.?))?$")


def _fallback(site: Dict[str, Any]) -> Dict[str, Any]:
    """No model: About from the about/home page, practices from headings on practice pages, attorneys
    from person-like headings followed by a title word on attorney pages."""
    pages = site.get("pages", [])
    about_page = next((p for p in pages if p["kind"] == "about"), None) or (pages[0] if pages else None)
    about = ""
    if about_page:
        paras = [l for l in about_page["text"].split("\n") if len(l) > 80 and not l.startswith("#")]
        about = " ".join(paras[:3]) or about_page.get("description", "")
    practices, attorneys = [], []
    for p in pages:
        lines = p["text"].split("\n")
        for i, line in enumerate(lines):
            if not line.startswith("#"):
                continue
            head = line.lstrip("# ").strip()
            nxt = next((l for l in lines[i + 1:i + 4] if l and not l.startswith("#")), "")
            if p["kind"] == "practice" and 2 < len(head) < 60 and not line.startswith("# "):
                practices.append({"name": head, "description": nxt[:300]})
            elif p["kind"] == "attorneys" and _PERSON.match(head) and not (set(head.lower().split()) & NOT_NAMES):
                title = nxt if any(w in nxt.lower() for w in TITLE_WORDS) and len(nxt) < 80 else ""
                if title or not line.startswith("# "):
                    attorneys.append({"name": head, "title": title, "bio": ""})
    return {"about": about, "practice_areas": practices, "attorneys": attorneys}


def extract_firm_details(site: Dict[str, Any]) -> Dict[str, Any]:
    """Draft {about, practice_areas[{name, description, area}], attorneys[{name, title, practice_areas, bio}],
    areas (onboarding practice-area keys to tick), engine, pages_read[], skipped[]}."""
    data = _with_gemini(site)
    engine = "gemini" if data else "headings"
    draft = _tidy(data or _fallback(site))
    if engine == "gemini" and not (draft["about"] or draft["practice_areas"] or draft["attorneys"]):
        draft, engine = _tidy(_fallback(site)), "headings"
    areas = sorted({p["area"] for p in draft["practice_areas"] if p["area"] in PRACTICE_AREAS and p["area"] != "other"})
    draft.update(engine=engine, areas=areas,
                 pages_read=[{"url": p["url"], "kind": p["kind"]} for p in site.get("pages", [])],
                 skipped=site.get("skipped", []))
    return draft
