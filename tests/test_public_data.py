"""公开数据真实快照、完整性及冻结边界验证，测试不发网络请求。"""

import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from finblocks.backtest import run_backtest
from finblocks.data import DataError, validate_bars
from finblocks.public_data import load_public_daily, public_manifest, parse_response, normalize_snapshot
from finblocks.research import document_hash
from finblocks.web import Workspace

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless((ROOT / "data/public_sources/manifest.json").exists(), "尚未下载公开快照")
class PublicDataTests(unittest.TestCase):
    def test_all_actual_snapshots_pass_without_network_or_original_changes(self):
        manifest = public_manifest(ROOT)
        self.assertEqual(len(manifest["members"]), 300)
        for code, entry in manifest["members"].items():
            with self.subTest(code=code):
                bars, source = load_public_daily(ROOT, code)
                self.assertTrue(validate_bars(bars)["passed"])
                self.assertEqual(len(bars), entry["rows"])
                self.assertEqual(source["member_sha256"], entry["sha256"])
                self.assertIsNone(bars[-1].values["market_cap"])

    def test_actual_adjusted_prices_match_raw_response_and_prefix_is_frozen(self):
        bars, source = load_public_daily(ROOT, "sz000001", "2026-06-01", "2026-08-19")
        evidence = source["responses"][1]
        rows, key = parse_response((ROOT / evidence["path"]).read_bytes(), "sz000001", "hfq")
        self.assertEqual(key, "hfqday")
        raw = {r["date"]: r for r in rows}
        for b in bars:
            self.assertEqual(b.values["close"], raw[b.date]["close"])
            self.assertEqual(b.values["volume"], raw[b.date]["volume_raw"] * 100)
        strategy = json.loads((ROOT / "examples/ma_strategy.json").read_text(encoding="utf-8"))
        full = run_backtest(bars, strategy, cost_bps=10)
        prefix_bars, _ = load_public_daily(ROOT, "sz000001", bars[0].date, bars[36].date)
        self.assertEqual(full["records"][:37], run_backtest(prefix_bars, strategy, cost_bps=10)["records"])

    def test_missing_coverage_and_unknown_code_do_not_fallback(self):
        for code, start, end in [("sz000001", "1990-01-01", "2026-08-19"),
                                 ("sh999999", "2026-06-01", "2026-08-19")]:
            with self.assertRaises(DataError):
                load_public_daily(ROOT, code, start, end)

    def test_response_parser_rejects_code_mismatch_script_and_duplicate_conflict(self):
        for raw in [b'alert(1);{}', b'{"code":0,"data":{"sh999999":{"day":[]}}}', b'{"code":1,"data":{}}']:
            with self.assertRaises(DataError):
                parse_response(raw, "sz000001", "")
        row = ["2026-06-01", "1", "1", "1", "1", "100", {}, "1", "1"]
        conflict = copy.deepcopy(row)
        conflict[2] = "2"
        raw = json.dumps({"code": 0, "data": {"sz000001": {"day": [row, conflict]}}}).encode()
        with self.assertRaises(DataError):
            parse_response(raw, "sz000001", "")

    def test_raw_or_normalized_tampering_blocks_execution(self):
        manifest = public_manifest(ROOT)
        entry = manifest["members"]["sz000001"]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for relative in [entry["path"], *[r["path"] for r in entry["responses"]]]:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((ROOT / relative).read_bytes())
            (root / "data/public_sources/manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            load_public_daily(root, "sz000001")
            raw = root / entry["responses"][0]["path"]
            raw.write_bytes(raw.read_bytes() + b" ")
            with self.assertRaises(DataError):
                load_public_daily(root, "sz000001")
            raw.write_bytes((ROOT / entry["responses"][0]["path"]).read_bytes())
            normalized = root / entry["path"]
            normalized.write_bytes(normalized.read_bytes() + b" ")
            with self.assertRaises(DataError):
                load_public_daily(root, "sz000001")

    def test_units_or_day_fallback_cannot_be_guessed(self):
        bars, source = load_public_daily(ROOT, "sz000001")
        plain, _ = parse_response((ROOT / source["responses"][0]["path"]).read_bytes(), "sz000001", "")
        adjusted, _ = parse_response((ROOT / source["responses"][1]["path"]).read_bytes(), "sz000001", "hfq")
        bad = copy.deepcopy(bars)
        bad[20].values["volume"] *= 100
        with self.assertRaises(DataError):
            normalize_snapshot("sz000001", "平安银行", plain, adjusted, "hfqday", bad, source["end"])
        with self.assertRaises(DataError):
            normalize_snapshot("sz000001", "平安银行", plain, adjusted, "day", bars, source["end"])

    def test_factor_freeze_rejects_changed_sample_before_reading_test(self):
        workspace = Workspace.__new__(Workspace)
        workspace.lock = threading.RLock()
        request = {"data_source": "public_hfq", "symbols": ["sz000001"], "start": "2026-06-01", "end": "2026-08-19"}
        workspace.journal = Mock()
        workspace.journal.get.return_value = {"kind": "factor_validation", "status": "ok", "request": request,
                                             "sample_hash": document_hash({"sz000001": "old"})}
        workspace.load_bars = Mock(return_value=([], {"member_sha256": "changed"}))
        workspace.factor = Mock()
        with self.assertRaises(DataError):
            workspace.freeze_factor({"research_id": "fixture", "confirmed": True}, "fixture-owner")
        workspace.factor.assert_not_called()

    def test_portfolio_preflight_rejects_snapshot_changed_during_loading(self):
        workspace = Workspace.__new__(Workspace)
        workspace.root = ROOT
        workspace.symbols = {"sz000001": {"name": "平安银行"}}
        workspace.manifest = {"archive_sha256": "original"}
        workspace.portfolio_spec = Mock(return_value=(["sz000001"], Mock(warmup=1)))
        workspace.ensure_archive = Mock()
        bars, source = load_public_daily(ROOT, "sz000001", "2026-06-01", "2026-08-19")
        workspace.load_bars = Mock(return_value=(bars, {**source, "manifest_sha256": "changed"}))
        with patch("finblocks.web.public_manifest", return_value=public_manifest(ROOT)):
            result = workspace.portfolio_preflight({"data_source": "public_hfq", "start": "2026-06-01", "end": "2026-08-19", "lag": 1})
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("检查期间已更新", result["items"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
