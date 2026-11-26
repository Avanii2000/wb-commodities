#!/usr/bin/env python3
"""
Build price_raw.json from the World Bank Pink Sheet monthly XLS.

The output has exactly the shape of the PRICE_RAW block embedded in the
Commodity Price Source Explorer artifact:

  {"months": [13 x "YYYYMmm"],
   "series": {"<lowercase name>": {"name", "unit", "v": [13 numbers]}},
   "released": "3 Nov 2026",
   "asOf": "Oct 2026"}

It also writes insights.md: the same "latest month vs. average of the 12
months before it" figure that drives the red/green arrows in the artifact,
so the numbers can be checked without opening the artifact.

The script fails loudly (non-zero exit) if anything looks wrong, so a bad
download can never overwrite good data.
"""
import argparse
import datetime
import json
import os
import re
import sys

import openpyxl

# (display name, unit) - the 28 series, names and units the artifact uses (Iron ore and Nickel added for Steel and Stainless Steel).
SERIES = [
    ("Cocoa", "$/kg"), ("Coffee, Arabica", "$/kg"), ("Tea, avg 3 auctions", "$/kg"),
    ("Coconut oil", "$/mt"), ("Groundnut oil", "$/mt"), ("Palm oil", "$/mt"),
    ("Palm kernel oil", "$/mt"), ("Soybean oil", "$/mt"), ("Rapeseed oil", "$/mt"),
    ("Sunflower oil", "$/mt"), ("Maize", "$/mt"), ("Rice, Thai 5%", "$/mt"),
    ("Wheat, US HRW", "$/mt"), ("Beef", "$/kg"), ("Chicken", "$/kg"), ("Lamb", "$/kg"),
    ("Sugar, EU", "$/kg"), ("Sugar, US", "$/kg"), ("Sugar, world", "$/kg"),
    ("Sawnwood, Cameroon", "$/cubic meter"), ("Sawnwood, Malaysian", "$/cubic meter"),
    ("Urea", "$/mt"), ("Potassium chloride", "$/mt"), ("Aluminum", "$/mt"),
    ("Copper", "$/mt"), ("Zinc", "$/mt"),
    ("Iron ore, cfr spot", "$/dmtu"), ("Nickel", "$/mt"),
]
N_MONTHS = 13
MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTH_RE = re.compile(r"^(\d{4})M(\d{2})$")


def clean(s):
    """Same normalisation the artifact uses: drop trailing '**', trim, lowercase."""
    return re.sub(r"\s*\*+\s*$", "", str(s)).strip().lower()


def fail(msg):
    print("ERROR: " + msg, file=sys.stderr)
    sys.exit(1)


def month_label(m):
    return "%s %s" % (MON[int(m[5:]) - 1], m[:4])


def find_columns(rows):
    """Find the header row (best match among the first 25 rows) and the column of each series."""
    wanted = {clean(n): n for n, _ in SERIES}
    best = (0, None, {})
    for i, r in enumerate(rows[:25]):
        cols = {}
        for j, c in enumerate(r):
            if isinstance(c, str):
                k = clean(c)
                if k in wanted and k not in cols:
                    cols[k] = j
        if len(cols) > best[0]:
            best = (len(cols), i, cols)
    n, hdr, cols = best
    missing = [wanted[k] for k in wanted if k not in cols]
    if missing:
        fail("could not find these columns in the XLS header: " + ", ".join(missing))
    return hdr, cols


def read_released(page_path, fallback):
    """Release date shown on worldbank.org/commodities, e.g. '(11/03/2026)'."""
    if page_path and os.path.exists(page_path):
        html = open(page_path, encoding="utf-8", errors="ignore").read()
        i = html.find("Latest Commodity Prices Published")
        chunk = html[i:i + 1500] if i >= 0 else html
        m = re.search(r"\((\d{2})/(\d{2})/(\d{4})\)", chunk)
        if m:
            mm, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return "%d %s %d" % (dd, MON[mm - 1], yy)
    print("WARNING: could not read the release date from the page; using fallback", file=sys.stderr)
    return fallback


def insights_markdown(out):
    lines = ["# Price insights (latest month vs. average of the 12 months before)", "",
             "As of %s, released %s." % (out["asOf"], out["released"]), "",
             "| Series | Latest | 12-mo avg | Change | 3-month trend |", "|---|---|---|---|---|"]
    rows = []
    for s in out["series"].values():
        v = s["v"]
        avg = sum(v[:12]) / 12
        dev = v[12] / avg - 1
        trend = "rising" if v[12] > v[11] > v[10] > v[9] else "falling" if v[12] < v[11] < v[10] < v[9] else "-"
        rows.append((abs(dev), s["name"], v[12], avg, dev, trend, s["unit"]))
    for _, name, last, avg, dev, trend, unit in sorted(rows, reverse=True):
        lines.append("| %s (%s) | %s | %.3f | %+.1f%% | %s |" % (name, unit, last, avg, dev * 100, trend))
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", required=True)
    ap.add_argument("--page", default=None, help="saved HTML of worldbank.org/commodities")
    ap.add_argument("--out", default="price_raw.json")
    ap.add_argument("--insights", default="insights.md")
    a = ap.parse_args()

    wb = openpyxl.load_workbook(a.xlsx, data_only=True, read_only=True)
    if "Monthly Prices" not in wb.sheetnames:
        fail("sheet 'Monthly Prices' not found; sheets are: " + ", ".join(wb.sheetnames))
    rows = [list(r) for r in wb["Monthly Prices"].iter_rows(values_only=True)]
    hdr, cols = find_columns(rows)

    data = [r for r in rows[hdr + 1:] if r and isinstance(r[0], str) and MONTH_RE.match(r[0].strip())]
    if len(data) < N_MONTHS:
        fail("fewer than %d monthly rows found" % N_MONTHS)
    data = data[-N_MONTHS:]
    months = [r[0].strip() for r in data]

    # months must be consecutive
    idx = [int(m[:4]) * 12 + int(m[5:]) for m in months]
    if any(b - a_ != 1 for a_, b in zip(idx, idx[1:])):
        fail("the last %d months are not consecutive: %s" % (N_MONTHS, months))

    series = {}
    for name, unit in SERIES:
        j = cols[clean(name)]
        nd = 2 if unit == "$/kg" else 1
        vals = []
        for m, r in zip(months, data):
            v = r[j] if j < len(r) else None
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                fail("%s has no numeric value for %s (found %r)" % (name, m, v))
            vals.append(round(float(v), nd))
        series[clean(name)] = {"name": name, "unit": unit, "v": vals}

    # keep the previous release date if nothing changed and the page can't be read
    prev = None
    if os.path.exists(a.out):
        try:
            prev = json.load(open(a.out, encoding="utf-8"))
        except Exception:
            prev = None
    fallback = prev["released"] if prev and prev.get("months") == months else \
        (lambda d: "%d %s %d" % (d.day, MON[d.month - 1], d.year))(datetime.datetime.now(datetime.timezone.utc))

    out = {"months": months, "series": series,
           "released": read_released(a.page, fallback), "asOf": month_label(months[-1])}

    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
        f.write("\n")
    with open(a.insights, "w", encoding="utf-8") as f:
        f.write(insights_markdown(out))
    print("OK: %d series, %s to %s, released %s" % (len(series), months[0], months[-1], out["released"]))


if __name__ == "__main__":
    main()
