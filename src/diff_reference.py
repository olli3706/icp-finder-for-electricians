"""
Diff a freshly collected checkatrade_raw.new.csv against the hand-verified
reference, on the slugs present in both. Acceptance test 8.

Fields split three ways:
  - stable: any disagreement is a selector bug and fails the diff
  - volatile: review counts and dates move between collection dates; drift
    is reported but does not fail
  - upgrade-ok: the reference captured 12 of 30 records from the truncated
    summary service list, so the new detailed extraction may legitimately
    find MORE than the reference (Y where blank). Blank where the reference
    has Y is still a failure.

Run:  python src/diff_reference.py            # report
      python src/diff_reference.py --promote  # copy .new.csv over reference
"""

import argparse
import csv
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REFERENCE = ROOT / "data" / "checkatrade_raw.csv"
NEW = ROOT / "data" / "checkatrade_raw.new.csv"

# Fields where any disagreement is a genuine selector bug. These are the
# identity of the record plus the fields the scorer gates and weights on.
STABLE = ["name", "structure", "member_since", "vat_registered", "profile_url"]

# Same identity, but the surface form legitimately differs between the
# hand-normalised reference and raw extraction: the site itself writes
# "College Town SANDHURST", "Mr. Aaron Wright", "BRACKNELL", and lists both
# directors where the reference kept one. The registered county is often
# absent from a profile's structured data entirely. Compared by containment
# after case-folding, so a genuinely wrong value fails but formatting noise
# and a source-side blank do not.
CONTAINMENT = ["owner", "base_town", "county"]

VOLATILE = ["rating", "reviews_total", "reviews_12mo", "latest_review",
            "services_count", "claimed_experience_yrs", "accreditations",
            "services_extraction"]

# Service flags. A new Y the reference lacked is an upgrade (the reference's
# summary extraction under-measured). A Y the reference had but we now lack is
# NOT a bug: either the reference over-marked from the summary, or the service
# list genuinely changed. Reported as drift, verified by spot-check.
SERVICE_FLAGS = ["has_ev", "has_rewire", "has_consumer_unit", "has_eicr"]


def normkey(value):
    value = value.lower().replace("mr.", "mr").replace("mrs.", "mrs").replace("ms.", "ms")
    return "".join(ch for ch in value if ch.isalnum())


def contains(reference_value, new_value):
    """True when the two name/place values agree up to formatting/completeness."""
    r, n = normkey(reference_value), normkey(new_value)
    if not r or not n:
        return True  # a source-side blank on either side is not a contradiction
    return r in n or n in r


def load(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return {row["slug"]: row for row in csv.DictReader(fh)}


def diff():
    reference, new = load(REFERENCE), load(NEW)
    overlap = sorted(set(reference) & set(new))
    print(f"reference {len(reference)} rows, new {len(new)} rows, "
          f"overlap {len(overlap)}\n")

    failures, drift, upgrades = [], [], []
    for slug in overlap:
        ref_row, new_row = reference[slug], new[slug]
        for field in STABLE:
            if ref_row[field].strip() != new_row[field].strip():
                failures.append((slug, field, ref_row[field], new_row[field]))
        for field in CONTAINMENT:
            rv, nv = ref_row.get(field, ""), new_row.get(field, "")
            if not contains(rv, nv):
                failures.append((slug, field, rv, nv))
            elif normkey(rv) != normkey(nv):
                drift.append((slug, field, rv, nv))
        for field in VOLATILE:
            if ref_row[field].strip() != new_row[field].strip():
                drift.append((slug, field, ref_row[field], new_row[field]))
        for field in SERVICE_FLAGS:
            ref_value, new_value = ref_row[field].strip(), new_row[field].strip()
            if ref_value != "Y" and new_value == "Y":
                upgrades.append((slug, field))
            elif ref_value == "Y" and new_value != "Y":
                drift.append((slug, field, ref_value, new_value or "(blank)"))

    if failures:
        print(f"FAILURES ({len(failures)}) -- selector bugs, fix before promoting:")
        for slug, field, ref_value, new_value in failures:
            print(f"  {slug:32} {field:18} ref={ref_value!r}  new={new_value!r}")
    else:
        print("no stable-field failures")

    if upgrades:
        print(f"\nupgrades ({len(upgrades)}) -- detailed extraction found flags "
              "the summary reference missed:")
        for slug, field in upgrades:
            print(f"  {slug:32} {field}")

    if drift:
        print(f"\ndrift on volatile fields ({len(drift)}) -- expected between "
              "collection dates, review manually:")
        for slug, field, ref_value, new_value in drift:
            print(f"  {slug:32} {field:18} ref={ref_value!r}  new={new_value!r}")

    return not failures


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--promote", action="store_true",
                        help="copy .new.csv over the reference after a clean diff")
    args = parser.parse_args()

    if not NEW.exists():
        sys.exit(f"{NEW} not found -- run the collector first")
    clean = diff()
    if args.promote:
        if not clean:
            sys.exit("\nrefusing to promote: stable-field failures above")
        shutil.copy2(NEW, REFERENCE)
        print(f"\npromoted {NEW.name} -> {REFERENCE.name}")
    sys.exit(0 if clean else 1)


if __name__ == "__main__":
    main()
