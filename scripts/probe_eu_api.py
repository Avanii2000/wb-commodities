#!/usr/bin/env python3
"""Probe the EU Agri-food Data Portal API and save what it returns."""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.tech.ec.europa.eu/agrifood"
OUT = "probe"
MAX_SAVED_ROWS = 3000


def get(path, params=None, tries=3):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote, safe=",")
    last = "unknown error"
    for _ in range(tries):
        try:
            req = urllib.request.Request(
                url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0 (wb-commodities probe)"})
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.status, r.read().decode("utf-8"), url
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "ignore"), url
        except Exception as e:
            last = str(e)
            time.sleep(3)
    return 0, last, url


def save(name, text):
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
        f.write(text)


def main():
    os.makedirs(OUT, exist_ok=True)
    s = ["# EU agri-food API probe", ""]

    st, body, url = get("/v3/api-docs")
    s.append("- OpenAPI spec: HTTP %s" % st)
    if st == 200:
        try:
            spec = json.loads(body)
            rows = []
            for p, ops in sorted(spec.get("paths", {}).items()):
                for _, op in ops.items():
                    tag = (op.get("tags") or [""])[0]
                    params = ", ".join(x.get("name", "") for x in op.get("parameters", []))
                    rows.append("| `%s` | %s | %s | %s |" % (p, tag, op.get("summary", ""), params))
            save("api_paths.md", "| Path | Sector | Summary | Parameters |\n|---|---|---|---|\n" + "\n".join(rows) + "\n")
            s.append("- %d endpoints listed in probe/api_paths.md" % len(rows))
        except Exception as e:
            s.append("- could not parse the spec: %s" % e)
    else:
        s.append("  - response: %s" % body[:300].replace("\n", " "))

    lists = {}
    for name, path in [("products", "/api/fruitAndVegetable/pricesSupplyChain/products"),
                       ("stages", "/api/fruitAndVegetable/pricesSupplyChain/productStages"),
                       ("varieties", "/api/fruitAndVegetable/pricesSupplyChain/varieties")]:
        st, body, _ = get(path)
        s.append("- F&V %s: HTTP %s" % (name, st))
        if st == 200:
            save("fv_%s.json" % name, body)
            try:
                lists[name] = json.loads(body)
                s.append("  - %d values, e.g. %s" % (len(lists[name]), lists[name][:5]))
            except Exception as e:
                s.append("  - could not parse: %s" % e)
        else:
            s.append("  - response: %s" % body[:300].replace("\n", " "))

    fuji = [v for v in lists.get("varieties", []) if isinstance(v, str) and "fuji" in v.lower()]
    s.append("- Fuji varieties found: %s" % fuji)
    if fuji:
        st, body, url = get("/api/fruitAndVegetable/pricesSupplyChain", {"varieties": fuji[0], "years": "2026"})
        s.append("- sample query `%s`: HTTP %s" % (url.replace(BASE, ""), st))
        if st == 200:
            try:
                data = json.loads(body)
                save("fv_fuji_2026.json", json.dumps(data[:MAX_SAVED_ROWS], indent=1))
                s.append("  - %d rows (first %d saved)" % (len(data), min(len(data), MAX_SAVED_ROWS)))
                for key in ("memberStateCode", "productStage", "periodType", "market", "unit"):
                    vals = sorted({str(r.get(key)) for r in data})
                    s.append("  - distinct %s (%d): %s" % (key, len(vals), vals[:40]))
                s.append("  - first 5 rows:")
                for r in data[:5]:
                    s.append("    - `%s`" % json.dumps(r, ensure_ascii=False))
            except Exception as e:
                s.append("  - could not parse the sample: %s" % e)
        else:
            s.append("  - response: %s" % body[:300].replace("\n", " "))

    save("summary.md", "\n".join(s) + "\n")
    print("\n".join(s))


if __name__ == "__main__":
    main()
