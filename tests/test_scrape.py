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
            rc = scrape.main(["--html", str(bad), "--root", tmp])
            self.assertEqual(rc, 1)
            log = (Path(tmp) / "logs" / "run_log.csv").read_text()
            self.assertIn(",error,", log)


if __name__ == "__main__":
    unittest.main()
