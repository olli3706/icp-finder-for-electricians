"""
Companies House cross-reference collector.

Reads the Checkatrade CSV, attempts to resolve every limited company against
the CH REST API, and writes data/companies_house.new.csv (never straight over
the hand-verified reference). Sole traders and partnerships are skipped: they
are unincorporated, so absence from the register is expected and a row would
only imply a failed match that never happened.

Resolution ladder, from the brief's hand-run findings:
  1. company name search, exact normalized match        -> high / exact_name
  2. the URL slug as a search term (it often encodes    -> high / url_slug...
     the legal entity when the display name does not)
  3. officer search on the owner's name; a single       -> officer_only
     result is taken as the company, cautiously
  4. nothing usable                                     -> not_found

Two evidence-honesty rules applied after matching:
  - member_since predating incorporation by more than 12 months most likely
    means the wrong company was matched: confidence drops to medium and the
    discrepancy is written into match_method so it surfaces in the report.
  - a match whose only owner-operator evidence is a residential registered
    office (no officer count retrieved) gets residential_office_only appended
    to match_method, so the reader can see which passes rest on the soft proxy.
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path

import requests

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.cache import Cache, FetchBlockedError, FetchStats, RateLimiter, ROOT, load_env

API = "https://api.company-information.service.gov.uk"
WEB = "https://find-and-update.company-information.service.gov.uk"

COLUMNS = ["slug", "ch_name", "ch_number", "ch_status", "incorporated",
           "reg_office", "office_type", "officers_active",
           "officers_shared_identity", "match_confidence", "match_method", "ch_url"]

ACCOUNTANT_FIRST_LINE = re.compile(r"\bC/O\b|ACCOUNTAN|\bTAX\b|\bLTD\b|\bLIMITED\b", re.I)
COMMERCIAL = re.compile(
    r"\bUNIT\b|BUSINESS PARK|INDUSTRIAL|ENTERPRISE|TRADING ESTATE|\bSUITE\b"
    r"|\bOFFICE\b|\bDEPOT\b|WAREHOUSE|WORKSHOP|FARM YARD|\bFARM\b", re.I)

TITLES = re.compile(r"^(mr|mrs|ms|miss|dr|mx)\.?\s+", re.I)


# ---------------------------------------------------------------- api client

class ChClient:
    def __init__(self, refresh=False, interval=2.5):
        env = load_env()
        key = env.get("CH_API_KEY", "")
        if not key or key == "REPLACE_WITH_YOUR_KEY":
            sys.exit("CH_API_KEY is not set in .env -- see MANUAL-STEPS.md step 1")
        self.auth = (key, "")
        contact = env.get("CONTACT_EMAIL", "")
        self.headers = {"User-Agent": f"HardwireICPFinder/1.0 (+{contact})"}
        self.cache = Cache()
        self.limiter = RateLimiter(interval)
        self.stats = FetchStats()
        self.refresh = refresh

    def get(self, path, params=None):
        """Cache-first GET returning decoded JSON, or None on 404."""
        url = path if path.startswith("http") else API + path
        if params:
            url += "?" + "&".join(f"{k}={requests.utils.quote(str(v))}"
                                  for k, v in sorted(params.items()))
        if not self.refresh:
            cached = self.cache.get(url)
            if cached is not None:
                self.stats.cache_hits += 1
                return json.loads(cached) if cached != "404" else None
        self.limiter.wait()
        self.stats.network_calls += 1
        response = requests.get(url, auth=self.auth, headers=self.headers, timeout=30)
        if response.status_code in (403, 429):
            raise FetchBlockedError(url, response.status_code)
        if response.status_code == 404:
            self.cache.put(url, "404", status=404, kind="json")
            return None
        response.raise_for_status()
        self.cache.put(url, response.text, kind="json")
        return response.json()


# ---------------------------------------------------------------- matching

def normalize(name):
    name = re.sub(r"[^A-Z0-9]", "", name.upper())
    return re.sub(r"(LIMITED|LTD)$", "", name)


def month_floor(iso):
    return iso[:7] if iso else ""


def months_between(earlier_ym, later_ym):
    ey, em = int(earlier_ym[:4]), int(earlier_ym[5:7])
    ly, lm = int(later_ym[:4]), int(later_ym[5:7])
    return (ly - ey) * 12 + (lm - em)


def classify_office(address):
    first = address.split(",")[0] if address else ""
    if ACCOUNTANT_FIRST_LINE.search(first):
        return "accountant"
    if COMMERCIAL.search(address or ""):
        return "commercial"
    return "residential"


def join_address(addr):
    parts = [addr.get(k) for k in ("premises", "address_line_1", "address_line_2",
                                   "locality", "postal_code")]
    return ", ".join(p for p in parts if p)


def search_company(client, business_name, slug):
    """Return (company_hit, method) or (None, None)."""
    target = normalize(business_name)
    slug_target = normalize(re.sub(r"\d+$", "", slug))
    for query, method in ((business_name, "exact_name"),
                          (re.sub(r"\d+$", "", slug), "url_slug")):
        found = client.get("/search/companies", {"q": query, "items_per_page": 20})
        for item in (found or {}).get("items", []):
            item_norm = normalize(item.get("title", ""))
            if item_norm in (target, slug_target):
                if method == "url_slug" and item_norm != target:
                    method = "url_slug_not_trading_name"
                return item, method
            # previous names count too (Mortimer Electrics was Mortimer Electrical)
            for prev in item.get("previous_company_names", []):
                if normalize(prev.get("name", "")) in (target, slug_target):
                    return item, "previous_name"
    return None, None


def search_officer(client, owner):
    """Single-result officer search, per the brief's one proven case."""
    name = TITLES.sub("", owner).strip()
    if not name:
        return None
    found = client.get("/search/officers", {"q": name, "items_per_page": 5})
    items = (found or {}).get("items", [])
    surname = name.split()[-1].upper()
    matches = [i for i in items if surname in i.get("title", "").upper()]
    if len(matches) == 1:
        return matches[0], name
    return None


def resolve(client, row):
    """One Checkatrade row -> one companies_house.new.csv row."""
    out = dict.fromkeys(COLUMNS, "")
    out["slug"] = row["slug"]

    hit, method = search_company(client, row["name"], row["slug"])
    if hit:
        number = hit["company_number"]
        profile = client.get(f"/company/{number}") or {}
        out.update({
            "ch_name": profile.get("company_name", hit.get("title", "")),
            "ch_number": number,
            "ch_status": profile.get("company_status", ""),
            "incorporated": profile.get("date_of_creation", ""),
            "reg_office": join_address(profile.get("registered_office_address", {})),
            "match_confidence": "high",
            "match_method": method,
            "ch_url": f"{WEB}/company/{number}",
        })
        out["office_type"] = classify_office(out["reg_office"])

        officers = client.get(f"/company/{number}/officers")
        if officers is not None:
            active = [o for o in officers.get("items", [])
                      if not o.get("resigned_on")
                      and "director" in (o.get("officer_role") or "")]
            out["officers_active"] = str(len(active))
            if len(active) == 1:
                out["officers_shared_identity"] = "n_a"
            elif len(active) >= 2:
                surnames = {o.get("name", "").split(",")[0].strip().upper()
                            for o in active}
                addresses = {join_address(o.get("address", {})).upper()
                             for o in active}
                shared = (len(surnames) < len(active)
                          or len(addresses) < len(active))
                out["officers_shared_identity"] = "yes" if shared else "no"
        elif out["office_type"] == "residential":
            # The pass would rest entirely on the address proxy; say so.
            out["match_method"] += "; residential_office_only"

        # A member_since well before incorporation usually means the wrong
        # company: surface it rather than trusting the match quietly.
        member, incorporated = row.get("member_since", ""), month_floor(out["incorporated"])
        if member and incorporated and member < incorporated:
            gap = months_between(member, incorporated)
            if gap > 12:
                out["match_confidence"] = "medium"
                out["match_method"] += (f"; member_since {member} predates "
                                        f"incorporation {incorporated} by {gap}mo")
        return out

    officer_hit = search_officer(client, row.get("owner", ""))
    if officer_hit:
        item, name = officer_hit
        appointments = item.get("appointment_count")
        if appointments == 1:
            out.update({
                "officers_active": "1",
                "match_confidence": "officer_only",
                "match_method": (f"officer_search_"
                                 f"{name.lower().replace(' ', '_')}_single_appointment"),
                "ch_url": f"{WEB}/search/officers?q={name.replace(' ', '+')}",
            })
            link = (item.get("links") or {}).get("self", "")
            if link:
                found = client.get(link + "/appointments") or {}
                items = found.get("items", [])
                if len(items) == 1:
                    appointed = items[0].get("appointed_to", {})
                    number = appointed.get("company_number", "")
                    if number:
                        profile = client.get(f"/company/{number}") or {}
                        out["ch_name"] = profile.get("company_name", "")
                        out["ch_number"] = number
                        out["ch_status"] = profile.get("company_status", "")
                        out["incorporated"] = profile.get("date_of_creation", "")
                        out["reg_office"] = join_address(
                            profile.get("registered_office_address", {}))
                        out["office_type"] = classify_office(out["reg_office"])
                        out["ch_url"] = f"{WEB}/company/{number}"
            return out

    out["match_confidence"] = "not_found"
    out["match_method"] = "no_usable_match"
    return out


# ---------------------------------------------------------------- pipeline

def collect(input_path, refresh=False, interval=2.5, only_slug=None):
    with open(input_path, newline="", encoding="utf-8") as fh:
        businesses = list(csv.DictReader(fh))
    client = ChClient(refresh=refresh, interval=interval)

    rows = []
    limited = [b for b in businesses if b["structure"] == "limited"]
    if only_slug:
        limited = [b for b in businesses if b["slug"] == only_slug]
    print(f"{len(limited)} limited companies to resolve "
          f"(of {len(businesses)} businesses)")
    for index, business in enumerate(limited, 1):
        try:
            row = resolve(client, business)
        except FetchBlockedError:
            raise
        rows.append(row)
        print(f"  [{index}/{len(limited)}] {business['slug']:36} "
              f"{row['match_confidence']:12} {row['match_method'][:60]}")

    stats_path = ROOT / "out" / "collect_ch_stats.json"
    stats_path.parent.mkdir(exist_ok=True)
    stats_path.write_text(json.dumps(client.stats.as_dict(), indent=1),
                          encoding="utf-8")
    print(f"network calls: {client.stats.network_calls}, "
          f"cache hits: {client.stats.cache_hits}")
    return rows


def main():
    parser = argparse.ArgumentParser()
    default_input = ROOT / "data" / "checkatrade_raw.new.csv"
    if not default_input.exists():
        default_input = ROOT / "data" / "checkatrade_raw.csv"
    parser.add_argument("--input", type=Path, default=default_input)
    parser.add_argument("--refresh", action="store_true", help="bypass cache")
    parser.add_argument("--interval", type=float, default=2.5)
    parser.add_argument("--slug", help="resolve one business and print the row")
    args = parser.parse_args()

    try:
        rows = collect(args.input, refresh=args.refresh, interval=args.interval,
                       only_slug=args.slug)
    except FetchBlockedError as err:
        print(f"\nSTOPPED: {err}\nNo retries were attempted, per the fetching rules.",
              file=sys.stderr)
        sys.exit(2)

    if args.slug:
        for key in COLUMNS:
            print(f"{key:26} {rows[0][key] if rows else '(no row)'}")
        return
    target = ROOT / "data" / "companies_house.new.csv"
    with open(target, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {target} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
