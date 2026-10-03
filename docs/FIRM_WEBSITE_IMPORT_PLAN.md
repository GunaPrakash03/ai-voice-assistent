# Firm details from the website — plan & progress

During onboarding (step 1, under **Website**) the admin ticks **"Fill in firm details from our website"**.
Ticked: the site is read and three editable sections are pre-filled — **About the firm**, **Practice areas**,
**Attorneys** — for the admin to review. Unticked: the same sections appear empty to type in. The details feed
the AI receptionist's script and can be edited later on the Profile page.

Decisions taken (change any time): checkbox starts ticked · attorneys are profile information, not logins ·
Gemini free tier is enough for one onboarding at a time.

## Progress

```
Overall  ████████████████████  12 / 12 tasks  100%
```

| Phase | Tasks | Done |
|---|---|---|
| A. Reading the website | T1–T3 | 3 / 3 |
| B. Data & API | T4–T5 | 2 / 2 |
| C. Onboarding screen | T6–T7 | 2 / 2 |
| D. Using the details | T8–T9 | 2 / 2 |
| E. Testing & ship | T10–T12 | 3 / 3 |

Status key: `[ ]` to do · `[~]` in progress · `[x]` done

## Tasks

### A. Reading the website

- [x] **T1 — Website reader** (`agent/website_scraper.py`, new)
  Open the homepage, follow up to 8 same-site links whose text or URL looks like About / Our firm / Attorneys /
  Lawyers / Team / People / Practice areas / Services, and reduce each page to plain text with Python's built-in
  HTML parser (no new dependency).
- [x] **T2 — Safety limits**
  Only `http`/`https`; refuse private, loopback and link-local addresses (checked after DNS and on every redirect);
  cap page size, page count and total time; respect `robots.txt`.
- [x] **T3 — Turn page text into firm details**
  Gemini (shared `gemini_json` helper) returns `about`, `practice_areas` (each with a short description, mapped to the
  existing practice-area options) and `attorneys` (name, title, practice areas, short bio). Without Gemini, a
  heading-based fallback fills what it can.

### B. Data & API

- [x] **T4 — Firm profile fields** (`agent/firm_profile.py`)
  New fields `about_firm`, `practice_details`, `attorneys`, `details_source` (`website` / `manual`),
  `details_read_at`, validated and length-limited like the others.
- [x] **T5 — Draft endpoint** `POST /api/v1/onboarding/website-draft`
  Admin-only, rate-limited per organization; returns the draft and saves nothing.

### C. Onboarding screen

- [x] **T6 — Checkbox and the three sections** (`web/onboarding.html`)
  Checkbox under Website (ticked by default). Ticked → "Reading your website…" then pre-filled, editable sections;
  unticked → empty sections to type in; read failure → plain message and the empty sections, keeping anything found.
  Matching practice-area options on step 2 are ticked automatically.
- [x] **T7 — Save with onboarding**
  The finish step sends the reviewed details; the review screen shows them.

### D. Using the details

- [x] **T8 — AI receptionist script** (`agent/onboarding.py`)
  The generated agent knows the firm description, attorney roster and practice details, so it can answer
  "who handles divorces?" — without giving legal advice.
- [x] **T9 — Profile page "Firm details" card**
  Edit the three sections later, plus a **Refresh from website** button.

### E. Testing & ship

- [x] **T10 — Automated checks** (`scripts/verify_website_import.py`, new)
  Local test website (about, attorneys, practice pages, a broken page, a slow page), refusal of private addresses
  and unsafe redirects, robots.txt, Gemini stand-in, profile validation, endpoint rules.
- [x] **T11 — Browser check**
  Both onboarding paths (ticked / unticked), the failure path, and the Profile card, in light/dark and phone width.
- [x] **T12 — Commit & push**

## Log

| Date | Task | Note |
|---|---|---|
| 2026-10-03 | T1–T2 | agent/website_scraper.py: same-site page picking, HTML→text, public-address check pinned to the checked IP, redirects re-checked, size/time/page caps, robots.txt |
| 2026-10-03 | T3 | agent/website_details.py: Gemini reads pages into about / practice areas / attorneys (facts from the pages only); heading fallback without Gemini; maps to onboarding practice-area options |
| 2026-10-03 | T4 | firm profile: about_firm, practice_details, attorneys, details_source, details_read_at — validated, capped (1500 chars / 20 areas / 40 attorneys) |
| 2026-10-03 | T5 | POST /api/v1/onboarding/website-draft: admin-only, 5 per firm per 10 min, saves nothing |
| 2026-10-03 | T6–T7 | onboarding step 1: checkbox (ticked), reading note, editable About / practice rows / attorney rows, manual + failure paths, ticks step-2 areas, sent on finish, review card, kept in tab draft |
| 2026-10-03 | T8 | receptionist script gets a capped ABOUT THE FIRM block (description, practice areas, attorneys; no legal advice, no promising an attorney) |
| 2026-10-03 | T9 | Profile → Firm details card: edit, Refresh from website, Save; GET/POST /api/v1/firm/details; saving rewrites only the firm block of the seeded agent's script |
| 2026-10-03 | T10 | scripts/verify_website_import.py 74/74 (local test site, address safety, Gemini stand-in, profile fields, agent block, HTTP); other suites still pass (209, 29, 104) |
| 2026-10-03 | T11 | browser: website path (5 pages read, details filled, step-2 areas ticked, saved, agent script updated), typed-in path (dark), unreadable site at 400px, Profile card save → agent updated; no console errors |
| 2026-10-03 | T12 | committed a66e6a5, 91db5eb and pushed to main |
