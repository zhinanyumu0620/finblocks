"""本地真实金融数据的集成验证，不以构造行情替代实际执行。"""

import csv
from decimal import Decimal
import io
import json
from pathlib import Path
import unittest
import zipfile

from finblocks.backtest import run_backtest
from finblocks.data import load_daily, validate_bars


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "data/demo_manifest.json"


@unittest.skipUnless(MANIFEST.exists(), "先运行select-demo建立真实数据清单")
class RealDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        cls.stats = json.loads((ROOT / "docs/audit_evidence/data_statistics.json").read_text(encoding="utf-8"))
        cls.archive = ROOT / cls.stats["archives"][0]["path"]
        cls.strategy = json.loads((ROOT / "examples/ma_strategy.json").read_text(encoding="utf-8"))
        cls.code = cls.manifest["selected"][0]["code"]
        cls.bars, _ = load_daily(cls.archive, cls.code)

    def test_selected_samples_are_actual_rows_with_quality_checks(self):
        for selected in self.manifest["selected"]:
            bars, source = load_daily(self.archive, selected["code"])
            with self.subTest(code=selected["code"]):
                self.assertEqual(source["member_sha256"], selected["source"]["member_sha256"])
                self.assertEqual(len(bars), selected["source"]["rows"])
                self.assertEqual(bars[-1].date, "2026-08-19")
                self.assertTrue(validate_bars(bars)["passed"])

    def test_signal_matches_independent_decimal_calculation_from_original_csv(self):
        with zipfile.ZipFile(self.archive) as archive:
            lines = io.StringIO(archive.read(self.code + ".csv").decode("gb18030"))
        next(lines)
        source_rows = list(csv.DictReader(lines))
        prices = [Decimal(row["收盘价"]) for row in source_rows]
        result = run_backtest(self.bars, self.strategy, cost_bps=10)
        for i, record in enumerate(result["records"]):
            expected = None if i < 19 else sum(prices[i - 4:i + 1]) / 5 > sum(prices[i - 19:i + 1]) / 20
            self.assertEqual(record["signal"], expected, record["date"])

    def test_real_ledger_conserves_cash_and_never_uses_same_day_signal(self):
        result = run_backtest(self.bars, self.strategy, cost_bps=10)
        cash_before, units_before = 1.0, 0.0
        for record in result["records"]:
            self.assertAlmostEqual(record["cash"], cash_before - (record["units"] - units_before) * record["close"] - record["fee"], places=10)
            self.assertAlmostEqual(record["equity"], record["cash"] + record["units"] * record["close"], places=10)
            if record["executed_signal_date"]:
                self.assertLess(record["executed_signal_date"], record["date"])
            cash_before, units_before = record["cash"], record["units"]

    def test_real_prefix_invariance(self):
        full = run_backtest(self.bars, self.strategy, cost_bps=10)
        prefix = run_backtest(self.bars[:200], self.strategy, cost_bps=10)
        self.assertEqual(full["records"][:200], prefix["records"])

    def test_real_ohlc_anomaly_is_blocked(self):
        dirty, _ = load_daily(self.archive, "sh600625", "2000-05-12", "2000-05-12")
        quality = validate_bars(dirty)
        self.assertFalse(quality["passed"])
        self.assertIn("OHLC 包络不一致", quality["issues"][0]["reasons"])


if __name__ == "__main__":
    unittest.main()
