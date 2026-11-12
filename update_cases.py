#!/usr/bin/env python3
"""
Daily USDA confirmed-case updater for the screwworm map.

Source: USDA APHIS "Confirmed cases" page, which embeds a public Tableau
dashboard. Its "ExportToCSV" worksheet is the same table as the dashboard's
Download button: one row per confirmed animal case or fly-trap detection with
state, county, case type, species, status and confirmed date.

How: read the dashboard URL off the APHIS page (it changes when USDA
republishes), open it in headless Chromium, capture Tableau's bootstrap
response and decode the worksheet (tableau_decode.py).

Output: cases.json — one record per case with date, day index, state, county,
type, species, status and a map position. USDA gives only the county, so each
case is placed INSIDE its county on a sunflower spiral around the county
center (deterministic: ordered by date, then a hash of USDA's animal ID, so
existing dots never move when new cases are added). Positions are approximate
by design and the map says so. The animal ID itself is never written out.

Safety: refuses to overwrite cases.json if the new table is empty or more
than 20% smaller than the stored one (dashboard broken/filtered), and exits
non-zero so the workflow opens an issue.

Usage: python update_cases.py [--commit]
"""
import argparse
import asyncio
import datetime as dt
import hashlib
import json
import math
import os
import re
import sys
import unicodedata
import urllib.request

import tableau_decode

APHIS_PAGE = ("https://www.aphis.usda.gov/animals/animal-health/livestock-and-poultry-disease/"
              "stop-screwworm/current-status/confirmed")
FALLBACK_VIEW = ("https://publicdashboards.dl.usda.gov/t/MRP_PUB/views/"
                 "NewWorldScrewwormPublicReporting_17805168329840/SummaryDashboard")
SHEET = "ExportToCSV"
MAP_SHEET = "New State Map No Dependencies"
OUT = "cases.json"
COUNTY_FILES = {"Texas": "counties-tx.geojson", "New Mexico": "counties-nm-az.geojson",
                "Arizona": "counties-nm-az.geojson"}
STATE_ABBR = {"Texas": None, "New Mexico": "NM", "Arizona": "AZ"}
DAY_ZERO = dt.date(2026, 5, 30)
SPIRAL_STEP_DEG = 0.05      # ~5.5 km between neighbouring dots in a county
UA = {"User-Agent": "Mozilla/5.0 (screwworm-map case updater; "
                    "+https://github.com/wrcage/Screwworm-Sterile-fly-dispersal-flight-tracks)"}


def log(m):
    print("[cases] " + m, flush=True)


# --- fetch ------------------------------------------------------------------

def dashboard_url():
    try:
        req = urllib.request.Request(APHIS_PAGE, headers=UA)
        html = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")
        m = re.search(r'src="(https://publicdashboards\.dl\.usda\.gov/[^"]+/views/[^"?]+)', html)
        if m:
            return m.group(1)
        log("WARNING: no dashboard iframe found on APHIS page; using last known URL")
    except Exception as e:
        log("WARNING: APHIS page unreachable (%s); using last known URL" % e)
    return FALLBACK_VIEW


async def fetch_bootstrap(view_url):
    from playwright.async_api import async_playwright
    url = view_url + "?:embed=y&:isGuestRedirectFromVizportal=y&:showVizHome=no"
    got = {}
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": 1600, "height": 1200})

        async def on_resp(r):
            if "bootstrapSession" in r.url and "text" not in got:
                try:
                    got["text"] = await r.text()
                except Exception as e:
                    got["err"] = str(e)
        pg.on("response", on_resp)
        await pg.goto(url, wait_until="load", timeout=120000)
        for _ in range(60):              # Tableau never goes network-idle
            if "text" in got:
                break
            await pg.wait_for_timeout(1000)
        await b.close()
    if "text" not in got:
        raise RuntimeError("no Tableau bootstrap captured (%s)" % got.get("err", "timeout"))
    return got["text"]


# --- geometry -----------------------------------------------------------------

def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", s.lower().replace("county", ""))


def load_counties():
    out = {}
    for state, f in COUNTY_FILES.items():
        for ft in json.load(open(f, encoding="utf-8"))["features"]:
            p = ft["properties"]
            if STATE_ABBR[state] and p.get("state") != STATE_ABBR[state]:
                continue
            out[(state, norm(p["name"]))] = ft["geometry"]
    return out


def polys(geom):
    return [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]


def inside(lon, lat, geom):
    for poly in polys(geom):
        ring_hits = 0
        for ri, ring in enumerate(poly):
            c = False
            j = len(ring) - 1
            for i in range(len(ring)):
                xi, yi = ring[i][0], ring[i][1]
                xj, yj = ring[j][0], ring[j][1]
                if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-15) + xi:
                    c = not c
                j = i
            if ri == 0 and not c:
                break
            if ri > 0 and c:            # inside a hole
                ring_hits = -1
                break
            ring_hits = 1
        if ring_hits == 1:
            return True
    return False


def center(geom):
    """Area-weighted centroid of the largest outer ring; if that falls outside
    (odd shapes), the grid point deepest inside the county."""
    best = max(polys(geom), key=lambda p: len(p[0]))[0]
    a = cx = cy = 0.0
    for i in range(len(best) - 1):
        x0, y0 = best[i][0], best[i][1]
        x1, y1 = best[i + 1][0], best[i + 1][1]
        f = x0 * y1 - x1 * y0
        a += f; cx += (x0 + x1) * f; cy += (y0 + y1) * f
    if a:
        cx, cy = cx / (3 * a), cy / (3 * a)
        if inside(cx, cy, geom):
            return cx, cy
    xs = [p[0] for p in best]; ys = [p[1] for p in best]
    pts = [(min(xs) + (max(xs) - min(xs)) * i / 20, min(ys) + (max(ys) - min(ys)) * j / 20)
           for i in range(1, 20) for j in range(1, 20)]
    pts = [q for q in pts if inside(q[0], q[1], geom)]
    return pts[len(pts) // 2] if pts else (sum(xs) / len(xs), sum(ys) / len(ys))


def spiral_point(c, k):
    """k-th point of a sunflower (Vogel) spiral around center c=(lon,lat)."""
    r = SPIRAL_STEP_DEG * math.sqrt(k)
    t = k * math.radians(137.508)
    return (c[0] + r * math.cos(t) / math.cos(math.radians(c[1])), c[1] + r * math.sin(t))


# --- build ----------------------------------------------------------------------

def parse_date(s):
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s or "")
    return dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else None


def build(tables):
    t = tables.get(SHEET)
    if not t:
        raise RuntimeError("worksheet %r not found; have %s" % (SHEET, list(tables)))
    n = len(t["County"])
    rows = []
    for i in range(n):
        d = parse_date(t["Confirmed Date"][i])
        if not d:
            continue
        rows.append({
            "date": d.isoformat(), "day": (d - DAY_ZERO).days,
            "state": t["State"][i], "county": t["County"][i],
            "type": t["Case Type"][i], "species": t["Species"][i],
            "status": t["Status"][i],
            "_key": hashlib.sha1((t.get("Animal ID", [""] * n)[i] or str(i)).encode()).hexdigest(),
        })
    # fallback county centers from USDA's own map sheet
    fallback = {}
    m = tables.get(MAP_SHEET) or {}
    for i in range(len(m.get("County", []))):
        try:
            fallback[(m["State"][i], norm(m["County"][i]))] = (
                float(m["Longitude (generated)"][i]), float(m["Latitude (generated)"][i]))
        except Exception:
            pass
    counties = load_counties()
    rows.sort(key=lambda r: (r["date"], r["_key"]))
    next_k = {}
    unmatched = set()
    for r in rows:
        ck = (r["state"], norm(r["county"]))
        geom = counties.get(ck)
        if geom is None:
            unmatched.add("%s, %s" % (r["county"], r["state"]))
            c = fallback.get(ck)
            if c is None:
                r["lon"] = r["lat"] = None
                continue
            k = next_k.get(ck, 0)
            p = spiral_point(c, k)
            next_k[ck] = k + 1
        else:
            if ck not in next_k:
                next_k[ck] = 0
            c = counties.setdefault(("_c",) + ck, center(geom))
            k = next_k[ck]
            for _ in range(500):
                p = spiral_point(c, k)
                k += 1
                if inside(p[0], p[1], geom):
                    break
            next_k[ck] = k
        r["lon"], r["lat"] = round(p[0], 4), round(p[1], 4)
    for r in rows:
        r.pop("_key", None)
    if unmatched:
        log("WARNING: counties not in county files (USDA centroid used): " + "; ".join(sorted(unmatched)))
    return [r for r in rows if r["lon"] is not None]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", default=os.environ.get("COMMIT") == "1")
    ap.add_argument("--bootstrap-file", help="decode a saved bootstrap instead of fetching")
    args = ap.parse_args()

    if args.bootstrap_file:
        text = open(args.bootstrap_file, encoding="utf-8").read()
    else:
        view = dashboard_url()
        log("dashboard: " + view)
        text = asyncio.run(fetch_bootstrap(view))
    cases = build(tableau_decode.decode(text))
    kinds = {}
    for c in cases:
        kinds[c["type"]] = kinds.get(c["type"], 0) + 1
    log("cases: %d %s; dates %s .. %s" % (len(cases), kinds,
        min(c["date"] for c in cases) if cases else "-", max(c["date"] for c in cases) if cases else "-"))

    prev = 0
    if os.path.exists(OUT):
        prev = len(json.load(open(OUT)).get("cases", []))
    if not cases or (prev and len(cases) < 0.8 * prev):
        log("REFUSING to write: %d cases now vs %d stored (dashboard broken or filtered?)" % (len(cases), prev))
        return 2
    if not args.commit:
        log("DRY RUN — not writing %s" % OUT)
        return 0
    doc = {"source": "USDA APHIS confirmed cases dashboard (ExportToCSV worksheet)",
           "page": APHIS_PAGE,
           "note": "Positions are approximate: placed inside the reported county, not the premises.",
           "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "cases": cases}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"), ensure_ascii=False)
    log("wrote %s (%d cases, was %d)" % (OUT, len(cases), prev))
    return 0


if __name__ == "__main__":
    sys.exit(main())
