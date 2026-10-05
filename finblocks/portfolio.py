"""多标的共享现金研究回测：按过去信号选股，扣费后求解目标权重。"""

import math

from .backtest import _return_statistics, validate_metric_parameters
from .data import DataError, require_valid
from .dsl import compile_strategy, evaluate


def validate_portfolio_options(options):
    if not isinstance(options, dict) or set(options) != {"max_positions", "position_cap", "rebalance_every"}:
        raise ValueError("组合设置须明确最大持股数、单股上限和调仓间隔")
    for key, upper in (("max_positions", 300), ("rebalance_every", 60)):
        if type(options[key]) is not int or not 1 <= options[key] <= upper:
            raise ValueError(f"{key}须为1–{upper}的整数，这是项目参数范围")
    if type(options["position_cap"]) not in (int, float) or not math.isfinite(options["position_cap"]) or not 0 < options["position_cap"] <= 1:
        raise ValueError("单股仓位上限须为大于0且不超过1的有限比例")


def rebalance_portfolio(cash, units, prices, weights, cost_rate):
    """统一求解E'=E-cΣ|w_i E'-v_i|，避免每只股票独立花掉同一份现金。"""
    if not 0 <= cost_rate < 1 or any(w < 0 or not math.isfinite(w) for w in weights.values()) or math.fsum(weights.values()) > 1 + 1e-12:
        raise ValueError("组合权重或成本违反无杠杆约束")
    codes = sorted(set(units) | set(weights))
    if any(code not in prices or not math.isfinite(prices[code]) or prices[code] <= 0 for code in codes):
        raise DataError("持仓或目标标的缺少有限正价格，不能补值成交")
    old = {code: units.get(code, 0.0) * prices[code] for code in codes}
    equity = cash + math.fsum(old.values())
    if not math.isfinite(equity) or equity <= 0 or cash < -1e-12 or any(value < -1e-12 for value in old.values()):
        raise ValueError("组合资金或原持仓无效")
    low, high = 0.0, equity
    for _ in range(64):
        middle = (low + high) / 2
        turnover = math.fsum(abs(weights.get(code, 0.0) * middle - old[code]) for code in codes)
        if middle + cost_rate * turnover > equity:
            high = middle
        else:
            low = middle
    post_equity = (low + high) / 2
    values = {code: weights.get(code, 0.0) * post_equity for code in codes}
    deltas = {code: values[code] - old[code] for code in codes}
    fees = {code: abs(deltas[code]) * cost_rate for code in codes}
    total_fee = math.fsum(fees.values())
    new_cash = equity - total_fee - math.fsum(values.values())
    if new_cash < -1e-10 * equity:
        raise ValueError("调仓后现金不足，不能通过杠杆买入")
    new_cash = max(0.0, new_cash)
    new_units = {code: values[code] / prices[code] for code in codes if values[code] > 0}
    trades = [{"symbol": code, "side": "买入" if deltas[code] > 0 else "卖出",
               "price": prices[code], "units_change": deltas[code] / prices[code],
               "notional": abs(deltas[code]), "fee": fees[code]}
              for code in sorted(codes, key=lambda code: (deltas[code] > 0, code)) if abs(deltas[code]) > 1e-12]
    return new_cash, new_units, math.fsum(abs(value) for value in deltas.values()), total_fee, trades


def run_portfolio(series, strategy, *, cost_bps, execution_lag_bars=1, portfolio,
                  periods_per_year=252, annual_risk_free_rate=0.0):
    validate_portfolio_options(portfolio)
    compiled = compile_strategy(strategy)
    if strategy["allocation"]["when_false"] != 0:
        raise ValueError("股票池筛选模式要求否则持仓比例为0；满足条件总仓位用于所选股票等权分配")
    if type(cost_bps) not in (int, float) or not math.isfinite(cost_bps) or not 0 <= cost_bps < 10000:
        raise ValueError("成本须为0至小于10000的有限基点数")
    if type(execution_lag_bars) is not int or not 1 <= execution_lag_bars <= 10:
        raise ValueError("组合信号须滞后1–10条共同日历记录")
    validate_metric_parameters(periods_per_year, annual_risk_free_rate)
    if not isinstance(series, dict) or not 1 <= len(series) <= 300:
        raise ValueError("须明确选择1–300个不重复标的")
    codes = sorted(series)
    dates = None
    signals = {}
    for code in codes:
        bars = series[code]
        require_valid(bars)
        if any(bar.code != code for bar in bars):
            raise DataError("股票池代码与实际行情不一致")
        current = [bar.date for bar in bars]
        if dates is None:
            dates = current
        elif dates != current:
            raise DataError(f"{code}缺少共同日历记录；不静默取日期交集或前向填充")
        if len(bars) < compiled.warmup + execution_lag_bars + 1:
            raise DataError(f"{code}记录不足，不能覆盖预热、信号滞后及持仓收益")
        signals[code] = evaluate(compiled, bars)[strategy["signal"]]
    cost_rate = cost_bps / 10000
    budget = strategy["allocation"]["when_true"]
    cash, units = 1.0, {}
    first_prices = {code: series[code][0].values["close"] for code in codes}
    base_weight = min(budget / len(codes), portfolio["position_cap"])
    base_cash, base_units, _, _, _ = rebalance_portfolio(1.0, {}, first_prices, {code: base_weight for code in codes}, cost_rate)
    peak, max_drawdown, previous_equity = 1.0, 0.0, 1.0
    fees_total, traded_total, trade_count, rebalance_count = 0.0, 0.0, 0, 0
    records, order_ledger = [], []
    selected, weights = [], {}
    for index, day in enumerate(dates):
        prices = {code: series[code][index].values["close"] for code in codes}
        before = cash + math.fsum(amount * prices[code] for code, amount in units.items())
        signal_index = index - execution_lag_bars
        matched = [code for code in codes if signal_index >= 0 and signals[code][signal_index] is True]
        is_rebalance = signal_index >= 0 and signal_index % portfolio["rebalance_every"] == 0
        traded, fee, trades = 0.0, 0.0, []
        if is_rebalance:
            selected = matched[:portfolio["max_positions"]]
            weight = min(budget / len(selected), portfolio["position_cap"]) if selected else 0.0
            weights = {code: weight for code in selected}
            cash, units, traded, fee, trades = rebalance_portfolio(cash, units, prices, weights, cost_rate)
            rebalance_count += 1
        signal_day = dates[signal_index] if signal_index >= 0 else None
        usable = signal_index >= 0 and any(signals[code][signal_index] is not None for code in codes)
        for trade in trades:
            order_ledger.append({"date": day, "signal_date": signal_day, **trade})
        equity = cash + math.fsum(amount * prices[code] for code, amount in units.items())
        if not math.isfinite(equity) or equity <= 0 or cash < -1e-12:
            raise ValueError("组合净值或现金违反无杠杆约束")
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1)
        fees_total += fee
        traded_total += traded
        trade_count += len(trades)
        records.append({"date": day, "signal": any(signals[code][index] is True for code in codes) if any(signals[code][index] is not None for code in codes) else None,
                        "executed_signal_date": signal_day if is_rebalance and usable else None,
                        "executed_signal": bool(matched) if is_rebalance and usable else None,
                        "target_weight": math.fsum(weights.values()), "cash": cash,
                        "equity_before_trade": before, "equity": equity, "fee": fee,
                        "traded_notional": traded, "period_return": equity / previous_equity - 1,
                        "buy_hold_equity": base_cash + math.fsum(amount * prices[code] for code, amount in base_units.items()),
                        "drawdown": equity / peak - 1, "rebalance": is_rebalance,
                        "matched_symbols": matched, "selected_symbols": list(selected),
                        "capacity_excluded": matched[portfolio["max_positions"]:] if is_rebalance else [],
                        "holdings": [{"symbol": code, "units": amount, "price": prices[code],
                                      "value": amount * prices[code], "weight": amount * prices[code] / equity,
                                      "target_weight": weights.get(code, 0.0)} for code, amount in sorted(units.items())],
                        "trades": trades})
        previous_equity = equity
    stats = _return_statistics([row["equity"] for row in records], periods_per_year, annual_risk_free_rate)
    base_stats = _return_statistics([row["buy_hold_equity"] for row in records], periods_per_year, annual_risk_free_rate)
    return {"kind": "portfolio", "strategy": strategy, "portfolio": portfolio, "symbols": codes,
            "warmup_records": compiled.warmup, "quality": {"passed": True, "gap_tolerance": 0.0},
            "assumptions": {"initial_capital": "整个股票池共享1个归一化资本单位，不是每股各1单位",
                            "execution": f"逐股收盘计算条件，滞后{execution_lag_bars}条共同记录，于调仓日收盘先卖后买；新持仓之后承担收益",
                            "execution_lag_bars": execution_lag_bars, "cost_bps": cost_bps,
                            "cost_basis": "每笔买卖按绝对成交金额计费，扣费后共同求解目标权重；成本为研究情景",
                            "fractional_units": True, "leverage": False, "terminal_liquidation": False,
                            "selection": "仅用已滞后的真信号选股；超出最大持股数按代码升序取前N，非收益排名或优化",
                            "allocation": "满足条件持仓比例为组合总预算，所选股等权且受单股上限约束，余额留现金；否则比例须为0",
                            "exit": "每个调仓日不再入选的股票卖出，间隔内保持股数不变；穿越信号只在发生当日为真",
                            "calendar": "要求所选股共同日历完整一致，不删日期、不补价格；不是外部核验交易日历",
                            "benchmark": "同一股票池初日按总预算与单股上限等权买入，之后不调仓；不是沪深300指数收益",
                            "price_basis": "源收盘价，复权口径未外部核验，不包含分红再投资",
                            "periods_per_year": periods_per_year, "annual_risk_free_rate": annual_risk_free_rate,
                            "statistics_scope": "包括预热现金及区间内费用；相邻净值计算统计，首日基准建仓费计累计收益，不计首条之前虚拟收益",
                            "annualization_basis": "按实际共同记录相邻收益年化；周期数与无风险利率为显式研究假设",
                            "sharpe_formula": "mean(r-rf_period)/sample_std(r,ddof=1)*sqrt(periods_per_year)",
                            "annualization_limitations": "平方根年化依赖平稳与近似独立等假设，本项目未验证；不预测未来绩效",
                            "not_modelled": ["整手交易", "完整T+1撮合", "涨跌停与停牌成交限制", "滑点", "分红与企业行动", "实盘成交"]},
            "metrics": {"rows": len(dates), "start": dates[0], "end": dates[-1],
                        "total_return": records[-1]["equity"] - 1, "buy_hold_return": records[-1]["buy_hold_equity"] - 1,
                        "max_drawdown": max_drawdown, "trade_records": trade_count, "rebalance_records": rebalance_count,
                        "traded_notional": traded_total, "fees": fees_total, **stats,
                        "buy_hold_sharpe_ratio": base_stats["sharpe_ratio"],
                        "buy_hold_sharpe_unavailable_reason": base_stats["sharpe_unavailable_reason"]},
            "last_signal_not_executed_within_sample": {code: signals[code][-execution_lag_bars:] for code in codes},
            "records": records, "order_ledger": order_ledger}
