#!/usr/bin/env python3
"""
Fetch the OECD-FAO Agricultural Outlook projected PRODUCER PRICES (annual, all years the dataset holds)
for three regions: European Union, United States and World, from the OECD Data Explorer API.

How it works
  1. Lists the Outlook editions the OECD publishes (dataflows DSD_AGR@DF_OUTLOOK_<from>_<to>) and picks the newest
     (set OUTLOOK_EDITION=2021_2030 to pin an older one; the edition in your original URL is 2021_2030).
  2. Asks the OECD whether the dataset has changed (contentconstraint query, the OECD's recommended cheap check).
     If it has not changed since the last run, the run ends without downloading anything (FORCE=1 overrides).
  3. Reads the dataflow structure to learn the dimension order and the area codes, then downloads ONE csv with
     labels for the three regions, measure PP (producer price) and every commodity the dataset holds for them.
     If the structure cannot be read it downloads all areas and picks the three regions by name instead.
  4. Writes price_raw_outlook.json, probe/outlook/log.txt, probe/outlook/coverage.csv, probe/outlook/structure.txt and,
     once, probe/outlook/raw_sample.csv (to check the shape of the response).

Uses only the Python standard library. No key is needed. Outputs contain no timestamps, so an unchanged run commits nothing.
"""
import csv
import hashlib
import io
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter

BASE = os.environ.get("OECD_BASE", "https://sdmx.oecd.org/public/rest").rstrip("/")
AGENCY = "OECD.TAD.ATM"
FALLBACK_FLOW = "DSD_AGR@DF_OUTLOOK_2026_2035"      # newest edition known when this was written: used only on a first run when editions cannot be listed
EDITION = (os.environ.get("OUTLOOK_EDITION", "latest").strip() or "latest")
MEASURE = os.environ.get("OUTLOOK_MEASURE", "PP").strip() or "PP"   # PP = Producer price
FORCE = os.environ.get("FORCE", "").strip() not in ("", "0")
PAUSE = float(os.environ.get("OECD_PAUSE", "1.0"))
REGIONS = [("Europe", "European Union"), ("US", "United States"), ("World", "World")]
USER_COMMODITIES = ["CPC_0112", "CPC_0113", "CPC_0114T0119", "CPC_0141", "CPC_0143", "CPC_0144", "CPC_0151T0155", "CPC_017",
                    "CPC_01801", "CPC_01802", "CPC_022", "CPC_04", "CPC_21233", "CPC_21525", "CPC_216", "CPC_21920",
                    "CPC_22211", "CPC_2222T2223", "CPC_2224", "CPC_2225", "CPC_2226", "CPC_223", "CPC_23210", "CPC_235",
                    "CPC_2351", "CPC_2352", "CPC_2354", "CPC_EX_DDG", "CPC_EX_BV", "CPC_EX_PK", "CPC_EX_PT", "CPC_EX_SH",
                    "CPC_EX_222121", "CPC_EX_222122", "CPC_35491", "CPC_EX_35492", "CPC_EX_FHC", "CPC_EX_AC",
                    "CPC_EX_AG_COM", "CPC_0111"]
FALLBACK_DIMS = ["REF_AREA", "FREQ", "COMMODITY", "MEASURE", "UNIT_MEASURE", "D6"]   # layout of the original URL: USA.A.<commodities>.PP..
OUT = "probe/outlook"
RESULT = "price_raw_outlook.json"
FORMAT_VERSION = 1
LOG, CALLS = [], [0]
CODE_RX = re.compile(r"^[A-Z][A-Z0-9_]*$")


class HttpFail(Exception):
    def __init__(self, code, msg):
        Exception.__init__(self, msg)
        self.code = code


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


def get(url, accept=None, tries=5, timeout=300):
    last = "no attempt"
    for attempt in range(tries):
        try:
            if CALLS[0]:
                time.sleep(PAUSE)
            CALLS[0] += 1
            headers = {"User-Agent": "wb-commodities-outlook/1.0"}
            if accept:
                headers["Accept"] = accept
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
                raw = r.read()
            return raw.decode("utf-8-sig", "replace")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:200].replace("\n", " ")
            except Exception:
                pass
            last = "HTTP %s %s" % (e.code, body)
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                ra = e.headers.get("Retry-After") if e.headers else None
                wait = int(ra) if ra and ra.isdigit() else 2 ** attempt * 5
                time.sleep(min(wait, 120) if PAUSE else 0)
                continue
            raise HttpFail(e.code, last)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = "%s: %s" % (type(e).__name__, str(e)[:160])
            time.sleep(min(2 ** attempt * 5, 60) if PAUSE else 0)
    raise HttpFail(0, "request failed after retries: " + last)


def local(tag):
    return tag.split("}")[-1]


def parse_dataflows(txt):
    """Returns {(from, to): (agency, dataflow id, version, name)} for every Outlook edition in a dataflow listing.
    An edition is recognised by its id (..DF_OUTLOOK_<from>_<to>) or by its name ('OECD-FAO Agricultural Outlook <from>-<to>')."""
    found = {}
    try:
        root = ET.fromstring(txt)
    except ET.ParseError:
        root = None
    if root is not None:
        by_name = {}
        for el in root.iter():
            if local(el.tag) != "Dataflow" or not el.get("id"):
                continue
            fid, ag, ver = el.get("id"), el.get("agencyID") or AGENCY, el.get("version") or "1.0"
            names = [(n.text or "").strip() for n in el if local(n.tag) == "Name"]
            m = re.search(r"DF_OUTLOOK_(\d{4})_(\d{4})", fid)
            if m:
                found[(int(m.group(1)), int(m.group(2)))] = (ag, fid, ver, names[0] if names else "")
                continue
            for nm in names:
                m = re.match(r"^OECD-FAO Agricultural Outlook\s+(\d{4})\s*[-\u2013]\s*(\d{4})\s*$", nm)
                if m:
                    by_name[(int(m.group(1)), int(m.group(2)))] = (ag, fid, ver, nm)
        for k, v in by_name.items():
            found.setdefault(k, v)
        return found
    for m in re.finditer(r"<[^<>]*DF_OUTLOOK_\d{4}_\d{4}[^<>]*>", txt):
        tag = m.group(0)
        idm = re.search(r'\bid="(DSD_AGR@DF_OUTLOOK_(\d{4})_(\d{4}))"', tag)
        if idm:
            ver = re.search(r'\bversion="([0-9.]+)"', tag)
            found[(int(idm.group(2)), int(idm.group(3)))] = (AGENCY, idm.group(1), ver.group(1) if ver else "1.0", "")
    if not found:   # not XML at all: look for the ids anywhere in the text
        for m in re.finditer(r"DSD_AGR@DF_OUTLOOK_(\d{4})_(\d{4})", txt):
            found[(int(m.group(1)), int(m.group(2)))] = (AGENCY, m.group(0), "1.0", "")
    return found


def list_editions():
    """Looks in the agency's dataflows first, then in the whole catalogue."""
    for label, url in (("agency listing", "%s/dataflow/%s" % (BASE, AGENCY)), ("whole catalogue", "%s/dataflow/all" % BASE)):
        try:
            found = parse_dataflows(get(url, tries=3))
        except HttpFail as e:
            log("could not read the %s (%s)" % (label, str(e)[:100]))
            continue
        if found:
            return found
        log("no Outlook edition found in the %s" % label)
    return {}


def choose_edition(found):
    """Returns (agency, dataflow id, version, (from, to)) of the newest edition, or of the one asked for."""
    if not found:
        return None
    if EDITION.lower() == "latest":
        k = max(found)
        return found[k][:3] + (k,)
    want = re.sub(r"[^0-9]+", "_", EDITION).strip("_")
    for k, v in found.items():
        if "%d_%d" % k == want:
            return v[:3] + (k,)
    return None


def read_structure(flow, ver):
    """Dimension order (without time) and the REF_AREA / MEASURE code names, from the dataflow structure."""
    txt = get("%s/dataflow/%s/%s/%s?references=all" % (BASE, AGENCY, flow, ver), tries=3)
    root = ET.fromstring(txt)
    dims, dim_cl, codelists = [], {}, {}
    for el in root.iter():
        t = local(el.tag)
        if t == "DimensionList":
            for d in el:
                if local(d.tag) == "Dimension":
                    pos = int(d.get("position") or (len(dims) + 1))
                    dims.append((pos, d.get("id")))
                    for r in d.iter():
                        if local(r.tag) == "Ref" and r.get("id") and r.get("id").startswith("CL_"):
                            dim_cl[d.get("id")] = r.get("id")
        elif t == "Codelist":
            names = {}
            for c in el:
                if local(c.tag) == "Code":
                    nm = ""
                    for n in c:
                        if local(n.tag) == "Name":
                            lang = n.get("{http://www.w3.org/XML/1998/namespace}lang")
                            if not nm or lang in (None, "en"):
                                nm = (n.text or "").strip()
                    names[c.get("id")] = nm
            codelists[el.get("id")] = names
    dims = [d for _, d in sorted(dims)]
    if not dims:
        return None
    return {"dims": dims, "areas": codelists.get(dim_cl.get("REF_AREA", ""), {}), "measures": codelists.get(dim_cl.get("MEASURE", ""), {})}


def region_key(label):
    s = (label or "").strip().lower()
    for key, start in REGIONS:
        if s.startswith(start.lower()):
            return key
    return None


def pick_best(cands):
    """cands: list of (code, label). Prefer the 27-member EU, then the shortest name."""
    return sorted(cands, key=lambda c: (0 if "27" in c[1] or "27" in c[0] else 1, len(c[1]), c[0]))[0]


def build_key(dims, areas, measure):
    parts = []
    for d in dims:
        parts.append("+".join(areas) if d == "REF_AREA" and areas else "A" if d == "FREQ" else measure if d == "MEASURE" else "")
    return ".".join(parts)


def looks_like_csv(text):
    first = text.lstrip().split("\n", 1)[0].upper()
    return "REF_AREA" in first and "TIME_PERIOD" in first and "OBS_VALUE" in first


def fetch_csv(flow, ver, key):
    path = "%s/data/%s,%s,%s/%s" % (BASE, AGENCY, flow, ver, key)
    errors = []
    for how, url, accept in (("format=csvfilewithlabels", path + "?dimensionAtObservation=AllDimensions&format=csvfilewithlabels", None),
                             ("Accept labels=both", path + "?dimensionAtObservation=AllDimensions", "application/vnd.sdmx.data+csv; charset=utf-8; labels=both")):
        try:
            txt = get(url, accept=accept)
            if looks_like_csv(txt):
                return txt, how
            errors.append("%s: the answer was not csv (%s)" % (how, txt[:80].replace("\n", " ")))
        except HttpFail as e:
            errors.append("%s: %s" % (how, e))
            if e.code in (404,) and "No" in str(e) and "Result" in str(e):
                break
    raise RuntimeError("could not download the data: " + " | ".join(errors))


def cell(row, idx, lab_idx, name):
    """Returns (code, label) for a dimension, from separate label columns or from 'CODE: Label' cells."""
    i = idx.get(name)
    if i is None or i >= len(row):
        return "", ""
    v = row[i].strip()
    j = lab_idx.get(name)
    if j is not None and j < len(row):
        return v, row[j].strip()
    m = re.match(r"^\s*([^:]+?)\s*:\s*(.*)$", v)
    return (m.group(1), m.group(2)) if m else (v, "")


def parse_csv(text):
    rd = csv.reader(io.StringIO(text))
    header = next(rd)
    names = [h.split(":", 1)[0].strip() for h in header]
    idx, lab = {}, {}
    for i, n in enumerate(names):
        if CODE_RX.match(n) and n not in idx:
            idx[n] = i
            if i + 1 < len(names) and not CODE_RX.match(names[i + 1]):
                lab[n] = i + 1
    need = [n for n in ("REF_AREA", "COMMODITY", "MEASURE", "TIME_PERIOD", "OBS_VALUE") if n not in idx]
    if need:
        raise RuntimeError("the csv has no column(s) %s; header: %s" % (", ".join(need), ",".join(header)[:300]))
    unit = next((n for n in names if n.startswith("UNIT_MEASURE")), None) or next((n for n in names if n.startswith("UNIT") and n in idx), None)
    rows = []
    for r in rd:
        if len(r) <= max(idx["OBS_VALUE"], idx["TIME_PERIOD"]):
            continue
        ym = re.match(r"\s*(\d{4})", r[idx["TIME_PERIOD"]])
        if not ym:
            continue
        v = r[idx["OBS_VALUE"]].strip().replace(",", "")
        try:
            val = float(v) if v not in ("", ".", "NaN", "nan") else None
        except ValueError:
            val = None
        rows.append({"area": cell(r, idx, lab, "REF_AREA"), "comm": cell(r, idx, lab, "COMMODITY"), "measure": cell(r, idx, lab, "MEASURE"),
                     "unit": cell(r, idx, lab, unit) if unit else ("", ""), "year": int(ym.group(1)), "val": val,
                     "status": r[idx["OBS_STATUS"]].strip() if "OBS_STATUS" in idx and idx["OBS_STATUS"] < len(r) else ""})
    return rows, header


def parse_overrides():
    """OUTLOOK_MEASURES='World=PW,Europe=PP' lets a region use another measure than the default."""
    out = {}
    for part in os.environ.get("OUTLOOK_MEASURES", "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            for rk, _ in REGIONS:
                if rk.lower() == k.strip().lower() and v.strip():
                    out[rk] = v.strip()
    return out


def collect(rows, code_region, measure_for):
    """Series per (region, area code, commodity, unit); the area code is kept until the best code per region is chosen."""
    series, years, dup, statuses, names = {}, set(), 0, Counter(), {}
    for r in rows:
        rk = code_region.get(r["area"][0])
        if not rk or r["measure"][0] != measure_for.get(rk, MEASURE):
            continue
        names[r["comm"][0]] = r["comm"][1] or names.get(r["comm"][0], "")
        k = "%s|%s|%s|%s" % (rk, r["area"][0], r["comm"][0], r["unit"][0])
        sr = series.setdefault(k, {"region": rk, "code": r["area"][0], "commodity": r["comm"][0], "name": r["comm"][1], "unit": r["unit"][1],
                                   "unitCode": r["unit"][0], "measure": r["measure"][0], "vals": {}})
        if r["year"] in sr["vals"] and sr["vals"][r["year"]] != r["val"]:
            dup += 1
            continue
        sr["vals"][r["year"]] = r["val"]
        years.add(r["year"])
        statuses[r["status"] or "(none)"] += 1
    return series, sorted(years), dup, statuses, names


def discover(flow, ver, dims, st, lacking, cands):
    """For regions with no data under the chosen measure: which price-type measures exist for their areas?"""
    pm = {c: n for c, n in st["measures"].items() if "price" in n.lower()}
    log("price-type measures in this edition: %s" % ("; ".join("%s=%s" % kv for kv in sorted(pm.items())) or "none"))
    areas = sorted({code for rk in lacking for code, _ in cands[rk]})
    if not pm or not areas:
        return
    text, how = fetch_csv(flow, ver, build_key(dims, areas, "+".join(sorted(pm))))
    rows, _ = parse_csv(text)
    agg = {}
    for r in rows:
        a = agg.setdefault((r["area"][0], r["area"][1], r["measure"][0], r["measure"][1]), {"units": set(), "comms": {}})
        a["units"].add(r["unit"][1] or r["unit"][0])
        a["comms"][r["comm"][0]] = r["comm"][1]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["area_code", "area", "measure_code", "measure", "units", "commodities", "examples"])
    for (ac, an, mc, mn), a in sorted(agg.items()):
        w.writerow([ac, an, mc, mn, " | ".join(sorted(a["units"])), len(a["comms"]), "; ".join(list(a["comms"].values())[:4])])
    save("discovery.csv", buf.getvalue())
    log("discovery download: %d rows | areas x price measures with data: %d (see discovery.csv)" % (len(rows), len(agg)))
    for rk in lacking:
        codes = {c for c, _ in cands[rk]}
        hits = sorted(((k, v) for k, v in agg.items() if k[0] in codes), key=lambda kv: -len(kv[1]["comms"]))[:6]
        for (ac, an, mc, mn), v in hits:
            log("   %s has data in %s (%s): %s - %d commodities, %s" % (rk, mc, mn, ac, len(v["comms"]), " | ".join(sorted(v["units"]))))
        if not hits:
            log("   %s has no data in any price-type measure" % rk)


def main():
    global AGENCY
    try:
        prev = {}
        if os.path.exists(RESULT):
            try:
                prev = json.load(open(RESULT, encoding="utf-8"))
            except Exception:
                prev = {}
        found = {}
        try:
            found = list_editions()
        except Exception as e:
            log("WARNING: could not list the Outlook editions (%s)" % str(e)[:140])
        pick = choose_edition(found)
        if pick is None:
            if found:
                log("edition %s is not offered by the OECD; available: %s" % (EDITION, ", ".join("%d-%d" % k for k in sorted(found))))
                finish(1)
            pf = prev.get("dataflow", "").split(",")
            em = re.match(r"^(\d{4})-(\d{4})$", prev.get("edition", ""))
            if EDITION.lower() != "latest":
                want = re.sub(r"[^0-9]+", "_", EDITION).strip("_").split("_")
                flow, ver, ed = "DSD_AGR@DF_OUTLOOK_%s" % "_".join(want[:2]), "1.0", (int(want[0]), int(want[1]))
                log("WARNING: the editions could not be listed: using the edition you asked for, %s" % flow)
            elif len(pf) == 3 and em:
                AGENCY, flow, ver, ed = pf[0], pf[1], pf[2], (int(em.group(1)), int(em.group(2)))
                log("WARNING: the editions could not be listed: staying with the stored edition %s" % prev["edition"])
            else:
                flow, ver, ed = FALLBACK_FLOW, "1.0", (2026, 2035)
                log("WARNING: the editions could not be listed and nothing is stored yet: trying %s" % flow)
        else:
            AGENCY, flow, ver, ed = pick
        if EDITION.lower() == "latest" and prev.get("projectionStart") and ed[0] < prev["projectionStart"]:
            log("WARNING: the newest edition found (%d-%d) is older than the stored one (%s): leaving %s untouched" % (ed[0], ed[1], prev.get("edition"), RESULT))
            finish(1)
        edition = "%d-%d" % ed
        log("editions listed by the OECD: %s | using %s (%s, agency %s)" % (", ".join("%d-%d" % k for k in sorted(found)) or "none listed", edition, flow, AGENCY))
        newer = [k for k in found if k > ed]
        if newer:
            log("NOTE: newer edition(s) exist: %s" % ", ".join("%d-%d" % k for k in sorted(newer)))
        # cheap change check
        chash = ""
        try:
            chash = hashlib.sha256(get("%s/contentconstraint/%s/CR_A_%s/" % (BASE, AGENCY, flow), tries=2).encode("utf-8")).hexdigest()[:16]
        except Exception as e:
            log("change check not available (%s): downloading anyway" % str(e)[:100])
        pm = prev.get("meta", {})
        if chash and not FORCE and pm.get("constraintHash") == chash and pm.get("formatVersion") == FORMAT_VERSION and prev.get("dataflow", "").find(flow) >= 0 and prev.get("series"):
            print("OECD Outlook %s is unchanged since the last run: nothing to download" % edition, flush=True)
            sys.exit(0)
        # structure
        st = None
        try:
            st = read_structure(flow, ver)
        except Exception as e:
            log("WARNING: could not read the dataflow structure (%s): using the original URL's layout and picking regions by name" % str(e)[:140])
        dims = st["dims"] if st else FALLBACK_DIMS
        overrides = parse_overrides()
        measure_for = {rk: overrides.get(rk, MEASURE) for rk, _ in REGIONS}
        if overrides:
            log("measure overrides: %s" % ", ".join("%s=%s" % kv for kv in sorted(overrides.items())))
        cands = {rk: [] for rk, _ in REGIONS}
        if st and st["areas"]:
            for rk, _ in REGIONS:
                cands[rk] = sorted((code, nm) for code, nm in st["areas"].items() if region_key(nm) == rk)
                if len(cands[rk]) > 1:
                    log("several areas look like %s: %s" % (rk, "; ".join("%s=%s" % x for x in cands[rk])))
        groups = {}
        for rk, _ in REGIONS:
            groups.setdefault(measure_for[rk], []).append(rk)
        rows, areas_seen = [], {}
        for meas, rks in groups.items():
            areas = [code for rk in rks for code, _ in cands[rk]]
            key = build_key(dims, areas, meas)
            mname = (st or {}).get("measures", {}).get(meas, "")
            log("dimensions: %s | key: %s | measure %s %s" % (".".join(dims), key if len(key) < 140 else key[:137] + "...", meas, ("(" + mname + ")") if mname else ""))
            try:
                text, how = fetch_csv(flow, ver, key)
            except RuntimeError as e:
                if len(groups) > 1 and rows:
                    log("WARNING: nothing could be downloaded for measure %s (%s)" % (meas, str(e)[:120]))
                    continue
                raise
            if not os.path.exists(os.path.join(OUT, "raw_sample.csv")):
                save("raw_sample.csv", "\n".join(text.split("\n")[:60]) + "\n")
            part, header = parse_csv(text)
            rows += part
            log("downloaded with %s | rows: %d | requests made: %d" % (how, len(part), CALLS[0]))
        if not rows:
            log("the download held no observations")
            finish(1)
        for r in rows:
            areas_seen[r["area"][0]] = r["area"][1]
        if not any(cands.values()):   # structure unknown: regions by name from what was downloaded
            for rk, _ in REGIONS:
                cands[rk] = sorted((code, nm) for code, nm in areas_seen.items() if region_key(nm) == rk)
            log("regions picked by name from %d areas in the download: %s" % (len(areas_seen), "; ".join("%s=%s" % (rk, ",".join(c for c, _ in cands[rk])) for rk, _ in REGIONS if cands[rk]) or "none"))
        for rk, nm in REGIONS:
            if not cands[rk]:
                log("WARNING: no area named %s was found" % nm)
        code_region = {code: rk for rk in cands for code, _ in cands[rk]}
        series, years, dup, statuses, comm_names = collect(rows, code_region, measure_for)
        count = Counter()
        for sr in series.values():
            if any(v is not None for v in sr["vals"].values()):
                count[(sr["region"], sr["code"])] += 1
        chosen, lacking = {}, []
        for rk, nm in REGIONS:
            if not cands[rk]:
                continue
            withdata = [c for c in cands[rk] if count[(rk, c[0])] > 0]
            if withdata:
                exact = dict(REGIONS)[rk].lower()   # the name shown in the Data Explorer filter comes first
                chosen[rk] = sorted(withdata, key=lambda c: (0 if c[1].strip().lower() == exact else 1, -count[(rk, c[0])], 0 if "27" in c[1] or "27" in c[0] else 1, len(c[1]), c[0]))[0]
                if len(cands[rk]) > 1:
                    log("%s: areas with %s data: %s -> using %s" % (rk, measure_for[rk], ", ".join("%s=%d series" % (c, count[(rk, c)]) for c, _ in cands[rk] if count[(rk, c)]), chosen[rk][0]))
            else:
                lacking.append(rk)
        if not chosen:
            save("structure.txt", "areas in the download:\n" + "\n".join("%s: %s" % kv for kv in sorted(areas_seen.items())) + "\n")
            log("no producer-price series for any of the three regions")
            finish(1)
        for rk in lacking:
            log("WARNING: %s has no %s series in this edition (areas tried: %s), so it is missing from the output" % (rk, measure_for[rk], ", ".join(c for c, _ in cands[rk])))
        if lacking and st and st["measures"]:
            try:
                discover(flow, ver, dims, st, lacking, cands)
            except Exception as e:
                log("discovery could not be completed (%s)" % str(e)[:140])
        order = {rk: i for i, (rk, _) in enumerate(REGIONS)}
        out_series, units = {}, Counter()
        for k in sorted(series, key=lambda x: (order[series[x]["region"]], series[x]["commodity"], series[x]["unitCode"])):
            sr = series[k]
            if sr["region"] not in chosen or sr["code"] != chosen[sr["region"]][0] or not any(v is not None for v in sr["vals"].values()):
                continue
            out_series["%s|%s|%s" % (sr["region"], sr["commodity"], sr["unitCode"])] = {"region": sr["region"], "commodity": sr["commodity"], "name": sr["name"], "unit": sr["unit"],
                                                                                       "unitCode": sr["unitCode"], "measure": sr["measure"], "v": [sr["vals"].get(y) for y in years]}
            units[(sr["region"], sr["unit"] or sr["unitCode"])] += 1
        missing = {rk: [c for c in USER_COMMODITIES if not any(v["region"] == rk and v["commodity"] == c for v in out_series.values())] for rk in chosen}
        log("series kept: %d | years %d-%d (%d) | observations with a status: %s | conflicting duplicates ignored: %d" % (len(out_series), years[0], years[-1], len(years), dict(statuses), dup))
        for rk, _ in REGIONS:
            if rk in chosen:
                n = sum(1 for v in out_series.values() if v["region"] == rk)
                log("   %-7s code %-10s %-24s series %3d | units: %s | of the %d commodities in your URL, missing: %s" % (rk, chosen[rk][0], chosen[rk][1][:24], n, "; ".join("%s x%d" % (u, c) for (r2, u), c in sorted(units.items()) if r2 == rk), len(USER_COMMODITIES), ", ".join(missing[rk]) or "none"))
        extra = sorted({v["commodity"] for v in out_series.values()} - set(USER_COMMODITIES))
        if extra:
            log("commodities in the data that are not in your original URL: %s" % ", ".join(extra))
        result = {"source": "OECD-FAO Agricultural Outlook %s (OECD Data Explorer): projected prices" % edition, "dataflow": "%s,%s,%s" % (AGENCY, flow, ver),
                  "edition": edition, "projectionStart": ed[0], "measure": MEASURE, "measures": {rk: measure_for[rk] for rk in chosen}, "noData": lacking,
                  "meta": {"constraintHash": chash, "formatVersion": FORMAT_VERSION},
                  "years": years, "regions": {rk: {"code": chosen[rk][0], "label": chosen[rk][1]} for rk, _ in REGIONS if rk in chosen},
                  "commodities": {c: comm_names.get(c, "") for c in sorted({v["commodity"] for v in out_series.values()})}, "series": out_series, "missing": missing}
        # coverage and structure files
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["commodity_code", "commodity"] + [rk for rk, _ in REGIONS if rk in chosen] + ["units"])
        for c in sorted({v["commodity"] for v in out_series.values()} | set(USER_COMMODITIES)):
            cellv, us = [], set()
            for rk, _ in REGIONS:
                if rk not in chosen:
                    continue
                m = [v for v in out_series.values() if v["region"] == rk and v["commodity"] == c]
                us.update(x["unit"] or x["unitCode"] for x in m)
                cellv.append("%d-%d" % (min(y for y, a in zip(years, m[0]["v"]) if a is not None), max(y for y, a in zip(years, m[0]["v"]) if a is not None)) if m else "")
            w.writerow([c, comm_names.get(c, "")] + cellv + [" | ".join(sorted(us))])
        save("coverage.csv", buf.getvalue())
        save("structure.txt", "dimensions: %s\n\nmeasures in this edition (%d):\n%s\n\ncandidate areas per region:\n%s\n\nareas in the download (%d):\n%s\n" % (
            ".".join(dims), len((st or {}).get("measures", {})), "\n".join("  %s: %s" % kv for kv in sorted((st or {}).get("measures", {}).items())),
            "\n".join("  %s: %s" % (rk, "; ".join("%s=%s" % c for c in cands[rk]) or "none") for rk, _ in REGIONS), len(areas_seen), "\n".join("  %s: %s" % kv for kv in sorted(areas_seen.items()))))
        same_flow = flow in prev.get("dataflow", "")
        if prev.get("series") and same_flow and len(out_series) < 0.5 * len(prev["series"]):
            log("only %d series vs %d before - NOT overwriting %s" % (len(out_series), len(prev["series"]), RESULT))
            save("rejected_result.json", json.dumps(result))
            finish(1)
        if prev.get("series") and not same_flow:
            log("NEW EDITION: switching from %s to %s (%d series before, %d now)" % (prev.get("edition", "an earlier edition"), edition, len(prev["series"]), len(out_series)))
        with open(RESULT, "w", encoding="utf-8", newline="") as f:
            json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
        log("wrote %s" % RESULT)
        finish(0)
    except SystemExit:
        raise
    except Exception as e:
        log("FAILED: %s" % str(e)[:500])
        log(traceback.format_exc()[-1500:])
        finish(1)


if __name__ == "__main__":
    main()
# END OF FILE
