"""CSV校验、账号隔离和无原始ZIP的真实案例闭环。构造数据仅用于边界测试。"""

import base64
import json
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
import shutil
import threading
from tempfile import TemporaryDirectory
import unittest

from finblocks.csv_data import CSVData, parse_csv
from finblocks.data import DataError
from finblocks.web import Workspace, make_handler


ROOT = Path(__file__).resolve().parents[1]
TEST_DOUBLE = ("symbol,name,date,open,high,low,close,volume\n"
               "TEST,隔离测试,2024-01-02,10,12,9,11,100\n"
               "TEST,隔离测试,2024-01-03,11,13,10,12,110\n")


def request(raw=None):
    return {"csv_base64": base64.b64encode(raw or TEST_DOUBLE.encode()).decode(), "label": "TEST_DOUBLE",
            "source_notice": "仅用于校验的构造数据", "price_basis": "unknown", "volume_unit": "shares",
            "currency": "TEST", "confirmed": True}


def stage_sample(target):
    for relative in ("docs/audit_evidence/data_statistics.json", "docs/audit_evidence/workspace_inventory.json",
                     "data/demo_manifest.json", "data/csi300_manifest.json", "data/fundamental_capabilities.json",
                     "examples/ma_strategy.json", "examples/sample_case.json"):
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    shutil.copytree(ROOT / "data/sample", target / "data/sample")


class CSVDataTests(unittest.TestCase):
    def test_http_import_cookie_ownership_export_and_restart_without_original_zip(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            stage_sample(root)
            workspace = Workspace(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(workspace))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def http(path, payload=None, cookie=None, token=True):
                connection = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
                headers = {"X-FinBlocks-Token": workspace.token} if token else {}
                if cookie:
                    headers["Cookie"] = cookie
                connection.request("GET" if payload is None else "POST", path,
                                   json.dumps(payload).encode() if payload is not None else None, headers)
                response = connection.getresponse()
                result = response.status, response.read(), dict(response.getheaders())
                connection.close()
                return result

            try:
                status, body, headers = http("/api/auth/register", {"username": "csv_owner", "password": "TEST_DOUBLE_PASSWORD"})
                self.assertEqual(status, 200)
                owner = json.loads(body)["user"]
                cookie = headers["Set-Cookie"].split(";", 1)[0]
                raw = (root / "data/sample/market.csv").read_bytes()
                self.assertEqual(http("/api/data/import-csv", request(raw), cookie, token=False)[0], 403)
                status, body, _ = http("/api/data/import-csv", request(raw), cookie)
                self.assertEqual(status, 200)
                source = json.loads(body)["source_id"]
                self.assertNotIn(source, json.loads(http("/api/bootstrap")[1])["data_sources"])
                self.assertIn(source, json.loads(http("/api/bootstrap", cookie=cookie)[1])["data_sources"])
                case = json.loads((root / "examples/sample_case.json").read_text(encoding="utf-8"))
                payload = {"strategy": case["strategy"], **case["config"], "data_source": source}
                payload.pop("mode")
                payload.pop("symbol")
                self.assertEqual(http("/api/portfolio/backtest", payload)[0], 400)
                status, body, _ = http("/api/portfolio/backtest", payload, cookie)
                self.assertEqual(status, 200)
                run = json.loads(body)
                self.assertEqual(run["report"]["source"]["members"][0]["dataset_sha256"], workspace.csv_data.public(source, owner["id"])["sha256"])
                status, body, _ = http("/api/export-artifact", {"kind": "report", "run_id": run["run_id"]}, cookie)
                self.assertEqual(status, 200)
                url = json.loads(body)["url"]
                self.assertEqual(http(url, cookie=cookie)[0], 200)
                self.assertEqual(http(url)[0], 404)
                self.assertEqual(http("/api/data/sample.csv")[1], raw)
                restarted = Workspace(root)
                self.assertIn(source, restarted.bootstrap(owner)["data_sources"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_chinese_headers_encoding_sort_and_previous_close(self):
        text = ("股票代码,股票名称,交易日期,开盘价,最高价,最低价,收盘价,成交量\n"
                "TEST,测试,2024-01-03,11,13,10,12,110\nTEST,测试,2024-01-02,10,12,9,11,100\n")
        panel, audit = parse_csv(text.encode("gb18030"))
        self.assertEqual(audit["encoding"], "gb18030")
        self.assertEqual([b.date for b in panel["TEST"]], ["2024-01-02", "2024-01-03"])
        self.assertEqual(panel["TEST"][1].values["previous_close"], 11)
        self.assertEqual(panel["TEST"][1].values["close"], 12)

    def test_duplicate_date_ambiguous_missing_header_and_bad_date_rejected(self):
        bad = [TEST_DOUBLE + TEST_DOUBLE.splitlines()[1] + "\n",
               TEST_DOUBLE.replace("symbol,name", "symbol,code"),
               TEST_DOUBLE.replace("volume", "unsupported"),
               TEST_DOUBLE.replace("2024-01-02", "2024-02-30")]
        for text in bad:
            with self.subTest(text=text), self.assertRaises(DataError):
                parse_csv(text.encode())

    def test_missing_nonfinite_price_invalid_ohlc_and_zero_volume_rejected(self):
        for old, new in (("10,12,9,11,100", ",12,9,11,100"), ("10,12,9,11,100", "NaN,12,9,11,100"),
                         ("10,12,9,11,100", "10,Inf,9,11,100"), ("10,12,9,11,100", "10,10,9,11,100"),
                         ("11,100", "11,0"), ("11,100", "11,-1")):
            with self.subTest(new=new), self.assertRaises(DataError):
                parse_csv(TEST_DOUBLE.replace(old, new).encode())

    def test_supplied_previous_close_is_checked_not_overwritten(self):
        text = TEST_DOUBLE.replace("volume\n", "volume,previous_close\n").replace("11,100\n", "11,100,11\n").replace("12,110\n", "12,110,10\n")
        with self.assertRaises(DataError):
            parse_csv(text.encode())

    def test_ownership_persists_without_exposing_other_users_or_owner_field(self):
        with TemporaryDirectory() as directory:
            store = CSVData(directory)
            item = store.import_data(request(), "owner-A")
            source = item["source_id"]
            self.assertNotIn("owner", item["source"])
            restarted = CSVData(directory)
            self.assertIn(source, restarted.sources("owner-A"))
            for owner in (None, "owner-B"):
                self.assertNotIn(source, restarted.sources(owner))
                with self.assertRaises(DataError):
                    restarted.load(source, "TEST", owner=owner)

    def test_tampered_snapshot_and_path_identifier_rejected(self):
        with TemporaryDirectory() as directory:
            store = CSVData(directory)
            source = store.import_data(request())["source_id"]
            path = store.directory / (source + ".csv")
            path.write_bytes(path.read_bytes().replace(b",100", b",101"))
            with self.assertRaisesRegex(DataError, "快照已变化"):
                store.load(source, "TEST")
            with self.assertRaises(DataError):
                store.get("../outside")

    def test_invalid_consent_units_encoding_currency_leave_no_files(self):
        with TemporaryDirectory() as directory:
            store = CSVData(directory)
            for field, value in (("confirmed", False), ("volume_unit", "lots"), ("csv_base64", "!"), ("currency", "")):
                payload = request()
                payload[field] = value
                with self.subTest(field=field), self.assertRaises(DataError):
                    store.import_data(payload)
            self.assertFalse(store.directory.exists())

    def test_unknown_symbol_and_empty_interval_do_not_fallback(self):
        with TemporaryDirectory() as directory:
            store = CSVData(directory)
            source = store.import_data(request())["source_id"]
            with self.assertRaises(DataError):
                store.load(source, "sh600455")
            with self.assertRaises(DataError):
                store.load(source, "TEST", "2025-01-01", "2025-12-31")

    def test_real_sample_without_original_zip_runs_portfolio_single_factor_and_freeze(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            stage_sample(root)
            workspace = Workspace(root)
            self.assertEqual(workspace.bootstrap()["default_data_source"], "sample")
            case = json.loads((root / "examples/sample_case.json").read_text(encoding="utf-8"))
            payload = {"strategy": case["strategy"], **case["config"]}
            payload.pop("mode")
            payload.pop("symbol")
            portfolio = workspace.portfolio_backtest(payload)["report"]
            self.assertEqual(portfolio["metrics"]["rows"], 250)
            self.assertEqual(len(portfolio["source"]["members"]), 3)
            self.assertGreater(portfolio["metrics"]["trade_records"], 0)
            self.assertEqual(portfolio["stock_pool"]["membership"], "explicit_csv_collection")
            single = {k: v for k, v in payload.items() if k not in ("symbols", "portfolio")}
            single["symbol"] = "AAPL"
            self.assertEqual(workspace.backtest(single)["report"]["metrics"]["rows"], 250)
            factor = {"expression": "close / MA(close, 20) - 1", "symbols": ["AAPL", "IBM", "MSFT"],
                      "start": "2012-01-03", "end": "2012-12-31", "data_source": "sample", "confirmed": True,
                      "protocol": {"horizon": 5, "train_fraction": 0.6, "validation_fraction": 0.2,
                                   "mode": "cross_section", "min_samples": 5}}
            validation = workspace.factor(factor, None)
            self.assertEqual(validation["kind"], "factor_validation")
            frozen = workspace.freeze_factor({"research_id": validation["research_id"], "confirmed": True}, None)
            self.assertEqual(frozen["kind"], "factor_test")
            with self.assertRaises(DataError):
                workspace.load_bars("sh600455")


if __name__ == "__main__":
    unittest.main()
