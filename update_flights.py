#!/usr/bin/env python3
"""
FR24 daily flight ingest for the screwworm sterile-fly dispersal map.

Once a day this pulls yesterday's completed flights for the tracked sterile-fly
aircraft from the Flightradar24 API, filters out ferry legs, and appends the
remaining drop missions to flights-history.json. The map reads that file on
load, so the timeline extends automatically without any hand editing.

Design notes (the "why", so future-you can follow it):

  * Ferry filter is DURATION-BASED, not geofence-based. Flight-summary already
    gives us takeoff and landing timestamps, so we compute duration BEFORE
    spending 40 credits on a track fetch. Anything under 3.5 hours is a ferry
    leg (KEBG->staging or reposition) and is skipped entirely. Real drop
    missions run 4-6 hours. If we ever see a real drop under 3.5h we widen
    this — but based on real N75G data the gap between drop and ferry is
    wide (4h37m vs 1h15m on Jun 30).

  * Dedup key is fr24_id, stored as an optional field on each new record.
    Existing historical records (KML/Excel imports) do not have fr24_id, and
    the FR24 cutover is Jul 2, so historical-vs-FR24 collision is impossible.
    Re-running the script for the same date is safe: already-ingested flights
    are recognized by fr24_id and skipped.

  * MIN_DATE = 2026-07-02 is enforced as a HARD floor. FR24 will not be used
    to backfill anything before the cutover — manual KML/Excel data owns
    everything through Jul 1. Trying to fetch an earlier date is refused
    (not silently ignored) so mistakes are loud.

  * Points are downsampled by keeping every Nth. FR24 sends ~1 point/10s
    (~1600 points for a 4.5h flight); historical data ran ~1 point/20s
    (~600-700 points). Keeping every 2nd matches historical density, keeps
    the JSON size sane, and leaves the drop-band classifier plenty to work
    with (MIN_DROP_POINTS=30 in the map is well below what a real drop run
    produces even at half density).

  * Rate limit is FR24's stated 10 req/min. We sleep 7 seconds between every
    API call, which gives 8.5 req/min — under the ceiling with margin for
    clock skew and retry wiggle room.

  * DRY RUN by default. Pass --commit to actually rewrite flights-history.json.
    The workflow passes --commit for automated runs; the manual first run
    should be left as a dry run so you can inspect what it would add.

  * Bundled summary query: FR24 accepts a comma-separated `registrations`
    filter, so all our tails are fetched in ONE summary call per date. That's
    faster and cheaper (one call vs twelve). We identify which tail each
    returned flight belongs to via the `callsign` field, which matches the
    registration for our fleet. If callsign doesn't match any known tail we
    log and skip.

On any transport-level failure the script exits non-zero without writing
anything, so the workflow can surface it as an issue rather than commit
partial or bad data.
"""

import argparse
import datetime as dt
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from collections import Counter, OrderedDict


# --- Configuration -----------------------------------------------------------

FR24_BASE = "https://fr24api.flightradar24.com/api"
FR24_HEADERS_BASE = {
    "Accept": "application/json",
    "Accept-Version": "v1",
    # Cloudflare in front of FR24 blocks Python's default urllib UA. A
    # descriptive UA is enough to pass the WAF — see the fr24_test.py notes.
    "User-Agent": (
        "screwworm-sterile-fly-map/1.0 "
        "(+https://github.com/wrcage/Screwworm-Sterile-fly-dispersal-flight-tracks)"
    ),
}

# The tracked sterile-fly fleet. N96S is a new tail (added Jul 2026); other
# tails are the existing roster from the manual ingest era.
TAILS = [
    "N37H", "N62V", "N67K", "N72L", "N75G", "N75V",
    "N90D", "N92B", "N96S", "N97D", "TGJAC", "TGJAK",
    "N75Z",    # added 2026-11-12: Dynamic Aviation King Air, Hermosillo ops (found by discover_aircraft.py)
    "N72J", "N7199D",   # added 2026-11-13: maintainer reports both now flying drops
    # XC-ROK was added and removed 2026-11-12: it belongs to the Servicio
    # Geologico Mexicano (aerial geophysical survey grids) and never flew US
    # work, so it isn't part of the dispersal picture this map is for.
]
# Display IDs above are hyphen-free (how the map labels them). FR24's
# `registrations` filter needs the real hyphenated form for non-US aircraft
# (XC-ROK, not XCROK) — the unhyphenated TGJAC/TGJAK likely never matched,
# which may be why they vanished after the switch to FR24.
FR24_REG = {"TGJAC": "TG-JAC", "TGJAK": "TG-JAK"}
MAX_REGS_PER_QUERY = 15   # FR24 limit


def _norm_reg(r):
    return (r or "").strip().upper().replace("-", "")

# Legend colors, copied from what the existing flights-history.json data
# already uses per tail. Grey (#6b7280) is the shared "lower-volume tails"
# color — assigned to N96S until we see how active it turns out to be.
TAIL_COLORS = {
    "N37H":  "#a16207",
    "N62V":  "#6b7280",
    "N67K":  "#6b7280",
    "N72L":  "#c026d3",
    "N75G":  "#ea580c",
    "N75V":  "#6b7280",
    "N90D":  "#6b7280",
    "N92B":  "#7c3aed",
    "N96S":  "#6b7280",  # new tail, grey until further notice
    "N75Z":  "#6b7280",
    "N72J":  "#6b7280",
    "N7199D": "#6b7280",
    "N97D":  "#0d9488",
    "TGJAC": "#84cc16",   # lime; drawn dashed (non-US)
    "TGJAK": "#84cc16",   # lime; drawn dashed (non-US)
}

# Airport coordinates for nearest-airport lookup when FR24 leaves orig/dest
# blank. Only airports the sterile-fly ops actually use — no exhaustive list
# needed. Latitude/longitude are approximate (runway centroid or so); we only
# need to be closer to the right one than to any of the others.
AIRPORTS = {
    "KEBG": (26.4415, -98.1218),   # Edinburg (Texas staging base)
    "KUVA": (29.2116, -99.7436),   # Uvalde
    "KDRT": (29.3742, -100.9271),  # Del Rio
    "KERV": (29.9767, -99.0855),   # Kerrville
    "KHOB": (32.6875, -103.2170),  # Hobbs NM
    "KCVB": (29.3421, -98.8515),   # Castroville
    "KE38": (30.3842, -103.6836),  # Alpine-Casparis (Big Bend staging)
    "KMRF": (30.3711, -104.0175),  # Marfa
    "KFST": (30.9153, -102.9128),  # Fort Stockton-Pecos County
    "KPRS": (29.6344, -104.3617),  # Presidio Lely International
    "1E2":  (29.4502, -103.3985),  # Terlingua Ranch (Big Bend)
    "89TE": (29.2683, -103.6889),  # Lajitas International (Big Bend)
    "KJCT": (30.5112, -99.7628),   # Junction / Kimble County
    "KCZT": (28.5222, -99.8237),   # Carrizo Springs / Dimmit County
    "KLRD": (27.5438, -99.4616),   # Laredo International
    "KMFE": (26.1758, -98.2386),   # McAllen International
    "5T9":  (28.8656, -100.5051),  # Eagle Pass
    "K5T9": (28.8656, -100.5051),  # Eagle Pass (FR24 sometimes K-prefixes it)
    # Added 2026-11-12: West Texas / Arizona / Sonora / Chihuahua expansion
    "KPEQ": (31.3824, -103.5107),  # Pecos Municipal
    "KSJT": (31.3577, -100.4963),  # San Angelo Regional
    "KHDO": (29.3595, -99.1767),   # Hondo Municipal
    "KTUS": (32.1161, -110.9410),  # Tucson International
    "KOLS": (31.4177, -110.8479),  # Nogales International (AZ)
    "MMNG": (31.2261, -110.9759),  # Nogales (Sonora)
    "MMHO": (29.0959, -111.0479),  # Hermosillo (staging base, Sonora)
    "MMCS": (31.6361, -106.4289),  # Ciudad Juarez
    "KELP": (31.8072, -106.3779),  # El Paso
    "KTCL": (33.2206, -87.6114),   # Tuscaloosa AL (maintenance ferries)
    "MMTM": (22.2964, -97.8656),   # Tampico
    "MMNL": (27.4437, -99.5705),   # Nuevo Laredo
    "MMMY": (25.7785, -100.1075),  # Monterrey (Escobedo / NTR)
    "MMAN": (25.8654, -100.2377),  # Monterrey (Del Norte)
    "MMRX": (26.0087, -98.2286),   # Reynosa
    "MMPG": (28.6275, -100.5350),  # Piedras Negras
    "MMMA": (25.7699, -97.5259),   # Matamoros
    "MMMV": (27.8817, -101.5261),  # Melchor Muzquiz
}

# Friendly names for the route string in flight names. KEBG is the ONLY
# origin that gets the ICAO code appended in parens — that's the historical
# convention already in flights-history.json ("Edinburg (KEBG) -> Uvalde"
# vs "Tampico -> Monterrey").
FRIENDLY = {
    "KEBG": "Edinburg",
    "KUVA": "Uvalde",
    "KDRT": "Del Rio",
    "KERV": "Kerrville",
    "KHOB": "Hobbs NM",
    "KCVB": "Castroville",
    "KE38": "Alpine",
    "KMRF": "Marfa",
    "KFST": "Fort Stockton",
    "KPRS": "Presidio",
    "1E2":  "Terlingua Ranch",
    "89TE": "Lajitas",
    "KPEQ": "Pecos",
    "KSJT": "San Angelo",
    "KHDO": "Hondo",
    "KTUS": "Tucson",
    "KOLS": "Nogales AZ",
    "MMNG": "Nogales",
    "MMHO": "Hermosillo",
    "MMCS": "Ciudad Juarez",
    "KELP": "El Paso",
    "KTCL": "Tuscaloosa",
    "KJCT": "Junction",
    "KCZT": "Carrizo Springs",
    "KLRD": "Laredo",
    "KMFE": "McAllen",
    "5T9":  "Eagle Pass",
    "K5T9": "Eagle Pass",
    "MMTM": "Tampico",
    "MMNL": "Nuevo Laredo",
    "MMMY": "Monterrey",       # NTR — the historical data uses just "Monterrey"
    "MMAN": "Monterrey (MMAN)",
    "MMRX": "Reynosa",
    "MMPG": "Piedras Negras",
    "MMMA": "Matamoros",
    "MMMV": "Melchor Muzquiz",
}

DAY_ZERO = dt.date(2026, 5, 30)   # timeline day 0 anchor, fixed forever
MIN_DATE = dt.date(2026, 7,  2)   # FR24 cutover; refuses to fetch earlier

FERRY_HOURS_MAX = 3.5    # under this = ferry, no track fetch
# Second ferry check, applied after the track is fetched (added 2026-11-12).
# A few repositioning legs run longer than 3.5 h (Edinburg->Tuscaloosa,
# Edinburg->Tucson, Monterrey->Hermosillo). Drop missions fly grids, so their
# path is far longer than the straight line between endpoints (ratio >= 1.5 in
# every real drop so far); ferries fly nearly straight (ratio 1.07-1.19).
# A flight that ends >150 mi from where it started with ratio < 1.3 is a ferry.
FERRY_MIN_STRAIGHT_MI = 150
FERRY_MAX_RATIO       = 1.3
# Default lookback: today plus the previous 2 UTC days. GitHub's scheduler
# routinely fires hours late, so "yesterday only" skipped whole days when one
# run fired late and the next early. Re-checking 3 days is cheap (summary only;
# tracks are never re-fetched for flights already stored or already rejected)
# and also picks up the same day's morning drops at the 2 PM run.
LOOKBACK_DAYS = 3
POINT_STEP      = 2      # keep every Nth FR24 track point (~1 pt/20s)
NEAREST_AIRPORT_MAX_DEG = 0.3   # ~33 km at these latitudes
RATE_LIMIT_SLEEP = 7     # seconds between API calls (safely under 10/min)
RETRY_SLEEP      = 15    # seconds before single retry on transient errors
REQUEST_TIMEOUT  = 45    # seconds per HTTP request

HISTORY_PATH_DEFAULT = "flights-history.json"


# --- Small helpers -----------------------------------------------------------

def log(msg):
    """Print with a leading tag so the Actions log is scannable."""
    print("[flight-update] " + msg, flush=True)


def parse_iso(ts):
    """Parse an FR24 ISO timestamp like '2026-06-30T11:28:41Z' into a UTC
    datetime. Raises ValueError on None or empty input so callers that wrap
    this in a try/except(ValueError) handle a missing timestamp cleanly — FR24
    can return a null datetime_landed for a flight that hasn't been finalized
    (e.g. still airborne, or landing not yet processed)."""
    if not ts:
        raise ValueError("empty or missing timestamp")
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return dt.datetime.fromisoformat(ts)


def day_index(date_obj):
    """Days since DAY_ZERO (May 30, 2026). Used as the flight's `day` field."""
    return (date_obj - DAY_ZERO).days


def nearest_airport(lat, lon):
    """Return the ICAO code of the nearest known airport within
    NEAREST_AIRPORT_MAX_DEG degrees, or None if the point isn't close to any."""
    best_code = None
    best_sq   = None
    for code, (alat, alon) in AIRPORTS.items():
        dlat = lat - alat
        dlon = lon - alon
        sq = dlat * dlat + dlon * dlon
        if best_sq is None or sq < best_sq:
            best_sq = sq
            best_code = code
    if best_sq is not None and best_sq < NEAREST_AIRPORT_MAX_DEG ** 2:
        return best_code
    return None


def format_orig(code):
    """Origin display string. KEBG uniquely gets '(KEBG)' appended to match
    the historical naming convention."""
    if code == "KEBG":
        return "Edinburg (KEBG)"
    if code and code in FRIENDLY:
        return FRIENDLY[code]
    if code:
        return "Friendly (" + code + ")"
    return "Unknown"


def format_dest(code):
    """Destination display string. No ICAO in parens for any destination —
    matches historical convention ('...-> Uvalde', not '-> KUVA')."""
    if code and code in FRIENDLY:
        return FRIENDLY[code]
    if code:
        return "Friendly (" + code + ")"
    return "Unknown"


def build_route_string(orig_code, dest_code):
    """Build the ROUTE portion of the flight name.

    Round trip (same origin & destination) uses the historical 'grid' form:
      * 'KEBG grid'      when origin is KEBG
      * 'Tampico grid'   when origin is MMTM
      * '{Friendly} grid' otherwise
    Point-to-point uses 'ORIG -> DEST' with the U+279C arrow character."""
    if orig_code and orig_code == dest_code:
        if orig_code == "KEBG":
            return "KEBG grid"
        if orig_code == "MMTM":
            return "Tampico grid"
        base = FRIENDLY.get(orig_code, orig_code)
        return base + " grid"
    return format_orig(orig_code) + " \u279c " + format_dest(dest_code)


def flight_name(takeoff_utc, orig_code, dest_code):
    """Full flight name in the historical format: 'D MMM (Day) -- ROUTE'."""
    # Historical data has no leading zero on day-of-month ('1 Jul', '31 May').
    # strftime's %-d strips the leading zero on Linux (GH Actions is Ubuntu).
    date_str = takeoff_utc.strftime("%-d %b (%a)")
    return date_str + " \u2014 " + build_route_string(orig_code, dest_code)


# --- HTTP -----------------------------------------------------------------

def fr24_get(endpoint, params=None):
    """
    GET wrapper for the FR24 API. Returns parsed JSON on success. Retries once
    on transient errors (5xx, network timeout, or an HTML body suggesting a
    Cloudflare edge hiccup). On persistent failure, raises RuntimeError with a
    diagnostic message; the caller can catch or let the process exit.

    Rate limiting is handled by sleeping RATE_LIMIT_SLEEP seconds AFTER each
    call regardless of outcome. Sleeping after (rather than before) means the
    very first call in the process happens immediately, which matters for a
    fresh workflow run where nothing has hit the API yet.
    """
    token = (os.environ.get("FR24_API_KEY") or "").strip()
    if not token:
        raise RuntimeError("FR24_API_KEY environment variable is empty.")

    url = FR24_BASE + endpoint
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = dict(FR24_HEADERS_BASE)
    headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers)

    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                code = resp.status
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            code = e.code
        except urllib.error.URLError as e:
            log("network error on " + endpoint + " attempt " + str(attempt) + ": " + str(e))
            time.sleep(RATE_LIMIT_SLEEP)
            if attempt == 1:
                time.sleep(RETRY_SLEEP)
                continue
            raise RuntimeError("network failure calling " + endpoint + ": " + str(e))

        # Success path
        if 200 <= code < 300:
            time.sleep(RATE_LIMIT_SLEEP)
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                raise RuntimeError(
                    endpoint + " returned HTTP " + str(code) + " but body was not JSON: "
                    + body[:200]
                )

        # Failure paths.
        stripped = body.lstrip().lower()
        looks_html = stripped.startswith("<!doctype") or stripped.startswith("<html")
        transient  = looks_html or 500 <= code < 600 or code == 429
        log("HTTP " + str(code) + " from " + endpoint
            + (" (HTML body — WAF/Cloudflare block or maintenance)" if looks_html else ""))
        time.sleep(RATE_LIMIT_SLEEP)
        if attempt == 1 and transient:
            log("transient; retrying after " + str(RETRY_SLEEP) + "s")
            time.sleep(RETRY_SLEEP)
            continue
        # Persistent failure — show a snippet so the log tells us why.
        raise RuntimeError(
            "FR24 " + endpoint + " failed with HTTP " + str(code)
            + "; body starts: " + body[:300]
        )


# FR24 caps /flight-summary/light at 20 results per request on this plan (the
# `limit` param does not raise it — observed 2026-11-12). Any window that comes
# back with exactly 20 may be truncated, so split it in half and re-query.
SUMMARY_CAP = 20
MIN_SPLIT = dt.timedelta(minutes=30)


def summary_window(params, start, end, _depth=0):
    """All /flight-summary/light results for params between start and end
    (UTC datetimes), splitting the window whenever the 20-result cap is hit.
    Results are de-duplicated by fr24_id."""
    p = dict(params)
    p["flight_datetime_from"] = start.strftime("%Y-%m-%dT%H:%M:%S")
    p["flight_datetime_to"] = end.strftime("%Y-%m-%dT%H:%M:%S")
    data = (fr24_get("/flight-summary/light", p) or {}).get("data") or []
    if len(data) < SUMMARY_CAP or (end - start) <= MIN_SPLIT:
        if len(data) >= SUMMARY_CAP:
            log("  warning: 20-result cap hit on a %s window; may be incomplete"
                % (end - start))
        return data
    mid = start + (end - start) / 2
    out, seen = [], set()
    for part in (summary_window(params, start, mid, _depth + 1),
                 summary_window(params, mid, end, _depth + 1)):
        for f in part:
            k = f.get("fr24_id")
            if k not in seen:
                seen.add(k)
                out.append(f)
    return out


def fetch_summary_for_date(date_obj, tails=None):
    """Return flight-summary/light records for our tails on the given UTC date.
    Registrations go in batches of 15 (FR24's limit), each split further if
    the 20-result cap is hit."""
    tails = tails or TAILS
    start = dt.datetime.combine(date_obj, dt.time(0, 0, 0))
    end = dt.datetime.combine(date_obj, dt.time(23, 59, 59))
    regs = [FR24_REG.get(t, t) for t in tails]
    out, seen = [], set()
    for i in range(0, len(regs), MAX_REGS_PER_QUERY):
        batch = regs[i:i + MAX_REGS_PER_QUERY]
        for f in summary_window({"registrations": ",".join(batch)}, start, end):
            if f.get("fr24_id") not in seen:
                seen.add(f.get("fr24_id"))
                out.append(f)
    return out


def fetch_track(fr24_id):
    """Return the list of point dicts for the given flight ID. A 404 ("flight
    could not be found") means FR24 has no track for this one flight: return
    [] so the caller skips it rather than aborting the whole run (that killed
    the 2026-11-12 backfill and discarded everything fetched before it)."""
    try:
        result = fr24_get("/flight-tracks", {"flight_id": fr24_id})
    except RuntimeError as e:
        if "HTTP 404" in str(e):
            log("      FR24 has no track for " + str(fr24_id) + " (404); skipping it")
            return []
        raise
    if isinstance(result, list):
        items = result
    else:
        items = (result or {}).get("data") or [result]
    for it in items:
        if isinstance(it, dict) and it.get("tracks"):
            return it["tracks"]
    return []


# --- Flight-record construction ---------------------------------------------

def downsample(points, step):
    """Take every step-th point, but always keep the final point so the last
    coordinate reflects the true landing location — otherwise a track ending
    at, say, index 1599 with step=2 could be silently truncated to 1598."""
    if step <= 1 or len(points) <= step:
        return list(points)
    out = points[::step]
    if out[-1] is not points[-1]:
        out.append(points[-1])
    return out


def _miles(a_lat, a_lon, b_lat, b_lon):
    import math
    la1, lo1, la2, lo2 = map(math.radians, [a_lat, a_lon, b_lat, b_lon])
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 3958.8 * 2 * math.asin(math.sqrt(min(1.0, h)))


def straightness(coords):
    """(path_miles, straight_miles, ratio) for a [[lat,lon,alt],...] track."""
    path = sum(_miles(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
               for i in range(len(coords) - 1))
    straight = _miles(coords[0][0], coords[0][1], coords[-1][0], coords[-1][1])
    return path, straight, (path / straight if straight > 0 else float("inf"))


def is_long_ferry(coords):
    if len(coords) < 2:
        return False
    _, straight, ratio = straightness(coords)
    return straight > FERRY_MIN_STRAIGHT_MI and ratio < FERRY_MAX_RATIO


def build_flight_record(summary_entry, track_points, tail):
    """Turn a summary entry + its track into a flights-history.json record.
    Returns None if the flight can't be usefully represented (empty track,
    unparseable timestamps)."""
    if not track_points:
        return None

    fr24_id = summary_entry.get("fr24_id")
    try:
        takeoff = parse_iso(summary_entry["datetime_takeoff"])
    except (KeyError, ValueError):
        return None

    kept = downsample(track_points, POINT_STEP)
    coords = []
    for p in kept:
        try:
            lat = float(p["lat"])
            lon = float(p["lon"])
            alt = p.get("alt")
            if alt is None:
                # Match the historical schema: coords with unknown altitude
                # still get three slots so the drop-band classifier can rely
                # on a consistent shape. Use 0 as the sentinel — matches how
                # KML data represents ground-level points.
                alt = 0
            coords.append([lat, lon, int(alt)])
        except (KeyError, ValueError, TypeError):
            continue

    if not coords:
        return None

    # Airports for the name. FR24's summary sometimes leaves orig_icao or
    # dest_icao blank; when it does, fall back to nearest airport by the
    # first/last track point.
    orig_code = summary_entry.get("orig_icao") or nearest_airport(coords[0][0], coords[0][1])
    dest_code = summary_entry.get("dest_icao") or nearest_airport(coords[-1][0], coords[-1][1])

    record = OrderedDict()
    record["ac"]     = tail
    record["name"]   = flight_name(takeoff, orig_code, dest_code)
    record["color"]  = TAIL_COLORS.get(tail, "#6b7280")
    record["day"]    = day_index(takeoff.date())
    record["coords"] = coords
    if fr24_id:
        record["fr24_id"] = fr24_id
    return record


# --- History file I/O -------------------------------------------------------

def load_history(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def recompute_stats(history):
    """Regenerate the `stats` block from the current flight list. Only fields
    that the existing block uses; nothing new is invented."""
    flights = history.get("flights", [])
    tail_counts = Counter(f.get("ac", "?") for f in flights)
    days = [f["day"] for f in flights if "day" in f]
    with_note = sum(1 for f in flights if f.get("dropNote"))
    with_override = sum(1 for f in flights if f.get("dropBandOverride"))
    total_pts = 0
    with_alt  = 0
    for f in flights:
        for c in f.get("coords", []):
            total_pts += 1
            if len(c) >= 3 and c[2] is not None:
                with_alt += 1
    pct = round(100.0 * with_alt / total_pts, 2) if total_pts else 0.0
    return {
        "flightCount":              len(flights),
        "tails":                    dict(sorted(tail_counts.items())),
        "dayMin":                   min(days) if days else 0,
        "dayMax":                   max(days) if days else 0,
        "flightsWithDropNote":      with_note,
        "flightsWithDropOverride":  with_override,
        "pointsWithAltitudePct":    pct,
    }


def write_history(history, path):
    """Write flights-history.json with compact JSON (no spaces) — the file
    grows quickly with FR24 data, so every byte matters for Pages load time."""
    history["generated"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    history["stats"] = recompute_stats(history)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, separators=(",", ":"))


# --- Main -------------------------------------------------------------------

_REJECTED_AT_LOAD = None


def _rejected_changed(history, rejected):
    return _REJECTED_AT_LOAD is not None and len(rejected) != _REJECTED_AT_LOAD

def daterange(start, end):
    """Inclusive UTC-date iterator from start to end."""
    n = (end - start).days
    for i in range(n + 1):
        yield start + dt.timedelta(days=i)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    p.add_argument("--date",
                   help="Single UTC date to fetch, YYYY-MM-DD. "
                        "Default: today plus the previous 2 UTC days.")
    p.add_argument("--date-from",
                   help="Range start (inclusive), YYYY-MM-DD UTC. Requires --date-to.")
    p.add_argument("--date-to",
                   help="Range end (inclusive), YYYY-MM-DD UTC. Requires --date-from.")
    p.add_argument("--commit", action="store_true",
                   default=(os.environ.get("COMMIT") == "1"),
                   help="Actually write flights-history.json. Default is dry run.")
    p.add_argument("--tails", default="",
                   help="Comma-separated subset of TAILS to fetch (default: all).")
    p.add_argument("--history", default=HISTORY_PATH_DEFAULT,
                   help="Path to flights-history.json. Default: " + HISTORY_PATH_DEFAULT)
    args = p.parse_args(argv)

    if bool(args.date_from) != bool(args.date_to):
        p.error("--date-from and --date-to must be used together.")
    if (args.date_from or args.date_to) and args.date:
        p.error("Use either --date, or --date-from/--date-to, not both.")
    return args


def resolve_target_dates(args):
    if args.date_from and args.date_to:
        d0 = dt.date.fromisoformat(args.date_from)
        d1 = dt.date.fromisoformat(args.date_to)
        if d1 < d0:
            raise SystemExit("--date-to is before --date-from")
        return list(daterange(d0, d1))
    if args.date:
        return [dt.date.fromisoformat(args.date)]
    # Default: today plus the previous LOOKBACK_DAYS-1 UTC days (see note at
    # LOOKBACK_DAYS). Never earlier than the cutover.
    today = dt.datetime.now(dt.timezone.utc).date()
    days = [today - dt.timedelta(days=i) for i in range(LOOKBACK_DAYS - 1, -1, -1)]
    return [d for d in days if d >= MIN_DATE]


def main(argv=None):
    args = parse_args(argv)
    dates = resolve_target_dates(args)

    for d in dates:
        if d < MIN_DATE:
            raise SystemExit(
                "Refusing to fetch " + d.isoformat() + ": before FR24 cutover ("
                + MIN_DATE.isoformat() + "). Earlier dates are covered by manual "
                "KML/Excel data already in flights-history.json."
            )

    only_tails = [_norm_reg(t) for t in args.tails.split(",") if t.strip()] or None
    if only_tails:
        bad = [t for t in only_tails if t not in TAILS]
        if bad:
            raise SystemExit("--tails not in TAILS: " + ", ".join(bad))
        log("Only tails: " + ", ".join(only_tails))
    log("Mode: " + ("COMMIT" if args.commit else "DRY RUN"))
    log("Target dates: " + ", ".join(d.isoformat() for d in dates))

    history = load_history(args.history)
    existing_fr24_ids = {
        f.get("fr24_id")
        for f in history.get("flights", [])
        if f.get("fr24_id")
    }
    # Flights already fetched and rejected as long ferries: never pay the
    # 40-credit track fetch for them again on a later lookback run.
    rejected = history.setdefault("rejectedFr24Ids", [])
    global _REJECTED_AT_LOAD
    _REJECTED_AT_LOAD = len(rejected)
    existing_fr24_ids |= set(rejected)
    log("Loaded " + str(len(history.get("flights", []))) + " existing flights ("
        + str(len(existing_fr24_ids)) + " with fr24_id).")

    new_flights = []
    ferry_count = 0
    dup_count   = 0
    unknown_callsign_count = 0

    try:
        for target_date in dates:
            log("=" * 60)
            log("Processing " + target_date.isoformat())
            summaries = fetch_summary_for_date(target_date, only_tails)
            log("  summary returned " + str(len(summaries)) + " flight(s)")

            for s in summaries:
                fid      = s.get("fr24_id")
                callsign = (s.get("callsign") or "").strip().upper()
                takeoff  = s.get("datetime_takeoff")
                landing  = s.get("datetime_landed")
                orig     = s.get("orig_icao") or s.get("orig_iata") or "?"
                dest     = s.get("dest_icao") or s.get("dest_iata") or "?"

                # Identify the tail: FR24's registration (hyphens removed) first,
                # then callsign (the US fleet flies with callsign == registration).
                reg_id = _norm_reg(s.get("reg"))
                if reg_id in TAILS:
                    tail = reg_id
                elif callsign in TAILS:
                    tail = callsign
                else:
                    unknown_callsign_count += 1
                    log("    skip: unknown callsign " + repr(callsign)
                        + " (fr24_id=" + str(fid) + ")")
                    continue

                if fid and fid in existing_fr24_ids:
                    dup_count += 1
                    log("    skip: fr24_id " + fid + " already in history (" + tail + ")")
                    continue

                try:
                    t0 = parse_iso(takeoff)
                    t1 = parse_iso(landing)
                    dur_h = (t1 - t0).total_seconds() / 3600.0
                except (TypeError, ValueError, AttributeError):
                    # Most common cause: datetime_landed is null because the flight
                    # wasn't finalized when we queried (still airborne, or FR24
                    # hasn't closed it out). We can't compute duration, so we can't
                    # tell drop from ferry — skip safely. A later run (or the next
                    # day's run) will pick it up once FR24 finalizes it.
                    log("    skip: missing/invalid timestamps for fr24_id " + str(fid)
                        + " (takeoff=" + repr(takeoff) + " landing=" + repr(landing) + ")")
                    continue

                if dur_h < FERRY_HOURS_MAX:
                    ferry_count += 1
                    log("    ferry: " + tail + " fr24_id=" + str(fid)
                        + " " + orig + "->" + dest
                        + " dur=" + ("%.2f" % dur_h) + "h  (skipped, no track fetch)")
                    continue

                log("    drop:  " + tail + " fr24_id=" + str(fid)
                    + " " + orig + "->" + dest
                    + " dur=" + ("%.2f" % dur_h) + "h  (fetching track...)")
                track = fetch_track(fid)
                if not track:
                    log("      warning: empty track returned; skipping")
                    continue
                record = build_flight_record(s, track, tail)
                if record is None:
                    log("      warning: could not build record; skipping")
                    continue
                if is_long_ferry(record["coords"]):
                    _, st, ra = straightness(record["coords"])
                    ferry_count += 1
                    log("      ferry (straight %.0f mi, path/straight %.2f): %s — rejected"
                        % (st, ra, record["name"]))
                    if fid:
                        rejected.append(fid)
                        existing_fr24_ids.add(fid)
                    continue
                log("      built record: name=" + repr(record["name"])
                    + " day=" + str(record["day"])
                    + " raw_pts=" + str(len(track))
                    + " kept_pts=" + str(len(record["coords"])))
                new_flights.append(record)
                # In-memory dedup so a run that spans multiple dates doesn't re-add
                # the same flight if it appears twice in the summary (edge case).
                if fid:
                    existing_fr24_ids.add(fid)
    except Exception as e:
        # Keep whatever was fetched before the failure so its credits are not
        # wasted; the next run continues from there (dedup skips these).
        log("FATAL mid-run: " + str(e))
        if args.commit and (new_flights or _rejected_changed(history, rejected)):
            history["flights"].extend(new_flights)
            write_history(history, args.history)
            log("Saved " + str(len(new_flights)) + " flight(s) fetched before the failure.")
        raise

    log("=" * 60)
    log("Summary:")
    log("  drop flights to add:       " + str(len(new_flights)))
    log("  ferry legs skipped:        " + str(ferry_count))
    log("  duplicates skipped:        " + str(dup_count))
    log("  unknown-callsign skipped:  " + str(unknown_callsign_count))
    if new_flights:
        by_day = Counter(f["day"] for f in new_flights)
        by_tail = Counter(f["ac"] for f in new_flights)
        log("  by day:  " + ", ".join("d" + str(k) + "=" + str(v)
                                       for k, v in sorted(by_day.items())))
        log("  by tail: " + ", ".join(k + "=" + str(v)
                                       for k, v in sorted(by_tail.items())))

    if not args.commit:
        log("DRY RUN — no file written. Re-run with --commit to persist.")
        return 0

    if not new_flights and not _rejected_changed(history, rejected):
        log("No new flights; leaving flights-history.json untouched.")
        return 0

    history["flights"].extend(new_flights)
    write_history(history, args.history)
    log("Wrote " + args.history + " (+" + str(len(new_flights)) + " flights).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
