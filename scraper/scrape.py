#!/usr/bin/env python3
"""
Scrape PM2.5 source apportionment from the AIRWISE-Delhi homepage
(Air Quality Early Warning System for Delhi, IITM Pune / MoES):
https://ews.tropmet.res.in/index.php

Two datasets are embedded in the homepage HTML as JavaScript arrays:

  * DSS - DELHI-NCR  (`var items = [...]`)  - one day, 29 sources, % share,
    local/non-local flag. The date is the card timestamp, e.g. "Wed, 11 Nov".
  * RTSA - Delhi     (`var rtData = [...]`) - rolling ~6 days, % share and
    +/- SD per source, a daily total, and a sulphate/nitrate/ammonia split of
    the "Secondary Inorganic" share. A date tab ending in "*" is a partial day.

It also reads the DSS page itself, https://ews.tropmet.res.in/dss/index.php,
which holds two Chart.js datasets the homepage does not show in full:

  * Daily-mean local and non-local fractional contribution to PM2.5 in Delhi
    for the model run's four days (`const data = {...}`), 29 sources.
  * Season-to-date daily-mean stubble-burning contribution (`const data2 = {...}`).

That page stalls for about two minutes before sending its data, so it is
fetched with a long read timeout. The homepage and the DSS page are handled
independently: a failure on one does not stop the other from being saved.

Values are stored exactly as published. Each run appends a line to
logs/run_log.csv; data files only change when the site's values change.
Exit code is non-zero if fetching or parsing either page fails.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html as htmllib
import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

URL = "https://ews.tropmet.res.in/index.php"
DSS_PAGE_URL = "https://ews.tropmet.res.in/dss/index.php"
IST = timezone(timedelta(hours=5, minutes=30))
USER_AGENT = (
    "ews-scraper/1.0 (research data archive; "
    "+https://github.com/mrafiuddin50/ews-scraper)"
)
ROOT = Path(__file__).resolve().parent.parent

DSS_FIELDS = ["sa_date", "source", "pct", "local", "scraped_at_utc"]
DSS_SUMMARY_FIELDS = [
    "sa_date", "local_pct", "nonlocal_pct", "dominant_source", "dominant_pct",
    "n_sources", "sum_pct", "scraped_at_utc",
]
RTSA_FIELDS = ["date", "source", "pct", "sd", "total", "partial", "scraped_at_utc"]
RTSA_SUB_FIELDS = ["date", "component", "pct", "sd", "partial", "scraped_at_utc"]
DSS_FC_FIELDS = [
    "run_date", "forecast_date", "lead_day", "source", "pct", "local", "scraped_at_utc",
]
STUBBLE_FIELDS = ["date", "stubble_pct", "scraped_at_utc"]
LOG_FIELDS = [
    "run_utc", "run_ist", "status", "dss_date", "dss_changed",
    "rtsa_dates", "rtsa_changed",
    "dsspage_run_date", "dsspage_changed", "stubble_last_date", "stubble_changed",
    "message",
]


class ScrapeError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #
def fetch(url: str = URL, retries: int = 3, timeout=60) -> str:
    last = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
            r.raise_for_status()
            r.encoding = r.encoding or "utf-8"
            return r.text
        except requests.RequestException as exc:
            last = exc
            print(f"fetch attempt {attempt}/{retries} failed: {exc}", file=sys.stderr)
            if attempt < retries:
                time.sleep(20 * 2 ** (attempt - 1))  # 20 s, 40 s
    raise ScrapeError(f"could not fetch {url}: {last}")


# --------------------------------------------------------------------------- #
# Parse
# --------------------------------------------------------------------------- #
_decoder = json.JSONDecoder()


def _js_arrays(html: str, var: str):
    """Yield every JSON array assigned as `var <var> = [...]` in the page."""
    for m in re.finditer(rf"\bvar\s+{re.escape(var)}\s*=\s*(?=\[)", html):
        try:
            value, _ = _decoder.raw_decode(html, m.end())
        except json.JSONDecodeError:
            continue
        if isinstance(value, list):
            yield value


def _strip_tags(s: str) -> str:
    return htmllib.unescape(re.sub(r"<[^>]+>", " ", s)).strip()


def infer_date(label: str, today: date) -> date:
    """'Wed, 11 Nov' -> date, choosing the year whose weekday matches."""
    m = re.search(r"([A-Za-z]{3})[a-z]*,?\s+(\d{1,2})\s+([A-Za-z]{3})", label)
    if not m:
        raise ScrapeError(f"unrecognised DSS date label: {label!r}")
    dow, day, mon = m.group(1).title(), int(m.group(2)), m.group(3).title()
    candidates = []
    for year in (today.year + 1, today.year, today.year - 1):
        try:
            d = datetime.strptime(f"{day} {mon} {year}", "%d %b %Y").date()
        except ValueError:
            continue
        if d <= today + timedelta(days=7):
            candidates.append(d)
    matching = [d for d in candidates if d.strftime("%a") == dow]
    pool = matching or candidates
    if not pool:
        raise ScrapeError(f"could not place DSS date label in a year: {label!r}")
    return max(pool)


def parse_dss(html: str, today: date) -> dict:
    items = None
    for arr in _js_arrays(html, "items"):
        if arr and all(isinstance(i, dict) and {"name", "y", "local"} <= i.keys() for i in arr):
            items = arr
            break
    if items is None:
        raise ScrapeError("DSS array (`var items`) not found or malformed")

    # Anchor on the card's HTML element; the class name also appears in CSS
    # earlier in the page, and other cards have their own .ews-ts timestamps.
    m = re.search(
        r'<div[^>]*class="[^"]*\bews-outlook-card\b[^"]*"[^>]*>.*?class="ews-ts"[^>]*>(.*?)</div>',
        html, flags=re.S,
    )
    if not m:
        raise ScrapeError("DSS date label (.ews-outlook-card .ews-ts) not found")
    label = _strip_tags(m.group(1))
    sa_date = infer_date(label, today)

    sources = [
        {"source": i["name"], "pct": float(i["y"]), "local": bool(i["local"])}
        for i in items
    ]
    local = round(sum(s["pct"] for s in sources if s["local"]), 2)
    total = round(sum(s["pct"] for s in sources), 2)
    top = max(sources, key=lambda s: s["pct"])
    return {
        "sa_date": sa_date.isoformat(),
        "date_label": label,
        "sources": sources,
        "summary": {
            "local_pct": local,
            "nonlocal_pct": round(total - local, 2),
            "dominant_source": top["source"],
            "dominant_pct": top["pct"],
            "n_sources": len(sources),
            "sum_pct": total,
        },
    }


def parse_rtsa(html: str) -> dict:
    data = None
    for arr in _js_arrays(html, "rtData"):
        if arr and all(isinstance(d, dict) and {"key", "items"} <= d.keys() for d in arr):
            data = arr
            break
    if data is None:
        raise ScrapeError("RTSA array (`var rtData`) not found or malformed")

    # Date tabs: <button ... class="ews-rt-date ..." data-i="5" ...><small>Thu</small>19 Nov*</button>
    partial_idx = set()
    for m in re.finditer(
        r'<button[^>]*class="ews-rt-date[^"]*"[^>]*data-i="(\d+)"[^>]*>(.*?)</button>',
        html, flags=re.S,
    ):
        if _strip_tags(m.group(2)).endswith("*"):
            partial_idx.add(int(m.group(1)))
    note = re.search(r'class="ews-rt-note"[^>]*>(.*?)</p>', html, flags=re.S)
    note_text = _strip_tags(note.group(1)) if note else ""

    days = []
    for i, d in enumerate(data):
        date.fromisoformat(d["key"])  # validates format
        days.append({
            "date": d["key"],
            "label": d.get("label", ""),
            "total": d.get("total"),
            "partial": i in partial_idx,
            "sources": [
                {"source": s["name"], "pct": s.get("y"), "sd": s.get("sd")}
                for s in d.get("items", [])
            ],
            "secondary_inorganic": [
                {"component": s["name"], "pct": s.get("y"), "sd": s.get("sd")}
                for s in (d.get("sub") or [])
            ],
        })
    return {"days": days, "partial_note": note_text}


# ---- DSS page (Chart.js object literals, not JSON) ------------------------- #
def _match_bracket(text: str, start: int) -> int:
    """Index just past the bracket that closes text[start], skipping strings and comments."""
    pairs = {"[": "]", "{": "}"}
    stack = [pairs[text[start]]]
    i = start + 1
    while i < len(text):
        c = text[i]
        if c in "'\"":
            j = i + 1
            while j < len(text) and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            continue
        if text.startswith("//", i):
            nl = text.find("\n", i)
            i = len(text) if nl < 0 else nl
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
            continue
        if c in pairs:
            stack.append(pairs[c])
        elif c in "]}":
            if not stack or c != stack.pop():
                raise ScrapeError("unbalanced brackets in chart data")
            if not stack:
                return i + 1
        i += 1
    raise ScrapeError("unterminated chart data object")


def _chart_object(html: str, name: str) -> str:
    m = re.search(rf"\bconst\s+{re.escape(name)}\s*=\s*\{{", html)
    if not m:
        raise ScrapeError(f"chart data object `const {name}` not found")
    start = m.end() - 1
    return html[start:_match_bracket(html, start)]


def _chart_series(obj: str, name: str) -> tuple[list[str], list[tuple[str, list[float]]]]:
    """Return (labels, [(dataset label, values), ...]) from a Chart.js data literal."""
    # Drop line comments that are not inside strings (e.g. `//barThickness: 120,`).
    clean = re.sub(r"(?m)(^|[\s,{\[])//[^\n]*", r"\1", obj)
    lm = re.search(r"\blabels\s*:\s*\[", clean)
    if not lm:
        raise ScrapeError(f"`{name}`: labels not found")
    lab_src = clean[lm.end() - 1:_match_bracket(clean, lm.end() - 1)]
    labels = [a or b for a, b in re.findall(r'"([^"]*)"|\'([^\']*)\'', lab_src)]

    dm = re.search(r"\bdatasets\s*:\s*\[", clean)
    if not dm:
        raise ScrapeError(f"`{name}`: datasets not found")
    ds_src = clean[dm.end() - 1:_match_bracket(clean, dm.end() - 1)]

    series = []
    pos = 1
    while True:
        k = ds_src.find("{", pos)
        if k < 0:
            break
        end = _match_bracket(ds_src, k)
        block = ds_src[k:end]
        pos = end
        lab = re.search(r"\blabel\s*:\s*(['\"])(.*?)\1", block, flags=re.S)
        dat = re.search(r"\bdata\s*:\s*\[([^\]]*)\]", block)
        if not lab or not dat:
            continue
        vals = []
        for tok in (t.strip() for t in dat.group(1).split(",")):
            if not tok:
                continue
            try:
                vals.append(float(tok))
            except ValueError as exc:
                raise ScrapeError(f"`{name}`/{lab.group(2)}: non-numeric value {tok!r}") from exc
        if len(vals) != len(labels):
            raise ScrapeError(
                f"`{name}`/{lab.group(2)}: {len(vals)} values for {len(labels)} labels"
            )
        series.append((htmllib.unescape(lab.group(2)), vals))
    if not series:
        raise ScrapeError(f"`{name}`: no datasets parsed")
    return labels, series


def parse_dss_page(html: str) -> dict:
    labels, series = _chart_series(_chart_object(html, "data"), "data")
    dates = [date.fromisoformat(l[:10]).isoformat() for l in labels]
    sources = [
        # The DSS page has no local flag; the homepage marks exactly the
        # "Delhi ..." sources as local, so the same rule is applied here.
        {"source": n, "local": n.startswith("Delhi"), "values": v}
        for n, v in series
    ]
    sums = [round(sum(s["values"][i] for s in sources), 2) for i in range(len(dates))]

    s_labels, s_series = _chart_series(_chart_object(html, "data2"), "data2")
    stub = dict(s_series)
    if "Stubble Burning" not in stub:
        raise ScrapeError("`data2`: 'Stubble Burning' series not found")
    stubble = [
        {"date": date.fromisoformat(l[:10]).isoformat(), "stubble_pct": v}
        for l, v in zip(s_labels, stub["Stubble Burning"])
    ]
    return {
        "forecast": {
            "run_date": dates[0],
            "dates": dates,
            "sources": sources,
            "sum_pct_by_day": sums,
        },
        "stubble": stubble,
    }


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def fingerprint(obj) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def replace_rows(path: Path, fields, key: str, dates: set[str], new_rows, sort_keys):
    rows = [r for r in read_csv(path) if r[key] not in dates]
    rows.extend({k: ("" if r.get(k) is None else r.get(k)) for k in fields} for r in new_rows)
    rows.sort(key=sort_keys)
    write_csv(path, fields, rows)


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def store(dss: dict, rtsa: dict, data_dir: Path, now_utc: datetime) -> dict:
    stamp = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    file_stamp = now_utc.strftime("%Y%m%dT%H%MZ")
    state_path = data_dir / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    state.setdefault("dss", {})
    state.setdefault("rtsa", {})

    # ---- DSS ----------------------------------------------------------------
    dss_fp = fingerprint(dss["sources"])
    dss_changed = state["dss"].get(dss["sa_date"]) != dss_fp
    if dss_changed:
        d = dss["sa_date"]
        replace_rows(
            data_dir / "dss" / "history.csv", DSS_FIELDS, "sa_date", {d},
            [{"sa_date": d, **s, "scraped_at_utc": stamp} for s in dss["sources"]],
            sort_keys=lambda r: (r["sa_date"], -_num(r["pct"])),
        )
        replace_rows(
            data_dir / "dss" / "daily_summary.csv", DSS_SUMMARY_FIELDS, "sa_date", {d},
            [{"sa_date": d, **dss["summary"], "scraped_at_utc": stamp}],
            sort_keys=lambda r: r["sa_date"],
        )
        snapshot = {"scraped_at_utc": stamp, "source_url": URL, **dss}
        write_json(data_dir / "dss" / "raw" / f"{d}_{file_stamp}.json", snapshot)
        write_json(data_dir / "dss" / "latest.json", snapshot)
        state["dss"][d] = dss_fp

    # ---- RTSA ---------------------------------------------------------------
    changed_days = []
    for day in rtsa["days"]:
        fp = fingerprint({k: day[k] for k in ("total", "partial", "sources", "secondary_inorganic")})
        if state["rtsa"].get(day["date"]) != fp:
            changed_days.append(day)
            state["rtsa"][day["date"]] = fp
    if changed_days:
        dates = {d["date"] for d in changed_days}
        replace_rows(
            data_dir / "rtsa" / "history.csv", RTSA_FIELDS, "date", dates,
            [
                {"date": d["date"], **s, "total": d["total"], "partial": d["partial"],
                 "scraped_at_utc": stamp}
                for d in changed_days for s in d["sources"]
            ],
            sort_keys=lambda r: (r["date"], -_num(r["pct"])),
        )
        replace_rows(
            data_dir / "rtsa" / "secondary_inorganic.csv", RTSA_SUB_FIELDS, "date", dates,
            [
                {"date": d["date"], **s, "partial": d["partial"], "scraped_at_utc": stamp}
                for d in changed_days for s in d["secondary_inorganic"]
            ],
            sort_keys=lambda r: (r["date"], r["component"]),
        )
        snapshot = {"scraped_at_utc": stamp, "source_url": URL, **rtsa}
        write_json(data_dir / "rtsa" / "raw" / f"{file_stamp}.json", snapshot)
        write_json(data_dir / "rtsa" / "latest.json", snapshot)

    write_json(state_path, state)
    return {
        "dss_date": dss["sa_date"],
        "dss_changed": dss_changed,
        "rtsa_dates": [d["date"] for d in rtsa["days"]],
        "rtsa_changed": sorted(d["date"] for d in changed_days),
    }


def store_dss_page(page: dict, data_dir: Path, now_utc: datetime) -> dict:
    stamp = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    file_stamp = now_utc.strftime("%Y%m%dT%H%MZ")
    out = data_dir / "dss_page"
    state_path = data_dir / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    state.setdefault("dss_page", {})
    state.setdefault("stubble", {})

    fc = page["forecast"]
    run = fc["run_date"]
    fp = fingerprint({"dates": fc["dates"], "sources": fc["sources"]})
    fc_changed = state["dss_page"].get(run) != fp
    if fc_changed:
        rows = [
            {"run_date": run, "forecast_date": d, "lead_day": i, "source": s["source"],
             "pct": s["values"][i], "local": s["local"], "scraped_at_utc": stamp}
            for s in fc["sources"] for i, d in enumerate(fc["dates"])
        ]
        replace_rows(
            out / "forecast_history.csv", DSS_FC_FIELDS, "run_date", {run}, rows,
            sort_keys=lambda r: (r["run_date"], r["forecast_date"], -_num(r["pct"])),
        )
        snapshot = {"scraped_at_utc": stamp, "source_url": DSS_PAGE_URL, **page}
        write_json(out / "raw" / f"{run}_{file_stamp}.json", snapshot)
        state["dss_page"][run] = fp

    changed_dates = []
    for row in page["stubble"]:
        f = fingerprint(row["stubble_pct"])
        if state["stubble"].get(row["date"]) != f:
            changed_dates.append(row["date"])
            state["stubble"][row["date"]] = f
    if changed_dates:
        replace_rows(
            out / "stubble_daily.csv", STUBBLE_FIELDS, "date", set(changed_dates),
            [{**r, "scraped_at_utc": stamp} for r in page["stubble"] if r["date"] in changed_dates],
            sort_keys=lambda r: r["date"],
        )
    if fc_changed or changed_dates:
        write_json(out / "latest.json", {"scraped_at_utc": stamp, "source_url": DSS_PAGE_URL, **page})

    write_json(state_path, state)
    return {
        "dsspage_run_date": run,
        "dsspage_changed": fc_changed,
        "stubble_last_date": page["stubble"][-1]["date"] if page["stubble"] else "",
        "stubble_changed": len(changed_dates),
    }


def append_log(log_path: Path, now_utc: datetime, status: str, result: dict | None, message: str):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Older logs have fewer columns: rewrite them once under the current header.
    if log_path.exists():
        with log_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            old_fields, old_rows = reader.fieldnames, list(reader)
        if old_fields != LOG_FIELDS:
            write_csv(log_path, LOG_FIELDS, [{k: r.get(k, "") for k in LOG_FIELDS} for r in old_rows])
    new = not log_path.exists()
    r = result or {}
    with log_path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS, lineterminator="\n")
        if new:
            w.writeheader()
        w.writerow({
            "run_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "run_ist": now_utc.astimezone(IST).strftime("%Y-%m-%d %H:%M"),
            "status": status,
            "dss_date": r.get("dss_date", ""),
            "dss_changed": r.get("dss_changed", ""),
            "rtsa_dates": ";".join(r.get("rtsa_dates", [])),
            "rtsa_changed": ";".join(r.get("rtsa_changed", [])),
            "dsspage_run_date": r.get("dsspage_run_date", ""),
            "dsspage_changed": r.get("dsspage_changed", ""),
            "stubble_last_date": r.get("stubble_last_date", ""),
            "stubble_changed": r.get("stubble_changed", ""),
            "message": message[:300],
        })


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--html", type=Path, help="parse a saved homepage HTML file instead of fetching")
    ap.add_argument("--dss-html", type=Path, help="parse a saved dss/index.php HTML file instead of fetching")
    ap.add_argument("--skip-home", action="store_true", help="do not scrape the homepage")
    ap.add_argument("--skip-dss-page", action="store_true", help="do not scrape dss/index.php")
    ap.add_argument("--root", type=Path, default=ROOT, help="repository root (default: auto)")
    args = ap.parse_args(argv)

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    log_path = args.root / "logs" / "run_log.csv"
    data_dir = args.root / "data"
    result, errors, changed = {}, [], False

    if not args.skip_home:
        try:
            html = args.html.read_text(encoding="utf-8") if args.html else fetch()
            today_ist = now_utc.astimezone(IST).date()
            r = store(parse_dss(html, today_ist), parse_rtsa(html), data_dir, now_utc)
            result.update(r)
            changed |= r["dss_changed"] or bool(r["rtsa_changed"])
        except Exception as exc:
            errors.append(f"homepage: {type(exc).__name__}: {exc}")

    if not args.skip_dss_page:
        try:
            if args.dss_html:
                page_html = args.dss_html.read_text(encoding="utf-8")
            else:
                # The page stalls ~120 s mid-response before sending its chart
                # data, so the read timeout must outlast that pause.
                page_html = fetch(DSS_PAGE_URL, retries=2, timeout=(30, 300))
            r = store_dss_page(parse_dss_page(page_html), data_dir, now_utc)
            result.update(r)
            changed |= r["dsspage_changed"] or bool(r["stubble_changed"])
        except Exception as exc:
            errors.append(f"dss page: {type(exc).__name__}: {exc}")

    attempted = (not args.skip_home) + (not args.skip_dss_page)
    if errors:
        status = "error" if len(errors) == attempted else "partial"
        msg = " | ".join(errors)
        if changed:
            msg = "new data; " + msg
    else:
        status, msg = "ok", ("new data" if changed else "no change")
    append_log(log_path, now_utc, status, result, msg)
    print(json.dumps({**result, "status": status, "message": msg}, indent=2, default=str))
    for e in errors:
        print(f"ERROR: {e}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
