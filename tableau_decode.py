#!/usr/bin/env python3
"""Decode a Tableau bootstrapSession response into per-worksheet tables.
Response format: "<len>;{json}<len>;{json}". The second JSON holds a shared
data dictionary (values by type) and, per worksheet, columns that point into
it by index. Negative alias indices point into the cstring list.
decode(text) -> {worksheet_name: {column_caption: [values...]}}"""
import json, re


def split_parts(t):
    m = re.search(r"\d+;({.*})\d+;({.*})", t, re.S)
    if not m:
        raise ValueError("not a Tableau bootstrap response")
    return json.loads(m.group(1)), json.loads(m.group(2))


def decode(t):
    info, data = split_parts(t)
    pmm = data["secondaryInfo"]["presModelMap"]
    segs = pmm["dataDictionary"]["presModelHolder"]["genDataDictionaryPresModel"]["dataSegments"]
    full = {}
    for k in sorted(segs, key=lambda x: int(x)):
        for col in segs[k]["dataColumns"]:
            full.setdefault(col["dataType"], []).extend(col["dataValues"])
    sheets = pmm["vizData"]["presModelHolder"]["genPresModelMapPresModel"]["presModelMap"]
    out = {}
    for name, sh in sheets.items():
        try:
            pcd = sh["presModelHolder"]["genVizDataPresModel"]["paneColumnsData"]
        except KeyError:
            continue
        panes = pcd["paneColumnsList"]
        cols = {}
        for c in pcd["vizDataColumns"]:
            cap = c.get("fieldCaption")
            if not cap:
                continue
            vals = []
            for pi, ci in zip(c.get("paneIndices", []), c.get("columnIndices", [])):
                vp = panes[pi]["vizPaneColumns"][ci]
                pool = full.get(c.get("dataType"), [])
                for it in vp.get("aliasIndices", []):
                    vals.append(pool[it] if it >= 0 and it < len(pool) else
                                full.get("cstring", [])[abs(it) - 1] if it < 0 else None)
            cols[cap] = vals
        out[name] = cols
    return out


if __name__ == "__main__":
    import sys
    tables = decode(open(sys.argv[1], encoding="utf-8").read())
    for name, cols in tables.items():
        lens = {k: len(v) for k, v in cols.items()}
        print("\n=== WORKSHEET %r  columns/lengths: %s" % (name, lens))
        n = max(lens.values()) if lens else 0
        keys = [k for k in cols if lens[k] == n]
        for i in range(min(n, 8)):
            print("   ", " | ".join(str(cols[k][i])[:28] for k in keys))
