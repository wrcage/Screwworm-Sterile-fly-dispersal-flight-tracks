#!/usr/bin/env python3
"""
Read-only probe: given an ArcGIS Online item id (an app or web map), follow it
to the underlying feature layers and print their URLs, fields, feature counts
and a few sample attribute rows. Used to find machine-readable zone data
(e.g. New Mexico's NWS zone map) that a zone updater could read.

Usage: python probe_arcgis.py <item_id> [<portal host, default www.arcgis.com>]
"""
import json
import sys
import urllib.parse
import urllib.request

UA = {"User-Agent": "screwworm-sterile-fly-map/1.0 (+https://github.com/wrcage/Screwworm-Sterile-fly-dispersal-flight-tracks)"}


def get(url, **params):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def item(host, iid):
    base = "https://%s/sharing/rest/content/items/%s" % (host, iid)
    meta = get(base, f="json")
    try:
        data = get(base + "/data", f="json")
    except Exception as e:
        data = {"_error": str(e)}
    return meta, data


def layer_urls(obj, out):
    """Collect every 'url' that looks like a FeatureServer/MapServer layer."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "url" and isinstance(v, str) and ("FeatureServer" in v or "MapServer" in v):
                out.append((obj.get("title") or obj.get("id") or "", v))
            else:
                layer_urls(v, out)
    elif isinstance(obj, list):
        for v in obj:
            layer_urls(v, out)
    return out


def describe_layer(title, url):
    print("\n=== LAYER: %s\n    %s" % (title, url))
    try:
        info = get(url, f="json")
    except Exception as e:
        print("    (could not read layer: %s)" % e)
        return
    if "layers" in info and "fields" not in info:      # a service, not a layer
        for L in info.get("layers", []):
            describe_layer(L.get("name"), url.rstrip("/") + "/" + str(L["id"]))
        return
    print("    name: %s | geometry: %s | updated: %s" % (
        info.get("name"), info.get("geometryType"),
        (info.get("editingInfo") or {}).get("lastEditDate")))
    print("    fields: " + ", ".join(f["name"] for f in info.get("fields", [])))
    try:
        cnt = get(url + "/query", where="1=1", returnCountOnly="true", f="json").get("count")
        print("    feature count: %s" % cnt)
        gj = get(url + "/query", where="1=1", outFields="*", outSR="4326", f="geojson")
        import math
        for ft in gj.get("features", []):
            g = ft.get("geometry") or {}
            pts = []
            def walk(c):
                if c and isinstance(c[0], (int, float)): pts.append(c)
                else:
                    for x in c: walk(x)
            walk(g.get("coordinates") or [])
            if pts:
                print("    geom %s: %d vertices, lon %.2f..%.2f lat %.2f..%.2f" % (
                    g.get("type"), len(pts), min(p[0] for p in pts), max(p[0] for p in pts),
                    min(p[1] for p in pts), max(p[1] for p in pts)))
        rows = get(url + "/query", where="1=1", outFields="*", returnGeometry="false",
                   resultRecordCount=8, f="json").get("features", [])
        for r in rows:
            print("    sample: " + json.dumps(r.get("attributes"), default=str)[:400])
    except Exception as e:
        print("    (query failed: %s)" % e)


def main():
    iid = sys.argv[1]
    host = sys.argv[2] if len(sys.argv) > 2 else "www.arcgis.com"
    meta, data = item(host, iid)
    print("ITEM %s: type=%s title=%r owner=%s modified=%s access=%s" % (
        iid, meta.get("type"), meta.get("title"), meta.get("owner"),
        meta.get("modified"), meta.get("access")))
    if meta.get("url"):
        print("item url: " + meta["url"])
    found = layer_urls(data, [])
    # Apps usually point at a web map; follow it.
    wm = None
    if isinstance(data, dict):
        wm = (data.get("values") or {}).get("webmap") or data.get("webmap") \
             or ((data.get("map") or {}).get("itemId") if isinstance(data.get("map"), dict) else None)
    if wm:
        print("follows web map: " + wm)
        m2, d2 = item(host, wm)
        print("WEBMAP %s: title=%r modified=%s" % (wm, m2.get("title"), m2.get("modified")))
        for L in d2.get("operationalLayers", []):
            print("  op layer: title=%r visible=%s url=%s sublayers=%s" % (
                L.get("title"), L.get("visibility"), L.get("url"),
                [(x.get("title"), x.get("visibility")) for x in L.get("layers", [])] or ""))
        found += layer_urls(d2, [])
    if not found:
        print("No layer URLs found. Raw data keys: " + str(list(data.keys()) if isinstance(data, dict) else type(data)))
        print(json.dumps(data, default=str)[:3000])
    seen = set()
    for title, url in found:
        if url not in seen:
            seen.add(url)
            describe_layer(title, url)


if __name__ == "__main__":
    main()
