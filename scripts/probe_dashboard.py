#!/usr/bin/env python3
"""
Read the EU column of the EU 'Monthly market prices' dashboard (a Qlik app). Single-file version.

  * Saves the full product dropdown to probe/dashboard/options.txt
  * No eu_products.txt in the repo -> DIAGNOSTIC mode (tests a few products)
  * eu_products.txt present        -> reads the EU column (latest 13 months) for each product listed
                                      (a single line with * means: every product in the dropdown)
  * Writes price_raw_eu.json and probe/dashboard/eu_coverage.csv
"""
import csv
import json
import os
import re
import traceback

from playwright.sync_api import sync_playwright

# ======================================================================== PART 1: settings and pure helpers
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
        return None, "no EU
     

# ======================================================================== PART 2: browser helpers
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
        os.makedirs(OUT, exist_ok=True)
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
    for i in range(loc.count()):
        el = loc.nth(i)
        if el.is_visible():
            return el
    return None


def load(page):
    page.goto(URL, wait_until="domcontentloaded", timeout=120000)
    page.wait_for_function("() => document.body && document.body.innerText.includes('All MS Category')",
                           timeout=120000)
    page.wait_for_timeout(5000)
    try:
        el = find_visible(page, "Accept only essential cookies")
        if el:
            el.click(timeout=5000)
            page.wait_for_timeout(1000)
    except Exception:
        pass


def read_all_options(page):
    label = current_product(page)
    el = find_visible(page, label) if label else None
    if el is None:
        raise RuntimeError("dropdown label %r not found on screen" % label)
    box = el.bounding_box()
    el.click(timeout=20000)
    page.wait_for_timeout(1500)
    shot(page, "02_dropdown_open.png")
    left, right, top = box["x"] - 40, box["x"] + 300, box["y"] + box["height"]
    page.mouse.move(box["x"] + 60, top + 150)
    seen, order, quiet = set(), [], 0
    for _ in range(1200):
        new = 0
        for t in region_texts(page.evaluate(JS_CELLS), left, right, top):
            if t not in seen:
                seen.add(t)
                order.append(t)
                new += 1
        quiet = quiet + 1 if new == 0 else 0
        if quiet >= 6:
            break
        page.mouse.wheel(0, 200)
        page.wait_for_timeout(180)
    note("options read by mouse wheel: %d" % len(order))
    if len(order) < 20:  # fallback: find the scrolling container in the page and scroll it
        note("few options - trying the scroll-container method; mark: " + page.evaluate(JS_MARK_SCROLLER, label))
        for _ in range(800):
            r = page.evaluate(JS_SCROLL_STEP)
            if not r:
                break
            for line in r["text"].split("\n"):
                line = line.strip()
                if line and line != "Product Description" and line not in seen:
                    seen.add(line)
                    order.append(line)
            if r["after"] == r["before"]:
                break
            page.wait_for_timeout(200)
    save("options.txt", "\n".join(order) + "\n")
    note("dropdown
 

# ======================================================================== PART 3: main program

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
# END OF FILE
