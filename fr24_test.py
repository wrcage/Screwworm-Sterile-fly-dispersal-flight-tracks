#!/usr/bin/env python3
"""
One-shot smoke test for the Flightradar24 API against the screwworm map's
data needs. Runs from a GitHub Actions workflow (fr24-test.yml) so nothing
has to be run locally.

Purpose:
  * Verify the FR24_API_KEY GitHub secret is valid and accepted.
  * Verify the flight-summary endpoint returns useful results for one of our
    tracked sterile-fly aircraft on a real day.
  * Verify the flight-tracks endpoint returns a coord path for one of those
    flights (this is the data we'll actually plot on the map).
  * Report how many credits the round trip cost so we can budget the daily
    updater before we build it.

Reads inputs from environment variables:
  FR24_API_KEY  - Bearer token (mandatory; from repo secret)
  TARGET_TAIL   - aircraft registration to test (defaults to N75G, our most
                  active sterile-fly aircraft)
  TARGET_DATE   - YYYY-MM-DD in UTC (defaults to yesterday UTC — completed
                  flights should be available by the time we test)

Writes nothing to disk and does not touch the repo files. All output is
printed to the Action log so we can review it after the run.
"""

import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request
import urllib.error

BASE = "https://fr24api.flightradar24.com/api"
# NOTE ON User-Agent: FR24's API sits behind Cloudflare, and Cloudflare's WAF
# rejects requests that use Python's default "Python-urllib/3.x" User-Agent —
# it returns a 403 with an HTML page that LOOKS like a "planned maintenance"
# notice, but it's really a bot-block. Setting an explicit, descriptive UA
# gets us past that layer so the API itself can respond.
COMMON_HEADERS = {
    "Accept": "application/json",
    "Accept-Version": "v1",
    "User-Agent": "screwworm-sterile-fly-map/1.0 (+https://github.com/wrcage/Screwworm-Sterile-fly-dispersal-flight-tracks)",
}


def call(endpoint, params=None):
    """
    GET wrapper that prints what it's doing to the Action log and always
    returns (status_code, raw_body, parsed_json_or_None). On network failure,
    returns (None, None, None). We never raise — we want the log to show
    every attempt even when one fails, so we can diagnose from the log alone.
    """
    url = BASE + endpoint
    if params:
        url += "?" + urllib.parse.urlencode(params)

    token = (os.environ.get("FR24_API_KEY") or "").strip()
    if not token:
        # Fail loudly and specifically — most likely cause is the GitHub
        # secret not being wired into the workflow env block.
        print("FATAL: FR24_API_KEY environment variable is empty.")
        print("Check that the repo secret exists and the workflow passes it in.")
        sys.exit(1)

    headers = dict(COMMON_HEADERS)
    headers["Authorization"] = "Bearer " + token

    print("\n--> GET " + endpoint)
    if params:
        for k, v in params.items():
            print("      " + k + " = " + str(v))

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            code = resp.status
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        code = e.code
    except urllib.error.URLError as e:
        print("    NETWORK ERROR: " + str(e))
        return None, None, None

    print("    HTTP " + str(code))
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        parsed = None
        print("    (non-JSON body, first 400 chars): " + body[:400])
    return code, body, parsed


def main():
    tail = ((os.environ.get("TARGET_TAIL") or "N75G").strip().upper())
    date_str = (os.environ.get("TARGET_DATE") or "").strip()
    if not date_str:
        yesterday = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
        date_str = yesterday.isoformat()

    print("=" * 60)
    print("FR24 API smoke test")
    print("  target tail: " + tail)
    print("  target date: " + date_str + " (UTC)")
    print("=" * 60)

    # --- Step 1: flight summary ------------------------------------------
    # /flight-summary/light returns one entry per completed flight for the
    # given aircraft in the given date range. We use the LIGHT variant on
    # purpose: it's cheaper (fewer credits) and already includes the
    # fr24_id, takeoff/landing times, and origin/destination airports —
    # which is everything we need to (a) confirm the query works and
    # (b) pick a flight to fetch the track for.
    from_iso = date_str + "T00:00:00"
    to_iso   = date_str + "T23:59:59"
    code, body, summary = call("/flight-summary/light", {
        "flight_datetime_from": from_iso,
        "flight_datetime_to":   to_iso,
        "registrations":        tail,
    })

    if code is None:
        print("\nAborting: network failure on summary call.")
        sys.exit(2)
    if code != 200:
        # Distinguish two very different failure modes:
        #   * HTML body: request was blocked/deflected before reaching the API
        #     (usually a Cloudflare/WAF block, sometimes real maintenance).
        #     The body starts with "<!DOCTYPE" or "<html".
        #   * JSON body: the API itself is refusing us — genuine auth issue,
        #     wrong endpoint, bad parameter, credits exhausted, etc. In this
        #     case FR24 will return a JSON error object with useful details.
        looks_html = (body or "").lstrip().lower().startswith(("<!doctype", "<html"))
        if looks_html:
            print("\nBLOCKED BEFORE API. Response was HTML, not JSON — this is")
            print("almost always a WAF/Cloudflare block (frequently disguised")
            print("as a 'planned maintenance' page). Not an auth failure.")
        elif code in (401, 403):
            print("\nAUTH REJECTED by the API. Check the FR24_API_KEY secret.")
        else:
            print("\nUnexpected status " + str(code) + " from the API.")
        print("Body (first 800 chars):")
        print((body or "")[:800])
        # Try /usage as a second data point — same result confirms it's a
        # transport-level issue, different result narrows it to the summary call.
        call("/usage")
        sys.exit(3)

    flights = (summary or {}).get("data") or []
    print("\nSummary returned " + str(len(flights)) + " flight(s) for " +
          tail + " on " + date_str + ":")
    for i, f in enumerate(flights):
        orig = f.get("orig_icao") or f.get("orig_iata") or "?"
        dest = f.get("dest_icao") or f.get("dest_iata") or "?"
        print("  [" + str(i) + "] fr24_id=" + str(f.get("fr24_id")) +
              "  " + orig + " -> " + dest +
              "  takeoff=" + str(f.get("datetime_takeoff")) +
              "  landing=" + str(f.get("datetime_landed")) +
              "  callsign=" + str(f.get("callsign")))

    # --- Step 2: fetch the track for the first flight --------------------
    # This is the endpoint that returns the actual lat/lon/alt sequence we
    # need to plot. If this works AND the point count is reasonable
    # (hundreds to a few thousand for a 3-5 hour flight), we know FR24 can
    # replace the manual KML/Excel workflow.
    if flights:
        fid = flights[0].get("fr24_id")
        if fid:
            code, body, tracks = call("/flight-tracks", {"flight_id": fid})
            if code == 200 and tracks is not None:
                # The tracks response is a list of one entry per fr24_id,
                # each with a "tracks" list of point objects. Handle both
                # bare-list and {"data":[...]} shapes defensively so a
                # future response-shape tweak doesn't blank the log.
                items = tracks if isinstance(tracks, list) else (tracks.get("data") or [tracks])
                total_points = 0
                for it in items:
                    pts = it.get("tracks") if isinstance(it, dict) else None
                    if pts:
                        total_points += len(pts)
                        print("\nTrack for fr24_id=" + str(fid) + ": " +
                              str(len(pts)) + " points")
                        print("  first: " + json.dumps(pts[0]))
                        print("  last:  " + json.dumps(pts[-1]))
                if not total_points:
                    print("\nTrack call returned 200 but no points found.")
                    print("Raw response (first 600 chars):")
                    print((body or "")[:600])
            elif code is not None:
                print("\nTrack call returned HTTP " + str(code) +
                      ". Body (first 600 chars):")
                print((body or "")[:600])
    else:
        print("\nNo flights in summary; skipping track fetch.")
        print("Re-run the workflow with a different date input if you know")
        print("this tail flew on a specific day within the last 30 days.")

    # --- Step 3: credit usage --------------------------------------------
    # /usage tells us cumulative credit spend by endpoint for the current
    # billing period. Print the full body so we can see exact per-endpoint
    # cost of this test — that's the number we'll use to budget the daily
    # updater against our 30,000-credit/month Explorer allowance.
    print("\n" + "-" * 60)
    print("Credit usage after this test:")
    _, usage_body, usage_json = call("/usage")
    if usage_json is not None:
        print(json.dumps(usage_json, indent=2))
    else:
        print("(usage response was not JSON; raw body first 400 chars:)")
        print((usage_body or "")[:400])

    print("\n" + "=" * 60)
    print("Test complete. Paste the full log above back to the assistant.")


if __name__ == "__main__":
    main()
