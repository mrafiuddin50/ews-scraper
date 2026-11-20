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

Values are stored exactly as published. Each run appends a line to
logs/run_log.csv; data files only change when the site's values change.
Exit code is non-zero if fetching or parsing fails.
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
LOG_FIELDS = [
    "run_utc", "run_ist", "status", "dss_date", "dss_changed",
    "rtsa_dates", "rtsa_changed", "message",
]


class ScrapeError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #
def fetch(url: str = URL, retries: int = 3, timeout: int = 60) -> str:
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
    state = json.loads(state_path.read_text()) if state_path.exists() else {"dss": {}, "rtsa": {}}

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


def append_log(log_path: Path, now_utc: datetime, status: str, result: dict | None, message: str):
    log_path.parent.mkdir(parents=True, exist_ok=True)
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
            "message": message[:300],
        })


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--html", type=Path, help="parse a saved HTML file instead of fetching")
    ap.add_argument("--root", type=Path, default=ROOT, help="repository root (default: auto)")
    args = ap.parse_args(argv)

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    log_path = args.root / "logs" / "run_log.csv"
    try:
        html = args.html.read_text(encoding="utf-8") if args.html else fetch()
        today_ist = now_utc.astimezone(IST).date()
        dss = parse_dss(html, today_ist)
        rtsa = parse_rtsa(html)
        result = store(dss, rtsa, args.root / "data", now_utc)
    except Exception as exc:  # log every failure, then fail the job
        append_log(log_path, now_utc, "error", None, f"{type(exc).__name__}: {exc}")
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    changed = result["dss_changed"] or bool(result["rtsa_changed"])
    msg = "new data" if changed else "no change"
    append_log(log_path, now_utc, "ok", result, msg)
    print(json.dumps({**result, "status": msg}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
