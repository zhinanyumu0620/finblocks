"""通过真实本机 HTTP 验证工作台边界及导出账目，不调用收费模型。"""

from copy import deepcopy
import base64
import csv
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import secrets
from tempfile import TemporaryDirectory
import threading
import unittest

from finblocks.backtest import run_backtest
from finblocks.auth import Accounts
from finblocks.data import load_daily
from finblocks.web import Workspace, make_handler
from finblocks.research import ResearchJournal


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = Workspace(Path(__file__).resolve().parents[1])
        cls.auth_directory = TemporaryDirectory()
        cls.workspace.accounts = Accounts(Path(cls.auth_directory.name) / "accounts.sqlite3")
        cls.workspace.journal = ResearchJournal(Path(cls.auth_directory.name) / "research.sqlite3")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.workspace))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.auth_directory.cleanup()

    def request(self, path, payload=None, *, token=True, headers=None, raw=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        request_headers = {}
        if token:
            request_headers["X-FinBlocks-Token"] = self.workspace.token
        request_headers.update(headers or {})
        body = raw if raw is not None else json.dumps(payload).encode() if payload is not None else None
        connection.request("POST" if body is not None else "GET", path, body, request_headers)
        response = connection.getresponse()
        result = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return result

    def payload(self):
        return {"strategy": deepcopy(self.workspace.default_strategy), "symbol": "sh600455",
                "cost_bps": 10, "lag": 1, "start": "2024-01-01", "end": "2026-08-19"}

    def test_account_cookie_login_logout_and_export_ownership(self):
        password = secrets.token_urlsafe(18)
        name = "web_" + secrets.token_hex(5)
        status, body, headers = self.request("/api/auth/register", {"username": name, "password": password})
        self.assertEqual(status, 200)
        user = json.loads(body)["user"]
        self.assertEqual(set(user), {"id", "username"})
        self.assertNotIn(password.encode(), body)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        auth_headers = {"Cookie": cookie}
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertEqual(json.loads(self.request("/api/auth/session", headers=auth_headers)[1])["user"], user)
        self.assertEqual(json.loads(self.request("/api/bootstrap", headers=auth_headers)[1])["user"], user)
        status, body, _ = self.request("/api/backtest", self.payload(), headers=auth_headers)
        self.assertEqual(status, 200)
        run_id = json.loads(body)["run_id"]
        status, body, _ = self.request("/api/export-artifact", {"kind": "report", "run_id": run_id}, headers=auth_headers)
        self.assertEqual(status, 200)
        url = json.loads(body)["url"]
        self.assertEqual(self.request(url, headers=auth_headers)[0], 200)
        self.assertEqual(self.request(url)[0], 404)
        _, _, second_headers = self.request("/api/auth/register", {"username": "web_" + secrets.token_hex(5), "password": password})
        other = {"Cookie": second_headers["Set-Cookie"].split(";", 1)[0]}
        self.assertEqual(self.request(url, headers=other)[0], 404)
        self.assertEqual(self.request("/api/export/" + run_id + "/report.json", headers=other)[0], 404)
        self.assertEqual(self.request("/api/export-artifact", {"kind": "report", "run_id": run_id}, headers=other)[0], 400)
        self.assertEqual(self.request("/api/auth/logout", {}, headers=auth_headers)[0], 200)
        self.assertIsNone(json.loads(self.request("/api/auth/session", headers=auth_headers)[1])["user"])
        self.assertEqual(self.request(url, headers=auth_headers)[0], 404)
        self.assertEqual(self.request("/api/backtest", self.payload(), headers=auth_headers)[0], 401)
        status, body, headers = self.request("/api/bootstrap", headers=auth_headers)
        self.assertIsNone(json.loads(body)["user"])
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        status, _, headers = self.request("/api/auth/login", {"username": name, "password": password})
        self.assertEqual(status, 200)
        new_cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertNotEqual(new_cookie, cookie)
        self.assertEqual(self.request(url, headers={"Cookie": new_cookie})[0], 200)

    def test_auth_invalid_credentials_and_csrf_are_rejected(self):
        payload = {"username": "no_such_test_user", "password": secrets.token_urlsafe(18)}
        self.assertEqual(self.request("/api/auth/login", payload, token=False)[0], 403)
        self.assertEqual(self.request("/api/auth/login", payload, headers={"Origin": "https://example.com"})[0], 403)
        self.assertEqual(self.request("/api/auth/login", payload)[0], 401)
        self.assertEqual(self.request("/api/auth/register", {**payload, "phone": "unused"})[0], 400)
        self.assertEqual(self.request("/private/test_accounts.sqlite3")[0], 404)

    def test_all_official_csi300_members_and_legacy_workspace_remain_available(self):
        status, body, _ = self.request("/api/bootstrap")
        self.assertEqual(status, 200)
        bootstrap = json.loads(body)
        members = [item for item in bootstrap["symbols"] if item["pool"] == "csi300"]
        reference = json.loads((self.workspace.root / "data/reference_sources/000300cons_read.json").read_text(encoding="utf-8-sig"))
        code_column = reference["rows"][0].index("成份券代码Constituent Code")
        official_codes = {row[code_column] for row in reference["rows"][1:]}
        self.assertEqual(len(members), 300)
        self.assertEqual({item["code"][2:] for item in members}, official_codes)
        self.assertTrue(all(item["local_data"] for item in members))
        self.assertIn("sh600455", {item["code"] for item in bootstrap["symbols"]})
        self.assertIn("非历史逐日成分池", bootstrap["stock_pool"]["limitations"])

    def test_csi300_full_history_rejected_and_explicit_checked_range_runs(self):
        payload = self.payload()
        item = self.workspace.symbols["sz000001"]
        payload.update(symbol=item["code"], start=item["source"]["start"], end=item["source"]["end"])
        status, body, _ = self.request("/api/backtest", payload)
        self.assertEqual(status, 400)
        self.assertIn("质量门槛未通过", json.loads(body)["error"])
        checked = item["quality"]["checked_range"]
        payload.update(start=checked["start"], end=checked["end"])
        status, body, _ = self.request("/api/backtest", payload)
        self.assertEqual(status, 200)
        result = json.loads(body)
        report = result["report"]
        self.assertEqual(report["source"]["rows"], checked["rows"])
        self.assertEqual(report["stock_pool"]["as_of"], self.workspace.pool["as_of"])
        self.assertEqual(report["stock_pool"]["official_sha256"], self.workspace.pool["official_sha256"])
        bars, _ = load_daily(self.workspace.archive, item["code"], checked["start"], checked["end"])
        expected = run_backtest(bars, payload["strategy"], cost_bps=10, execution_lag_bars=1)
        self.assertEqual(report["metrics"], expected["metrics"])
        self.assertEqual(report["records"], expected["records"])
        status, body, _ = self.request("/api/export-artifact", {"kind": "report", "run_id": result["run_id"]})
        self.assertEqual(status, 200)
        saved = json.loads((self.workspace.root / "artifacts/web" / json.loads(body)["filename"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["stock_pool"], report["stock_pool"])

    def test_static_and_no_arbitrary_file_download(self):
        for path in ("/", "/app.js", "/styles.css", "/research.js"):
            status, body, headers = self.request(path)
            self.assertEqual(status, 200)
            self.assertTrue(body)
            self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        for path in ("/../README.md", "/.env", "/docs/audit_evidence/data_statistics.json"):
            self.assertEqual(self.request(path)[0], 404)

    def test_research_http_evidence_audit_export_and_failure_boundaries(self):
        status, body, _ = self.request("/api/backtest", self.payload())
        self.assertEqual(status, 200)
        run = json.loads(body)
        self.assertEqual(self.request("/api/research/evidence", {"run_id": run["run_id"]}, token=False)[0], 403)
        status, body, _ = self.request("/api/research/evidence", {"run_id": run["run_id"]})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["facts"])
        self.assertEqual(self.request("/api/research/explain", {"run_id": run["run_id"], "confirmed": False})[0], 400)
        status, body, _ = self.request("/api/research/audit", {"prompt": "收盘价MA5上穿MA20", "strategy": self.workspace.default_strategy})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["application_blocked"])
        status, body, _ = self.request("/api/export-artifact", {"kind": "research", "research_id": run["report"]["research_id"]})
        self.assertEqual(status, 200)
        saved = json.loads(body)
        self.assertEqual(json.loads(self.request(saved["url"])[1])["metrics"], run["report"]["metrics"])
        self.assertEqual(self.request("/api/factor/inspect", {"expression": "__import__('os')"})[0], 400)
        self.assertEqual(self.request("/api/factor/evaluate", {"confirmed": False})[0], 400)
        self.assertEqual(self.request("/api/backtest", raw=b'{"symbol":"sh600455","strategy":{},"cost_bps":1e999,"lag":1}')[0], 400)

    def test_session_host_and_origin_boundaries(self):
        payload = {"strategy": self.workspace.default_strategy}
        self.assertEqual(self.request("/api/validate", payload, token=False)[0], 403)
        self.assertEqual(self.request("/api/validate", payload, headers={"X-FinBlocks-Token": "é"})[0], 403)
        self.assertEqual(self.request("/api/validate", payload, headers={"Origin": "https://example.com"})[0], 403)
        self.assertEqual(self.request("/api/bootstrap", headers={"Host": "example.com"})[0], 403)
        self.assertEqual(self.request("/api/validate", payload)[0], 200)

    def test_malformed_and_unsupported_strategy_block_execution(self):
        for raw in (b'{"strategy":NaN}', b'[]', b'{'):
            self.assertEqual(self.request("/api/validate", raw=raw)[0], 400)
        payload = self.payload()
        payload["strategy"]["nodes"][0]["field"] = "pe"
        status, body, _ = self.request("/api/backtest", payload)
        self.assertEqual(status, 400)
        self.assertIn("未开放", json.loads(body)["error"])
        self.assertEqual(self.request("/api/validate", raw=b" " * 65537)[0], 400)

    def test_invalid_dates_cost_lag_and_symbol_are_rejected(self):
        for patch in ({"start": "2026-02-30"}, {"start": "2026-08-19", "end": "2024-01-01"},
                      {"cost_bps": None}, {"cost_bps": True}, {"lag": 0}, {"symbol": "../sh600455"},
                      {"unknown": 1}, {"start": "2026-08-18"}):
            payload = self.payload()
            payload.update(patch)
            self.assertEqual(self.request("/api/backtest", payload)[0], 400, patch)

    def test_real_report_and_csv_match_kernel(self):
        payload = self.payload()
        status, body, _ = self.request("/api/backtest", payload)
        self.assertEqual(status, 200)
        result = json.loads(body)
        report = result["report"]
        bars = [b for b in self.workspace.cache[payload["symbol"]][0] if payload["start"] <= b.date <= payload["end"]]
        expected = run_backtest(bars, payload["strategy"], cost_bps=10, execution_lag_bars=1)
        self.assertEqual(report["records"], expected["records"])
        self.assertEqual(report["metrics"], expected["metrics"])
        self.assertEqual(report["strategy_origin"]["type"], "manual_or_imported_workspace")
        base = "/api/export/" + result["run_id"] + "/"
        status, body, headers = self.request(base + "report.json")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), report)
        self.assertIn("attachment", headers["Content-Disposition"])
        status, body, _ = self.request(base + "records.csv")
        self.assertEqual(status, 200)
        rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
        self.assertEqual(len(rows), len(bars))
        for record, row in zip(report["records"], rows):
            self.assertEqual(row["date"], record["date"])
            self.assertEqual(float(row["equity"]), record["equity"])
            self.assertEqual(float(row["fee"]), record["fee"])
        self.assertEqual(self.request("/api/export/" + "0" * 32 + "/report.json")[0], 404)

    def test_fake_generation_id_cannot_claim_real_ai(self):
        payload = self.payload()
        payload["generation_id"] = "forged-id"
        status, body, _ = self.request("/api/backtest", payload)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["report"]["strategy_origin"]["type"], "manual_or_imported_workspace")

    def test_indicator_report_has_explicit_statistics_parameters(self):
        payload = self.payload()
        payload["strategy"] = json.loads((self.workspace.root / "examples/macd_strategy.json").read_text(encoding="utf-8"))
        payload.update(periods_per_year=244, annual_risk_free_rate=0.02)
        status, body, _ = self.request("/api/backtest", payload)
        self.assertEqual(status, 200)
        report = json.loads(body)["report"]
        full, _ = load_daily(self.workspace.archive, payload["symbol"], payload["start"], payload["end"])
        expected = run_backtest(full, payload["strategy"], cost_bps=10, periods_per_year=244, annual_risk_free_rate=0.02)
        self.assertEqual(report["metrics"], expected["metrics"])
        self.assertEqual(report["assumptions"]["annual_risk_free_rate"], 0.02)
        self.assertEqual(report["assumptions"]["periods_per_year"], 244)

    def test_statistics_parameters_rejected_and_real_financial_versions_preserved(self):
        for values in ({"periods_per_year": True}, {"periods_per_year": 0}, {"annual_risk_free_rate": -1}):
            payload = self.payload(); payload.update(values)
            self.assertEqual(self.request("/api/backtest", payload)[0], 400)
        status, body, _ = self.request("/api/financials", {"symbol": "sh600455"})
        self.assertEqual(status, 200)
        report = json.loads(body)
        self.assertEqual(report["rows"], 154)
        self.assertFalse(report["historical_factor_supported"])
        self.assertEqual(report["unit"], "UNKNOWN")
        versions = [row for row in report["reports"] if row["report_date"] == "20091231"]
        values = {row["values"]["R_np_atoopc@xbx"] for row in versions}
        self.assertIn("-77780399.24", values)
        self.assertIn("-70459300.5", values)
        self.assertEqual(self.request("/api/financials", {"symbol": "../private"})[0], 400)

    def test_saved_workspace_is_actual_file_with_settings_and_positions(self):
        request = self.payload()
        strategy = request.pop("strategy")
        document = {"format": "finblocks-workspace", "version": 1, "strategy": strategy,
                    "config": request, "view": {"positions": {"price": {"x": 20, "y": 130}}},
                    "unknown_credential": "must-not-be-exported"}
        status, body, _ = self.request("/api/export-artifact", {"kind": "workspace", "workspace": document})
        self.assertEqual(status, 200)
        saved = json.loads(body)
        destination = self.workspace.root / "artifacts/web" / saved["filename"]
        self.assertEqual(destination.stat().st_size, saved["bytes"])
        self.assertNotIn(b"must-not-be-exported", destination.read_bytes())
        clean = json.loads(destination.read_bytes())
        self.assertEqual(clean["strategy"], strategy)
        self.assertEqual(clean["config"], {"periods_per_year": 252, "annual_risk_free_rate": 0.0, **request})
        status, downloaded, _ = self.request(saved["url"])
        self.assertEqual(status, 200)
        self.assertEqual(downloaded, destination.read_bytes())
        document["config"]["start"] = "2026-02-30"
        self.assertEqual(self.request("/api/export-artifact", {"kind": "workspace", "workspace": document})[0], 400)

    def test_persistent_report_exports_match_and_invalid_chart_is_blocked(self):
        status, body, _ = self.request("/api/backtest", self.payload())
        result = json.loads(body)
        for kind in ("report", "records"):
            status, body, _ = self.request("/api/export-artifact", {"kind": kind, "run_id": result["run_id"]})
            self.assertEqual(status, 200)
            saved = json.loads(body)
            status, exported, _ = self.request(saved["url"])
            self.assertEqual(status, 200)
            if kind == "report":
                self.assertEqual(json.loads(exported), result["report"])
            else:
                rows = list(csv.DictReader(io.StringIO(exported.decode("utf-8-sig"))))
                self.assertEqual(len(rows), result["report"]["metrics"]["rows"])
        for image in ("not-png", "data:image/png;base64,invalid", "data:image/png;base64,YWJj"):
            self.assertEqual(self.request("/api/export-artifact", {"kind": "chart", "run_id": result["run_id"], "image": image})[0], 400)

    def test_actual_canvas_png_and_corruption_are_distinguished(self):
        # 使用浏览器已实际生成的图，不以构造价格或占位图测试产品导出。
        original = self.workspace.root / "artifacts/web/3738db2874474937ac824d4d838b420d_equity.png"
        if not original.exists():
            self.skipTest("本机浏览器PNG证据尚未生成")
        status, body, _ = self.request("/api/backtest", self.payload())
        run_id = json.loads(body)["run_id"]
        image = original.read_bytes()
        prefix = "data:image/png;base64,"
        status, body, _ = self.request("/api/export-artifact", {"kind": "chart", "run_id": run_id,
                                         "image": prefix + base64.b64encode(image).decode()})
        self.assertEqual(status, 200)
        saved = json.loads(body)
        self.assertEqual(self.request(saved["url"])[1], image)
        corrupted = bytearray(image)
        corrupted[32] ^= 1
        for invalid in (image[:16], image[:-8], bytes(corrupted), image + b"trailing"):
            self.assertEqual(self.request("/api/export-artifact", {"kind": "chart", "run_id": run_id,
                                             "image": prefix + base64.b64encode(invalid).decode()})[0], 400)

    def test_real_portfolio_preflight_backtest_exports_and_evidence(self):
        payload = {"strategy": deepcopy(self.workspace.default_strategy), "symbols": ["sh688047", "sh688506", "sh688521", "sh688981"],
                   "start": "2026-06-01", "end": "2026-08-19", "cost_bps": 10, "lag": 1,
                   "portfolio": {"max_positions": 4, "position_cap": .25, "rebalance_every": 1}}
        self.assertEqual(self.request("/portfolio.js")[0], 200)
        status, body, _ = self.request("/api/portfolio/preflight", payload)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "PASS")
        status, body, _ = self.request("/api/portfolio/backtest", payload)
        self.assertEqual(status, 200)
        result = json.loads(body); report = result["report"]
        self.assertEqual(report["kind"], "portfolio")
        self.assertEqual(report["metrics"]["rows"], 57)
        self.assertTrue(any(len(row["trades"]) > 1 for row in report["records"]))
        self.assertTrue(all(row["cash"] >= 0 for row in report["records"]))
        for row in report["records"]:
            self.assertAlmostEqual(row["cash"] + sum(h["value"] for h in row["holdings"]), row["equity"], places=12)
        _, body, _ = self.request("/api/export-artifact", {"kind": "records", "run_id": result["run_id"]})
        saved = json.loads(body)
        rows = list(csv.DictReader(io.StringIO(self.request(saved["url"])[1].decode("utf-8-sig"))))
        self.assertEqual(json.loads(rows[-1]["holdings"]), report["records"][-1]["holdings"])
        _, body, _ = self.request("/api/research/evidence", {"run_id": result["run_id"], "date": "2026-07-01"})
        self.assertIn("P-holdings", {fact["id"] for fact in json.loads(body)["facts"]})
        settings = {key: value for key, value in payload.items() if key != "strategy"} | {"symbol": "sh688981", "mode": "portfolio"}
        document = {"format": "finblocks-workspace", "version": 1, "strategy": payload["strategy"], "config": settings}
        status, body, _ = self.request("/api/export-artifact", {"kind": "workspace", "workspace": document})
        self.assertEqual(status, 200)
        restored = json.loads(self.request(json.loads(body)["url"])[1])
        self.assertEqual(restored["config"]["symbols"], payload["symbols"])
        self.assertEqual(restored["config"]["portfolio"], payload["portfolio"])
        self.assertEqual(self.request("/api/research/experiment", {"run_id": result["run_id"], "axis": "cost_bps", "values": [10], "confirmed": True})[0], 400)

    def test_portfolio_rejects_failed_members_and_invalid_protocol(self):
        payload = {"strategy": deepcopy(self.workspace.default_strategy), "symbols": ["sz000166"],
                   "start": "2026-08-19", "end": "2026-08-19", "cost_bps": 10, "lag": 1,
                   "portfolio": {"max_positions": 4, "position_cap": .25, "rebalance_every": 1}}
        status, body, _ = self.request("/api/portfolio/preflight", payload)
        self.assertEqual(status, 200)
        checked = json.loads(body)
        self.assertEqual(checked["status"], "BLOCKED")
        self.assertEqual(checked["eligible_symbols"], [])
        self.assertIn("reason", checked["items"][0])
        self.assertEqual(self.request("/api/portfolio/backtest", payload)[0], 400)
        self.assertEqual(self.request("/api/portfolio/preflight", {**payload, "symbols": ["sz000166", "sz000166"]})[0], 400)
        self.assertEqual(self.request("/api/portfolio/preflight", {**payload, "portfolio": {"max_positions": True, "position_cap": .25, "rebalance_every": 1}})[0], 400)
        self.assertEqual(self.request("/api/portfolio/backtest", payload, token=False)[0], 403)
        status, body, _ = self.request("/api/portfolio/preflight", {**payload, "symbols": ["sh688047"], "start": "2020-01-01"})
        self.assertEqual(status, 200)
        self.assertIn("超出本地覆盖", json.loads(body)["items"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
