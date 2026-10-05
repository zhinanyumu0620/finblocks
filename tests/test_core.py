"""隔离的数学测试样例不是金融数据，不用于产品收益展示。"""

import copy
from datetime import date, timedelta
import math
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from finblocks.ai import AIError, generate_strategy
from finblocks.backtest import rebalance, run_backtest
from finblocks.data import Bar, DataError, load_daily, validate_bars
from finblocks.dsl import CompileError, compile_strategy, evaluate


def fixture_bars(prices=(10, 11, 12, 13, 12, 11, 10, 12)):
    # 构造输入仅验证公式和时点；不是伪造的历史金融样本。
    return [Bar("sh600000", "公式测试样例", (date(2020, 1, 1) + timedelta(days=i)).isoformat(),
                {"open": float(p), "high": float(p), "low": float(p), "close": float(p),
                 "previous_close": float(prices[i - 1] if i else p), "volume": 100.0}) for i, p in enumerate(prices)]


def fixture_strategy():
    return {"version": 1, "name": "公式测试", "nodes": [
        {"id": "p", "op": "field", "field": "close"},
        {"id": "a", "op": "ma", "input": "p", "window": 2},
        {"id": "b", "op": "ma", "input": "p", "window": 3},
        {"id": "s", "op": "gt", "left": "a", "right": "b"}],
        "signal": "s", "allocation": {"when_true": 1.0, "when_false": 0.0}}


class CompilerTests(unittest.TestCase):
    def test_manual_moving_average_and_warmup(self):
        c = compile_strategy(fixture_strategy())
        self.assertEqual(c.warmup, 3)
        out = evaluate(c, fixture_bars())
        self.assertEqual(out["b"][:4], [None, None, 11.0, 12.0])
        self.assertEqual(out["s"][:4], [None, None, True, True])

    def test_cycle_unknown_field_hidden_node_and_type(self):
        cases = []
        s = fixture_strategy(); s["nodes"][1]["input"] = "a"; cases.append(s)
        s = fixture_strategy(); s["nodes"][0]["field"] = "PE"; cases.append(s)
        s = fixture_strategy(); s["nodes"].append({"id": "hidden", "op": "field", "field": "close"}); cases.append(s)
        s = fixture_strategy(); s["nodes"][3]["op"] = "and"; cases.append(s)
        s = fixture_strategy(); s["nodes"][1]["window"] = True; cases.append(s)
        s = fixture_strategy(); s["allocation"]["when_true"] = math.nan; cases.append(s)
        s = fixture_strategy(); s["nodes"][0]["op"] = []; cases.append(s)
        s = fixture_strategy(); s["nodes"][0]["field"] = []; cases.append(s)
        for strategy in cases:
            with self.subTest(strategy=strategy), self.assertRaises(CompileError):
                compile_strategy(strategy)

    def test_cross_is_event_and_not_persistent_comparison(self):
        strategy = fixture_strategy(); strategy["nodes"][3]["op"] = "cross_up"
        out = evaluate(compile_strategy(strategy), fixture_bars((10, 11, 12, 13, 12, 11, 10, 14)))
        self.assertIsNone(out["s"][2])
        self.assertFalse(out["s"][3])
        self.assertTrue(out["s"][-1])


class DataTests(unittest.TestCase):
    def test_data_quality_rejects_missing_gap_envelope_and_zero_volume(self):
        bars = fixture_bars()
        self.assertTrue(validate_bars(bars)["passed"])
        for key, value in [("close", None), ("previous_close", 99), ("high", 1), ("volume", 0)]:
            changed = copy.deepcopy(bars); changed[2].values[key] = value
            with self.subTest(key=key):
                self.assertFalse(validate_bars(changed)["passed"])

    def test_duplicate_date_and_mixed_codes(self):
        bars = fixture_bars()
        self.assertFalse(validate_bars([bars[0], bars[0]])["passed"])
        other = Bar("sz000001", bars[1].name, bars[1].date, bars[1].values)
        self.assertFalse(validate_bars([bars[0], other])["passed"])

    def test_pathlike_code_rejected_before_zip_read(self):
        with self.assertRaises(DataError):
            load_daily(Path("not-used.zip"), "../private")


class BacktestTests(unittest.TestCase):
    def test_no_same_bar_execution(self):
        result = run_backtest(fixture_bars(), fixture_strategy(), cost_bps=0)
        records = result["records"]
        self.assertEqual(records[2]["target_weight"], 0)
        self.assertEqual(records[3]["executed_signal_date"], records[2]["date"])
        self.assertAlmostEqual(records[3]["equity"], 1)
        self.assertAlmostEqual(records[4]["equity"], 12 / 13)
        with self.assertRaises(ValueError):
            run_backtest(fixture_bars(), fixture_strategy(), cost_bps=0, execution_lag_bars=0)

    def test_prefix_invariance_and_future_changes(self):
        bars = fixture_bars()
        full = run_backtest(bars, fixture_strategy(), cost_bps=10)
        prefix = run_backtest(bars[:6], fixture_strategy(), cost_bps=10)
        self.assertEqual(full["records"][:6], prefix["records"])
        changed = fixture_bars((10, 11, 12, 13, 12, 11, 30, 40))
        future = run_backtest(changed, fixture_strategy(), cost_bps=10)
        self.assertEqual(full["records"][:6], future["records"][:6])

    def test_rebalance_conservation_and_post_fee_target(self):
        cash, units = 1.0, 0.0
        for target in (0.4, 0.8, 0.2, 0.0, 1.0):
            before = cash + units * 10
            cash, units, traded, fee = rebalance(cash, units, 10, target, 0.001)
            equity = cash + units * 10
            self.assertAlmostEqual(before - fee, equity)
            self.assertAlmostEqual(units * 10 / equity, target)
            self.assertAlmostEqual(fee, traded * 0.001)
            self.assertGreaterEqual(cash, -1e-12)

    def test_cost_is_paid_on_trades_and_last_signal_is_pending(self):
        free = run_backtest(fixture_bars(), fixture_strategy(), cost_bps=0)
        paid = run_backtest(fixture_bars(), fixture_strategy(), cost_bps=10)
        self.assertGreater(paid["metrics"]["fees"], 0)
        self.assertLess(paid["metrics"]["total_return"], free["metrics"]["total_return"])
        self.assertEqual(paid["last_signal_not_executed_within_sample"], [False])
        self.assertAlmostEqual(paid["records"][0]["buy_hold_equity"], 1 / 1.001)
        for r in paid["records"]:
            self.assertAlmostEqual(r["equity"], r["cash"] + r["units"] * r["close"])

    def test_short_empty_dirty_data_and_nonfinite_cost(self):
        for bars in ([], fixture_bars()[:3]):
            with self.assertRaises(ValueError):
                run_backtest(bars, fixture_strategy(), cost_bps=0)
        for cost in (math.nan, -1, 10000, True):
            with self.assertRaises(ValueError):
                run_backtest(fixture_bars(), fixture_strategy(), cost_bps=cost)


class AIFailureTests(unittest.TestCase):
    def test_missing_key_is_real_failure_not_mock_success(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(AIError):
            generate_strategy("生成MA5/20策略")

    def test_empty_truncated_and_invalid_outputs_are_rejected(self):
        # 只模拟失败响应来验证阻断；成功演示证据来自真实API。
        for content, finish in [("", "stop"), ('{"status":"ok"}', "length"), ("不是JSON", "stop"), ('{"status":"ok","strategy":{}}', "stop")]:
            response = {"choices": [{"finish_reason": finish, "message": {"content": content}}]}
            with self.subTest(content=content), patch("finblocks.ai._request", side_effect=[{"data": [{"id": "deepseek-flash"}]}, response]), self.assertRaises(AIError):
                generate_strategy("生成MA5/20策略")


if __name__ == "__main__":
    unittest.main()
