# Hardwire ICP Finder — build brief

A brief for Claude Code. Read this whole file before writing anything.

---

## 1. What this is

A small tool that finds and ranks UK domestic electricians who fit Hardwire's ICP.

Hardwire sells an AI-native operating system for small electrical contractors. The
initial wedge is **estimating and quoting**: turn an enquiry into a priced quote and a
materials list. The stated ICP is **sole traders up to teams of about 10**, with
particular interest in electricians who have recently gone independent from PAYE or
agency work.

The commercial goal is to get a 15-minute call or product walkthrough booked. So the
ranking answers one question: **which of these people should we call first.**

The tool is a prospecting aid, not a database. Judgment quality matters more than
record count. Do not overbuild.

---

## 2. What already exists

Working, tested, do not rewrite:

| File | What it does |
|---|---|
| `src/score.py` | Gates, weighted scoring, funnel. Reads the two CSVs, writes `out/ranked.json` and `out/ranked.csv` |
| `src/report.py` | Renders `out/scorecard.html` from `ranked.json` |
| `data/checkatrade_raw.csv` | 30 businesses, collected by hand |
| `data/companies_house.csv` | 18 cross-reference rows, collected by hand |

**Your job is the two collectors that currently do not exist**, so that the CSVs above
are produced automatically instead of by hand. The CSV schemas are the contract between
your code and the existing scorer — match them exactly, column for column.

---

## 3. What to build

```
src/collect_checkatrade.py     # search pages -> profile URLs -> profiles -> data/checkatrade_raw.csv
src/collect_companies_house.py # name/officer resolution -> data/companies_house.csv
src/cache.py                   # shared cache-first fetch helper
run.py                         # orchestrates collect -> score -> report
```

`run.py` takes `--towns Reading Wokingham Bracknell` and `--refresh` (bypass cache).
Default is cache-only for everything already fetched.

---

## 4. Constraints already discovered — read these, they will save you hours

These were found by hand. Trust them; verify only if something breaks.

**Checkatrade**

- `robots.txt` **permits** `/trades/` and `/Search/`. It disallows `/Account/`,
  `/GiveFeedback/`, `*/bookable-services/` and `*/raq-message*`. Stay out of those.
  There is no crawl-delay directive, so set your own.
- Search URL: `https://www.checkatrade.com/Search/Electrician/in/{Town}`
- Profile URL: `https://www.checkatrade.com/trades/{slug}`
- **Search pagination is client-side.** `?page=2` is ignored and returns page 1. A plain
  HTTP GET yields only the first 12 results. You need Playwright for the listing stage —
  click or scroll through pagination and harvest profile hrefs.
- **Profile pages are server-rendered.** Plain `requests` + BeautifulSoup works fine
  there. Do not launch a browser per profile; it is 5–10x slower for no gain.
- **Search is service-radius, not location.** The same business appears under Reading,
  Wokingham, Bracknell and Newbury. Deduplicate on slug. Take the business's real
  location from the *profile*, never from the town you searched — several businesses
  surfacing under "Reading" are based in Surrey, Buckinghamshire and Hampshire.
- Reading returns ~64 results, Wokingham ~82, Bracknell ~92, Newbury ~42, with heavy
  overlap between them.

**Companies House**

- Free API key from `developer.company-information.service.gov.uk`. HTTP Basic auth,
  API key as username, blank password. Prefer the API over scraping the web UI.
- `GET /advanced-search/companies` filters on `sic_codes=43210`, `company_status=active`,
  `location`, and incorporation date range, all at once.
- `GET /company/{number}` for profile, `GET /company/{number}/officers` for directors.
- **Name matching fails on generic names.** "F & H Projects Ltd" returns 10,000 matches
  and no usable result. Fall back to `GET /search/officers?q={owner_name}` using the
  owner name from Checkatrade — if that person has exactly one appointment, you have
  your company and your officer count in a single call.
- **Trading name often is not the registered name.** Carter's Electrical Services is
  registered as Oxford Smart Homes Ltd. The Checkatrade URL slug (`oxfordsmarthomesltd`)
  gave it away. **Always try the slug as a search term** — it frequently encodes the
  legal entity when the display name does not.
- Watch for previous names in the CH record (Mortimer Electrics was Mortimer Electrical).

---

## 5. Data contract

### `data/checkatrade_raw.csv`

```
slug, name, owner, structure, member_since, vat_registered, base_town, county,
rating, reviews_total, reviews_12mo, latest_review, accreditations, services_count,
has_ev, has_rewire, has_consumer_unit, has_eicr, claimed_experience_yrs,
services_extraction, profile_url
```

- `structure`: one of `sole_trader`, `limited`, `partnership`. Checkatrade states this
  explicitly on the profile. **Do not infer it from the business name** — "Bradley James
  Electrical" is a limited company and "EMS Electrical" is a sole trader.
- `member_since`: `YYYY-MM`
- `vat_registered`: `yes` / `no`. Shown on the profile. This is a free revenue band
  against the £90k VAT threshold.
- `reviews_12mo`: the "last 12 months" figure, shown separately from the lifetime total.
  **This is the single most valuable field on the page** — it is the only one that
  measures whether the business is working right now.
- `has_ev` / `has_rewire` / `has_consumer_unit` / `has_eicr`: `Y` or empty. Match against
  the full service list, not the summary. Blank means *not recorded*, not *not offered*.
- `services_extraction`: set to `detailed` when you parsed the full service list.
  The scorer uses this to lower confidence rather than to punish the record.
- `accreditations`: semicolon-separated.

### `data/companies_house.csv`

```
slug, ch_name, ch_number, ch_status, incorporated, reg_office, office_type,
officers_active, officers_shared_identity, match_confidence, match_method, ch_url
```

- `slug` is the join key back to the Checkatrade row.
- `incorporated`: `YYYY-MM-DD`
- `office_type`: `residential` | `commercial` | `accountant`. Classify by the address
  string. **`accountant` is a required third category** — "C/O Taxassist Accountants"
  and similar tell you nothing about company size, and must not be read as commercial.
  Detect `C/O`, "Accountants", "Accountancy", "Ltd" in the first address line.
- `officers_active`: count of directors not resigned.
- `officers_shared_identity`: `yes` when two directors share a surname **or** a
  correspondence address. This matters more than it sounds — see §6.
- `match_confidence`: `high` | `medium` | `officer_only` | `not_found` | `not_checked`
- `match_method`: free text describing how the match was made. It appears in the report.

---

## 6. The scoring logic, and why it is what it is

Do not change this without a reason. It was arrived at empirically and each rule exists
because a naive version of it failed on real data.

### Gates

**Gate 1 — owner-operated.** Passes if structure is sole trader or partnership, or if
the limited company looks owner-run: one director, **two directors sharing a surname or
address**, or a residential registered office. Fails on commercial premises. Returns
`unverified` when Companies House could not be resolved.

> Four of the five two-director companies checked were an electrician plus a spouse
> (Taranenko/Taranenko, Chaudhary/Chaudhary, Timpov/Timpov, and Collins with a
> co-director at the same address). A "one director" rule rejects the commonest shape of
> UK micro-business — and that spouse doing the books at the kitchen table is exactly
> who Hardwire's product replaces.

**Gate 2 — actively trading.** At least one reviewed job in the last 12 months.

> This replaced a "joined Checkatrade within 4 years" filter. That filter passed only
> 3 of the first 12 businesses, and it passed the wrong ones: Wrightsparks joined in
> 2021 but its last review was August 2024 and it does 3 jobs a year. Recency of
> *joining a platform* is not recency of *trading*.

**Three states, not two.** `unverified` must never be collapsed into `fail`. In the first
run, F & H Projects — the top-scoring prospect at 95 — was silently dropped because its
Companies House record hadn't been resolved. Absence of evidence is a work queue item,
not a rejection.

### Score, 100 points

| Signal | Weight | Rationale |
|---|---|---|
| Job throughput (`reviews_12mo`) | 30 | Only field measuring current activity. Spread the real sample from 0 to 79 |
| Business recency | 20 | No entrenched process to displace — the cheapest sale |
| Quoting intensity (EV, rewires, consumer units, EICRs) | 20 | The wedge is estimating; it only bites where they quote constantly |
| Revenue band (VAT) | 15 | Ability to pay. A trader just under £90k wants margin, not volume |
| Service breadth | 10 | Mild preference; self-declared so deliberately low weight |
| Credibility (NICEIC / NAPIT) | 5 | Filter for legitimacy, not a differentiator |

Bands: High ≥70, Medium 50–69, Low <50.

**Business age must come from the earliest credible evidence, not from Checkatrade.**
Across all ten companies resolved, the Checkatrade join date was *later* than the
Companies House incorporation date, by a median of about two and a half years. Galaxy
Electrical joined Checkatrade five months ago and incorporated in 2019. Take
`min(incorporation, member_since)` and record which source won.

**Confidence is scored separately from fit.** A high score at low confidence is a
research task; a high score at high confidence is a call to make today.

---

## 7. Fetching rules

- **Cache first, always.** Hash the URL, store the raw response under `data/cache/`.
  A second run must make zero network calls unless `--refresh` is passed. This keeps the
  run reproducible for a reviewer and is the polite thing to do.
- Rate limit: one request per 2–3 seconds, single-threaded. This is a 30–100 page job,
  not a crawl.
- Set a real `User-Agent` identifying the project and a contact address.
- Stop on the first 429 or 403 and report it. Do not retry in a loop.
- Never fetch a path disallowed by robots.txt.

---

## 8. Acceptance tests

Write these as you go, not afterwards.

1. `python run.py --towns Reading` produces ≥25 rows in `checkatrade_raw.csv` with no
   duplicate slugs.
2. Every row has a non-empty `structure` drawn from the profile, and the distribution is
   not 100% one value.
3. Re-running without `--refresh` makes zero network calls (assert on a request counter).
4. `score.py` runs unchanged against the generated CSVs and the funnel totals sum to the
   collected count.
5. A business whose registered office contains "C/O" is classified `accountant`, not
   `commercial`.
6. A limited company with no CH match lands in `needs_verification`, never in `excluded`.
7. `report.py` output opens with no console errors and renders in both light and dark.

---

## 9. Known weaknesses to leave alone for now

Documented in the scorecard's limitations panel. Do not silently "fix" these — they are
honest boundaries of the prototype and are part of the submission.

- Checkatrade-only sourcing means everyone in the sample pays for membership and has
  passed vetting. Electricians too new or too broke to join are invisible, and they may
  be a large slice of the target market.
- "Sole trader" does not mean solo — a sole trader can employ people. Two businesses in
  the sample do 79 jobs a year.
- Review counts are a proxy for job counts, and solicitation habits vary.
- 12 of the 30 records have `services_extraction=summary`, so their quoting-intensity
  score is under-measured.

---

## 10. What is deliberately out of scope

- Any login-gated data.
- Contacting anyone. This tool produces a list; it does not send anything.
- A database. CSV and JSON are correct at this size.
- A web framework. `report.py` writes one static HTML file.
