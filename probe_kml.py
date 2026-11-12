#!/usr/bin/env python3
"""Read-only: download a Google My Maps (or any) KML and summarize it:
document name/description, folders, placemarks with geometry type, vertex
counts, extents and descriptions. Usage: python probe_kml.py <kml_url>"""
import io, re, sys, urllib.request, zipfile
import xml.etree.ElementTree as ET

url = sys.argv[1]
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 screwworm-map-probe"})
raw = urllib.request.urlopen(req, timeout=90).read()
if raw[:2] == b"PK":
    z = zipfile.ZipFile(io.BytesIO(raw)); raw = z.read([n for n in z.namelist() if n.endswith(".kml")][0])
root = ET.fromstring(raw)
ns = {"k": "http://www.opengis.net/kml/2.2"}
txt = lambda e, t: (e.findtext("k:" + t, default="", namespaces=ns) or "").strip()
doc = root.find("k:Document", ns)
print("DOC:", txt(doc, "name")); print("DESC:", re.sub(r"\s+", " ", txt(doc, "description"))[:1500])

def coords(pm):
    out = []
    for c in pm.iter("{http://www.opengis.net/kml/2.2}coordinates"):
        for tok in (c.text or "").split():
            p = tok.split(",")
            if len(p) >= 2: out.append((float(p[0]), float(p[1])))
    return out

for f in doc.findall("k:Folder", ns):
    pms = f.findall(".//k:Placemark", ns)
    print("\nFOLDER: %r (%d placemarks)" % (txt(f, "name"), len(pms)))
    for pm in pms[:60]:
        geom = [g.tag.split("}")[1] for g in pm if g.tag.split("}")[1] in ("Polygon", "MultiGeometry", "Point", "LineString")]
        c = coords(pm)
        ext = "lon %.3f..%.3f lat %.3f..%.3f" % (min(x for x, _ in c), max(x for x, _ in c), min(y for _, y in c), max(y for _, y in c)) if c else ""
        d = re.sub(r"<[^>]+>", " ", txt(pm, "description")); d = re.sub(r"\s+", " ", d)[:200]
        print("  - %r %s n=%d %s | %s" % (txt(pm, "name"), geom, len(c), ext, d))
