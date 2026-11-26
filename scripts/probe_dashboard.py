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
(text) => {
  const hit = Array.from(document.querySelectorAll('*'))
    .find(e => e.children.length === 0 && e.textContent.trim() === text);
  if (!hit) return 'option text not found';
  let e = hit;
  while (e && e !== document.body) {
    if (e.scrollHeight > e.clientHeight + 5 && getComputedStyle(e).overflowY !== 'visible') {
      e.setAttribute('data-probe-scroller', '1');
      return 'ok';
    }
    e = e.parentElement;
  }
  return 'no scrollable parent';
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
    out = {}
    for per, pc in periods.items():
        if pc["y"] <= hdr["y"] or cx(pc) >= x0:
            continue
        val = None
        for c in cells:
            if c is pc or abs(cx(c) - x0) > COL_TOLERANCE or abs(cy(c) - cy(pc)) > ROW_TOLERANCE:
                continue
            t = c["t"].replace(",", "").strip()
            if NUM_RE.match(t):
                val = float(t)
                break
        out[per] = val
    return out, ""


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


def load(page):
    page.goto(URL, wait_until="domcontentloaded", timeout=120000)
    page.get_by_text("Year/Month", exact=False).first.wait_for(timeout=120000)
    page.wait_for_timeout(10000)


def read_all_options(page):
    label = current_product(page) or "Apples Fuji"
    page.get_by_text(label, exact=True).first.click(timeout=20000)
    page.wait_for_timeout(1500)
    shot(page, "02_dropdown_open.png")
    note("mark scroller: " + page.evaluate(JS_MARK_SCROLLER, "Apples Boskoop"))
    seen, order, stuck = set(), [], 0
    for _ in range(800):
        r = page.evaluate(JS_SCROLL_STEP)
        if not r:
            break
        for line in r["text"].split("\n"):
            line = line.strip()
            if line and line != "Product Description" and line not in seen:
                seen.add(line)
                order.append(line)
        stuck = stuck + 1 if r["after"] == r["before"] else 0
        if stuck >= 2:
            break
        page.wait_for_timeout(200)
    save("options.txt", "\n".join(order) + "\n")
    note("dropdown options collected: %d (probe/dashboard/options.txt)" % len(order))
    page.keyboard.press("Escape")
    page.wait_for_timeout(800)
    return order


def select_product(page, name):
    cur = current_product(page)
    if cur and cur.lower() == name.lower():
        return True
    page.get_by_text(cur or "Apples Fuji", exact=True).first.click(timeout=20000)
    page.wait_for_timeout(700)
    page.keyboard.type(name, delay=30)
    page.wait_for_timeout(900)
    try:
        page.get_by_text(name, exact=True).first.click(timeout=3000)
    except Exception:
        page.keyboard.press("Enter")
    for _ in range(40):
        cur = current_product(page)
        if cur and cur.lower() == name.lower():
            return True
        page.wait_for_timeout(500)
    return False


def read_table_stable(page):
    last, err = None, "table not found"
    for _ in range(5):
        page.wait_for_timeout(800)
        vals, err = parse_table(page.evaluate(JS_CELLS))
        if vals is not None and last is not None and vals == last:
            return vals, ""
        last = vals
    return last, ("" if last else err)


def read_products_file():
    if not os.path.exists(PRODUCTS_FILE):
        return None
    names = [ln.strip() for ln in open(PRODUCTS_FILE, encoding="utf-8") if ln.strip() and not ln.startswith("#")]
    return names or None


def write_coverage(rows):
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "eu_coverage.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["product", "status", "periods_seen", "latest", "unit", "sector", "category", "note"])
        w.writerows(rows)


def main():
    wanted = read_products_file()
    mode = "diagnostic" if wanted is None else ("all" if wanted == ["*"] else "list")
    note("mode: %s" % mode)
    results, cov = {}, []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 2300, "height": 1400})
        try:
            load(page)
            shot(page, "01_loaded.png")
            body = page.inner_text("body")
            save("page_text.txt", body)
            released = parse_last_update(body)
            note("loaded; last update on page: %s; title: %s" % (released, title_info(body)))
        except Exception:
            note("LOAD FAILED:\n" + traceback.format_exc())
            shot(page, "01_load_failed.png", force=True)
            save("log.txt", "\n".join(LOG))
            browser.close()
            return

        options = []
        try:
            options = read_all_options(page)
        except Exception:
            note("OPTIONS FAILED:\n" + traceback.format_exc())
            shot(page, "02_options_failed.png", force=True)

        if mode == "diagnostic":
            products = DIAGNOSTIC_PRODUCTS
        elif mode == "all":
            products = options
        else:
            products = wanted
            lower = {o.lower() for o in options}
            missing = [n for n in wanted if options and n.lower() not in lower]
            if missing:
                note("not in the dropdown: %s" % missing)

        failures_in_a_row, reloads = 0, 0
        for i, name in enumerate(products):
            try:
                if not select_product(page, name):
                    raise RuntimeError("product did not become selected")
                info = title_info(page.inner_text("body"))
                vals, err = read_table_stable(page)
                if vals is None:
                    raise RuntimeError(err)
                win, why = window13(vals)
                r = {"unit": info.get("unit", ""), "sector": info.get("sector", ""),
                     "category": info.get("category", ""), "values": vals, "note": why}
                if win:
                    r["window"] = win
                results[name] = r
                latest = sorted(vals)[-1] if vals else ""
                cov.append([name, "ok" if win else "incomplete", len(vals), latest, r["unit"], r["sector"],
                            r["category"], why])
                if i < 3 or not win:
                    shot(page, "03_%s.png" % re.sub(r"\W+", "_", name))
                note("%s: %s%s" % (name, "ok" if win else "incomplete", "" if win else " (%s)" % why))
                failures_in_a_row = 0
            except Exception as e:
                failures_in_a_row += 1
                note("PRODUCT %s FAILED: %s" % (name, e))
                cov.append([name, "failed", 0, "", "", "", "", str(e)[:150]])
                shot(page, "03_failed_%s.png" % re.sub(r"\W+", "_", name))
                if i < 3 and failures_in_a_row >= 3:
                    note("first three products all failed - stopping early")
                    break
                if reloads < 6:
                    reloads += 1
                    try:
                        load(page)
                    except Exception:
                        note("reload failed")
        browser.close()

    write_coverage(cov)
    out = build_output(results, released)
    target = os.path.join(OUT, "diagnostic_result.json") if mode == "diagnostic" else RESULT_FILE
    if mode != "diagnostic" and os.path.exists(RESULT_FILE):
        try:
            prev = len(json.load(open(RESULT_FILE, encoding="utf-8")).get("series", {}))
        except Exception:
            prev = 0
        if prev and len(out["series"]) < 0.5 * prev:
            note("only %d series read vs %d before - NOT overwriting %s" % (len(out["series"]), prev, RESULT_FILE))
            target = os.path.join(OUT, "rejected_result.json")
    if out["series"] or mode == "diagnostic":
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        note("wrote %s: %d series, %d incomplete" % (target, len(out["series"]), len(out["incomplete"])))
    save("log.txt", "\n".join(LOG))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        save("log.txt", "\n".join(LOG) + "\nFATAL:\n" + traceback.format_exc())
