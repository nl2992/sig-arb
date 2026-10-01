"""
mappings.py — build and review the SIG <-> Kalshi / Polymarket mapping registry.

Cross-venue scanners only use APPROVED rows of config/market_matches.json. This tool:

    python mappings.py propose          # discovered links -> REVIEW_REQUIRED rows with automated checks
    python mappings.py status           # counts by venue and status
    python mappings.py review           # walk pending rows: [a]pprove / [r]eject / [s]kip / [q]uit
    python mappings.py approve-passing  # approve every pending row whose checks all pass (asks y/N once)

Checks are evidence for the reviewer, not a decision: settlement differences (e.g. Kalshi
resolving on the January swearing-in vs. Polymarket on the November result) are listed as
flags. APPROVED and REJECTED rows are never overwritten by `propose`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys

from arb_engine import TITLE_RE
from crossvenue_adapters import fetch_public, load_market_links
from market_matches import load_registry

ROOT = pathlib.Path(__file__).parent
REGISTRY = ROOT / "config" / "market_matches.json"
LINKS = ROOT / "docs" / "market-links.csv"
PARTY_WORDS = {"Republican": ("republican",), "Democratic": ("democrat",), "Independent": ("independent",)}
PARTY_TICKER = {"Republican": "R", "Democratic": "D", "Independent": "I"}
STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}
CHECKS = ("party", "race", "outcome", "rules_present", "active")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def parse_sig(title: str) -> tuple[str | None, str | None]:
    m = TITLE_RE.match(title or "")
    return (m.group(1), m.group(2)) if m else (None, None)


def race_matches(race: str, text: str) -> bool:
    """Does the reference text describe the same SIG race?"""
    chamber = re.match(r"^U\.S\. (House|Senate)$", race)
    if chamber:                       # chamber control: no single state or district
        return bool(re.search(rf"\b{chamber.group(1).lower()}\b", text)) and not re.search(r"\b[a-z]{2}-\d{2}\b", text)
    house = re.match(r"^([A-Z]{2})-(\d{2}) House race$", race)
    if house:
        st, num = house.group(1), house.group(2)
        if re.search(rf"\b{st.lower()}-{num}\b", text):
            return True
        places = [STATES.get(st, st).lower(), st.lower()]
        return any(re.search(rf"\b{re.escape(p)}\b", text) for p in places) and \
            bool(re.search(rf"\b0?{int(num)}(st|nd|rd|th)?\b", text))
    places, offices = race_terms(race)
    return any(re.search(rf"\b{re.escape(p)}\b", text) for p in places) and \
        all(re.search(rf"\b{re.escape(o)}", text) for o in offices)


def race_terms(race: str) -> tuple[list, list]:
    """(alternatives that must appear, office words) for a statewide SIG race name."""
    office = "governor" if race.endswith("Governor") else "senate" if race.endswith("Senate") else ""
    state = race.rsplit(" ", 1)[0]
    abbr = next((k for k, v in STATES.items() if v == state), state)
    return [state.lower(), abbr.lower()], [office] if office else []


def check_pair(sig_title: str, venue: str, ref: dict | None) -> dict:
    """Automated evidence for one SIG market vs one reference market."""
    party, race = parse_sig(sig_title)
    if ref is None:
        return {"checks": {c: False for c in CHECKS}, "flags": ["reference market not returned by the venue API"]}
    title = f"{ref.get('title') or ''} {ref.get('market_id') or ''}".lower()
    rules = (ref.get("rules_text") or "").lower()
    text = f"{title} {rules}"
    ticker = str(ref.get("market_id") or "")
    checks = {}
    words = PARTY_WORDS.get(party, ())
    others = [w for p, ws in PARTY_WORDS.items() if p != party for w in ws]
    party_in_title = any(w in title for w in words) and not any(w in title for w in others)
    party_in_ticker = venue == "kalshi" and party is not None and ticker.upper().endswith("-" + PARTY_TICKER[party])
    checks["party"] = bool(party_in_title or party_in_ticker)
    checks["race"] = bool(race) and race_matches(race, text)
    checks["outcome"] = True        # registry rows always map SIG YES to reference YES
    checks["rules_present"] = bool(rules.strip())
    checks["active"] = str(ref.get("status") or "").upper() in ("ACTIVE", "OPEN")
    flags = []
    if "sworn in" in rules:
        flags.append("resolves on swearing-in (Jan 2027), not the election result")
    if "run-off" in rules or "runoff" in rules:
        flags.append("rules mention run-offs")
    if "recount" in rules:
        flags.append("rules mention recounts")
    if ref.get("close_ts"):
        flags.append(f"closes {ref['close_ts']}")
    return {"checks": checks, "flags": flags}


def _load_rows(path: pathlib.Path) -> list:
    if not path.exists():
        return []
    payload = json.loads(path.read_text())
    return payload if isinstance(payload, list) else payload.get("matches", [])


def save_rows(rows: list, path: pathlib.Path = REGISTRY) -> None:
    payload = {"registry_version": 1, "updated_at": _now(), "matches": rows}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=1))
    load_registry(tmp)              # never write a registry the loader would reject
    tmp.replace(path)


def propose(links: list, refs: dict, existing: list) -> list:
    """Merge discovered links into registry rows; decided rows are kept as they are."""
    rows = {(r["sig_market_id"], r["reference_venue"], str(r["reference_market_id"])): r for r in existing}
    for link in links:
        for venue in ("kalshi", "polymarket"):
            ref_id = (link.get(f"{venue}_market_id") or "").strip()
            if not ref_id:
                continue
            key = (int(link["sig_market_id"]), venue, ref_id)
            if key in rows and rows[key].get("status") in ("APPROVED", "REJECTED", "REVOKED"):
                continue
            result = check_pair(link.get("event", ""), venue, refs.get(venue, {}).get(ref_id))
            passed = sum(result["checks"].values())
            rows[key] = {
                "sig_market_id": key[0], "sig_title": link.get("event"), "reference_venue": venue,
                "reference_market_id": ref_id, "reference_outcome_id": "YES", "outcome_relation": "SAME",
                "reference_title": (refs.get(venue, {}).get(ref_id) or {}).get("title"),
                "reference_url": link.get(f"{venue}_url"),
                "confidence": round(passed / len(CHECKS), 2), "status": "REVIEW_REQUIRED",
                "checks": result["checks"], "flags": result["flags"], "checked_at": _now(),
            }
    return sorted(rows.values(), key=lambda r: (r["sig_market_id"], r["reference_venue"]))


def approve(row: dict, how: str) -> dict:
    flags = "; ".join(row.get("flags") or []) or "none"
    row.update(status="APPROVED", reviewed_at=_now(),
               evidence=f"{how}: checks {json.dumps(row.get('checks'))}; flags: {flags}")
    return row


def all_pass(row: dict) -> bool:
    return bool(row.get("checks")) and all(row["checks"].get(c) for c in CHECKS)


def fetch_refs(links: list) -> dict:
    ids = {v: sorted({(l.get(f"{v}_market_id") or "").strip() for l in links} - {""})
           for v in ("kalshi", "polymarket")}
    data = fetch_public(["kalshi", "polymarket"], 0, market_ids=ids)
    return {v: {str(m["market_id"]): m for m in data[v].get("markets", [])} for v in data}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("cmd", choices=["propose", "status", "review", "approve-passing"])
    ap.add_argument("--registry", type=pathlib.Path, default=REGISTRY)
    a = ap.parse_args(argv)
    rows = _load_rows(a.registry)

    if a.cmd == "propose":
        links = load_market_links(LINKS)
        print(f"fetching {sum(bool(l.get('kalshi_market_id')) for l in links)} Kalshi and "
              f"{sum(bool(l.get('polymarket_market_id')) for l in links)} Polymarket reference markets...")
        rows = propose(links, fetch_refs(links), rows)
        save_rows(rows, a.registry)
        a.cmd = "status"

    if a.cmd == "status":
        for venue in ("kalshi", "polymarket"):
            vr = [r for r in rows if r["reference_venue"] == venue]
            by = {s: sum(r["status"] == s for r in vr) for s in sorted({r["status"] for r in vr})}
            pending = [r for r in vr if r["status"] == "REVIEW_REQUIRED"]
            print(f"{venue:<11} {len(vr):>4} rows  {by}  pending passing all checks: "
                  f"{sum(all_pass(r) for r in pending)}")
        return 0

    pending = [r for r in rows if r["status"] == "REVIEW_REQUIRED"]
    if a.cmd == "approve-passing":
        ok = [r for r in pending if all_pass(r)]
        flagged = sum(any(not f.startswith("closes ") for f in r.get("flags") or []) for r in ok)
        print(f"{len(ok)} of {len(pending)} pending rows pass every automated check "
              f"({flagged} carry settlement-rule flags, recorded in their evidence).")
        if not ok or input("Approve them all? [y/N] ").strip().lower() != "y":
            print("nothing approved")
            return 0
        for r in ok:
            approve(r, "bulk approval of rows passing all automated checks")
        save_rows(rows, a.registry)
        print(f"approved {len(ok)}")
        return 0

    for i, r in enumerate(pending, 1):
        print(f"\n[{i}/{len(pending)}] SIG #{r['sig_market_id']}: {r.get('sig_title')}")
        print(f"  {r['reference_venue']} {r['reference_market_id']}: {r.get('reference_title')}")
        print(f"  {r.get('reference_url') or ''}")
        print("  checks:", ", ".join(f"{k}={'ok' if v else 'FAIL'}" for k, v in (r.get("checks") or {}).items()))
        for f in r.get("flags") or []:
            print("  flag:", f)
        ans = input("  [a]pprove / [r]eject / [s]kip / [q]uit: ").strip().lower()
        if ans == "q":
            break
        if ans == "a":
            approve(r, "operator review")
        elif ans == "r":
            r.update(status="REJECTED", reviewed_at=_now())
        save_rows(rows, a.registry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
