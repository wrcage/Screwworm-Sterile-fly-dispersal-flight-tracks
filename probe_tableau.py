#!/usr/bin/env python3
"""Read-only: find the Tableau dashboard embedded in a web page and try
Tableau's CSV export on it. Prints what it finds; changes nothing.
Usage: python probe_tableau.py <page_url> [<extra tableau view url> ...]"""
import re, sys, urllib.parse, urllib.request
UA = {"User-Agent": "Mozilla/5.0 (screwworm-map probe; +https://github.com/wrcage/Screwworm-Sterile-fly-dispersal-flight-tracks)"}

def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read()

page = sys.argv[1]
st, ct, body = get(page)
html = body.decode("utf-8", "replace")
print("PAGE", st, ct, len(html), "bytes")
cands = set(re.findall(r'https?://[^\s"\'<>]*(?:tableau|publicdashboards)[^\s"\'<>]*', html))
for m in re.finditer(r'<(?:iframe|tableau-viz|object|param)[^>]+>', html, re.I):
    t = m.group(0)
    if re.search(r'tableau|dashboard', t, re.I):
        print("TAG:", t[:400])
for name, val in re.findall(r'<param\s+name=["\'](\w+)["\']\s+value=["\']([^"\']*)', html, re.I):
    print("PARAM", name, "=", val)
for c in sorted(cands):
    print("URL:", c)
views = [u.replace("&amp;", "&") for u in cands if "/views/" in u] + sys.argv[2:]
# List the workbook's sheets/views from the dashboard page bootstrap data.
for v in list(views):
    base = v.split("?")[0]
    try:
        st, ct, b = get(base + "?:embed=y&:showVizHome=no")
        t = b.decode("utf-8", "replace")
        names = sorted(set(re.findall(r'"(?:sheetName|sheet_name|name|repositoryUrl|viewName)"\s*:\s*"([^"]{2,80})"', t)))
        print("VIEW PAGE", st, len(t), "bytes; candidate sheet names:", names[:80])
        for m in sorted(set(re.findall(r'views/[A-Za-z0-9_]+/[A-Za-z0-9_%]+', t)))[:40]:
            print("  view ref:", m)
    except Exception as e:
        print("VIEW PAGE FAIL", e)
for v in views:
    base = v.split("?")[0].rstrip("/")
    for cand in (base + ".csv", base + ".csv?:showVizHome=no", base + "?:format=csv"):
        try:
            st, ct, b = get(cand)
            txt = b[:3000].decode("utf-8", "replace")
            print("\nTRY", cand, "->", st, ct, len(b), "bytes")
            print(txt[:3000])
        except Exception as e:
            print("\nTRY", cand, "-> FAIL", e)
