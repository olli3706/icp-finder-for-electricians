"""
Hardwire ICP scorer.

Reads Checkatrade profile data and Companies House cross-reference data,
applies ICP gates, scores survivors, and writes a ranked prospect list.

Every judgment carries the evidence and the source it came from.
"""

import csv
import json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TODAY = date(2026, 8, 31)

# ---------------------------------------------------------------- loading


def load(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def month_to_date(value):
    """'2024-03' -> date(2024, 3, 1). Empty -> None."""
    if not value:
        return None
    year, month = value.split("-")[:2]
    return date(int(year), int(month), 1)


def iso_to_date(value):
    if not value:
        return None
    parts = value.split("-")
    return date(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 1)


def years_between(earlier, later=TODAY):
    return round((later - earlier).days / 365.25, 1)


def as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- gates

def gate_owner_operator(row, ch):
    """
    Hardwire sells to owner-operators: sole traders up to ~10 people.

    A literal 'sole trader' filter is wrong, because a large share of UK
    owner-operators incorporate for tax and liability reasons. Worse, the
    commonest UK micro-business shape is the electrician plus a spouse
    listed as second director doing the books -- which a naive 'one officer'
    rule rejects. Four of the five two-director companies we checked were
    exactly that. So we admit a limited company when it looks owner-operated:
    one officer, two officers sharing a surname or address, or a registered
    office that is a residential address.

    Returns 'pass', 'fail' or 'unverified'. 'unverified' matters: absence of
    a Companies House match means we did not check, not that the business
    failed. Conflating the two silently drops good prospects.
    """
    structure = row["structure"]
    if structure in ("sole_trader", "partnership"):
        return "pass", f"Checkatrade lists structure as {structure.replace('_', ' ')}"

    if not ch or ch.get("match_confidence") in ("not_found", "not_checked", ""):
        return "unverified", "Limited company; no Companies House match resolved"

    officers = as_int(ch.get("officers_active"), 0)
    shared = ch.get("officers_shared_identity") == "yes"
    office = ch.get("office_type", "")

    if officers == 1:
        label = ch["ch_name"] or "via officer search"
        return "pass", f"Companies House: single director ({label})"
    if officers == 2 and shared:
        return "pass", "Companies House: two directors sharing surname/address (owner + partner)"
    if office == "residential":
        return "pass", f"Companies House: registered at a residential address ({ch['reg_office']})"
    if office == "commercial":
        return "fail", f"Companies House: registered at commercial premises ({ch['reg_office']})"
    if office == "accountant":
        # Registered office is the accountant's, which says nothing about size.
        return "unverified", f"Registered at an accountant's address ({ch['reg_office']})"
    return "unverified", "Limited company; insufficient evidence either way"


def gate_active(row):
    recent = as_int(row["reviews_12mo"])
    if recent >= 1:
        return True, f"{recent} reviewed jobs in the last 12 months"
    return False, f"No reviewed jobs in 12 months (last review {row['latest_review']})"


# ---------------------------------------------------------------- scoring

def score_velocity(row):
    """30 pts. Current job throughput -- the strongest observed discriminator."""
    n = as_int(row["reviews_12mo"])
    for threshold, points in ((30, 30), (18, 25), (10, 20), (5, 13), (1, 6)):
        if n >= threshold:
            return points, f"{n} reviewed jobs in 12 months"
    return 0, "no recent reviewed jobs"


def true_age(row, ch):
    """
    Business age from the earliest credible evidence.

    Checkatrade 'member since' is when they joined a platform, not when they
    started trading -- it understated true age in every company we checked.
    Companies House incorporation wins where available.
    """
    member = month_to_date(row["member_since"])
    inc = iso_to_date(ch["incorporated"]) if ch and ch.get("incorporated") else None
    if inc and member:
        return min(inc, member), ("Companies House incorporation" if inc < member
                                  else "Checkatrade member since")
    if inc:
        return inc, "Companies House incorporation"
    return member, "Checkatrade member since (no CH record; may understate age)"


def score_recency(row, ch):
    """20 pts. Newer businesses have no entrenched process to displace."""
    start, basis = true_age(row, ch)
    if start is None:
        return 0, "no trading-start evidence"
    age = years_between(start)
    for threshold, points in ((9, 2), (6, 5), (4, 10), (2, 15), (1, 18)):
        if age >= threshold:
            return points, f"{age} yrs trading ({basis})"
    return 20, f"{age} yrs trading ({basis})"


QUOTE_FIELDS = ("has_ev", "has_rewire", "has_consumer_unit", "has_eicr")
QUOTE_LABELS = {
    "has_ev": "EV charger",
    "has_rewire": "rewires",
    "has_consumer_unit": "consumer units",
    "has_eicr": "EICRs",
}


def score_quoting(row):
    """
    20 pts. Hardwire's wedge is estimating, so it only bites on businesses
    that quote often. Rewires, consumer units, EICRs and EV chargers are
    materials-heavy domestic jobs that need a priced quote every time.
    """
    present = [QUOTE_LABELS[f] for f in QUOTE_FIELDS if row[f] == "Y"]
    points = {0: 0, 1: 5, 2: 10, 3: 15, 4: 20}[len(present)]
    confidence = "high" if row["services_extraction"] == "detailed" else "low"
    detail = ", ".join(present) if present else "none confirmed"
    return points, f"quotable work: {detail}", confidence


def score_vat(row):
    """15 pts. Registering for VAT is compulsory above GBP 90k turnover, so
    registration is a free read on whether the business has enough coming in
    to invest in itself."""
    if row["vat_registered"] == "yes":
        return 15, "VAT registered: turnover above GBP 90k threshold"
    return 5, "not VAT registered: turnover likely below GBP 90k"


def score_breadth(row):
    """10 pts. Mild preference for a broader service list; deliberately low weight."""
    n = as_int(row["services_count"])
    for threshold, points in ((25, 10), (15, 7), (6, 4)):
        if n >= threshold:
            return points, f"{n} services listed"
    return 2, f"{n} services listed"


def score_credibility(row):
    """5 pts. Competent-person scheme registration and qualifications."""
    accreditations = [a.strip() for a in row["accreditations"].split(";") if a.strip()]
    n = len(accreditations)
    points = 5 if n >= 2 else (3 if n == 1 else 0)
    return points, f"{n} accreditation(s): {row['accreditations'] or 'none listed'}"


# ---------------------------------------------------------------- pipeline

def build():
    profiles = load(ROOT / "data" / "checkatrade_raw.csv")
    ch_rows = {r["slug"]: r for r in load(ROOT / "data" / "companies_house.csv")}

    results = []
    for row in profiles:
        ch = ch_rows.get(row["slug"])

        owner_state, owner_why = gate_owner_operator(row, ch)
        active_ok, active_why = gate_active(row)
        gates = {
            "owner_operator": {"state": owner_state, "evidence": owner_why},
            "actively_trading": {"state": "pass" if active_ok else "fail",
                                 "evidence": active_why},
        }

        if not active_ok or owner_state == "fail":
            status = "excluded"
        elif owner_state == "unverified":
            status = "needs_verification"
        else:
            status = "qualified"

        record = {
            "slug": row["slug"],
            "name": row["name"],
            "owner": row["owner"],
            "structure": row["structure"],
            "base_town": row["base_town"],
            "county": row["county"],
            "status": status,
            "gates": gates,
            "checkatrade_url": row["profile_url"],
            "companies_house_url": (ch or {}).get("ch_url", ""),
            "ch_match_method": (ch or {}).get("match_method", ""),
            "reviews_12mo": as_int(row["reviews_12mo"]),
            "reviews_total": as_int(row["reviews_total"]),
            "vat": row["vat_registered"],
        }

        if status == "excluded":
            record["score"] = None
            record["band"] = "excluded"
            results.append(record)
            continue

        v_pts, v_why = score_velocity(row)
        r_pts, r_why = score_recency(row, ch)
        q_pts, q_why, q_conf = score_quoting(row)
        vat_pts, vat_why = score_vat(row)
        b_pts, b_why = score_breadth(row)
        c_pts, c_why = score_credibility(row)

        signals = [
            {"signal": "Job throughput", "weight": 30, "points": v_pts,
             "evidence": v_why, "source": "Checkatrade"},
            {"signal": "Business recency", "weight": 20, "points": r_pts,
             "evidence": r_why, "source": "Companies House / Checkatrade"},
            {"signal": "Quoting intensity", "weight": 20, "points": q_pts,
             "evidence": q_why, "source": "Checkatrade", "confidence": q_conf},
            {"signal": "Revenue band", "weight": 15, "points": vat_pts,
             "evidence": vat_why, "source": "Checkatrade"},
            {"signal": "Service breadth", "weight": 10, "points": b_pts,
             "evidence": b_why, "source": "Checkatrade"},
            {"signal": "Credibility", "weight": 5, "points": c_pts,
             "evidence": c_why, "source": "Checkatrade"},
        ]
        total = sum(s["points"] for s in signals)

        # Confidence is separate from score: how sure are we of the record,
        # versus how good a fit it is. A high score at low confidence is a
        # research task; a high score at high confidence is a call to make.
        ch_resolved = bool(ch and ch.get("match_confidence") == "high")
        needs_ch = row["structure"] == "limited"
        if q_conf == "high" and (ch_resolved or not needs_ch):
            confidence = "high"
        elif q_conf == "high" or ch_resolved:
            confidence = "medium"
        else:
            confidence = "low"

        record.update({
            "score": total,
            "band": "High" if total >= 70 else ("Medium" if total >= 50 else "Low"),
            "signals": signals,
            "confidence": confidence,
        })
        results.append(record)

    qualified = sorted([r for r in results if r["status"] == "qualified"],
                       key=lambda r: r["score"], reverse=True)
    unverified = sorted([r for r in results if r["status"] == "needs_verification"],
                        key=lambda r: r["score"], reverse=True)
    excluded = [r for r in results if r["status"] == "excluded"]

    funnel = {
        "collected": len(results),
        "failed_owner_operator": sum(
            1 for r in excluded if r["gates"]["owner_operator"]["state"] == "fail"),
        "failed_active": sum(
            1 for r in excluded if r["gates"]["actively_trading"]["state"] == "fail"),
        "needs_verification": len(unverified),
        "qualified": len(qualified),
        "high": sum(1 for r in qualified if r["band"] == "High"),
        "medium": sum(1 for r in qualified if r["band"] == "Medium"),
        "low": sum(1 for r in qualified if r["band"] == "Low"),
    }
    return {"funnel": funnel, "ranked": qualified,
            "needs_verification": unverified, "excluded": excluded}


def write_outputs(data):
    out = ROOT / "out"
    out.mkdir(exist_ok=True)

    with open(out / "ranked.json", "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)

    columns = ["rank", "name", "owner", "band", "score", "confidence", "structure",
               "base_town", "county", "reviews_12mo", "vat", "throughput_pts",
               "recency_pts", "quoting_pts", "revenue_pts", "breadth_pts",
               "credibility_pts", "checkatrade_url", "companies_house_url"]
    columns.insert(1, "status")
    with open(out / "ranked.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        rows = [("qualified", r) for r in data["ranked"]]
        rows += [("needs_verification", r) for r in data["needs_verification"]]
        for i, (status, r) in enumerate(rows, 1):
            pts = {s["signal"]: s["points"] for s in r["signals"]}
            writer.writerow([
                i, status, r["name"], r["owner"], r["band"], r["score"], r["confidence"],
                r["structure"], r["base_town"], r["county"], r["reviews_12mo"], r["vat"],
                pts["Job throughput"], pts["Business recency"], pts["Quoting intensity"],
                pts["Revenue band"], pts["Service breadth"], pts["Credibility"],
                r["checkatrade_url"], r["companies_house_url"],
            ])
    return out


if __name__ == "__main__":
    data = build()
    path = write_outputs(data)
    f = data["funnel"]
    print(f"collected                      {f['collected']}")
    print(f"  excluded: not owner-operator  {f['failed_owner_operator']}")
    print(f"  excluded: not trading         {f['failed_active']}")
    print(f"  needs verification            {f['needs_verification']}")
    print(f"qualified                      {f['qualified']}  "
          f"(High {f['high']} / Medium {f['medium']} / Low {f['low']})")
    print("\nQUALIFIED")
    for i, r in enumerate(data["ranked"], 1):
        print(f"{i:2}. {r['score']:3}  {r['band']:6} conf={r['confidence']:6} "
              f"{r['name'][:36]:36} {r['base_town']}")
    print("\nNEEDS VERIFICATION (scored, Companies House match unresolved)")
    for r in data["needs_verification"]:
        print(f"    {r['score']:3}  {r['band']:6} conf={r['confidence']:6} "
              f"{r['name'][:36]:36} {r['base_town']}")
    print("\nEXCLUDED")
    for r in data["excluded"]:
        reason = (r["gates"]["owner_operator"]["evidence"]
                  if r["gates"]["owner_operator"]["state"] == "fail"
                  else r["gates"]["actively_trading"]["evidence"])
        print(f"    {r['name'][:36]:36} {reason[:70]}")
    print(f"\nwritten to {path}")
