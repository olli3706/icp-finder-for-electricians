# ICP Finder for Electricians

Finds UK domestic electricians on Checkatrade, checks the limited companies against
Companies House, and ranks every business by how good a sales prospect it is. The
output is a static HTML scorecard that answers one question: **who should we call first?**

It was built for a company selling AI estimating and quoting software to small
electrical contractors: sole traders up to teams of about 10. It's a prospecting
aid, not a database. It produces a list and never contacts anyone.

-- this was built as consultancy work for an AI startup and built using claude code 
---

## How it works

```
Checkatrade search pages ──► profile pages ──► data/checkatrade_raw.csv
                                                        │
Companies House API (limited companies only) ──► data/companies_house.csv
                                                        │
                                            src/score.py (gates + scoring)
                                                        │
                                   out/ranked.json, out/ranked.csv
                                                        │
                                   src/report.py ──► out/scorecard.html
```

1. **Collect from Checkatrade** (`src/collect_checkatrade.py`): searches each town
   from several nearby "anchor" points, because Checkatrade only shows 12
   results per search and pagination doesn't work. It removes duplicates by
   profile slug, then reads each profile from the page's embedded JSON.
2. **Cross-reference Companies House** (`src/collect_companies_house.py`): for
   limited companies only, it tries four ways to find the right company, in
   order: exact name, then the URL slug (it often contains the legal name),
   then an officer search on the owner's name, then gives up as not found. It
   also classifies the registered office as residential, commercial or
   accountant, and counts active directors.
3. **Score** (`src/score.py`): applies two gates, then a 100-point weighted
   score. See [Scoring](#scoring).
4. **Report** (`src/report.py`): writes a single self-contained HTML page with
   the funnel, the ranked list, evidence for every judgment, and a panel for
   re-weighting the scores in the browser.

`run.py` runs all four steps in order.

---

## Setup

Requires Python 3.10+.

and a companies house api key this can be inserted into the .env file 

```bash
pip install requests beautifulsoup4 playwright pytest
```

```bash
playwright install chromium
```

```bash
cp env.example .env
```

Then edit `.env`:

| Key | Purpose |
|---|---|
| `CH_API_KEY` | Free Companies House REST API key from [developer.company-information.service.gov.uk](https://developer.company-information.service.gov.uk). Optional, see below. |
| `CONTACT_EMAIL` | Sent in the User-Agent so site operators can identify and contact you. |

`.env` is gitignored. Never commit it.

**Without a Companies House key** the pipeline still runs. The CH step is
skipped and every limited company scores as `needs_verification` rather than
being dropped. This is what the committed sample data shows.

---

## Usage

```bash
python run.py
```

This asks which areas to run for. You can type towns or postcode districts
separated by commas, "and", "&" or "+", e.g. `fulham, reading and RG1`.
Pressing Enter on an empty line uses the defaults (Reading, Maidenhead,
Wokingham).

| Command | What it does |
|---|---|
| `python run.py --towns Reading Wokingham` | Run for these areas without asking |
| `python run.py --refresh` | Ignore the cache and fetch everything again |
| `python run.py --no-collect` | Skip collection, re-score and re-render from the existing CSVs |

Open `out/scorecard.html` in a browser to see the result.

### Running stages on their own

```bash
python src/collect_checkatrade.py --towns Reading --headed
```

```bash
python src/collect_checkatrade.py --profile some-slug
```

```bash
python src/collect_companies_house.py --slug some-slug
```

```bash
python src/diff_reference.py
```

The collectors write to `*.new.csv` files, never directly over the main data.
`run.py` compares the new file with the current one (`diff_reference.py`) and
only replaces the current file when key fields match. When it does, the old
data is copied into `data/archive/<timestamp>/` first.

---

## Fetching rules

These are enforced in code, not just written down:

- **Cache first.** Every response is stored under `data/cache/`, named by a hash
  of its URL. A second run makes zero network calls unless you pass `--refresh`.
- **Rate limited.** At most one request every 2.5 seconds, one at a time.
- **No retry loops.** The first 403, 429 or Cloudflare challenge stops the run
  and reports it.
- **robots.txt respected.** Only `/trades/` and `/Search/` on Checkatrade are
  fetched. `/Account/`, `/GiveFeedback/`, `*/bookable-services/` and
  `*/raq-message*` are refused in `src/cache.py`.

### Cloudflare

Checkatrade puts a Cloudflare bot check in front of its listing and profile
pages. Playwright's bundled Chromium is usually challenged from its second
page load. By default the collector instead launches your installed
Chrome/Edge with a separate profile (`data/chrome-profile/`, gitignored) and
connects to it through the DevTools protocol (`--engine cdp`).

If you're still blocked, open the pages yourself in a normal browser and save
them into the cache:

```bash
python src/pane_ingest.py profile <slug> saved-page.html
```

```bash
python src/pane_ingest.py listing <anchor-town> slugs.txt
```

A later run that uses only the cache then rebuilds the CSVs with no network
access.

---

## Scoring

### Gates

| Gate | Pass | Fail | Unverified |
|---|---|---|---|
| **Owner-operated** | Sole trader or partnership; or a limited company with one director, two directors sharing a surname/address, or a residential registered office | Commercial premises | Companies House record not found |
| **Actively trading** | At least one review in the last 12 months | Zero | n/a |

`unverified` never counts as `fail`. A limited company with no Companies
House match goes into `needs_verification`, a list of companies still to
check by hand, rather than being excluded. Two directors who share a surname
are allowed because the most common UK micro-business is an electrician plus
a spouse doing the books.

### Score (100 points)

| Signal | Weight | Source |
|---|---|---|
| Job throughput | 30 | `reviews_12mo`, the only field that shows current activity |
| Business recency | 20 | Earlier of incorporation date and Checkatrade join date |
| Quoting intensity | 20 | Offers EV charging, rewires, consumer units, EICRs |
| Revenue band | 15 | VAT registration (proxy for the £90k threshold) |
| Service breadth | 10 | Size of the service list the business lists itself |
| Credibility | 5 | NICEIC / NAPIT accreditation |

Bands: **High** ≥ 70, **Medium** 50–69, **Low** < 50.

Confidence is scored separately from fit. A high score at low confidence means
more research is needed. A high score at high confidence means call today.

---

## Data files

| Path | Contents | In git? |
|---|---|---|
| `data/checkatrade_raw.csv` | Current Checkatrade collection | Yes (sample) |
| `data/companies_house.csv` | Companies House cross-reference | Header row only |
| `data/collection_meta.json` | Which towns the current data covers, and when | Yes |
| `data/reference/checkatrade_raw.handcollected.csv` | Original hand-collected sample | Yes |
| `data/cache/` | Raw fetched pages | No |
| `data/archive/` | Earlier collections | No |
| `out/` | Ranked list and scorecard | Yes (sample) |

The column definitions for both CSVs are listed in `BRIEF.md`, section 5. The
two collectors and `score.py` all rely on those exact columns, so change them
in all three places together.

---

## Tests

```bash
python -m pytest tests -q
```

The acceptance tests check what a run leaves behind (row counts, unique
slugs, zero network calls on a warm cache, funnel totals, office
classification, unverified-not-excluded) rather than collecting live data.
Run the collector first: several tests read `data/checkatrade_raw.new.csv`
and fail without it.

---

## Known limitations

- **Checkatrade-only sourcing.** Everyone in the sample pays for membership and
  has passed vetting. Electricians too new or too small to join are missing,
  and they may be a large part of the target market.
- **"Sole trader" doesn't mean working alone.** A sole trader can employ people.
- **Reviews are a proxy for jobs.** How often businesses ask for reviews varies.
- **Summary-only service lists** under-count quoting intensity for some records.
- **Hard-coded date.** `TODAY` in `src/score.py` is fixed at 2026-08-31 so
  results are reproducible. Update it before scoring fresh data.
- **Windows-only browser paths.** The CDP engine looks for Chrome/Edge only
  at Windows install paths (`Session.BROWSER_EXES` in
  `src/collect_checkatrade.py`). On macOS/Linux it falls back to the bundled
  Chromium, which Cloudflare usually blocks. Add your browser's path there.

---

## Out of scope

Any data behind a login, contacting anyone, a database, and a web framework.
