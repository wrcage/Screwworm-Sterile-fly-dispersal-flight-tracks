#!/usr/bin/env python3
"""
Find aircraft that look like sterile-fly dispersal planes but are NOT in the
tracked tail list (update_flights.TAILS). Read-only: writes nothing to the map
data. Prints a report to the Actions log and the run's Summary page.

How it searches (all via FR24 /flight-summary/light, 4 credits per result):
  1. Our known tails over the window -> learn the aircraft type code(s) FR24
     uses for this fleet (expected King Air 90 family, e.g. BE9L).
  2. Every flight of those type codes touching the staging/working airports.
  3. Every flight of ANY type at the small fields the fleet works from
     (Edinburg, Uvalde, Alpine), in case a different airframe joined.
Then groups by registration and flags unknown ones, ranked by how many long
(>= 3.5 h, i.e. drop-length) flights they flew.

Safety: checks /usage (free) first and refuses to run if this billing period
has already used more than CREDIT_STOP credits; every query has a hard limit.

Usage: python discover_aircraft.py [--days 10]
"""

import argparse
import datetime as dt
import os
import sys
from collections import defaultdict

import update_flights as uf

# FR24's airport filter rejects codes containing digits (KE38 Alpine, 5T9
# Eagle Pass): HTTP 400 "airports.N field format is invalid". Letters only.
STAGING_AIRPORTS = ["KEBG", "KUVA", "KDRT", "KPEQ", "KSJT", "KFST", "KMRF",
                    "KLRD", "KCVB", "MMTM", "MMAN", "MMHO", "MMCS", "MMNL",
                    "MMPG"]                      # FR24 allows max 15
SMALL_FIELDS = ["KEBG", "KUVA", "KMRF"]           # GA fields: any type is OK
FALLBACK_TYPES = ["BE9L"]
LIMIT_TYPE_QUERY = 300
LIMIT_ANY_QUERY = 250
CREDIT_STOP = 24000      # refuse to run past this many credits this period
LONG_H = uf.FERRY_HOURS_MAX


def usage_total():
    try:
        u = uf.fr24_get("/usage") or {}
        return sum(int(float(r.get("credits") or 0)) for r in u.get("data", []))
    except Exception as e:
        print("could not read /usage: " + str(e))
        return None


def summary(params, days, limit):
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    p = dict(params)
    p["flight_datetime_from"] = (now - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    p["flight_datetime_to"] = now.strftime("%Y-%m-%dT%H:%M:%S")
    p["limit"] = limit
    res = uf.fr24_get("/flight-summary/light", p) or {}
    return res.get("data") or []


def hours(f):
    try:
        return (uf.parse_iso(f.get("datetime_landed")) -
                uf.parse_iso(f.get("datetime_takeoff"))).total_seconds() / 3600
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10)
    args = ap.parse_args()
    known = set(uf.TAILS)

    before = usage_total()
    print("credits used this period before search: " + str(before))
    if before is not None and before > CREDIT_STOP:
        print("STOP: over the %d-credit safety line; not searching." % CREDIT_STOP)
        return 0

    # 1) learn type codes from our own fleet
    own = summary({"registrations": ",".join(uf.TAILS)}, args.days, 60)
    types = sorted({(f.get("type") or "").strip() for f in own if f.get("type")})
    print("fleet type codes seen: " + (", ".join(types) or "(none)"))
    if not types:
        types = FALLBACK_TYPES

    # 2) same type at staging airports, 3) any type at small GA fields
    by_type = summary({"aircraft": ",".join(types[:15]),
                       "airports": ",".join(STAGING_AIRPORTS)},
                      args.days, LIMIT_TYPE_QUERY)
    any_type = summary({"airports": ",".join(SMALL_FIELDS)},
                       args.days, LIMIT_ANY_QUERY)
    print("results: fleet=%d same-type=%d any-type=%d"
          % (len(own), len(by_type), len(any_type)))
    if len(by_type) >= LIMIT_TYPE_QUERY or len(any_type) >= LIMIT_ANY_QUERY:
        print("NOTE: a query hit its limit; results may be incomplete.")

    agg = defaultdict(lambda: {"n": 0, "long": 0, "types": set(), "hex": set(),
                               "callsigns": set(), "airports": defaultdict(int),
                               "first": None, "last": None, "maxh": 0.0})
    seen = set()
    for f in own + by_type + any_type:
        fid = f.get("fr24_id")
        if fid in seen:
            continue
        seen.add(fid)
        reg = (f.get("reg") or "").strip().upper().replace("-", "")
        if not reg:
            continue
        a = agg[reg]
        a["n"] += 1
        h = hours(f)
        if h is not None:
            a["maxh"] = max(a["maxh"], h)
            if h >= LONG_H:
                a["long"] += 1
        for k, s in (("type", "types"), ("hex", "hex"), ("callsign", "callsigns")):
            if f.get(k):
                a[s].add(str(f[k]))
        for k in ("orig_icao", "dest_icao"):
            if f.get(k):
                a["airports"][f[k]] += 1
        d = (f.get("datetime_takeoff") or f.get("first_seen") or "")[:10]
        if d:
            a["first"] = min(a["first"] or d, d)
            a["last"] = max(a["last"] or d, d)

    def line(reg, a):
        aps = ", ".join(k for k, _ in sorted(a["airports"].items(), key=lambda x: -x[1])[:5])
        return ("| %s | %s | %d | %d | %.1f | %s–%s | %s | %s | %s |"
                % (reg, "/".join(sorted(a["types"])) or "?", a["n"], a["long"], a["maxh"],
                   a["first"] or "?", a["last"] or "?", "/".join(sorted(a["hex"])) or "?",
                   "/".join(sorted(a["callsigns"])) or "?", aps))

    hdr = ("| reg | type | flights | >=3.5h | max h | dates | hex | callsign | airports |\n"
           "|---|---|---|---|---|---|---|---|---|")
    new = sorted(((r, a) for r, a in agg.items() if r not in known),
                 key=lambda x: (-x[1]["long"], -x[1]["n"]))
    cand = [(r, a) for r, a in new if a["long"] > 0]
    other = [(r, a) for r, a in new if a["long"] == 0]
    mine = sorted((r, a) for r, a in agg.items() if r in known)
    silent = sorted(known - set(agg))

    out = ["## Aircraft discovery — last %d days" % args.days, "",
           "Fleet type codes searched: " + ", ".join(types), "",
           "### NOT tracked, flew drop-length (>= %.1f h) flights — candidates" % LONG_H,
           hdr] + [line(r, a) for r, a in cand] + (["| (none) |"] if not cand else []) + [
           "", "### NOT tracked, short flights only (probably unrelated)", hdr
           ] + [line(r, a) for r, a in other[:40]] + (["| (none) |"] if not other else []) + [
           "", "### Tracked tails seen", hdr] + [line(r, a) for r, a in mine] + [
           "", "Tracked tails with NO flights in window: " + (", ".join(silent) or "none")]
    after = usage_total()
    if before is not None and after is not None:
        out += ["", "Credits used by this search: %d (period total now %d)"
                % (after - before, after)]
    text = "\n".join(out)
    print("\n" + text)
    sp = os.environ.get("GITHUB_STEP_SUMMARY")
    if sp:
        with open(sp, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
