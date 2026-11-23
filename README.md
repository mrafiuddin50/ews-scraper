# ews-scraper

Twice-daily archive of the **PM2.5 source apportionment** published on the
[Air Quality Early Warning System for Delhi (AIRWISE-Delhi)](https://ews.tropmet.res.in/index.php)
and its [Decision Support System (DSS) page](https://ews.tropmet.res.in/dss/index.php),
run by the Indian Institute of Tropical Meteorology (IITM), Pune, Ministry of Earth Sciences, Government of India.

All data belong to IITM / MoES. This repository only records the values shown on the
site, unchanged, so that a history builds up over time. Please cite the original source
and check values against the live site before using them in any publication.

## What is captured

| Dataset | Page | Site label | Coverage | Fields |
|---|---|---|---|---|
| `data/dss/` | homepage | DSS - DELHI-NCR | first day of each DSS model run | 29 sources (Delhi sectors, NCR districts, stubble burning, others): % share of Delhi's daily-mean PM2.5 (2 decimals), local/non-local flag |
| `data/dss_page/forecast_history.csv` | DSS page | Daily Mean of Local and Non-Local Fractional Contribution to PM2.5 in Delhi for the next four days | all four days of each DSS model run | same 29 sources, % share at full published precision, by forecast day |
| `data/dss_page/stubble_daily.csv` | DSS page | Daily Mean contribution from Stubble Burning to the PM2.5 mass concentration in Delhi | season to date (from 3 Oct 2026) | stubble-burning % share of Delhi's daily-mean PM2.5 |
| `data/rtsa/` | homepage | RTSA - Delhi | rolling ~6 dates | source category % share ± SD (%), daily `total`, and a sulphate/nitrate/ammonia split of "Secondary Inorganic" |

Source names, labels and values are kept exactly as published (including spellings).
The homepage DSS figures are the DSS page's day-one figures rounded to two decimals.

### Files

```
data/
  dss/history.csv              sa_date, source, pct, local, scraped_at_utc
  dss/daily_summary.csv        sa_date, local_pct, nonlocal_pct, dominant_source, dominant_pct, n_sources, sum_pct, scraped_at_utc
  dss/latest.json              last extracted homepage DSS snapshot
  dss/raw/                     one JSON per new or revised DSS date
  dss_page/forecast_history.csv  run_date, forecast_date, lead_day (0-3), source, pct, local, scraped_at_utc
  dss_page/stubble_daily.csv   date, stubble_pct, scraped_at_utc
  dss_page/latest.json         last extracted DSS page snapshot (both charts)
  dss_page/raw/                one JSON per new or revised DSS model run
  rtsa/history.csv             date, source, pct, sd, total, partial, scraped_at_utc
  rtsa/secondary_inorganic.csv date, component, pct, sd, partial, scraped_at_utc
  rtsa/latest.json             last extracted RTSA snapshot (all dates shown on the site)
  rtsa/raw/                    one JSON per run in which any RTSA date changed
  state.json                   fingerprints used to detect changes
logs/run_log.csv               one line per run: time, status, dates seen, what changed
```

### Caveats

- **DSS model runs:** `run_date` is the first date on the DSS page's four-day chart. The DSS page notes
  the contributions are based on a 2021 emissions inventory, and that the stubble-burning share uses
  VIIRS fire counts from the previous evening, with climatological fire data for the forecast days.
- **Local flag (DSS page):** the DSS page does not label sources as local; the scraper marks the
  "Delhi ..." sources as local, which is exactly how the homepage labels them.
- **Homepage DSS date** is the card's own timestamp (e.g. "Wed, 11 Nov"). The site gives no year, so the year
  is inferred from the weekday.
- **Stubble series gaps:** dates missing on the site (e.g. 5 Oct 2026) are left missing, not filled.
- **RTSA partial days:** a date tab marked `*` ("Only from 1 AM to 9 AM") is stored with `partial = True`.
  When the site later completes or revises a date, that date's rows are replaced; the earlier version
  stays in `rtsa/raw/`.
- **RTSA `total`:** stored as published; the page does not state its units.
- **Staleness:** the scraper records the site's dates, not the scrape date, so a stale feed shows up as
  repeated "no change" runs in `logs/run_log.csv` rather than as new data.

## How it runs

GitHub Actions (`.github/workflows/scrape.yml`) is scheduled for **08:23 and 14:23 UTC (13:53 and 19:53 IST)**;
GitHub often starts scheduled runs several hours late. The second run is a backup: if the first already
captured the day's values, it only adds a log line.

Each run fetches the homepage (60 s timeout, 3 attempts) and the DSS page (which pauses for about two
minutes mid-response, so it gets a 300 s read timeout, 2 attempts). The two pages are handled independently:
if one fails, the other is still saved, the run is logged as `partial`, and the job fails so GitHub emails the owner.

Run locally:

```bash
pip install -r requirements.txt
python scraper/scrape.py                          # fetch both live pages and update data/
python scraper/scrape.py --skip-dss-page          # homepage only (fast)
python scraper/scrape.py --html home.html --dss-html dss.html   # parse saved copies instead
python -m unittest discover -s tests -v
```
