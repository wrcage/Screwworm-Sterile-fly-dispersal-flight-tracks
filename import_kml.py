#!/usr/bin/env python3
"""
Import ADS-B Exchange KML track downloads (e.g. for N72J, whose owner blocks
it on Flightradar24) into flights-history.json.

Each KML may hold several <gx:Track> legs (a day's drop flight plus ferries).
Legs are joined into flights at ground stops, then the same rules as the FR24
updater apply: < 3.5 h = ferry (skipped), long straight legs = ferry (skipped).
KML altitudes are metres (EGM96, i.e. ~MSL, geometric) -> feet.
Points are thinned to about one per 18 s to match FR24 density.
Re-running is safe: each imported flight gets a `kml_id` (tail + takeoff
time) and existing ids are skipped.

Usage: python import_kml.py TAIL file1.kml [file2.kml ...] [--commit]
"""
import datetime as dt
import re
import sys
from collections import OrderedDict

import update_flights as uf

M2FT = 3.28084
MIN_GAP_S = 18           # thin to ~1 point per 18 s
SPLIT_GROUND_S = 20 * 60 # a ground stop this long splits flights
AIRBORNE_FT = 1500       # both sides of a gap above this = coverage hole, not a landing
GAP_S = 15 * 60          # receiver holes this long get a map break (ADSBx thins straight legs to a point every 1-2 min, so short gaps are normal)


def parse_kml(path):
    txt = open(path, encoding="utf-8", errors="replace").read()
    legs = []
    for chunk in txt.split("<gx:Track")[1:]:
        whens = re.findall(r"<when\s*>(.*?)</when\s*>", chunk)
        coords = re.findall(r"<gx:coord\s*>(.*?)</gx:coord\s*>", chunk)
        pts = []
        for w, c in zip(whens, coords):
            try:
                lon, lat, alt = (c.split() + ["0"])[:3]
                t = dt.datetime.strptime(w[:19], "%Y-%m-%dT%H:%M:%S")
                pts.append((t, float(lat), float(lon), round(float(alt) * M2FT)))
            except Exception:
                continue
        if pts:
            legs.append(pts)
    return legs


def split_flights(legs):
    """Merge all points, then cut wherever the plane sat on the ground (low
    and slow) or the data had a long gap."""
    pts = sorted(p for leg in legs for p in leg)
    flights, cur = [], []
    for p in pts:
        # A long gap only ends a flight if the plane was on the ground on
        # either side. Airborne on both sides = a coverage hole (common over
        # Big Bend): same flight, drawn with a break (see `gaps`).
        if cur and (p[0] - cur[-1][0]).total_seconds() > SPLIT_GROUND_S \
                and (cur[-1][3] < AIRBORNE_FT or p[3] < AIRBORNE_FT):
            flights.append(cur); cur = []
        cur.append(p)
    if cur:
        flights.append(cur)
    out = []
    for f in flights:
        # trim ground taxi at both ends (alt 0 / below 200 ft AGL-ish)
        air = [i for i, p in enumerate(f) if p[3] > 0]
        if len(air) < 10:
            continue
        out.append(f[max(0, air[0] - 1): air[-1] + 2])
    return out


def thin(f):
    kept = [f[0]]
    for p in f[1:-1]:
        if (p[0] - kept[-1][0]).total_seconds() >= MIN_GAP_S:
            kept.append(p)
    kept.append(f[-1])
    return kept


def main(argv):
    commit = "--commit" in argv
    args = [a for a in argv if a != "--commit"]
    tail, files = args[0].upper(), args[1:]
    hist = uf.load_history("flights-history.json")
    have = {f.get("kml_id") for f in hist["flights"] if f.get("kml_id")}
    added = []
    for path in files:
        for f in split_flights(parse_kml(path)):
            t0, t1 = f[0][0], f[-1][0]
            hrs = (t1 - t0).total_seconds() / 3600
            th = thin(f)
            coords = [[round(p[1], 5), round(p[2], 5), p[3]] for p in th]
            gaps = [i for i in range(1, len(th)) if (th[i][0] - th[i-1][0]).total_seconds() > GAP_S]
            kid = "%s-%s" % (tail, t0.strftime("%Y-%m-%dT%H:%MZ"))
            tag = "%s %s %.2fh %d pts %d gaps" % (path.split("/")[-1][:22], t0.strftime("%m-%d %H:%M"), hrs, len(coords), len(gaps))
            if hrs < uf.FERRY_HOURS_MAX:
                print("  ferry (short)  " + tag); continue
            if uf.is_long_ferry(coords):
                print("  ferry (straight) " + tag); continue
            if kid in have:
                print("  already have   " + tag); continue
            orig = uf.nearest_airport(coords[0][0], coords[0][1])
            dest = uf.nearest_airport(coords[-1][0], coords[-1][1])
            rec = OrderedDict()
            rec["ac"] = tail
            rec["name"] = uf.flight_name(t0, orig, dest)
            rec["color"] = uf.TAIL_COLORS.get(tail, "#6b7280")
            rec["day"] = uf.day_index(t0.date())
            rec["coords"] = coords
            if gaps:
                rec["gaps"] = gaps   # coverage holes: no line drawn between gaps[k]-1 and gaps[k]
            rec["kml_id"] = kid
            rec["source"] = "adsbx-kml"
            have.add(kid); added.append(rec)
            print("  DROP           " + tag + "  " + rec["name"])
    print("new flights: %d" % len(added))
    if commit and added:
        hist["flights"].extend(added)
        hist["flights"].sort(key=lambda r: r.get("day", 0))
        uf.write_history(hist, "flights-history.json")
        print("wrote flights-history.json")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
