# ews-scraper

Twice-daily archive of the **PM2.5 source apportionment** published on the
[Air Quality Early Warning System for Delhi (AIRWISE-Delhi)](https://ews.tropmet.res.in/index.php),
run by the Indian Institute of Tropical Meteorology (IITM), Pune, Ministry of Earth Sciences, Government of India.

All data belong to IITM / MoES. This repository only records the values shown on the
homepage, unchanged, so that a history builds up over time. Please cite the original source
and check values against the live site before using them in any publication.

## What is captured

The homepage's "Source apportionment of PM2.5" card has two tabs. Both are stored.

| Dataset | Site label | Coverage | Fields |
|---|---|---|---|
| `data/dss/` | DSS - DELHI-NCR | one date per update | 29 sources (Delhi sectors, NCR districts, stubble burning, others): % share of Delhi's daily-mean PM2.5, local/non-local flag |
| `data/rtsa/` | RTSA - Delhi | rolling ~6 dates | source category % share ± SD (%), daily `total`, and a sulphate/nitrate/ammonia split of "Secondary Inorganic" |

Source names, labels and values are kept exactly as published (including spellings).

### Files

```
data/
  dss/history.csv            sa_date, source, pct, local, scraped_at_utc
  dss/daily_summary.csv      sa_date, local_pct, nonlocal_pct, dominant_source, dominant_pct, n_sources, sum_pct, scraped_at_utc
  dss/latest.json            last extracted DSS snapshot
  dss/raw/                   one JSON per new or revised DSS date
  rtsa/history.csv           date, source, pct, sd, total, partial, scraped_at_utc
  rtsa/secondary_inorganic.csv  date, component, pct, sd, partial, scraped_at_utc
  rtsa/latest.json           last extracted RTSA snapshot (all dates shown on the site)
  rtsa/raw/                  one JSON per run in which any RTSA date changed
  state.json                 fingerprints used to detect changes
logs/run_log.csv             one line per run: time, status, dates seen, what changed
```

### Caveats (from the site)

- **DSS date** is the card's own timestamp (e.g. "Wed, 11 Nov"). The site gives no year, so the year
  is inferred from the weekday. The DSS page notes the contributions are based on a 2021 emissions inventory.
- **RTSA partial days:** a date tab marked `*` ("Only from 1 AM to 9 AM") is stored with `partial = True`.
  When the site later completes or revises a date, that date's rows are replaced; the earlier version
  stays in `rtsa/raw/`.
- **RTSA `total`:** stored as published; the page does not state its units.
- **Staleness:** the scraper records the site's dates, not the scrape date, so a stale feed shows up as
  repeated "no change" runs in `logs/run_log.csv` rather than as new data.

## How it runs

GitHub Actions (`.github/workflows/scrape.yml`) runs at **08:23 and 14:23 UTC (13:53 and 19:53 IST)**.
The second run is a backup: if the first already captured the day's values, it only adds a log line.
Each run fetches the homepage once (60 s timeout, 3 attempts), runs the unit tests, parses both
datasets, commits changes, and fails the job (GitHub emails the owner) if the page could not be
fetched or the data were not found, for example after a site redesign.

Run locally:

```bash
pip install -r requirements.txt
python scraper/scrape.py                 # fetch live page and update data/
python scraper/scrape.py --html page.html  # parse a saved copy instead
python -m unittest discover -s tests -v
```
