#!/usr/bin/env python3
"""
Read the EU column of the EU 'Monthly market prices' dashboard (a Qlik app).

  * Always saves the full product dropdown to probe/dashboard/options.txt
  * No eu_products.txt in the repo  -> DIAGNOSTIC mode: tests a few products
  * eu_products.txt present         -> reads the EU column (latest 13 months) for every product listed
                                       (a single line containing * means: every product in the dropdown)
  * Writes price_raw_eu.json and probe/dashboard/eu_coverage.csv

Never raises: every stage logs its problems and the run continues. Never overwrites a good
price_raw_eu.json with a much smaller result.
"""
import csv
import datetime
import json
import os
import re
import traceback

from playwright.sync_api import sync_playwright

URL = "https://agridata.ec.europa.eu/extensions/DashboardPrice/DashboardMarketPrices.html"
OUT = "probe/dashboard"
PRODUCTS_FILE = "eu_products.txt"
RESULT_FILE = "price_raw_eu.json"
DIAGNOSTIC_PRODUCTS = ["Apples Fuji", "Butter", "Bread Wheat", "Asparagus"]
N_MONTHS = 13
MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
PERIOD_RE = re.compile(r"^\d{4}/\d{2}$")
NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
COL_TOLERANCE = 28   # px: half a column (columns are ~61 px wide)
ROW_TOLERANCE = 12   # px
MAX_SHOTS = 8
LOG = []
SHOTS = [0]

JS_CELLS = """
() => {
  const out = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const t = walker.currentNode.textContent.trim();
    if (!t) continue;
    const el = walker.currentNode.parentElement;
    if (!el) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    if (r.bottom < 0 || r.top > window.innerHeight || r.right < 0 || r.left > window.innerWidth) continue;
    out.push({t: t, x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height)});
  }
  return out;
}
"""
JS_MARK_SCROLLER = """
(label) => {
  const cands = Array.from(document.querySelectorAll('*')).filter(e => {
    const cs = getComputedStyle(e);
    if (!(e.scrollHeight > e.clientHeight + 5) || cs.overflowY === 'visible') return false;
    const r = e.getBoundingClientRect();
    if (r.width < 50 || r.width > 500 || r.height < 100) return false;
    return e.innerText.split('\\n').map(x => x.trim()).includes(label);
  });
  if (!cands.length) return 'no candidate';
  cands.sort((a, b) => {
    const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
    return ra.width * ra.height - rb.width * rb.height;
  });
  cands[0].setAttribute('data-probe-scroller', '1');
  return 'ok';
}
"""
JS_SCROLL_STEP = """
() => {
  const e = document.querySelector('[data-probe-scroller]');
  if (!e) return null;
  const before = e.scrollTop;
  e.scrollTop = before + e.clientHeight * 0.8;
  return {before: before, after: e.scrollTop, text: e.innerText, h: e.scrollHeight};
}
"""


# ---------------------------------------------------------------- pure functions (unit-tested)
def cx(c):
    return c["x"] + c["w"] / 2.0


def cy(c):
    return c["y"] + c["h"] / 2.0


def title_info(body):
    """'All MS Category: X - Sector Code: FRL - Product: Apples Fuji - €/100 kg net weight' -> dict"""
    body = re.sub(r"\s+", " ", body)
    m = re.search(r"All MS Category:\s*(.*?)\s+-\s+Sector Code:\s*(\S+)\s+-\s+Product:\s*(.*?)\s+-\s+(€.*?)(?: From \d{4}/\d{2}|$)",
                  body)
    if not m:
        return {}
    return {"category": m.group(1).strip(), "sector": m.group(2), "product": m.group(3).strip(), "unit": m.group(4).strip()}


def parse_last_update(body):
    m = re.search(r"Last update:\s*(\d{2})/(\d{2})/(\d{4})", body)
    if not m:
        return ""
    return "%d %s %s" % (int(m.group(1)), MON[int(m.group(2)) - 1], m.group(3))


def parse_table(cells):
    """cells: [{t,x,y,w,h}] -> ({'2026/10': 134.0 or None, ...}, error)"""
    periods = {}
    for c in cells:
        if PERIOD_RE.match(c["t"]):
            periods.setdefault(c["t"], c)
    headers = sorted((c for c in cells if c["t"] == "EU"), key=lambda c: c["y"])
    hdr = None
    for h in headers:
        if any(p["y"] > h["y"] and cx(p) < cx(h) for p in periods.values()):
            hdr = h
            break
    if hdr is None:
        return None, "EU header with period rows below it not found"
    x0 = cx(hdr)
    # tolerances from the real geometry: distance to the next country header, and row pitch
    nxt = [cx(c) for c in cells if abs(c["y"] - hdr["y"]) <= 6 and cx(c) > x0 + 5 and re.fullmatch(r"[A-Z]{2}", c["t"])]
    col_tol = max(18.0, 0.45 * (min(nxt) - x0)) if nxt else COL_TOLERANCE
    ys = sorted({cy(p) for p in periods.values() if p["y"] > hdr["y"]})
    gaps = [b - a for a, b in zip(ys, ys[1:]) if b - a > 4]
    row_tol = max(6.0, 0.45 * min(gaps)) if gaps else ROW_TOLERANCE
    out = {}
    for per, pc in periods.items():
        if pc["y"] <= hdr["y"] or cx(pc) >= x0:
            continue
        val = None
        for c in cells:
            if c is pc or abs(cx(c) - x0) > col_tol or abs(cy(c) - cy(pc)) > row_tol:
                continue
            t = c["t"].replace(",", "").strip()
            if NUM_RE.match(t):
                val = float(t)
                break
        out[per] = val
    return out, ""


def region_texts(cells, left, right, top):
    """texts of the open dropdown list: below `top`, within [left, right]; table noise removed"""
    out = []
    for c in sorted(cells, key=lambda c: (c["y"], c["x"])):
        t = c["t"].strip()
        if c["y"] <= top or not (left <= cx(c) <= right) or c["w"] > 300:
            continue
        if NUM_RE.match(t.replace(",", "")) or PERIOD_RE.match(t) or re.fullmatch(r"[A-Z]{2}", t):
            continue
        if t in ("Product Description", "Year/Month", "MS Code"):
            continue
        out.append(t)
    return out


def window13(vals):
    """latest 13 periods -> ((['2025M10', ...], [values]), '') or (None, reason)"""
    pers = sorted(vals, reverse=True)[:N_MONTHS]
    if len(pers) < N_MONTHS:
        return None, "only %d periods visible" % len(pers)
    pers = sorted(pers)
    idx = [int(p[:4]) * 12 + int(p[5:]) for p in pers]
    if any(b - a != 1 for a, b in zip(idx, idx[1:])):
        return None, "months not consecutive (%s ... %s)" % (pers[0], pers[-1])
    missing = [p for p in pers if vals[p] is None]
    if missing:
        return None, "no EU value for " + ", ".join(missing[:4])
    return ([p.replace("/", "M") for p in pers], [vals[p] for p in pers]), ""


def month_label(m):
    return "%s %s" % (MON[int(m[5:]) - 1], m[:4])


def build_output(results, released):
    series, incomplete, latest = {}, {}, ""
    for name, r in results.items():
        if r.get("window"):
            months, values = r["window"]
            series[name.lower()] = {"name": name, "unit": r.get("unit", ""), "sector": r.get("sector", ""),
                                    "category": r.get("category", ""), "months": months, "v": values}
            latest = max(latest, months[-1])
        elif r.get("values"):
            last = sorted(r["values"], reverse=True)[:24]
            incomplete[name] = {"reason": r.get("note", ""), "unit": r.get("unit", ""),
                                "values": {p: r["values"][p] for p in last}}
    return {"source": "EU Agri-food data portal, Monthly market prices (EU column)", "released": released,
            "asOf": month_label(latest) if latest else "", "series": series, "incomplete": incomplete}


# ---------------------------------------------------------------- browser glue
def note(msg):
    print(msg, flush=True)
    LOG.append(msg)


def save(name, text):
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
        f.write(text)


def shot(page, name, force=False):
    if SHOTS[0] >= MAX_SHOTS and not force:
        return
    SHOTS[0] += 1
    try:
        page.screenshot(path=os.path.join(OUT, name))
    except Exception as e:
        note("screenshot %s failed: %s" % (name, e))


def current_product(page):
    try:
        return title_info(page.inner_text("body")).get("product")
    except Exception:
        return None


def find_visible(page, text):
    loc = page.get_by_text(text, exact=True)
    for i
