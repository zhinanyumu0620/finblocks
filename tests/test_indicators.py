"""指标公式与真实行情前缀验证；构造序列仅用于数学测试。"""

import copy
from datetime import date, timedelta
from fractions import Fraction
import json
import math
from pathlib import Path
import unittest

from finblocks.backtest import run_backtest
from finblocks.data import Bar, load_daily
from finblocks.dsl import CompileError, compile_strategy, evaluate


ROOT = Path(__file__).resolve().parents[1]


def formula_bars(prices):
    return [Bar("sh600000", "公式测试样例", (date(2020, 1, 1) + timedelta(days=i)).isoformat(),
                {"close": value}) for i, value in enumerate(prices)]


def indicator_strategy(op, **parameters):
    return {"version": 1, "name": "指标公式测试", "nodes": [
        {"id": "price", "op": "field", "field": "close"},
        {"id": "indicator", "op": op, "input": "price", **parameters},
        {"id": "threshold", "op": "const", "value": 0},
        {"id": "condition", "op": "gt", "left": "indicator", "right": "threshold"}],
        "signal": "condition", "allocation": {"when_true": 1.0, "when_false": 0.0}}


class IndicatorFormulaTests(unittest.TestCase):
    def assert_series(self, actual, expected):
        self.assertEqual(len(actual), len(expected))
        for i, (a, b) in enumerate(zip(actual, expected)):
            with self.subTest(index=i):
                if b is None:
                    self.assertIsNone(a)
                else:
                    self.assertAlmostEqual(a, float(b), places=11)

    def test_ema_sma_seed_and_manual_recurrence(self):
        strategy = indicator_strategy("ema", window=3)
        compiled = compile_strategy(strategy)
        self.assertEqual(compiled.warmup, 3)
        result = evaluate(compiled, formula_bars([1, 2, 4, 3, 6, 5]))
        self.assert_series(result["indicator"], [None, None, Fraction(7, 3), Fraction(8, 3), Fraction(13, 3), Fraction(14, 3)])
        self.assertEqual(result["threshold"], [0] * 6)

    def test_ema_missing_value_restarts_seed(self):
        strategy = indicator_strategy("ema", window=3)
        actual = evaluate(compile_strategy(strategy), formula_bars([1, 2, 3, 4, None, 10, 12, 14, 16]))
        self.assert_series(actual["indicator"], [None, None, 2, 3, None, None, None, 12, 14])

    def test_rsi_wilder_manual_recurrence(self):
        compiled = compile_strategy(indicator_strategy("rsi", window=3))
        self.assertEqual(compiled.warmup, 4)
        actual = evaluate(compiled, formula_bars([1, 3, 2, 5, 4, 6]))
        # 初始平均涨幅=5/3、跌幅=1/3；下一期分别为10/9和5/9。
        self.assert_series(actual["indicator"], [None, None, None, Fraction(250, 3), Fraction(200, 3), Fraction(475, 6)])

    def test_rsi_flat_rising_falling_and_missing_values(self):
        compiled = compile_strategy(indicator_strategy("rsi", window=2))
        for prices, expected in [([3, 3, 3, 3], [None, None, 50, 50]),
                                 ([1, 2, 3, 4], [None, None, 100, 100]),
                                 ([4, 3, 2, 1], [None, None, 0, 0]),
                                 ([1, 2, 3, None, 8, 7, 6], [None, None, 100, None, None, None, 0])]:
            with self.subTest(prices=prices):
                self.assert_series(evaluate(compiled, formula_bars(prices))["indicator"], expected)

    def test_bollinger_population_standard_deviation_and_missing_window(self):
        prices = [1, 2, 3, None, 4, 4, 4]
        for band, offset in [("upper", 1), ("middle", 0), ("lower", -1)]:
            with self.subTest(band=band):
                compiled = compile_strategy(indicator_strategy("bollinger", window=3, multiplier=2, band=band))
                self.assertEqual(compiled.warmup, 3)
                expected = [None, None, 2 + offset * 2 * math.sqrt(2 / 3), None, None, None, 4]
                self.assert_series(evaluate(compiled, formula_bars(prices))["indicator"], expected)

    def test_macd_manual_fractions_and_histogram_convention(self):
        expected = {
            "line": [None, None, Fraction(5, 6), Fraction(7, 18), Fraction(37, 54), Fraction(55, 162)],
            "signal": [None, None, None, Fraction(11, 18), Fraction(107, 162), Fraction(217, 486)],
            "histogram": [None, None, None, Fraction(-2, 9), Fraction(2, 81), Fraction(-26, 243)],
        }
        for component, values in expected.items():
            with self.subTest(component=component):
                compiled = compile_strategy(indicator_strategy("macd", fast=2, slow=3, signal=2, component=component))
                self.assertEqual(compiled.warmup, 3 if component == "line" else 4)
                self.assert_series(evaluate(compiled, formula_bars([1, 2, 4, 3, 6, 5]))["indicator"], values)

    def test_macd_missing_values_reset_both_emas_and_signal(self):
        prices = [1, 2, 3, 4, None, 10, 11, 12, 13]
        for component in ("line", "signal", "histogram"):
            with self.subTest(component=component):
                compiled = compile_strategy(indicator_strategy("macd", fast=2, slow=3, signal=2, component=component))
                expected = ([None, None, 0.5, 0.5, None, None, None, 0.5, 0.5] if component == "line" else
                            [None, None, None, 0.5 if component == "signal" else 0, None, None, None, None,
                             0.5 if component == "signal" else 0])
                self.assert_series(evaluate(compiled, formula_bars(prices))["indicator"], expected)

    def test_nested_indicator_warmup_is_additive(self):
        for op, parameters, expected in [
            ("ema", {"window": 3}, 7), ("rsi", {"window": 3}, 8),
            ("bollinger", {"window": 3, "multiplier": 2, "band": "upper"}, 7),
            ("macd", {"fast": 2, "slow": 3, "signal": 2, "component": "histogram"}, 8),
        ]:
            strategy = indicator_strategy(op, **parameters)
            strategy["nodes"].insert(1, {"id": "smoothed", "op": "ma", "input": "price", "window": 5})
            strategy["nodes"][2]["input"] = "smoothed"
            with self.subTest(op=op):
                self.assertEqual(compile_strategy(strategy).warmup, expected)

    def test_const_rejects_boolean_nonfinite_huge_integer_and_extra_keys(self):
        for value in (True, False, None, "30", math.nan, math.inf, -math.inf, 10 ** 1000):
            strategy = indicator_strategy("ema", window=3)
            strategy["nodes"][2]["value"] = value
            with self.subTest(value_type=type(value).__name__), self.assertRaises(CompileError):
                compile_strategy(strategy)
        strategy = indicator_strategy("ema", window=3)
        strategy["nodes"][2]["extra"] = "hidden"
        with self.assertRaises(CompileError):
            compile_strategy(strategy)

    def test_indicator_parameters_are_strict_and_numeric_inputs_required(self):
        templates = [indicator_strategy("ema", window=3), indicator_strategy("rsi", window=3),
                     indicator_strategy("bollinger", window=3, multiplier=2, band="upper"),
                     indicator_strategy("macd", fast=2, slow=3, signal=2, component="histogram")]
        mutations = [
            (0, "window", True), (0, "window", 1), (1, "window", 501), (1, "window", 3.0),
            (2, "window", False), (2, "multiplier", True), (2, "multiplier", 0),
            (2, "multiplier", 11), (2, "multiplier", math.inf), (2, "multiplier", "2"),
            (2, "band", []), (2, "band", "sideways"),
            (3, "fast", True), (3, "fast", 3), (3, "slow", 501), (3, "signal", 1),
            (3, "signal", True), (3, "component", []), (3, "component", "macd"),
        ]
        for template_index, key, value in mutations:
            strategy = copy.deepcopy(templates[template_index])
            strategy["nodes"][1][key] = value
            with self.subTest(op=strategy["nodes"][1]["op"], key=key, value=value), self.assertRaises(CompileError):
                compile_strategy(strategy)
        for template in templates:
            for mutation in ("extra", "missing", "boolean", "cycle"):
                strategy = copy.deepcopy(template)
                if mutation == "extra":
                    strategy["nodes"][1]["extra"] = 0
                elif mutation == "missing":
                    del strategy["nodes"][1]["input"]
                elif mutation == "boolean":
                    strategy["nodes"][1]["input"] = "precondition"
                    strategy["nodes"].append({"id": "precondition", "op": "gt", "left": "price", "right": "threshold"})
                else:
                    strategy["nodes"][1]["input"] = "indicator"
                with self.subTest(op=template["nodes"][1]["op"], mutation=mutation), self.assertRaises(CompileError):
                    compile_strategy(strategy)

    def test_const_only_numeric_signal_is_rejected(self):
        strategy = {"version": 1, "name": "无条件值测试", "nodes": [{"id": "constant", "op": "const", "value": 0}],
                    "signal": "constant", "allocation": {"when_true": 1, "when_false": 0}}
        with self.assertRaises(CompileError):
            compile_strategy(strategy)


@unittest.skipUnless((ROOT / "data/demo_manifest.json").exists(), "先建立真实数据演示清单")
class RealIndicatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        stats = json.loads((ROOT / "docs/audit_evidence/data_statistics.json").read_text(encoding="utf-8"))
        manifest = json.loads((ROOT / "data/demo_manifest.json").read_text(encoding="utf-8"))
        cls.bars, cls.source = load_daily(ROOT / stats["archives"][0]["path"], manifest["selected"][0]["code"])

    def test_real_data_all_indicator_prefixes_are_causal(self):
        templates = [indicator_strategy("ema", window=20), indicator_strategy("rsi", window=14),
                     indicator_strategy("bollinger", window=20, multiplier=2, band="upper")]
        templates.extend(indicator_strategy("macd", fast=12, slow=26, signal=9, component=component)
                         for component in ("line", "signal", "histogram"))
        for strategy in templates:
            compiled = compile_strategy(strategy)
            full = evaluate(compiled, self.bars)
            prefix = evaluate(compiled, self.bars[:200])
            with self.subTest(indicator=strategy["nodes"][1]):
                self.assertEqual(full["indicator"][:200], prefix["indicator"])
                self.assertEqual(full["condition"][:200], prefix["condition"])
                self.assertIsNone(full["indicator"][compiled.warmup - 2])
                self.assertIsNotNone(full["indicator"][compiled.warmup - 1])

    def test_real_macd_and_bollinger_examples_execute_with_lag(self):
        for name in ("macd_strategy.json", "bollinger_strategy.json"):
            strategy = json.loads((ROOT / "examples" / name).read_text(encoding="utf-8"))
            full = run_backtest(self.bars, strategy, cost_bps=10)
            prefix = run_backtest(self.bars[:200], strategy, cost_bps=10)
            with self.subTest(example=name):
                self.assertEqual(full["records"][:200], prefix["records"])
                for record in full["records"]:
                    if record["executed_signal_date"]:
                        self.assertLess(record["executed_signal_date"], record["date"])


if __name__ == "__main__":
    unittest.main()
