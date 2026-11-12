#!/usr/bin/env python3
"""Read-only: open a Tableau view in headless Chromium, capture the vizql
bootstrap responses (which carry the dashboard's data), and report worksheet
names, field captions and a sample of string values. Saves the raw bootstrap
to tableau_bootstrap.txt for inspection. Usage: python probe_tableau3.py <url>"""
import asyncio, json, re, sys
from playwright.async_api import async_playwright

URL = sys.argv[1]
captured = []

async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": 1600, "height": 1200})
        async def on_resp(r):
            u = r.url
            if "vizql" in u or "bootstrap" in u.lower():
                try:
                    t = await r.text()
                except Exception:
                    t = ""
                captured.append((u, r.status, t))
        pg.on("response", on_resp)
        await pg.goto(URL, wait_until="load", timeout=120000)
        await pg.wait_for_timeout(20000)   # Tableau keeps a socket open; never "idle"
        await pg.screenshot(path="tableau_view.png", full_page=True)
        print("TITLE:", await pg.title())
        try:
            txt = await pg.inner_text("body")
            print("BODY TEXT (first 1500):", re.sub(r"\s+", " ", txt)[:1500])
        except Exception as e:
            print("body text failed", e)
        await b.close()

asyncio.run(main())
big = ""
for u, st, t in captured:
    print("RESP %s %d %d bytes" % (u[:160], st, len(t)))
    if "bootstrapSession" in u or len(t) > len(big):
        big = t if len(t) > len(big) else big
open("tableau_bootstrap.txt", "w").write(big)
print("\nBOOTSTRAP bytes:", len(big))
for key in ("worksheet", "sheetName", "fieldCaption", "caption"):
    vals = sorted(set(re.findall(r'"%s"\s*:\s*"([^"]{1,80})"' % key, big)))
    print("%s (%d): %s" % (key, len(vals), vals[:120]))
# string values in the data dictionary (cities/counties/species show up here)
m = re.search(r'"dataValues"\s*:\s*(\[.*?\])\s*[,}]', big)
strs = re.findall(r'"cstring"[^]]*?"dataValues"\s*:\s*\[([^\]]*)\]', big)
for s_ in strs[:3]:
    vals = json.loads("[" + s_ + "]")
    print("\nCSTRING values (%d): %s" % (len(vals), vals[:200]))
