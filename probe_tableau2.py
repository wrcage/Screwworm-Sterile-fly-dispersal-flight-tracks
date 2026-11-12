#!/usr/bin/env python3
"""Read-only: open a public Tableau view with tableauscraper and list every
worksheet (columns, row count, first rows), plus other dashboards in the
workbook. Usage: python probe_tableau2.py <tableau view url>"""
import sys
from tableauscraper import TableauScraper as TS

url = sys.argv[1]
ts = TS()
ts.loads(url)
wb = ts.getWorkbook()
try:
    print("DASHBOARDS/SHEETS in workbook:", [s["sheet"] for s in wb.getSheets()])
except Exception as e:
    print("getSheets failed:", e)

def dump(wbk, label):
    for ws in wbk.worksheets:
        df = ws.data
        print("\n=== %s :: WORKSHEET %r  rows=%d" % (label, ws.name, len(df)))
        print("columns:", list(df.columns))
        print(df.head(8).to_string(max_colwidth=40))

dump(wb, "SummaryDashboard")
try:
    for s in wb.getSheets():
        name = s["sheet"]
        if name == "SummaryDashboard":
            continue
        try:
            other = wb.goToSheet(name)
            dump(other, name)
        except Exception as e:
            print("\ngoToSheet(%r) failed: %s" % (name, e))
except Exception as e:
    print("sheet loop failed:", e)
