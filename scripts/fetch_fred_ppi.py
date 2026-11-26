#!/usr/bin/env python3
"""
Fetch the US Producer Price Index by commodity (FRED release 46, Table 9, goods) and keep every
lowest-level series that has recent prices.

What it does
  * reads the whole Table 9 tree through the official FRED API (release/tables), one request per month
    (about 16 requests); falls back to one request per bucket if the API returns a shallow tree
  * lowest level = a series row that has no child rows
  * for each lowest-level series keeps 13 calendar months ending at its latest priced month, only when
    that month is at most 2 months older than the newest month in the table and at least 6 of the 12
    earlier months have a price (the same rule as the EU data)
  * writes price_raw_us.json, probe/fred/us_series_list.csv (every lowest-level series and why it was kept
    or dropped), probe/fred/log.txt and, once, probe/fred/raw_sample.txt (to check the API's response shape)

Needs the environment variable FRED_API_KEY (a GitHub secret). Uses only the Python standard library.
The key is never printed or saved.
"""
import csv
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict

BASE = os.environ.get("FRED_BASE", "https://api.stlouisfed.org").rstrip("/")
KEY = os.environ.get("FRED_API_KEY", "").strip()
PAUSE = float(os.environ.get("FRED_PAUSE", "0.7"))
RELEASE_ID = 46
ROOT = "142100"
BUCKETS = [("142101", "Goods"), ("142105", "Farm Products"), ("142257", "Processed foods and feeds"),
           ("142587", "Textile products and apparel"), ("142744", "Hides, skins, leather, and related products"),
           ("142776", "Fuels and related products and power"), ("142872", "Chemicals and allied products"),
           ("143097", "Rubber and plastic products"), ("143187", "Lumber and Wood Products"),
           ("143284", "Pulp, Paper, and Allied Products"), ("143448", "Metal and Metal Products"),
           ("143799", "Machinery and Equipment"), ("145662", "Agricultural Machinery and Equipment"),
           ("145663", "Construction Machinery and Equipment"), ("145664", "Metalworking Machinery and Equipment"),
           ("145666", "General Purpose Machinery and Equipment"), ("145667", "Electronic Computers and Computer Equipment"),
           ("145668", "Special Industry Machinery and Equipment"), ("145669", "Electrical Machinery and Equipment"),
           ("145670", "Miscellaneous Instruments"), ("145671", "Miscellaneous Machinery"),
           ("144335", "Furniture and Household Durables"), ("144478", "Nonmetallic Mineral Products"),
           ("144620", "Transportation Equipment"), ("144747", "Miscellaneous Products")]
FETCH_MONTHS = 15      # 13 months plus 2 months of slack for series whose latest price is older
N_MONTHS = 13
MAX_LAG = 2
MIN_PRIOR = 6
MIN_ROOT_SERIES = int(os.environ.get("FRED_MIN_ROOT", "1500"))  # fewer than this from the root request means a shallow tree: use buckets
OUT = "probe/fred"
RESULT = "price_raw_us.json"
MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
LOG, CALLS, MODE, BAD_BUCKETS = [], [0], ["root"], set()


def log(msg):
    LOG.append(msg)
    print(msg, flush=True)


def save(name, text):
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, name), "w", encoding="utf-8", newline="") as f:
        f.write(text)


def finish(code):
    save("log.txt", "\n".join(LOG) + "\n")
    sys.exit(code)


def month_idx(p):
    return int(p[:4]) * 12 + int(p[5:7]) - 1


def shift_month(p, n):
    k = month_idx(p) + n
    return "%d/%02d" % (k // 12, k % 12 + 1)


def month_label(p):
    return "%s %s" % (MON[int(p[5:7]) - 1], p[:4])


def label_ym(lab):
    m = re.match(r"^\s*([A-Za-z]{3})[A-Za-z]*\.?\s+(\d{4})\s*$", str(lab or ""))
    if not m:
        return None
    names = [x.lower() for x in MON]
    if m.group(1).lower() not in names:
        return None
    return "%s/%02d" % (m.group(2), names.index(m.group(1).lower()) + 1)


def num(x):
    if x is None:
        return None
    s = str(x).strip().replace(",", "")
    if s in ("", "."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def api(path, params):
    q = dict(params)
    q["api_key"], q["file_type"] = KEY, "json"
    url = "%s/fred/%s?%s" % (BASE, path, urllib.parse.urlencode(q))
    last = "no attempt"
    for attempt in range(5):
        try:
            if CALLS[0]:
                time.sleep(PAUSE)
            CALLS[0] += 1
            req = urllib.request.Request(url, headers={"User-Agent": "wb-commodities-fred/1.0"})
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.loads(r.read().decode("utf-8"))
            if isinstance(data, dict) and data.get("error_code"):
                raise RuntimeError("FRED error %s: %s" % (data.get("error_code"), str(data.get("error_message", ""))[:200]))
            return data
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            last = "HTTP %s %s" % (e.code, body.replace(KEY, "***"))
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(min(60, 2 ** attempt * 2) if PAUSE else 0)
                continue
            raise RuntimeError("FRED said: " + last)
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
            last = "%s: %s" % (type(e).__name__, str(e).replace(KEY, "***")[:200])
            time.sleep(min(60, 2 ** attempt * 2) if PAUSE else 0)
    raise RuntimeError("FRED request failed after retries: " + last)


def walk(obj, parent, out):
    """Collect every element, whether the response is nested, flat, or a mix."""
    if isinstance(obj, dict):
        nxt = parent
        if "element_id" in obj:
            eid = str(obj["element_id"])
            par = obj.get("parent_id")
            par = str(par) if par not in (None, "") else parent
            rec = out.setdefault(eid, {"id": eid, "has_kids": False})
            rec["parent"] = par if par != eid else None
            rec["name"] = obj.get("name", rec.get("name", ""))
            rec["sid"] = obj.get("series_id") or rec.get("sid") or ""
            rec["val"] = obj.get("observation_value", rec.get("val"))
            rec["lab"] = obj.get("observation_date", rec.get("lab"))
            ch = obj.get("children")
            if ch:
                rec["has_kids"] = True
            nxt = eid
        for k, v in obj.items():
            if k in ("element_id", "parent_id"):
                continue
            walk(v, nxt, out)
    elif isinstance(obj, list):
        for v in obj:
            walk(v, parent, out)


def fetch_tree(element, date):
    p = {"release_id": RELEASE_ID, "element_id": element, "include_observation_values": "true"}
    if date:
        p["observation_date"] = date
    data = api("release/tables", p)
    if not os.path.exists(os.path.join(OUT, "raw_sample.txt")):
        save("raw_sample.txt", json.dumps(data)[:150000])
    out = {}
    walk(data, None, out)
    return out


def fetch_all(date):
    if MODE[0] == "root":
        return fetch_tree(ROOT, date)
    merged = {}
    for eid, name in BUCKETS:
        if eid in BAD_BUCKETS:
            continue
        try:
            merged.update(fetch_tree(eid, date))
        except RuntimeError as e:
            BAD_BUCKETS.add(eid)
            log("bucket %s (%s) skipped: %s" % (name, eid, str(e)[:160]))
    return merged


def analyse(el):
    kids = defaultdict(list)
    for e in el.values():
        if e.get("parent") and e["parent"] in el:
            kids[e["parent"]].append(e["id"])
    leaves = [e for e in el.values() if e.get("sid") and not kids.get(e["id"]) and not e["has_kids"]]
    for e in leaves:
        chain, cur, seen = [], e, set()
        while cur and cur["id"] not in seen:
            seen.add(cur["id"])
            chain.append(cur)
            cur = el.get(cur["parent"]) if cur.get("parent") else None
        chain = [c for c in reversed(chain) if c["id"] != ROOT]
        e["bucket"] = chain[0]["name"] if chain else ""
        e["path"] = " > ".join(c["name"] for c in chain)
    return leaves


def calendar_window(values, global_latest):
    have = sorted(p for p, x in values.items() if x is not None)
    if not have:
        return None, None, None, "no prices in the last %d months" % FETCH_MONTHS
    last = have[-1]
    if month_idx(global_latest) - month_idx(last) > MAX_LAG:
        return None, None, None, "stale: latest price %s" % month_label(last)
    months = [shift_month(last, -k) for k in range(N_MONTHS - 1, -1, -1)]
    v = [values.get(m) for m in months]
    prior = sum(1 for x in v[:-1] if x is not None)
    if prior < MIN_PRIOR:
        return None, None, None, "only %d of 12 earlier months priced" % prior
    return months, v, month_label(last), ""


def release_date():
    try:
        d = api("release/dates", {"release_id": RELEASE_ID, "sort_order": "desc", "limit": 5})
        dates = [x.get("date", "") for x in d.get("release_dates", []) if x.get("date")]
        if dates:
            y, m, dd = dates[0].split("-")
            return "%d %s %s" % (int(dd), MON[int(m) - 1], y)
    except Exception as e:
        log("release date not available: %s" % str(e)[:120])
    return ""


def main():
    if not KEY:
        log("FRED_API_KEY is not set. Add it in the repository: Settings > Secrets and variables > Actions > New repository secret.")
        finish(1)
    try:
        tree0 = fetch_all(None)
        n_series = sum(1 for e in tree0.values() if e.get("sid"))
        if MODE[0] == "root" and n_series < MIN_ROOT_SERIES:
            log("root request returned only %d series rows, so the tree looks shallow: switching to one request per bucket" % n_series)
            MODE[0] = "buckets"
            tree0 = fetch_all(None)
        leaves = analyse(tree0)
        log("mode: %s | table rows read: %d | series rows: %d | lowest-level series: %d" % (MODE[0], len(tree0), sum(1 for e in tree0.values() if e.get("sid")), len(leaves)))
        if not leaves:
            log("no lowest-level series found: see probe/fred/raw_sample.txt for the shape of the response")
            finish(1)
        cnt = Counter()
        for e in leaves:
            ym = label_ym(e.get("lab"))
            if ym and num(e.get("val")) is not None:
                cnt[ym] += 1
        if not cnt:
            log("no dated values found in the response: see probe/fred/raw_sample.txt")
            finish(1)
        big = [ym for ym, c in cnt.items() if c >= 20]
        latest = max(big or list(cnt))
        log("newest month with prices: %s (%d lowest-level series have it)" % (month_label(latest), cnt[latest]))
        months = [shift_month(latest, -k) for k in range(FETCH_MONTHS - 1, -1, -1)]
        sids = {e["sid"]: e for e in leaves}
        vals, mismatch = defaultdict(dict), 0
        for ym in months:
            tree = fetch_all("%s-01" % ym.replace("/", "-"))
            for e in tree.values():
                sid = e.get("sid")
                if sid not in sids:
                    continue
                v = num(e.get("val"))
                if v is not None and label_ym(e.get("lab")) != ym:
                    mismatch += 1
                    v = None
                vals[sid][ym] = v
        log("requests made: %d | values whose month label did not match the month asked for (ignored): %d" % (CALLS[0], mismatch))
        released = release_date()
        series, unusable, rows = {}, {}, []
        per_bucket = defaultdict(lambda: [0, 0])
        for e in sorted(sids.values(), key=lambda x: (x["bucket"], x["path"], x["sid"])):
            sid = e["sid"]
            v13m, v13, asof, why = calendar_window(vals.get(sid, {}), latest)
            priced = [p for p in months if vals.get(sid, {}).get(p) is not None]
            per_bucket[e["bucket"]][0] += 1
            if v13m:
                per_bucket[e["bucket"]][1] += 1
                series[sid.lower()] = {"id": sid, "name": e["name"], "bucket": e["bucket"], "path": e["path"], "unit": "Index",
                                       "months": [m.replace("/", "M") for m in v13m], "v": v13, "asOf": asof,
                                       "prior": sum(1 for x in v13[:-1] if x is not None)}
            else:
                unusable[sid] = why
            rows.append([sid, e["name"], e["bucket"], len(priced), month_label(priced[-1]) if priced else "", "usable" if v13m else "unusable", why, e["path"]])
        out = {"source": "US Bureau of Labor Statistics, Producer Price Index by Commodity, not seasonally adjusted (FRED release 46, Table 9)",
               "attribution": "This product uses the FRED API but is not endorsed or certified by the Federal Reserve Bank of St. Louis.",
               "released": released, "asOf": month_label(latest), "series": series, "unusable": unusable}
        reasons = Counter(re.sub(r"[0-9]+", "N", w).replace("latest price ", "latest price: ") if w.startswith("stale") else re.sub(r"\d+", "N", w) for w in unusable.values())
        log("usable series: %d | unusable: %d | released: %s | asOf: %s" % (len(series), len(unusable), released or "unknown", month_label(latest)))
        for r_, c in reasons.most_common():
            log("   unusable (%s): %d" % (r_, c))
        log("per bucket (lowest-level series / usable):")
        for b in sorted(per_bucket):
            log("   %-46s %4d / %4d" % (b, per_bucket[b][0], per_bucket[b][1]))
        sample = sids.get("WPU01110101")
        if sample:
            log("check, Grapefruits WPU01110101: %s" % [vals["WPU01110101"].get(m) for m in months[-6:]])
        prev = 0
        if os.path.exists(RESULT):
            try:
                prev = len(json.load(open(RESULT, encoding="utf-8")).get("series", {}))
            except Exception:
                prev = 0
        os.makedirs(OUT, exist_ok=True)
        with open(os.path.join(OUT, "us_series_list.csv"), "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["series_id", "name", "bucket", "months_priced_of_%d" % FETCH_MONTHS, "latest_priced", "status", "reason", "path"])
            w.writerows(rows)
        if prev and len(series) < 0.5 * prev:
            log("only %d usable series vs %d before - NOT overwriting %s" % (len(series), prev, RESULT))
            save("rejected_result.json", json.dumps(out))
            finish(1)
        with open(RESULT, "w", encoding="utf-8", newline="") as f:
            json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
        log("wrote %s" % RESULT)
        finish(0)
    except SystemExit:
        raise
    except Exception as e:
        log("FAILED: %s" % str(e).replace(KEY, "***")[:400])
        log(traceback.format_exc().replace(KEY, "***")[-1500:])
        finish(1)


if __name__ == "__main__":
    main()
# END OF FILE
