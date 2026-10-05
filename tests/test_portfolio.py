"""隔离公式测试，不把构造价格作为金融数据展示。"""

from copy import deepcopy
from datetime import date, timedelta
import math
import unittest

from finblocks.backtest import run_backtest
from finblocks.data import Bar, DataError
from finblocks.portfolio import rebalance_portfolio, run_portfolio


def bars(code, prices):
    return [Bar(code, "公式测试", (date(2020, 1, 1) + timedelta(days=i)).isoformat(),
                {"open": p, "high": p, "low": p, "close": p, "previous_close": prices[i-1] if i else p, "volume": 100.0})
            for i, p in enumerate(prices)]


def strategy(threshold=1):
    return {"version": 1, "name": "组合公式测试", "nodes": [{"id": "p", "op": "field", "field": "close"},
            {"id": "c", "op": "const", "value": threshold}, {"id": "s", "op": "gt", "left": "p", "right": "c"}],
            "signal": "s", "allocation": {"when_true": 1.0, "when_false": 0.0}}


class PortfolioTests(unittest.TestCase):
    def run_case(self, series, rule=None, **changes):
        options = {"cost_bps": 10, "execution_lag_bars": 1,
                   "portfolio": {"max_positions": 2, "position_cap": 1.0, "rebalance_every": 1}}
        options.update(changes)
        return run_portfolio(series, rule or strategy(), **options)

    def test_simultaneous_entries_share_capital_and_charge_all_fees(self):
        result = self.run_case({"sh600000": bars("sh600000", [10.0]*6), "sz000001": bars("sz000001", [20.0]*6)})
        entry = result["records"][1]
        self.assertEqual(len(entry["trades"]), 2)
        self.assertAlmostEqual(entry["equity"], 1/1.001, places=13)
        self.assertAlmostEqual(sum(h["value"] for h in entry["holdings"]), entry["equity"], places=13)
        self.assertTrue(all(abs(h["weight"]-.5)<1e-12 for h in entry["holdings"]))
        self.assertAlmostEqual(entry["fee"], sum(t["notional"] for t in entry["trades"])*.001, places=13)
        self.assertGreaterEqual(entry["cash"], 0)

    def test_known_fee_free_portfolio_growth_and_cash_cap(self):
        series={"sh600000": bars("sh600000", [10.,10.,20.,20.]), "sz000001": bars("sz000001", [20.,20.,20.,20.])}
        result=self.run_case(series,cost_bps=0)
        self.assertAlmostEqual(result["records"][2]["equity"],1.5)
        capped=self.run_case(series,cost_bps=0,portfolio={"max_positions":2,"position_cap":.25,"rebalance_every":1})
        self.assertAlmostEqual(capped["records"][1]["cash"],.5)
        self.assertAlmostEqual(capped["records"][2]["equity"],1.25)

    def test_signal_day_precedes_entry_exit_and_no_future_values(self):
        series={"sh600000":bars("sh600000",[9.,12.,8.,15.,9.,16.])}
        result=self.run_case(series,strategy(10))
        self.assertEqual([t["date"] for t in result["order_ledger"]][:3],["2020-01-03","2020-01-04","2020-01-05"])
        self.assertEqual(result["order_ledger"][0]["signal_date"],"2020-01-02")
        self.assertEqual(result["order_ledger"][0]["side"],"买入")
        self.assertEqual(result["order_ledger"][1]["side"],"卖出")
        prefix=self.run_case({code: rows[:4] for code,rows in series.items()},strategy(10))
        self.assertEqual(result["records"][:4],prefix["records"])

    def test_selection_order_and_delayed_rebalance_are_explicit(self):
        series={"sz000001":bars("sz000001",[20.]*6),"sh600000":bars("sh600000",[10.]*6)}
        result=self.run_case(series,portfolio={"max_positions":1,"position_cap":.25,"rebalance_every":2})
        self.assertEqual(result["records"][1]["selected_symbols"],["sh600000"])
        self.assertEqual(result["records"][1]["capacity_excluded"],["sz000001"])
        self.assertFalse(result["records"][2]["rebalance"])
        self.assertEqual(result["records"][2]["trades"],[])
        self.assertEqual(result["records"][2]["holdings"][0]["units"],result["records"][1]["holdings"][0]["units"])
        reversed_result=self.run_case(dict(reversed(list(series.items()))),portfolio=result["portfolio"])
        self.assertEqual(result["records"],reversed_result["records"])

    def test_sell_then_buy_rotation_preserves_cash_and_equity(self):
        cash, units, turnover, fee, trades=rebalance_portfolio(0, {"A":.1}, {"A":10,"B":20}, {"B":1}, .01)
        self.assertAlmostEqual(cash+units["B"]*20,1-fee,places=13)
        self.assertAlmostEqual(fee,turnover*.01,places=13)
        self.assertEqual([t["side"] for t in trades],["卖出","买入"])
        self.assertGreaterEqual(cash,0)
        self.assertAlmostEqual(units["B"]*20,.99/1.01,places=13)

    def test_failed_condition_exits_only_on_next_scheduled_rebalance(self):
        result=self.run_case({"sh600000":bars("sh600000",[12.,12.,8.,8.,12.,12.])},strategy(10),
                             portfolio={"max_positions":1,"position_cap":1.,"rebalance_every":3})
        self.assertTrue(result["records"][3]["holdings"])
        self.assertEqual(result["records"][3]["trades"],[])
        self.assertEqual(result["records"][4]["holdings"],[])
        self.assertEqual(result["records"][4]["trades"][0]["side"],"卖出")

    def test_single_asset_matches_existing_engine(self):
        series=bars("sh600000",[9.,12.,8.,15.,9.,16.]);rule=strategy(10)
        single=run_backtest(series,rule,cost_bps=10)
        multi=self.run_case({"sh600000":series},rule)
        for a,b in zip(single["records"],multi["records"]):
            for key in ["equity","fee","buy_hold_equity","cash"]:
                self.assertAlmostEqual(a[key],b[key],places=12)
        self.assertAlmostEqual(single["metrics"]["sharpe_ratio"],multi["metrics"]["sharpe_ratio"],places=12)

    def test_missing_calendar_bad_price_and_invalid_settings_are_rejected(self):
        a=bars("sh600000",[10.]*6);b=bars("sz000001",[20.]*6)
        with self.assertRaises(DataError):self.run_case({"sh600000":a,"sz000001":b[1:]})
        bad=deepcopy(a);bad[2].values["close"]=0
        with self.assertRaises(DataError):self.run_case({"sh600000":bad})
        for options in [{"max_positions":0,"position_cap":1,"rebalance_every":1},{"max_positions":2,"position_cap":math.nan,"rebalance_every":1}]:
            with self.assertRaises(ValueError):self.run_case({"sh600000":a},portfolio=options)
        rule=strategy();rule["allocation"]["when_false"]=.2
        with self.assertRaises(ValueError):self.run_case({"sh600000":a},rule)
        with self.assertRaises(ValueError):self.run_case({"sh600000":a},execution_lag_bars=0)
