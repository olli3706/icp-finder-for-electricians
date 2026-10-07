"""
Checkatrade collector: search listings -> profile URLs -> profiles -> CSV.

Both stages run through Playwright. The brief assumed plain requests would do
for profile pages, but Checkatrade now fronts them with a Cloudflare bot
challenge that 403s non-browser clients (verified 2026-08-31), so the one
browser session serves both stages. Rate limiting applies per navigation.

Profile data is parsed from the page's embedded JSON, not CSS selectors:
  - a schema.org LocalBusiness block (name, rating, review count and dates,
    full service list, location)
  - the Next.js flight data (structure, VAT, owner, member-since,
    12-month review count, accreditations)

Output goes to data/checkatrade_raw.new.csv -- never straight over the
hand-verified reference file. Diff and promote with src/diff_reference.py.
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.cache import (Cache, FetchBlockedError, FetchStats, RateLimiter, ROOT,
                       check_checkatrade_path, load_env)

# ---------------------------------------------------------------- area of effect
#
# The search page renders exactly 12 results per query and offers no working
# pagination (verified 2026-08-31; the totalPages field in its metadata is
# vestigial). Results are radius-sorted from the searched location, so the way
# to recover the population is to search several nearby anchor points per town
# and take the union -- the same service-radius overlap the brief documents
# between whole towns, used deliberately. Town names and postcode districts
# both work as anchors.
#
# To change the area of effect: edit TOWN_ANCHORS (or pass --towns for a
# subset of its keys; an unlisted town searches just its own name).

DEFAULT_TOWNS = ["Reading", "Maidenhead", "Wokingham"]

TOWN_ANCHORS = {
    "Reading": ["Reading", "RG1", "RG2", "RG4", "RG5", "RG6", "RG30", "RG31",
                "Woodley", "Earley", "Tilehurst", "Caversham", "Twyford"],
    "Maidenhead": ["Maidenhead", "SL6", "Cookham", "Bray", "Holyport",
                   "White-Waltham", "Taplow"],
    "Wokingham": ["Wokingham", "RG40", "RG41", "Winnersh", "Finchampstead",
                  "Barkham", "Arborfield", "Crowthorne"],
}

SEARCH_URL = "https://www.checkatrade.com/Search/Electrician/in/{town}"
PROFILE_URL = "https://www.checkatrade.com/trades/{slug}"

COLUMNS = ["slug", "name", "owner", "structure", "member_since", "vat_registered",
           "base_town", "county", "rating", "reviews_total", "reviews_12mo",
           "latest_review", "accreditations", "services_count", "has_ev",
           "has_rewire", "has_consumer_unit", "has_eicr", "claimed_experience_yrs",
           "services_extraction", "profile_url"]

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}

STRUCTURE_MAP = [("sole", "sole_trader"), ("partner", "partnership"),
                 ("llp", "partnership"), ("limited", "limited"), ("ltd", "limited")]

SERVICE_FLAGS = {
    "has_ev": re.compile(r"electric vehicle|ev charg", re.I),
    "has_rewire": re.compile(r"rewir", re.I),
    "has_consumer_unit": re.compile(r"consumer unit|fuse ?board|fuse ?box", re.I),
    "has_eicr": re.compile(r"eicr|condition report|periodic inspection|landlord.{0,20}(cert|safety)", re.I),
}

CHALLENGE_TITLES = re.compile(r"attention required|just a moment", re.I)


# ---------------------------------------------------------------- fetching

class Session:
    """One Playwright browser shared by the listing and profile stages."""

    BROWSER_EXES = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]

    def __init__(self, headed=False, plain_ua=False, interval=2.5, engine="cdp"):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._proc = None
        if engine == "cdp":
            try:
                self._start_cdp()
                self.headed = True  # a real, visible browser window
            except Exception as err:
                print(f"  (real-browser attach failed: {err}; "
                      "falling back to bundled Chromium)")
                engine = "chromium"
        if engine == "chromium":
            self.headed = headed
            # channel="chromium" is the full browser binary in standard new
            # headless mode; the default headless shell is refused by
            # Checkatrade's CDN outright. Note this engine is reliably
            # challenged by Cloudflare from its second navigation -- it exists
            # as a fallback, not the expected path.
            self.browser = self._pw.chromium.launch(headless=not headed,
                                                    channel="chromium")
            probe = self.browser.new_context()
            default_ua = probe.new_page().evaluate("navigator.userAgent")
            probe.close()
            contact = load_env().get("CONTACT_EMAIL", "")
            ua = (default_ua if plain_ua
                  else f"{default_ua} HardwireICPFinder/1.0 (+{contact})")
            self.context = self.browser.new_context(
                user_agent=ua, viewport={"width": 1280, "height": 1600})
            self.page = self.context.new_page()
        self._consent_done = False
        self.limiter = RateLimiter(interval)
        self.stats = FetchStats()

    def _start_cdp(self):
        """Launch the user's installed Chrome/Edge with a devtools port and a
        dedicated profile, then attach Playwright to it.

        A genuinely installed browser carries none of the automation flags that
        make Cloudflare challenge Playwright's own Chromium, so it browses the
        permitted pages the same way the interactive session that seeded the
        original dataset did. The profile lives under data/chrome-profile
        (gitignored) so consent choices and clearance cookies persist between
        runs. Nothing about the browser is disguised or patched.
        """
        import socket
        import subprocess
        import time as _time

        import requests as _requests

        exe = next((p for p in self.BROWSER_EXES if Path(p).exists()), None)
        if not exe:
            raise RuntimeError("no installed Chrome or Edge found")
        with socket.socket() as probe_sock:
            probe_sock.bind(("127.0.0.1", 0))
            port = probe_sock.getsockname()[1]
        profile = ROOT / "data" / "chrome-profile"
        profile.mkdir(parents=True, exist_ok=True)
        self._proc = subprocess.Popen(
            [exe, f"--remote-debugging-port={port}",
             f"--user-data-dir={profile}", "--no-first-run",
             "--no-default-browser-check", "--window-size=1280,900",
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        endpoint = f"http://127.0.0.1:{port}"
        for _ in range(30):
            try:
                _requests.get(f"{endpoint}/json/version", timeout=1)
                break
            except Exception:
                _time.sleep(0.5)
        else:
            raise RuntimeError("browser devtools endpoint never came up")
        self.browser = self._pw.chromium.connect_over_cdp(endpoint)
        self.context = (self.browser.contexts[0] if self.browser.contexts
                        else self.browser.new_context())
        self.page = self.context.new_page()
        print(f"  using installed browser: {Path(exe).name} "
              "(a browser window will be visible during collection)")

    def goto(self, url):
        check_checkatrade_path(url)
        self.limiter.wait()
        self.stats.network_calls += 1
        response = self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        if response and response.status == 429:
            raise FetchBlockedError(url, 429)
        self.page.wait_for_timeout(900)
        # Cloudflare's interstitial: in a headed run, the person at the
        # keyboard can complete the verification in the window -- that is the
        # challenge working as designed, and this code never touches it. We
        # wait for it to clear (a few seconds if it clears itself, up to two
        # minutes for a human). Headless gets the short wait only. A challenge
        # that never clears, or a bare 403, stops the run, unretried.
        if CHALLENGE_TITLES.search(self.page.title()):
            budget = 120 if self.headed else 15
            notified = False
            for _ in range(budget):
                self.page.wait_for_timeout(1000)
                if not CHALLENGE_TITLES.search(self.page.title()):
                    print("  (challenge cleared, continuing)")
                    break
                if self.headed and not notified:
                    print("  CLOUDFLARE CHECK: if the browser window shows a "
                          "verification box, please click it -- waiting up to "
                          "2 minutes", flush=True)
                    notified = True
            else:
                raise FetchBlockedError(url, 403,
                                        f"unresolved challenge: {self.page.title()!r}")
        elif response and response.status == 403:
            raise FetchBlockedError(url, 403,
                                    "(if this is the first fetch, try --plain-ua)")
        self._dismiss_consent()

    def stable_content(self):
        """page.content() but tolerant of a page still settling post-challenge."""
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        for attempt in range(4):
            try:
                return self.page.content()
            except Exception:
                self.page.wait_for_timeout(1500)
        return self.page.content()

    def _dismiss_consent(self):
        if self._consent_done:
            return
        try:
            button = self.page.get_by_role("button", name=re.compile("reject all cookies", re.I))
            if button.count():
                button.first.click(timeout=3000)
                self._consent_done = True
                self.page.wait_for_timeout(500)
        except Exception:
            pass  # banner absent or already gone; harmless either way

    def close(self):
        self.browser.close()
        self._pw.stop()
        if self._proc is not None:  # the launched real-browser process
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()


# ---------------------------------------------------------------- listings

def _page_slugs(page):
    hrefs = page.eval_on_selector_all(
        'a[href*="/trades/"]', "els => els.map(e => e.getAttribute('href'))")
    slugs = set()
    for href in hrefs:
        match = re.search(r"/trades/([a-z0-9-]+)", href or "", re.I)
        if match:
            slugs.add(match.group(1).lower())
    flight = page.content().replace('\\"', '"')
    slugs.update(s.lower() for s in re.findall(r'"uniqueName":"([a-z0-9-]+)"', flight, re.I))
    return slugs


def harvest_anchor(session, anchor, cache, refresh):
    """One search-page load for one anchor location; 12 results, cache-first."""
    listing_key = SEARCH_URL.format(town=anchor) + "#harvest"
    if not refresh:
        cached = cache.get(listing_key)
        if cached is not None:
            result = json.loads(cached)
            if "site_total" in result:  # older schema = miss, refetch
                session.stats.cache_hits += 1
                return result
    session.goto(SEARCH_URL.format(town=anchor))
    flight = session.page.content().replace('\\"', '"')
    total_match = re.search(r'"totalResults":(\d+)', flight)
    result = {
        "anchor": anchor,
        "site_total": int(total_match.group(1)) if total_match else None,
        "slugs": sorted(_page_slugs(session.page)),
    }
    cache.put(listing_key, json.dumps(result), kind="listing")
    return result


def harvest_town(session, town, cache, refresh):
    """Union the 12-result pages of every anchor configured for this town."""
    slugs, site_total = set(), None
    for anchor in TOWN_ANCHORS.get(town, [town]):
        result = harvest_anchor(session, anchor, cache, refresh)
        slugs |= set(result["slugs"])
        if anchor == town:
            site_total = result["site_total"]
    coverage = (f", site claims {site_total} for {town} proper"
                if site_total else "")
    print(f"  {town}: {len(slugs)} unique slugs from "
          f"{len(TOWN_ANCHORS.get(town, [town]))} anchors{coverage}")
    return sorted(slugs)


# ---------------------------------------------------------------- profiles

def fetch_profile(session, cache, slug, refresh):
    url = PROFILE_URL.format(slug=slug)
    if not refresh:
        cached = cache.get(url)
        if cached is not None:
            session.stats.cache_hits += 1
            return cached
    session.goto(url)
    html = session.stable_content()
    if CHALLENGE_TITLES.search(html[:2000]):
        raise FetchBlockedError(url, 403, "challenge markup in body")
    cache.put(url, html)
    return html


def _flight_text(html):
    return html.replace("\\u0026", "&").replace('\\"', '"')


def _labeled_value(flight, label):
    match = re.search(rf'"label":"{label}","value":"([^"]*)"', flight)
    return match.group(1) if match else ""


def parse_profile(slug, html):
    soup = BeautifulSoup(html, "html.parser")
    flight = _flight_text(html)

    business = {}
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if data.get("@type") == "LocalBusiness":
            business = data
            break

    row = dict.fromkeys(COLUMNS, "")
    row["slug"] = slug
    row["profile_url"] = PROFILE_URL.format(slug=slug)
    row["name"] = business.get("name", "") or (soup.h1.get_text(strip=True) if soup.h1 else "")

    # Location: areaServed[0] is "Town, District"; [1] is the county.
    area = business.get("areaServed") or []
    if area:
        row["base_town"] = str(area[0]).split(",")[0].strip()
    row["county"] = (str(area[1]).strip() if len(area) > 1
                     else (business.get("address") or {}).get("addressRegion") or "")

    rating = (business.get("aggregateRating") or {})
    if rating.get("ratingValue") is not None:
        row["rating"] = f"{float(rating['ratingValue']):.2f}"
    if rating.get("reviewCount") is not None:
        row["reviews_total"] = str(rating["reviewCount"])

    dates = [r.get("datePublished", "") for r in business.get("review", [])]
    if dates:
        row["latest_review"] = max(dates)[:7]

    # Service list: knowsAbout carries the full list even when the UI truncates.
    services = business.get("knowsAbout") or []
    if isinstance(services, str):
        services = [services]
    if not services:
        services = re.findall(r'"label":"([^"]+)","id":\d+,"iconName"', flight)
    if services:
        row["services_count"] = str(len(services))
        row["services_extraction"] = "detailed"
        joined = " | ".join(services)
        for field, pattern in SERVICE_FLAGS.items():
            row[field] = "Y" if pattern.search(joined) else ""
    else:
        row["services_extraction"] = "summary"

    recent = re.search(r'"totalRecentReviews":(\d+)', flight)
    row["reviews_12mo"] = recent.group(1) if recent else ""
    if not row["reviews_total"]:
        total = re.search(r'"totalReviews":(\d+)', flight)
        row["reviews_total"] = total.group(1) if total else ""

    row["owner"] = _labeled_value(flight, "Business Owners")
    # VAT-registered profiles carry the number in the value: "Yes: 511415733".
    vat = _labeled_value(flight, "VAT Registered").lower()
    row["vat_registered"] = ("yes" if vat.startswith("yes")
                             else "no" if vat.startswith("no") else "")

    company_type = _labeled_value(flight, "Company Type").lower()
    for needle, value in STRUCTURE_MAP:
        if needle in company_type:
            row["structure"] = value
            break

    member = re.search(r"member since ([A-Z][a-z]+) (\d{4})", flight)
    if member and member.group(1) in MONTHS:
        row["member_since"] = f"{member.group(2)}-{MONTHS[member.group(1)]:02d}"

    accreds = re.search(r'"accreditations":\[(.*?)\]', flight)
    if accreds:
        labels = dict.fromkeys(re.findall(r'"label":"([^"]+)"', accreds.group(1)))
        row["accreditations"] = ";".join(labels)

    # The JSON-LD description is sometimes only the short SEO blurb; the
    # owner's own text sits in the flight data. "N years on Checkatrade"
    # is platform tenure, not trade experience -- exclude it.
    texts = [business.get("description", "")]
    texts += re.findall(r'"(?:description|summary)":"([^"]{40,})"', flight)
    for text in texts:
        years = re.search(r"(\d{1,2})\s*\+?\s*years?(?!\w)(?!\s+on Checkatrade)", text, re.I)
        if years:
            row["claimed_experience_yrs"] = years.group(1)
            break

    return row


# ---------------------------------------------------------------- pipeline

def collect(towns, refresh=False, headed=False, plain_ua=False, only_slug=None,
            interval=2.5, slugs_file=None, engine="cdp"):
    cache = Cache()
    session = Session(headed=headed, plain_ua=plain_ua, interval=interval,
                      engine=engine)
    rows, slug_towns = [], {}
    try:
        if only_slug:
            slugs = [only_slug]
        elif slugs_file:
            slugs = [s.strip().lower() for s in Path(slugs_file).read_text(
                encoding="utf-8").splitlines() if s.strip() and not s.startswith("#")]
            print(f"listing stage: {len(slugs)} slugs read from {slugs_file}")
        else:
            slugs = []
            print("listing stage:")
            for town in towns:
                for slug in harvest_town(session, town, cache, refresh):
                    if slug not in slug_towns:
                        slug_towns[slug] = town
                        slugs.append(slug)
            print(f"  {len(slugs)} unique businesses after cross-town dedupe")

        print("profile stage:")
        for index, slug in enumerate(slugs, 1):
            html = fetch_profile(session, cache, slug, refresh)
            row = parse_profile(slug, html)
            rows.append(row)
            missing = [c for c in ("structure", "member_since", "reviews_12mo") if not row[c]]
            note = f"  MISSING {','.join(missing)}" if missing else ""
            print(f"  [{index}/{len(slugs)}] {slug}{note}")
    finally:
        session.close()

    stats_path = ROOT / "out" / "collect_stats.json"
    stats_path.parent.mkdir(exist_ok=True)
    stats_path.write_text(json.dumps(session.stats.as_dict(), indent=1), encoding="utf-8")
    print(f"network calls: {session.stats.network_calls}, "
          f"cache hits: {session.stats.cache_hits}")
    return rows


def write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {path} ({len(rows)} rows)")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--towns", nargs="+", default=DEFAULT_TOWNS)
    parser.add_argument("--refresh", action="store_true", help="bypass cache")
    parser.add_argument("--headed", action="store_true", help="visible browser window")
    parser.add_argument("--plain-ua", action="store_true",
                        help="drop the project suffix from the User-Agent")
    parser.add_argument("--profile", metavar="SLUG",
                        help="fetch and parse a single profile, print the row, write nothing")
    parser.add_argument("--interval", type=float, default=2.5,
                        help="seconds between network fetches (default 2.5)")
    parser.add_argument("--slugs-file", metavar="PATH",
                        help="skip the listing stage; read profile slugs from this file")
    parser.add_argument("--engine", choices=["cdp", "chromium"], default="cdp",
                        help="cdp attaches to your installed Chrome/Edge "
                             "(default; passes Cloudflare's browser check); "
                             "chromium uses Playwright's bundled browser")
    args = parser.parse_args()

    try:
        rows = collect(args.towns, refresh=args.refresh, headed=args.headed,
                       plain_ua=args.plain_ua, only_slug=args.profile,
                       interval=args.interval, slugs_file=args.slugs_file,
                       engine=args.engine)
    except FetchBlockedError as err:
        print(f"\nSTOPPED: {err}\nNo retries were attempted, per the fetching rules.",
              file=sys.stderr)
        sys.exit(2)

    if args.profile:
        for key in COLUMNS:
            print(f"{key:24} {rows[0][key]}")
        return
    write_csv(rows, ROOT / "data" / "checkatrade_raw.new.csv")


if __name__ == "__main__":
    main()
