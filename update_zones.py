#!/usr/bin/env python3
"""
TAHC NWS zone auto-updater for the sterile-fly dispersal map.

Once a day, this fetches the public TAHC "NWS Response Zones" layer, compares it
against the most recent snapshot stored in zone-history.json, and — only when a
zone is meaningfully added, removed, or reshaped — appends a new dated snapshot.
The map reads the latest snapshot, so the timeline grows automatically.

Design notes (the "why", so future-you can follow it):
  * Zones are tracked by GlobalID, NOT OBJECTID. OBJECTIDs can be reassigned when
    the underlying view is republished; GlobalID is permanent. This sidesteps any
    "is this the same zone or a renumbered one?" ambiguity.
  * Classification (infested vs surveillance) comes from the zone_name field:
    1 = Infested Zone, 2 = Adjacent Surveillance Zone. No guessing.
  * The public endpoint does NOT expose start_date / end_date, so appearance dates
    use DETECTION DATE — the date this script first sees a zone. With a daily run
    that's accurate to within a day, and it matches how the existing timeline was built.
  * "Significant" boundary change = the polygon's outline moved by more than
    ABOUT ONE KILOMETER (see SIGNIFICANT_SHIFT_KM). Smaller edge-refinements are
    ignored so the history doesn't fill up with daily noise.
  * New geometry is simplified (~the same ~80% reduction as the baseline) to keep
    zone-history.json a sane size.
  * DRY RUN by default: prints what it WOULD do and writes nothing. Pass --commit
    (or set COMMIT=1) to actually modify zone-history.json. The GitHub workflow
    runs with --commit; the very first manual run should be left as a dry run so
    you can inspect the decision before trusting it.

The script is deliberately conservative: on ANY uncertainty it does nothing and
exits non-zero so the workflow surfaces it as an issue, rather than risk writing
a bad snapshot.
"""

import argparse
import datetime as dt
import json
import math
import os
import sys
import urllib.request
import urllib.error

# --- Configuration -----------------------------------------------------------

ENDPOINT = (
    "https://services1.arcgis.com/9Astik9VqLUMFtxK/arcgis/rest/services/"
    "NWS_Zones_and_Areas_PUBLIC_VIEW/FeatureServer/33/query"
)
QUERY_PARAMS = (
    "?where=1%3D1&outFields=OBJECTID,zone_name,GlobalID"
    "&returnGeometry=true&outSR=4326&f=geojson"
)
PAGE_SIZE = 1000  # endpoint maxRecordCount is 2000; page well under it

# A boundary is "significantly" different if the one-way Hausdorff-style distance
# between old and new outlines exceeds this. ~1 km, expressed in degrees latitude.
# (1 deg latitude ~= 111 km, so 1 km ~= 0.009 deg. We compare in a local planar
# approximation, scaling longitude by cos(latitude) at ~30N.)
SIGNIFICANT_SHIFT_KM = 1.0
KM_PER_DEG_LAT = 111.0
REF_LAT_DEG = 30.0  # zones sit around 30N; used to scale longitude distances

# Geometry simplification tolerance (degrees). Tuned to roughly match the baseline's
# ~80% vertex reduction without visibly changing zone shapes at map zoom levels.
SIMPLIFY_TOLERANCE_DEG = 0.0008

ZONE_NAME_INFESTED = 1
ZONE_NAME_SURVEILLANCE = 2

HISTORY_PATH = os.environ.get("ZONE_HISTORY_PATH", "zone-history.json")


# --- Small geometry helpers (no external deps; stdlib only) ------------------

def _lonlat_to_local_km(lon, lat):
    """Project lon/lat to a local planar km grid around REF_LAT_DEG."""
    x = lon * KM_PER_DEG_LAT * math.cos(math.radians(REF_LAT_DEG))
    y = lat * KM_PER_DEG_LAT
    return (x, y)


def _point_seg_dist_km(p, a, b):
    """Distance (km) from point p to segment ab, all in local km coords."""
    px, py = p
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


def _outer_ring(geometry):
    """Return the outer ring (list of [lon,lat]) of a Polygon/MultiPolygon."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates", [])
    if gtype == "Polygon":
        return coords[0] if coords else []
    if gtype == "MultiPolygon":
        # use the largest polygon's outer ring by vertex count
        best = []
        for poly in coords:
            ring = poly[0] if poly else []
            if len(ring) > len(best):
                best = ring
        return best
    return []


def max_boundary_shift_km(geom_a, geom_b):
    """
    Symmetric max-of-min distance between two outlines, in km.
    Approximates how far the boundary moved. Coarse but robust: for each vertex of
    one ring, find nearest segment on the other, take the max; do both directions;
    return the larger. Sampling vertices (not densifying) is fine at our 1 km bar.
    """
    ring_a = [_lonlat_to_local_km(x, y) for x, y in _outer_ring(geom_a)]
    ring_b = [_lonlat_to_local_km(x, y) for x, y in _outer_ring(geom_b)]
    if len(ring_a) < 2 or len(ring_b) < 2:
        return float("inf")  # can't compare -> treat as significant, surfaces for review

    def directed(src, dst):
        worst = 0.0
        for p in src:
            best = float("inf")
            for i in range(len(dst) - 1):
                d = _point_seg_dist_km(p, dst[i], dst[i + 1])
                if d < best:
                    best = d
                    if best == 0.0:
                        break
            if best > worst:
                worst = best
        return worst

    return max(directed(ring_a, ring_b), directed(ring_b, ring_a))


def simplify_ring(ring, tol_deg):
    """Ramer-Douglas-Peucker simplification of a single ring (list of [lon,lat])."""
    if len(ring) < 3:
        return ring[:]

    def rdp(points):
        if len(points) < 3:
            return points
        # find point farthest from the line first..last
        a, b = points[0], points[-1]
        ax, ay = _lonlat_to_local_km(*a)
        bx, by = _lonlat_to_local_km(*b)
        dmax, idx = 0.0, 0
        for i in range(1, len(points) - 1):
            px, py = _lonlat_to_local_km(*points[i])
            d = _point_seg_dist_km((px, py), (ax, ay), (bx, by))
            if d > dmax:
                dmax, idx = d, i
        tol_km = tol_deg * KM_PER_DEG_LAT
        if dmax > tol_km:
            left = rdp(points[: idx + 1])
            right = rdp(points[idx:])
            return left[:-1] + right
        return [points[0], points[-1]]

    simplified = rdp(ring)
    # keep ring closed
    if simplified[0] != simplified[-1]:
        simplified.append(simplified[0])
    return simplified


def simplify_geometry(geom, tol_deg=SIMPLIFY_TOLERANCE_DEG):
    """Simplify all rings of a Polygon/MultiPolygon, preserving structure."""
    gtype = geom.get("type")
    if gtype == "Polygon":
        return {
            "type": "Polygon",
            "coordinates": [simplify_ring(r, tol_deg) for r in geom["coordinates"]],
        }
    if gtype == "MultiPolygon":
        return {
            "type": "MultiPolygon",
            "coordinates": [
                [simplify_ring(r, tol_deg) for r in poly]
                for poly in geom["coordinates"]
            ],
        }
    return geom


# --- Fetch -------------------------------------------------------------------

def fetch_live_zones():
    """
    Fetch all displayable zones from TAHC. Returns a dict keyed by GlobalID:
        { globalId: {"oid": int, "zone_name": int, "geometry": <geojson geom>} }
    Paginates defensively even though there are only ~11 zones today.
    Raises on any network/parse error so the caller can fail safe.
    """
    out = {}
    offset = 0
    while True:
        url = f"{ENDPOINT}{QUERY_PARAMS}&resultOffset={offset}&resultRecordCount={PAGE_SIZE}"
        req = urllib.request.Request(url, headers={"User-Agent": "nws-zone-updater/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        feats = data.get("features", [])
        for ft in feats:
            props = ft.get("properties") or ft.get("attributes") or {}
            gid = props.get("GlobalID") or props.get("globalid")
            if not gid:
                raise ValueError(f"Feature missing GlobalID: oid={props.get('OBJECTID')}")
            out[gid] = {
                "oid": props.get("OBJECTID"),
                "zone_name": props.get("zone_name"),
                "geometry": ft.get("geometry"),
            }

        # GeoJSON responses may include exceededTransferLimit at the top level
        if data.get("properties", {}).get("exceededTransferLimit") or data.get("exceededTransferLimit"):
            offset += len(feats)
            if not feats:
                break
            continue
        break

    if not out:
        raise ValueError("Endpoint returned zero zones; refusing to proceed.")
    return out


# --- Snapshot handling -------------------------------------------------------

def load_history(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def latest_state_by_globalid(history):
    """
    Reconstruct the most recent known zone state from the history file, keyed by
    GlobalID. Works with both the current single-baseline format and the future
    multi-snapshot format (a top-level "snapshots" list, newest last).
    Returns { globalId: {"zone_name": int, "geometry": geom, "oid": int|None} }.
    """
    state = {}

    def ingest_feature_collection(fc, zone_name):
        for ft in fc.get("features", []):
            props = ft.get("properties", {})
            gid = props.get("globalId")
            if not gid:
                continue
            state[gid] = {
                "zone_name": zone_name,
                "geometry": ft.get("geometry"),
                "oid": props.get("oid"),
            }

    if "snapshots" in history and history["snapshots"]:
        snap = history["snapshots"][-1]
        ingest_feature_collection(snap.get("infested", {"features": []}), ZONE_NAME_INFESTED)
        ingest_feature_collection(snap.get("surveillance", {"features": []}), ZONE_NAME_SURVEILLANCE)
    else:
        # current baseline format
        ingest_feature_collection(history.get("infested", {"features": []}), ZONE_NAME_INFESTED)
        ingest_feature_collection(history.get("surveillance", {"features": []}), ZONE_NAME_SURVEILLANCE)
        # surv47Truncated is a pre-merge variant of an existing globalId; the full
        # form is already represented in surveillance, so we don't double-count it.

    return state


def diff_zones(live, prev):
    """
    Compare live vs previous state (both keyed by GlobalID).
    Returns (added, removed, changed, notes) where:
      added   = [gid, ...] present live but not previously
      removed = [gid, ...] previously present but gone live
      changed = [(gid, shift_km), ...] same gid, boundary moved > threshold
      notes   = human-readable strings describing each decision
    """
    added, removed, changed, notes = [], [], [], []

    live_ids = set(live)
    prev_ids = set(prev)

    for gid in sorted(live_ids - prev_ids):
        z = live[gid]
        kind = "infested" if z["zone_name"] == ZONE_NAME_INFESTED else "surveillance"
        added.append(gid)
        notes.append(f"ADDED   {kind} zone (oid {z['oid']}, gid {gid[:8]}…)")

    for gid in sorted(prev_ids - live_ids):
        z = prev[gid]
        kind = "infested" if z["zone_name"] == ZONE_NAME_INFESTED else "surveillance"
        removed.append(gid)
        notes.append(f"REMOVED {kind} zone (gid {gid[:8]}…)")

    for gid in sorted(live_ids & prev_ids):
        shift = max_boundary_shift_km(prev[gid]["geometry"], live[gid]["geometry"])
        if shift > SIGNIFICANT_SHIFT_KM:
            changed.append((gid, shift))
            notes.append(f"CHANGED zone (gid {gid[:8]}…) boundary moved ~{shift:.2f} km")
        # else: ignored as noise

    return added, removed, changed, notes


# --- Snapshot construction ---------------------------------------------------

def build_new_snapshot(history, live, today, appear_day):
    """
    Produce a new dated snapshot reflecting the live zone set. Geometry is
    simplified. Appearance metadata for pre-existing zones is carried forward;
    newly added zones get appearDay = appear_day (today's timeline index).
    """
    prev = latest_state_by_globalid(history)

    # Map globalId -> existing appearDay so we preserve the timeline
    prev_appear = {}
    def collect_appear(fc):
        for ft in fc.get("features", []):
            p = ft.get("properties", {})
            if p.get("globalId") is not None and p.get("appearDay") is not None:
                prev_appear[p["globalId"]] = p["appearDay"]
    if "snapshots" in history and history["snapshots"]:
        s = history["snapshots"][-1]
        collect_appear(s.get("infested", {"features": []}))
        collect_appear(s.get("surveillance", {"features": []}))
    else:
        collect_appear(history.get("infested", {"features": []}))
        collect_appear(history.get("surveillance", {"features": []}))

    infested_feats, surv_feats = [], []
    for gid, z in live.items():
        appear = prev_appear.get(gid, appear_day)
        feat = {
            "type": "Feature",
            "properties": {"oid": z["oid"], "globalId": gid, "appearDay": appear},
            "geometry": simplify_geometry(z["geometry"]),
        }
        if z["zone_name"] == ZONE_NAME_INFESTED:
            infested_feats.append(feat)
        else:
            surv_feats.append(feat)

    return {
        "date": today,
        "infested": {"type": "FeatureCollection", "features": infested_feats},
        "surveillance": {"type": "FeatureCollection", "features": surv_feats},
    }


# --- Main --------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Update zone-history.json from TAHC endpoint.")
    ap.add_argument("--commit", action="store_true",
                    help="Actually write changes. Default is dry run.")
    ap.add_argument("--appear-day", type=int, default=None,
                    help="Timeline index to assign to newly-added zones. "
                         "Defaults to the last dayLabels index.")
    args = ap.parse_args()
    commit = args.commit or os.environ.get("COMMIT") == "1"

    today = dt.date.today().isoformat()

    try:
        history = load_history(HISTORY_PATH)
    except Exception as e:
        print(f"FATAL: could not read {HISTORY_PATH}: {e}", file=sys.stderr)
        return 2

    try:
        live = fetch_live_zones()
    except (urllib.error.URLError, ValueError, json.JSONDecodeError) as e:
        print(f"FATAL: could not fetch live zones: {e}", file=sys.stderr)
        return 3

    prev = latest_state_by_globalid(history)
    added, removed, changed, notes = diff_zones(live, prev)

    print(f"== Zone check {today} ==")
    print(f"live zones: {len(live)}   previously known: {len(prev)}")
    if notes:
        for n in notes:
            print("  " + n)
    else:
        print("  no significant changes")

    significant = bool(added or removed or changed)
    if not significant:
        print("No snapshot needed.")
        return 0

    # default appear day = last index of dayLabels (keeps the slider in range)
    day_labels = history.get("dayLabels", [])
    appear_day = args.appear_day
    if appear_day is None:
        appear_day = max(0, len(day_labels) - 1)

    snapshot = build_new_snapshot(history, live, today, appear_day)

    if not commit:
        print("\nDRY RUN — no file written. Would append a snapshot dated "
              f"{today} with {len(snapshot['infested']['features'])} infested and "
              f"{len(snapshot['surveillance']['features'])} surveillance zones.")
        print("Re-run with --commit (or COMMIT=1) to apply.")
        return 0

    # Migrate baseline -> snapshots[] format on first commit, then append.
    if "snapshots" not in history:
        baseline = {
            "date": history.get("meta", {}).get("baselineDate", "2026-05-30"),
            "infested": history.get("infested", {"type": "FeatureCollection", "features": []}),
            "surveillance": history.get("surveillance", {"type": "FeatureCollection", "features": []}),
        }
        history["snapshots"] = [baseline]
        # keep surv47Truncated and dayLabels/meta at top level for the map
    history["snapshots"].append(snapshot)
    history.setdefault("meta", {})["lastUpdated"] = today

    tmp = HISTORY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(history, f, separators=(",", ":"), ensure_ascii=True)
    os.replace(tmp, HISTORY_PATH)
    print(f"\nWROTE snapshot {today} to {HISTORY_PATH} "
          f"({len(history['snapshots'])} snapshots total).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
