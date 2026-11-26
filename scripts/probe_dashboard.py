#!/usr/bin/env python3
"""
Read the EU column of the EU 'Monthly market prices' dashboard (a Qlik app). Single-file version.

  * Saves the full product dropdown to probe/dashboard/options.txt
  * No eu_products.txt in the repo -> reads the EU column (latest 13 months) for EVERY product in the dropdown
  * eu_products.txt present        -> only the products listed in it (one per line; a line with * = all)
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
STOP_LINES = ("Show/hide container", "No title", "Loading completed")
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
JS_SELECTS = """
() => Array.from(document.querySelectorAll('select')).map((s, i) => (
  {i: i, texts: Array.from(s.options).map(o => o.text.trim())}))
"""
JS_DROPDOWN_HTML = """
() => {
  const out = {selects: Array.from(document.querySelectorAll('select')).map(s => (
    {id: s.id, cls: s.className, n: s.options.length, first: Array.from(s.options).slice(0, 3).map(o => o.text.trim()),
     html: s.parentElement ? s.parentElement.outerHTML.slice(0, 1500) : ''}))};
  const leaf = Array.from(document.querySelectorAll('*')).find(e => e.children.length === 0 && e.textContent.trim() === 'Product Description');
  out.leaf = leaf ? leaf.tagName + '.' + leaf.className : null;
  out.leafHtml = leaf && leaf.parentElement && leaf.parentElement.parentElement ? leaf.parentElement.parentElement.outerHTML.slice(0, 3000) : '';
  return JSON.stringify(out, null, 1);
}
"""


JS_HAS_DASH = """
() => ((document.body ? document.body.innerText : '').replace(/\\s+/g, ' ')).includes('All MS Category')
"""
JS_FRAME_TEXT = """
() => (document.body ? document.body.innerText : '').replace(/\\s+/g, ' ')
"""


class Ctx:
    """Wraps a Playwright page: text, locators and JS run inside the frame that holds the dashboard;
    mouse, keyboard, screenshots and waiting stay on the page."""

    def __init__(self, page):
        self.page = page
        self.frame = page.main_frame

    def __getattr__(self, name):
        return getattr(self.page, name)

    def get_by_text(self, *a, **k):
        return self.frame.get_by_text(*a, **k)

    def inner_text(self, selector):
        return self.frame.inner_text(selector)

    def evaluate(self, js, arg=None):
        return self.frame.evaluate(js) if arg is None else self.frame.evaluate(js, arg)

    def find_dashboard_frame(self, seconds=90):
        for _ in range(max(1, seconds // 2)):
            for fr in self.page.frames:
                try:
                    if fr.evaluate(JS_HAS_DASH):
                        self.frame = fr
                        return True
                except Exception:
                    pass
            self.page.wait_for_timeout(2000)
        return False

    def describe_frames(self):
        lines = []
        for i, fr in enumerate(self.page.frames):
            try:
                txt = fr.evaluate(JS_FRAME_TEXT)
            except Exception as e:
                txt = "<cannot read: %s>" % e
            lines.append("frame %d url=%s name=%r text_len=%d has_All_MS_Category=%s sample=%r"
                         % (i, fr.url, fr.name, len(txt), "All MS Category" in txt, txt[:160]))
        return lines


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


def load(page):
    page.goto(URL, wait_until="domcontentloaded", timeout=120000)
    found = page.find_dashboard_frame(90)
    save("frames.txt", "\n".join(page.describe_frames()) + "\n")
    if not found:
        try:
            save("outer_page.html", page.page.content()[:150000])
        except Exception:
            pass
        raise RuntimeError("dashboard text not found in any frame (see frames.txt)")
    page.wait_for_timeout(5000)
    try:
        for fr in page.page.frames:
            btn = fr.get_by_text("Accept only essential cookies", exact=True)
            for i in range(btn.count()):
                if btn.nth(i).is_visible():
                    btn.nth(i).click(timeout=5000)
                    page.wait_for_timeout(1000)
                    return
    except Exception:
        pass


def product_select(page):
    """-> (index of the product <select> in the dashboard frame, its option texts) or (None, [])"""
    try:
        for info in page.evaluate(JS_SELECTS):
            if "Product Description" in info["texts"] or len(info["texts"]) >= 20:
                return info["i"], info["texts"]
    except Exception:
        pass
    return None, []


def options_from_text(body):
    """fallback: the product list is printed in the page text right after the 'Product Description' line"""
    lines = [ln.strip() for ln in body.split("\n")]
    if "Product Description" not in lines:
        return []
    out = []
    for ln in lines[lines.index("Product Description") + 1:]:
        if not ln or ln in STOP_LINES:
            break
        out.append(ln)
    return out


def read_all_options(page):
    try:
        save("dropdown_html.json", page.evaluate(JS_DROPDOWN_HTML))
    except Exception as e:
        note("dropdown html dump failed: %s" % e)
    idx, texts = product_select(page)
    if idx is not None:
        order = [t for t in texts if t and t != "Product Description"]
        note("options read from the <select> element: %d" % len(order))
    else:
        order = options_from_text(page.inner_text("body"))
        note("no <select> found; options read from the page text: %d" % len(order))
    save("options.txt", "\n".join(order) + "\n")
    note("dropdown options collected: %d (probe/dashboard/options.txt)" % len(order))
    return order


def select_product(page, name):
    cur = current_product(page)
    if cur and cur.lower() == name.lower():
        return True
    idx, texts = product_select(page)
    if idx is None:
        raise RuntimeError("no product <select> in the dashboard frame (see dropdown_html.json)")
    label = next((t for t in texts if t.lower() == name.lower()), None)
    if label is None:
        raise RuntimeError("%r is not in the dropdown" % name)
    page.frame.locator("select").nth(idx).select_option(label=label, force=True, timeout=15000)
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
    mode = "all" if wanted is None or wanted == ["*"] else "list"
    note("mode: %s" % mode)
    results, cov = {}, []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = Ctx(browser.new_page(viewport={"width": 2300, "height": 1400}))
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

        if mode == "all":
            products = options
            if not options:
                note("no options found - nothing to read")
        else:
            lower = {o.lower() for o in options}
            products = [n for n in wanted if not options or n.lower() in lower]
            for n in wanted:
                if options and n.lower() not in lower:
                    note("not in the dropdown (skipped): %s" % n)
                    cov.append([n, "missing", 0, "", "", "", "", "not in the dropdown"])

        failures_in_a_row, reloads = 0, 0
        for i, name in enumerate(products):
            try:
                if not select_product(page, name):
                    raise RuntimeError("product did not become selected")
                info = title_info(page.inner_text("body"))
                vals, err = read_table_stable(page)
                if vals is None:
                    raise RuntimeError(err)
                if i < 2:  # raw cells of the first products, to debug the table reader offline
                    save("cells_%s.json" % re.sub(r"\W+", "_", name), json.dumps(page.evaluate(JS_CELLS)))
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
                if failures_in_a_row >= 5:
                    note("5 products failed in a row - stopping early")
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
    target = RESULT_FILE
    failed = sum(1 for r in cov if r[1] == "failed")
    prev = 0
    if os.path.exists(RESULT_FILE):
        try:
            prev = len(json.load(open(RESULT_FILE, encoding="utf-8")).get("series", {}))
        except Exception:
            prev = 0
    if failed > 0.5 * len(products):
        note("%d of %d products failed - NOT overwriting %s" % (failed, len(products), RESULT_FILE))
        target = os.path.join(OUT, "rejected_result.json")
    elif mode == "all" and prev and len(out["series"]) < 0.5 * prev:
        note("only %d series read vs %d before - NOT overwriting %s" % (len(out["series"]), prev, RESULT_FILE))
        target = os.path.join(OUT, "rejected_result.json")
    if out["series"]:
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
