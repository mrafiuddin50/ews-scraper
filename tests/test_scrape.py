import csv
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scraper"))

import scrape  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "index_sample.html"


class ParseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = FIXTURE.read_text(encoding="utf-8")

    def test_dss(self):
        dss = scrape.parse_dss(self.html, date(2026, 11, 20))
        self.assertEqual(dss["sa_date"], "2026-11-11")
        self.assertEqual(len(dss["sources"]), 29)
        self.assertEqual(dss["sources"][0], {"source": "Delhi Transport", "pct": 20.17, "local": True})
        self.assertEqual(dss["sources"][6]["source"], "Delhi & peripheral Industries")
        s = dss["summary"]
        self.assertEqual(s["dominant_source"], "Delhi Transport")
        self.assertAlmostEqual(s["local_pct"], 38.31, places=2)  # site shows 38%
        self.assertAlmostEqual(s["local_pct"] + s["nonlocal_pct"], s["sum_pct"], places=2)

    def test_rtsa(self):
        rt = scrape.parse_rtsa(self.html)
        self.assertEqual([d["date"] for d in rt["days"]], ["2026-11-14", "2026-11-19"])
        self.assertEqual([d["partial"] for d in rt["days"]], [False, True])
        self.assertEqual(rt["partial_note"], "* Only from 1 AM to 9 AM")
        d0 = rt["days"][0]
        self.assertEqual(d0["total"], 152.3)
        self.assertEqual(d0["sources"][1]["source"],
                         "Biomass burning (Stubble burning, Domestic solid biofuel/dunk-cake/wood)")
        self.assertEqual([c["component"] for c in d0["secondary_inorganic"]],
                         ["Sulphate", "Nitrate", "Ammonia"])

    def test_year_inference(self):
        self.assertEqual(scrape.infer_date("Wed, 11 Nov", date(2026, 11, 20)), date(2026, 11, 11))
        # Late-December label read in early January belongs to the previous year
        self.assertEqual(scrape.infer_date("Thu, 31 Dec", date(2027, 1, 2)), date(2026, 12, 31))

    def test_missing_data_fails(self):
        with self.assertRaises(scrape.ScrapeError):
            scrape.parse_dss("<html></html>", date(2026, 11, 20))
        with self.assertRaises(scrape.ScrapeError):
            scrape.parse_rtsa("<html></html>")


class StoreTests(unittest.TestCase):
    def test_dedupe_and_revision(self):
        html = FIXTURE.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            t1 = datetime(2026, 11, 20, 8, 23, tzinfo=timezone.utc)
            t2 = datetime(2026, 11, 20, 14, 23, tzinfo=timezone.utc)
            dss = scrape.parse_dss(html, date(2026, 11, 20))
            rt = scrape.parse_rtsa(html)

            r1 = scrape.store(dss, rt, root / "data", t1)
            self.assertTrue(r1["dss_changed"])
            self.assertEqual(r1["rtsa_changed"], ["2026-11-14", "2026-11-19"])

            # Second run, same values: nothing changes
            r2 = scrape.store(dss, rt, root / "data", t2)
            self.assertFalse(r2["dss_changed"])
            self.assertEqual(r2["rtsa_changed"], [])
            self.assertEqual(len(list((root / "data" / "rtsa" / "raw").iterdir())), 1)

            # Partial day completed later: only that date is replaced
            rt["days"][1]["partial"] = False
            rt["days"][1]["total"] = 171.0
            r3 = scrape.store(dss, rt, root / "data", t2)
            self.assertEqual(r3["rtsa_changed"], ["2026-11-19"])
            with (root / "data" / "rtsa" / "history.csv").open() as f:
                rows = list(csv.DictReader(f))
            day19 = [r for r in rows if r["date"] == "2026-11-19"]
            self.assertEqual(len(day19), 9)
            self.assertTrue(all(r["total"] == "171.0" and r["partial"] == "False" for r in day19))
            self.assertEqual(len([r for r in rows if r["date"] == "2026-11-14"]), 9)

            with (root / "data" / "dss" / "history.csv").open() as f:
                self.assertEqual(len(list(csv.DictReader(f))), 29)
            latest = json.loads((root / "data" / "dss" / "latest.json").read_text())
            self.assertEqual(latest["sa_date"], "2026-11-11")

    def test_main_logs_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.html"
            bad.write_text("<html>maintenance</html>")
            rc = scrape.main(["--html", str(bad), "--skip-dss-page", "--root", tmp])
            self.assertEqual(rc, 1)
            log = (Path(tmp) / "logs" / "run_log.csv").read_text()
            self.assertIn(",error,", log)

    def test_one_page_failing_still_saves_the_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.html"
            bad.write_text("<html>maintenance</html>")
            rc = scrape.main(["--html", str(bad), "--dss-html", str(DSS_FIXTURE), "--root", tmp])
            self.assertEqual(rc, 1)
            self.assertTrue((Path(tmp) / "data" / "dss_page" / "forecast_history.csv").exists())
            with (Path(tmp) / "logs" / "run_log.csv").open() as f:
                last = list(csv.DictReader(f))[-1]
            self.assertEqual(last["status"], "partial")
            self.assertEqual(last["dsspage_run_date"], "2026-11-11")

    def test_old_log_header_is_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "logs" / "run_log.csv"
            log.parent.mkdir()
            log.write_text(
                "run_utc,run_ist,status,dss_date,dss_changed,rtsa_dates,rtsa_changed,message\n"
                "2026-11-20T05:18:57Z,2026-11-20 10:48,ok,2026-11-11,True,2026-11-14,2026-11-14,new data\n"
            )
            scrape.main(["--skip-home", "--dss-html", str(DSS_FIXTURE), "--root", tmp])
            with log.open() as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(list(rows[0].keys()), scrape.LOG_FIELDS)
            self.assertEqual(rows[0]["dss_date"], "2026-11-11")
            self.assertEqual(rows[0]["message"], "new data")
            self.assertEqual(rows[1]["dsspage_changed"], "True")


DSS_FIXTURE = ROOT / "tests" / "fixtures" / "dss_index_sample.html"


class DssPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = scrape.parse_dss_page(DSS_FIXTURE.read_text(encoding="utf-8"))

    def test_forecast(self):
        fc = self.page["forecast"]
        self.assertEqual(fc["run_date"], "2026-11-11")
        self.assertEqual(fc["dates"], ["2026-11-11", "2026-11-12", "2026-11-13", "2026-11-14"])
        names = [s["source"] for s in fc["sources"]]
        self.assertEqual(names, ["Delhi Energy", "Delhi Transport", "Delhi & peripheral Industries",
                                 "Stubble Burning", "Others"])
        self.assertEqual(fc["sources"][1]["values"], [20.166625, 16.004166666667, 18.519, 20.543916666667])
        self.assertEqual([s["local"] for s in fc["sources"]], [True, True, True, False, False])

    def test_stubble(self):
        st = self.page["stubble"]
        self.assertEqual(len(st), 39)
        self.assertEqual(st[0], {"date": "2026-10-03", "stubble_pct": 0.00158})
        self.assertEqual(st[-1], {"date": "2026-11-11", "stubble_pct": 9.19096})
        self.assertNotIn("2026-10-05", [r["date"] for r in st])  # gap on the site

    def test_matches_homepage_day_one(self):
        home = scrape.parse_dss(FIXTURE.read_text(encoding="utf-8"), date(2026, 11, 20))
        home_vals = {s["source"]: s["pct"] for s in home["sources"]}
        for s in self.page["forecast"]["sources"]:
            self.assertAlmostEqual(round(s["values"][0], 2), home_vals[s["source"]], places=2)

    def test_store_dedupe_and_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "data"
            t = datetime(2026, 11, 23, 8, 23, tzinfo=timezone.utc)
            r1 = scrape.store_dss_page(self.page, d, t)
            self.assertTrue(r1["dsspage_changed"])
            self.assertEqual(r1["stubble_changed"], 39)
            r2 = scrape.store_dss_page(self.page, d, t)
            self.assertFalse(r2["dsspage_changed"])
            self.assertEqual(r2["stubble_changed"], 0)
            with (d / "dss_page" / "forecast_history.csv").open() as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 5 * 4)
            self.assertEqual({r["lead_day"] for r in rows}, {"0", "1", "2", "3"})

            # A new model run adds rows; earlier runs are kept
            page2 = json.loads(json.dumps(self.page))
            page2["forecast"]["run_date"] = "2026-11-24"
            page2["forecast"]["dates"] = ["2026-11-24", "2026-11-25", "2026-11-26", "2026-11-27"]
            page2["stubble"].append({"date": "2026-11-24", "stubble_pct": 3.2})
            r3 = scrape.store_dss_page(page2, d, t)
            self.assertTrue(r3["dsspage_changed"])
            self.assertEqual(r3["stubble_changed"], 1)
            with (d / "dss_page" / "forecast_history.csv").open() as f:
                runs = {r["run_date"] for r in csv.DictReader(f)}
            self.assertEqual(runs, {"2026-11-11", "2026-11-24"})

    def test_malformed_fails(self):
        with self.assertRaises(scrape.ScrapeError):
            scrape.parse_dss_page("<html><script>const data = {labels:[\"2026-11-11\"]};</script></html>")
        broken = DSS_FIXTURE.read_text(encoding="utf-8").replace(
            "data:[1.6814166666667,1.395375,1.6804583333333,1.5775416666667]",
            "data:[1.6814166666667,1.395375]")
        with self.assertRaises(scrape.ScrapeError):
            scrape.parse_dss_page(broken)


if __name__ == "__main__":
    unittest.main()
