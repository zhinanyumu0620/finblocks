"""统计公式的隔离样例只用于测试；真实收益核对读取已审计的本地行情。"""

from datetime import date, timedelta
import json
import math
from pathlib import Path
import unittest

from finblocks.backtest import _return_statistics, run_backtest
from finblocks.data import Bar, load_daily


ROOT = Path(__file__).resolve().parents[1]


def formula_bars(prices=(10, 11, 12, 13, 12, 11, 10, 12)):
    # 明示为数学样例，不进入产品展示或历史金融数据目录。
    return [Bar("sh600000", "统计公式样例", (date(2020, 1, 1) + timedelta(days=i)).isoformat(),
                {"open": float(price), "high": float(price), "low": float(price),
                 "close": float(price), "previous_close": float(prices[i - 1] if i else price),
                 "volume": 100.0}) for i, price in enumerate(prices)]


def formula_strategy(weight=1.0):
    return {"version": 1, "name": "统计公式样例", "nodes": [
        {"id": "p", "op": "field", "field": "close"},
        {"id": "m", "op": "ma", "input": "p", "window": 2},
        {"id": "s", "op": "gt", "left": "p", "right": "m"}],
        "signal": "s", "allocation": {"when_true": weight, "when_false": weight}}


def independent_statistics(equities, periods, annual_rf):
    # 与实现独立的直算公式：未缩放、未用对数或 expm1。
    returns = [equities[i] / equities[i - 1] - 1 for i in range(1, len(equities))]
    mean = sum(returns) / len(returns)
    deviation = math.sqrt(sum((value - mean) ** 2 for value in returns) / (len(returns) - 1))
    periodic_rf = (1 + annual_rf) ** (1 / periods) - 1
    return ((mean - periodic_rf) / deviation * math.sqrt(periods),
            deviation * math.sqrt(periods),
            (equities[-1] / equities[0]) ** (periods / len(returns)) - 1)


class MetricsFormulaTests(unittest.TestCase):
    def test_independent_sample_std_sharpe_and_cagr(self):
        equities = [1.0, 1.1, 0.99, 1.188]
        statistics = _return_statistics(equities, 12, 0.0)
        sharpe, volatility, annual_return = independent_statistics(equities, 12, 0.0)
        self.assertEqual(statistics["return_periods"], 3)
        self.assertAlmostEqual(statistics["sharpe_ratio"], sharpe, places=12)
        self.assertAlmostEqual(statistics["annualized_volatility"], volatility, places=12)
        self.assertAlmostEqual(statistics["annualized_return"], annual_return, places=12)
        self.assertIsNone(statistics["sharpe_unavailable_reason"])

    def test_risk_free_and_annualization_are_effective_inputs(self):
        equities = [1.0, 1.1, 0.99, 1.188]
        zero_rf = _return_statistics(equities, 12, 0.0)
        with_rf = _return_statistics(equities, 12, 0.12)
        changed_periods = _return_statistics(equities, 24, 0.12)
        for periods, result in ((12, with_rf), (24, changed_periods)):
            expected = independent_statistics(equities, periods, 0.12)
            for key, value in zip(("sharpe_ratio", "annualized_volatility", "annualized_return"), expected):
                self.assertAlmostEqual(result[key], value, places=11)
        self.assertLess(with_rf["sharpe_ratio"], zero_rf["sharpe_ratio"])
        self.assertEqual(with_rf["annualized_volatility"], zero_rf["annualized_volatility"])
        self.assertEqual(with_rf["annualized_return"], zero_rf["annualized_return"])
        self.assertNotEqual(changed_periods["sharpe_ratio"], with_rf["sharpe_ratio"])

    def test_flat_cash_has_no_sharpe_but_zero_volatility(self):
        result = run_backtest(formula_bars(), formula_strategy(0.0), cost_bps=10,
                              annual_risk_free_rate=0.03)
        metrics = result["metrics"]
        self.assertIsNone(metrics["sharpe_ratio"])
        self.assertIn("波动为零", metrics["sharpe_unavailable_reason"])
        self.assertEqual(metrics["annualized_volatility"], 0)
        self.assertEqual(metrics["annualized_return"], 0)
        json.dumps(result, ensure_ascii=False, allow_nan=False)

    def test_short_sequence_has_explicit_reason(self):
        for equities, count in (([], 0), ([1.0], 0), ([1.0, 1.1], 1)):
            with self.subTest(equities=equities):
                result = _return_statistics(equities, 252, 0)
                self.assertEqual(result["return_periods"], count)
                self.assertIsNone(result["sharpe_ratio"])
                self.assertIsNone(result["annualized_volatility"])
                self.assertTrue(result["sharpe_unavailable_reason"])

    def test_overflow_never_returns_nan_or_infinity(self):
        for equities in ([1e-300, 1e300, 1e-300], [1.0, 1e200, 1e300]):
            with self.subTest(equities=equities):
                result = _return_statistics(equities, 252, 0)
                json.dumps(result, allow_nan=False)
                for key in ("sharpe_ratio", "annualized_volatility", "annualized_return"):
                    value = result[key]
                    self.assertTrue(value is None or math.isfinite(value))
        growth_overflow = _return_statistics([1.0, 1e200, 1e300], 252, 0)
        self.assertIsNone(growth_overflow["annualized_return"])
        self.assertIn("有限数值", growth_overflow["annualized_return_unavailable_reason"])

    def test_invalid_parameter_types_and_ranges(self):
        for periods in (True, False, 0, -1, 367, 252.0, "252", math.nan, math.inf):
            with self.subTest(periods=periods), self.assertRaises(ValueError):
                run_backtest(formula_bars(), formula_strategy(), cost_bps=0, periods_per_year=periods)
        for annual_rf in (True, False, -1, -2, 1.01, "0", math.nan, math.inf, -math.inf):
            with self.subTest(annual_rf=annual_rf), self.assertRaises(ValueError):
                run_backtest(formula_bars(), formula_strategy(), cost_bps=0,
                             annual_risk_free_rate=annual_rf)
        for periods, annual_rf in ((1, -0.99), (366, 1.0)):
            run_backtest(formula_bars(), formula_strategy(), cost_bps=0,
                         periods_per_year=periods, annual_risk_free_rate=annual_rf)

    def test_old_call_and_explicit_defaults_keep_identical_ledger(self):
        original_call = run_backtest(formula_bars(), formula_strategy(), cost_bps=10)
        explicit = run_backtest(formula_bars(), formula_strategy(), cost_bps=10,
                                 periods_per_year=252, annual_risk_free_rate=0.0)
        self.assertEqual(original_call, explicit)
        changed = run_backtest(formula_bars(), formula_strategy(), cost_bps=10,
                               periods_per_year=250, annual_risk_free_rate=0.04)
        self.assertEqual(original_call["records"], changed["records"])
        for key in ("total_return", "buy_hold_return", "fees", "trade_records", "max_drawdown"):
            self.assertEqual(original_call["metrics"][key], changed["metrics"][key])
        self.assertEqual(changed["assumptions"]["periods_per_year"], 250)
        self.assertEqual(changed["assumptions"]["annual_risk_free_rate"], 0.04)

    def test_benchmark_initial_fee_excluded_from_period_statistics(self):
        result = run_backtest(formula_bars((10,) * 8), formula_strategy(0.0), cost_bps=10)
        self.assertLess(result["metrics"]["buy_hold_return"], 0)
        self.assertIsNone(result["metrics"]["buy_hold_sharpe_ratio"])
        self.assertIn("波动为零", result["metrics"]["buy_hold_sharpe_unavailable_reason"])
        benchmark = _return_statistics([record["buy_hold_equity"] for record in result["records"]], 252, 0)
        self.assertEqual(benchmark["annualized_return"], 0)
        self.assertEqual(benchmark["annualized_volatility"], 0)


@unittest.skipUnless((ROOT / "data/demo_manifest.json").exists(), "先建立真实行情清单")
class RealMetricsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        manifest = json.loads((ROOT / "data/demo_manifest.json").read_text(encoding="utf-8"))
        stats = json.loads((ROOT / "docs/audit_evidence/data_statistics.json").read_text(encoding="utf-8"))
        archive = ROOT / stats["archives"][0]["path"]
        cls.bars, cls.source = load_daily(archive, manifest["selected"][0]["code"])
        cls.strategy = json.loads((ROOT / "examples/ma_strategy.json").read_text(encoding="utf-8"))

    def test_real_returns_match_independent_formula_with_nonzero_rf(self):
        result = run_backtest(self.bars, self.strategy, cost_bps=10,
                              periods_per_year=250, annual_risk_free_rate=0.02)
        equities = [record["equity"] for record in result["records"]]
        expected = independent_statistics(equities, 250, 0.02)
        for key, value in zip(("sharpe_ratio", "annualized_volatility", "annualized_return"), expected):
            self.assertAlmostEqual(result["metrics"][key], value, places=12)
        self.assertEqual(result["metrics"]["return_periods"], len(self.bars) - 1)
        self.assertEqual([record["period_return"] for record in result["records"]][1:],
                         [equities[i] / equities[i - 1] - 1 for i in range(1, len(equities))])
        json.dumps(result, ensure_ascii=False, allow_nan=False)

    def test_real_benchmark_uses_the_same_observed_intervals(self):
        result = run_backtest(self.bars, self.strategy, cost_bps=10)
        equities = [record["buy_hold_equity"] for record in result["records"]]
        expected, _, _ = independent_statistics(equities, 252, 0.0)
        self.assertAlmostEqual(result["metrics"]["buy_hold_sharpe_ratio"], expected, places=12)
        self.assertIn("预热期", result["assumptions"]["statistics_scope"])
        self.assertIn("非官方交易日历", result["assumptions"]["annualization_basis"])


if __name__ == "__main__":
    unittest.main()
