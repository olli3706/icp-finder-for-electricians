"""
Orchestrator: collect -> score -> report.

    python run.py                    # asks which areas to run for, then goes
    python run.py --towns Reading Maidenhead Wokingham   # no prompt
    python run.py --refresh          # bypass every cache
    python run.py --no-collect       # score + report over existing CSVs only

Run without --towns and it prompts:
    What areas would you like to run for? [Reading, Maidenhead, Wokingham]
    > fulham, reading and cheltenham
Answers accept commas, "and", "&" or "+" as separators; postcode districts
like RG1 work too. Enter on an empty line keeps the default.

Collection is cache-first. A second run makes zero network calls unless
--refresh is passed.

Two live-site realities are handled rather than papered over:

  * Checkatrade now fronts both its listing and profile pages with a
    Cloudflare challenge that blocks automated/headless clients (see
    src/collect_checkatrade.py). When the automated collector is blocked we
    do NOT fabricate or retry; we report it and continue to score + report
    over the committed data/checkatrade_raw.csv, which was produced through an
    assisted real-browser session running the same verified parser.
  * Companies House needs an API key in .env. Without it, the CH step is
    skipped and every limited company scores as 'needs_verification' rather
    than being dropped -- exactly the three-state behaviour score.py expects.
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.cache import FetchBlockedError, load_env
from src import collect_checkatrade, collect_companies_house, diff_reference, score, report

DEFAULT_TOWNS = collect_checkatrade.DEFAULT_TOWNS


def step(msg):
    print(f"\n=== {msg} ===")


def parse_area_answer(answer):
    """Free text -> town list: 'fulham, reading and cheltenham' -> three towns.

    Postcode districts (RG1, SL6) are kept upper-case; everything else is
    title-cased to match Checkatrade's /Search/Electrician/in/{Town} URLs.
    """
    import re
    answer = answer.replace("﻿", "")  # BOM from piped Windows input
    answer = re.sub(r"[\\/\"'.!?]", " ", answer)  # stray punctuation
    tokens = re.split(r",|&|\band\b|\+", answer, flags=re.I)
    towns = []
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        if re.fullmatch(r"[A-Za-z]{1,2}\d{1,2}", token):
            towns.append(token.upper())
        else:
            towns.append(token.title())
    return towns


def prompt_towns():
    """Ask which areas to run for. Enter on an empty line keeps the default."""
    default = ", ".join(DEFAULT_TOWNS)
    try:
        answer = input(f"What areas would you like to run for? [{default}]\n> ").strip()
    except EOFError:  # piped/non-interactive stdin: keep the default silently
        return DEFAULT_TOWNS
    towns = parse_area_answer(answer)
    if not towns:
        return DEFAULT_TOWNS
    print(f"running for: {', '.join(towns)}")
    return towns


META = ROOT / "data" / "collection_meta.json"


def read_meta():
    import json
    if META.exists():
        return json.loads(META.read_text(encoding="utf-8"))
    return {}


def write_meta(towns, rows):
    import json
    from datetime import date
    META.write_text(json.dumps(
        {"towns": towns, "collected": date.today().isoformat(), "rows": rows},
        indent=1), encoding="utf-8")


def archive_current():
    """Keep the previous collection before a new area replaces it."""
    import shutil
    from datetime import datetime
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = ROOT / "data" / "archive" / stamp
    copied = False
    for name in ("checkatrade_raw.csv", "companies_house.csv",
                 "collection_meta.json"):
        source = ROOT / "data" / name
        if source.exists():
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target / name)
            copied = True
    if copied:
        print(f"previous collection archived to data/archive/{stamp}/")


def collect_checkatrade_step(towns, refresh):
    step(f"Checkatrade: {', '.join(towns)}")
    try:
        rows = collect_checkatrade.collect(towns, refresh=refresh)
    except FetchBlockedError as err:
        print(f"blocked: {err}")
        meta = read_meta()
        covered = ", ".join(meta.get("towns", [])) or "an earlier collection"
        print("Checkatrade blocked the collection and no retry was attempted.\n"
              f"!! The report will reflect the existing data ({covered}), "
              f"NOT the areas you asked for ({', '.join(towns)}).")
        return False
    collect_checkatrade.write_csv(rows, ROOT / "data" / "checkatrade_raw.new.csv")
    if diff_reference.diff():
        import shutil
        archive_current()
        shutil.copy2(ROOT / "data" / "checkatrade_raw.new.csv",
                     ROOT / "data" / "checkatrade_raw.csv")
        # The old CH cross-reference belongs to the old area's businesses.
        # Reset to headers-only so an unresolved limited company scores as
        # needs_verification instead of borrowing a stale match.
        import csv
        with open(ROOT / "data" / "companies_house.csv", "w", newline="",
                  encoding="utf-8") as fh:
            csv.writer(fh).writerow(collect_companies_house.COLUMNS)
        write_meta(towns, len(rows))
        print("diff clean -> promoted new Checkatrade CSV")
    else:
        print("diff shows stable-field failures -> keeping existing CSV, review needed")
    return True


def collect_ch_step(refresh):
    step("Companies House")
    env = load_env()
    if env.get("CH_API_KEY", "REPLACE_WITH_YOUR_KEY") == "REPLACE_WITH_YOUR_KEY":
        print("CH_API_KEY not set in .env -- skipping CH resolution.\n"
              "Limited companies will score as 'needs_verification' (not dropped).\n"
              "Add the key (see MANUAL-STEPS.md) and re-run to resolve them.")
        return False
    try:
        rows = collect_companies_house.collect(
            ROOT / "data" / "checkatrade_raw.csv", refresh=refresh)
    except FetchBlockedError as err:
        print(f"blocked: {err}\nStopping CH collection, not retrying.")
        return False
    import csv
    target = ROOT / "data" / "companies_house.new.csv"
    with open(target, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=collect_companies_house.COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    import shutil
    shutil.copy2(target, ROOT / "data" / "companies_house.csv")
    print(f"wrote and promoted CH CSV ({len(rows)} rows)")
    return True


def score_and_report():
    step("Score")
    data = score.build()
    score.write_outputs(data)
    f = data["funnel"]
    print(f"collected {f['collected']} | qualified {f['qualified']} "
          f"(High {f['high']}/Med {f['medium']}/Low {f['low']}) | "
          f"needs_verification {f['needs_verification']} | "
          f"excluded {f['failed_owner_operator'] + f['failed_active']}")

    step("Report")
    html = report.build_html(data)
    target = ROOT / "out" / "scorecard.html"
    target.write_text(html, encoding="utf-8")
    print(f"wrote {target} ({target.stat().st_size:,} bytes)")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--towns", nargs="+", default=None,
                        help="areas to run for; omit to be asked interactively")
    parser.add_argument("--refresh", action="store_true",
                        help="bypass all caches and re-fetch")
    parser.add_argument("--no-collect", action="store_true",
                        help="skip collection, score + report over existing CSVs")
    args = parser.parse_args()

    if not args.no_collect:
        towns = args.towns if args.towns else prompt_towns()
        collect_checkatrade_step(towns, args.refresh)
        collect_ch_step(args.refresh)
    score_and_report()
    print("\ndone.")


if __name__ == "__main__":
    main()
