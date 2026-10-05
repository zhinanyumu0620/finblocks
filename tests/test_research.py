"""研究层专测：真实数据复算、非法输入、分区边界、归属与模型失败。"""

from copy import deepcopy
from html.parser import HTMLParser
import json
from pathlib import Path
import statistics
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from finblocks.ai import AIError, explain_research, generate_factor
from finblocks.backtest import run_backtest
from finblocks.data import load_daily
from finblocks.dsl import evaluate
from finblocks.factors import compile_factor, correlation, describe_factor, evaluate_factor, ranks
from finblocks.research import ResearchJournal, audit_intent, evidence_pack, repair_strategy
from finblocks.web import Workspace


ROOT = Path(__file__).resolve().parents[1]


class ResearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = Workspace(ROOT)
        cls.directory = TemporaryDirectory()
        cls.workspace.journal = ResearchJournal(Path(cls.directory.name) / "research.sqlite3")
        cls.bars, cls.source = load_daily(cls.workspace.archive, "sh600455", "2023-01-01", "2026-08-19")
        cls.strategy = deepcopy(cls.workspace.default_strategy)
        cls.protocol = {"mode": "time_series", "horizon": 3, "train_fraction": .6, "validation_fraction": .2, "min_samples": 30}

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def payload(self):
        return {"symbol": "sh600455", "strategy": deepcopy(self.strategy), "start": "2023-01-01", "end": "2026-08-19", "cost_bps": 10, "lag": 1}

    def factor_payload(self):
        return {"expression": "close / MA(close, 20) - 1", "symbols": ["sh600455"], "start": "2023-01-01", "end": "2026-08-19", "protocol": dict(self.protocol), "confirmed": True}

    def test_event_state_mismatch_and_preview_does_not_mutate(self):
        audit = audit_intent("收盘价MA5上穿MA20", self.strategy)
        self.assertTrue(audit["application_blocked"])
        old = deepcopy(self.strategy)
        result = repair_strategy("上穿", self.strategy, {"relation": "cross_up", "ma_windows": [5, 20]})
        self.assertEqual(self.strategy, old)
        self.assertTrue(result["diff"])
        self.assertFalse(result["audit"]["application_blocked"])
        self.assertEqual(result["strategy"]["nodes"][-1]["op"], "cross_up")

    def test_persistent_position_and_fundamentals_are_blocked(self):
        for prompt in ("金叉买入并持有直到死叉卖出", "基于市盈率PE和ROE选股"):
            self.assertTrue(audit_intent(prompt, self.strategy)["application_blocked"])
        self.assertEqual(audit_intent("研究一个策略", self.strategy)["status"], "待确认")

    def test_explicit_contract_checks_allocation_and_input(self):
        audit = audit_intent("", self.strategy, {"field": "open", "ma_windows": [8, 20], "when_true": .5})
        self.assertEqual(sum(c["status"] == "存在差异" for c in audit["checks"]), 3)

    def test_signal_evidence_uses_lagged_day_and_exact_node_values(self):
        result = self.workspace.backtest(self.payload(), owner="evidence-owner")
        report = result["report"]
        row = next(r for r in report["records"] if r["traded_notional"] > 1e-12)
        pack = evidence_pack(report, row["date"])
        facts = {f["id"]: f["value"] for f in pack["facts"]}
        index = next(i for i, b in enumerate(self.bars) if b.date == row["executed_signal_date"])
        self.assertEqual(facts["N-price"], self.bars[index].values["close"])
        self.assertEqual(facts["N-fast"], statistics.mean(b.values["close"] for b in self.bars[index-4:index+1]))
        self.assertEqual(facts["R-fee"], row["fee"])
        with self.assertRaises(ValueError):
            self.workspace.research_evidence({"run_id": result["run_id"]}, "other-owner")

    def test_controlled_experiments_include_failure_and_match_independent_run(self):
        base = self.workspace.backtest(self.payload(), owner="experiment-owner")
        result = self.workspace.experiment({"run_id": base["run_id"], "axis": "cost_bps", "values": [0, 10, 10000], "confirmed": True}, "experiment-owner")
        self.assertEqual(result["results"][0]["metrics"], run_backtest(self.bars, self.strategy, cost_bps=0)["metrics"])
        self.assertEqual(result["results"][2]["status"], "FAILED")
        self.assertEqual(result["baseline"], base["report"]["metrics"])
        with self.assertRaises(ValueError):
            self.workspace.experiment({"run_id": base["run_id"], "axis": "lag", "values": [1], "confirmed": False}, "experiment-owner")

    def test_formula_matches_independent_real_ohlcv_computation(self):
        compiled, fid, _ = compile_factor("(close / MA(close, 20) - 1) * (volume / MA(volume, 20))")
        values = evaluate(compiled, self.bars)[fid]
        for i in (19, 40, len(self.bars)-1):
            price_mean = statistics.mean(b.values["close"] for b in self.bars[i-19:i+1])
            volume_mean = statistics.mean(b.values["volume"] for b in self.bars[i-19:i+1])
            expected = (self.bars[i].values["close"] / price_mean - 1) * self.bars[i].values["volume"] / volume_mean
            self.assertAlmostEqual(values[i], expected, places=12)

    def test_formula_prefix_invariance_and_lag_no_future(self):
        compiled, fid, _ = compile_factor("close / LAG(close, 1) - 1")
        full = evaluate(compiled, self.bars)[fid]
        prefix = evaluate(compiled, self.bars[:100])[fid]
        self.assertEqual(full[:100], prefix)
        self.assertIsNone(full[0])
        self.assertAlmostEqual(full[1], self.bars[1].values["close"] / self.bars[0].values["close"] - 1)

    def test_arbitrary_code_future_names_and_unset_windows_are_rejected(self):
        for expression in ("__import__('os').system('whoami')", "close.__class__", "LAG(close,-1)", "MA(close,n)", "PE", "close ** 2", "True", "[close][0]", "9"*500):
            with self.subTest(expression=expression[:50]), self.assertRaises(ValueError):
                compile_factor(expression)

    def test_zero_denominator_is_unavailable_not_fake_zero(self):
        compiled, fid, _ = compile_factor("close / (close - close)")
        self.assertTrue(all(value is None for value in evaluate(compiled, self.bars)[fid]))
        with self.assertRaises(ValueError):
            evaluate_factor({"sh600455": self.bars}, "close / (close-close)", self.protocol)

    def test_constant_correlations_and_average_tie_ranks(self):
        self.assertIsNone(correlation([1, 1, 1], [2, 3, 4]))
        self.assertEqual(ranks([3, 1, 1, 4]), [3, 1.5, 1.5, 4])
        self.assertAlmostEqual(correlation([1, 2, 3, 4], [4, 3, 2, 1]), -1)

    def test_training_correlation_is_independently_recomputed_with_purged_labels(self):
        result = evaluate_factor({"sh600455": self.bars}, "close / MA(close,20)-1", self.protocol)
        cut, horizon = int(len(self.bars)*.6), self.protocol["horizon"]
        x, y = [], []
        for i in range(19, cut-horizon):
            x.append(self.bars[i].values["close"] / statistics.mean(b.values["close"] for b in self.bars[i-19:i+1])-1)
            y.append(self.bars[i+horizon].values["close"] / self.bars[i].values["close"]-1)
        self.assertEqual(result["train"]["pairs"], len(x))
        self.assertAlmostEqual(result["train"]["correlation"], statistics.correlation(x, y), places=12)
        self.assertNotIn("correlation", result["test"])

    def test_short_real_interval_and_cross_section_mode_are_not_fabricated(self):
        with self.assertRaises(ValueError):
            evaluate_factor({"sh600455": self.bars[-47:]}, "close", self.protocol)
        with self.assertRaises(ValueError):
            evaluate_factor({"sh600455": self.bars}, "close", {**self.protocol, "mode": "cross_section"})

    def test_real_cross_section_ic_matches_independent_date_by_date_formula(self):
        codes = ["sh688047", "sh688506", "sh688521", "sh688981"]
        panel = {code: self.workspace.load_bars(code, "2023-02-01", "2026-08-19")[0] for code in codes}
        protocol = {**self.protocol, "mode": "cross_section", "horizon": 1}
        result = evaluate_factor(panel, "close / MA(close,20)-1", protocol)
        calendar = sorted({bar.date for bars in panel.values() for bar in bars})
        cut = int(len(calendar)*protocol["train_fraction"])
        lookup = {}
        for code, bars in panel.items():
            lookup[code] = {bar.date: (bar.values["close"], None if i<19 else bar.values["close"] / statistics.mean(b.values["close"] for b in bars[i-19:i+1])-1) for i, bar in enumerate(bars)}
        daily = []
        for i, day in enumerate(calendar[:cut-1]):
            samples = [(data[day][1], data[calendar[i+1]][0]/data[day][0]-1) for data in lookup.values() if day in data and calendar[i+1] in data and data[day][1] is not None]
            if len(samples)>=3:
                daily.append(statistics.correlation([p[0] for p in samples], [p[1] for p in samples]))
        self.assertEqual(result["train"]["valid_cross_sections"], len(daily))
        self.assertAlmostEqual(result["train"]["correlation"], statistics.mean(daily), places=12)

    def test_freezing_test_requires_validation_and_tracks_reuse(self):
        result = self.workspace.factor(self.factor_payload(), "factor-owner")
        self.assertFalse(result["test_revealed"])
        frozen = self.workspace.freeze_factor({"research_id": result["research_id"], "confirmed": True}, "factor-owner")
        self.assertTrue(frozen["test_revealed"])
        second = self.workspace.freeze_factor({"research_id": result["research_id"], "confirmed": True}, "factor-owner")
        self.assertGreaterEqual(second["test_views_before_this"], 1)
        with self.assertRaises(ValueError):
            self.workspace.freeze_factor({"research_id": result["research_id"], "confirmed": True}, "wrong-owner")
        with self.assertRaises(ValueError):
            self.workspace.factor_explain({"research_id": frozen["research_id"], "confirmed": True}, "factor-owner")

    def test_classification_references_do_not_assert_predictive_direction(self):
        compiled, _, expression = compile_factor("close / MA(close,20) * volume / MA(volume,20)")
        description = describe_factor(compiled, expression)
        self.assertIn("待验证", description["categories"][0]["type"])
        self.assertEqual(len(description["references"]), 3)
        self.assertTrue(all(r["url"].startswith("https://") for r in description["references"]))

    def test_journal_persists_and_public_document_has_no_owner(self):
        identifier = self.workspace.journal.append("fixture", {"value": "隔离专测，不是金融结果"}, "isolated")
        reopened = ResearchJournal(self.workspace.journal.path)
        self.assertEqual(reopened.get(identifier, "isolated"), {"value": "隔离专测，不是金融结果"})
        self.assertNotIn("owner", reopened.list("isolated")[0])
        with self.assertRaises(ValueError):
            reopened.get(identifier, None)

    def test_model_explanation_unknown_reference_and_new_numbers_rejected(self):
        pack = {"facts": [{"id": "M-one", "value": .2}]}
        evidence = {"model_returned": "TEST_FIXTURE_NOT_REAL_API"}
        for claim in ({"evidence_id": "unknown", "explanation": "文字"}, {"evidence_id": "M-one", "explanation": "收益100%"},
                      {"evidence_id": "M-one", "explanation": "样本有八百条"}, {"evidence_id": [], "explanation": "文字"}):
            with patch("finblocks.ai.research_call", return_value=({"claims": [claim]}, evidence)), self.assertRaises(AIError):
                explain_research(pack)

    def test_ai_factor_window_not_in_user_request_rejected(self):
        parsed = {"status": "ok", "expression": "close / MA(close, 50)", "hypothesis": "待验证", "failure_conditions": "可能失效"}
        with patch("finblocks.ai.research_call", return_value=(parsed, {})), self.assertRaises(AIError):
            generate_factor("研究MA20的价格偏离")

    def test_non_numeric_same_protocol_wording_is_not_treated_as_a_number(self):
        pack = {"facts": [{"id": "M-one", "value": .2}]}
        parsed = {"claims": [{"evidence_id": "M-one", "explanation": "应在同一协议下比较历史表现，不能保证未来有效。"}]}
        with patch("finblocks.ai.research_call", return_value=(parsed, {"model_returned": "TEST_FIXTURE_NOT_REAL_API"})):
            self.assertEqual(explain_research(pack)["claims"], parsed["claims"])

    def test_static_research_controls_have_unique_existing_ids(self):
        class IDs(HTMLParser):
            def __init__(self):
                super().__init__()
                self.ids = []
            def handle_starttag(self, tag, attrs):
                self.ids.extend(value for key, value in attrs if key == "id")
        parser = IDs()
        parser.feed((ROOT / "web/index.html").read_text(encoding="utf-8"))
        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        import re
        script = (ROOT / "web/research.js").read_text(encoding="utf-8")
        used = set(re.findall(r'\$\("([a-z][a-z0-9-]+)"\)', script))
        self.assertFalse((used - set(parser.ids)) - {"research-message"})  # 该节点由安装函数显式插入，也可通过变量引用。
        self.assertIn('id="research-message"', script)


if __name__ == "__main__":
    unittest.main()
