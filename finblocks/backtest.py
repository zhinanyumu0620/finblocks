"""日频分数持仓研究：信号之后成交，现金和资产守恒。"""

import math

from .data import PRICE_GAP_TOLERANCE, require_valid
from .dsl import compile_strategy, evaluate


def rebalance(cash, units, price, target, cost_rate):
    equity = cash + units * price
    old_value = units * price
    if target * equity >= old_value:
        new_value = target * (equity + cost_rate * old_value) / (1 + target * cost_rate)
    else:
        new_value = target * (equity - cost_rate * old_value) / (1 - target * cost_rate)
    traded = abs(new_value - old_value)
    fee = traded * cost_rate
    # 目标比例按扣除费用后的净值求解，不忽略分数持仓的比例漂移。
    return equity - fee - new_value, new_value / price, traded, fee


def validate_metric_parameters(periods_per_year, annual_risk_free_rate):
    """共用回测、保存和命令行的研究统计参数门槛。"""
    if type(periods_per_year) is not int or not 1 <= periods_per_year <= 366:
        raise ValueError("每年收益区间数必须是 1–366 的整数，此为可编辑研究假设而非官方交易日历")
    if (type(annual_risk_free_rate) not in (int, float)
            or not math.isfinite(annual_risk_free_rate) or not -1 < annual_risk_free_rate <= 1):
        raise ValueError("年化无风险利率须为 -1 至 1 的有限小数，且不能等于 -1；此为研究参数范围")


def _return_statistics(equities, periods_per_year, annual_risk_free_rate):
    """按相邻真实净值区间统计，不把首条之前的虚拟资本变化当作收益。"""
    count = max(len(equities) - 1, 0)
    result = {
        "return_periods": count, "sharpe_ratio": None,
        "annualized_volatility": None, "annualized_return": None,
        "sharpe_unavailable_reason": None,
        "annualized_volatility_unavailable_reason": None,
        "annualized_return_unavailable_reason": None,
    }
    if count == 0:
        reason = "没有相邻净值收益区间"
        result.update({key: reason for key in (
            "sharpe_unavailable_reason", "annualized_volatility_unavailable_reason",
            "annualized_return_unavailable_reason")})
        return result
    try:
        # 用对数差避免直接计算首末净值比率溢出；不能表示的年化收益返回空值。
        growth = math.expm1((math.log(equities[-1]) - math.log(equities[0]))
                           * periods_per_year / count)
        if not math.isfinite(growth):
            raise OverflowError
        result["annualized_return"] = growth
    except (ValueError, OverflowError, ZeroDivisionError):
        result["annualized_return_unavailable_reason"] = "年化收益超出有限数值范围"
    if count < 2:
        reason = "收益区间不足：样本标准差与夏普至少需要 2 个收益区间"
        result["sharpe_unavailable_reason"] = reason
        result["annualized_volatility_unavailable_reason"] = reason
        return result
    try:
        returns = [current / previous - 1 for previous, current in zip(equities, equities[1:])]
        if any(not math.isfinite(value) for value in returns):
            raise OverflowError
        # 先缩放再求均值和样本标准差，避免大数平方或求和提前溢出。
        scale = max(abs(value) for value in returns)
        scaled = [value / scale for value in returns] if scale else [0.0] * count
        mean_scaled = math.fsum(scaled) / count
        variance_scaled = math.fsum((value - mean_scaled) ** 2 for value in scaled) / (count - 1)
        deviation = math.sqrt(variance_scaled) * scale
        if not math.isfinite(deviation):
            raise OverflowError
        volatility = deviation * math.sqrt(periods_per_year)
        if math.isfinite(volatility):
            result["annualized_volatility"] = volatility
        else:
            result["annualized_volatility_unavailable_reason"] = "年化波动率超出有限数值范围"
        if deviation == 0:
            result["sharpe_unavailable_reason"] = "收益波动为零，夏普比率无定义"
            return result
        daily_rf = math.expm1(math.log1p(annual_risk_free_rate) / periods_per_year)
        sharpe = (mean_scaled * scale - daily_rf) / deviation * math.sqrt(periods_per_year)
        if math.isfinite(sharpe):
            result["sharpe_ratio"] = sharpe
        else:
            result["sharpe_unavailable_reason"] = "夏普比率超出有限数值范围"
    except (ValueError, OverflowError, ZeroDivisionError):
        reason = "区间收益或标准差超出有限数值范围"
        result["sharpe_unavailable_reason"] = reason
        result["annualized_volatility_unavailable_reason"] = reason
    return result


def run_backtest(bars, strategy, *, cost_bps, execution_lag_bars=1,
                 gap_tolerance=PRICE_GAP_TOLERANCE, periods_per_year=252,
                 annual_risk_free_rate=0.0):
    if type(cost_bps) not in (int, float) or not math.isfinite(cost_bps) or not 0 <= cost_bps < 10000:
        raise ValueError("成本情景须显式填写，且为 0 至小于 10000 的有限基点数")
    if type(execution_lag_bars) is not int or not 1 <= execution_lag_bars <= 10:
        raise ValueError("收盘信号必须滞后 1–10 条记录生效，不能同日或使用未来信息")
    validate_metric_parameters(periods_per_year, annual_risk_free_rate)
    quality = require_valid(bars, gap_tolerance)
    compiled = compile_strategy(strategy)
    if len(bars) < compiled.warmup + execution_lag_bars + 1:
        raise ValueError("记录不足：必须覆盖指标预热、信号滞后和至少一个持仓收益区间")
    outputs = evaluate(compiled, bars)
    signals = outputs[strategy["signal"]]
    cost_rate = cost_bps / 10000
    cash, units = 1.0, 0.0
    base_cash, base_units, _, _ = rebalance(1.0, 0.0, bars[0].values["close"], 1.0, cost_rate)
    previous_equity, peak, max_drawdown = 1.0, 1.0, 0.0
    traded_total, fees_total, trades = 0.0, 0.0, 0
    records = []
    for i, bar in enumerate(bars):
        close = bar.values["close"]
        before_equity = cash + units * close
        weight_before = units * close / before_equity
        signal_index = i - execution_lag_bars
        executed_signal = signals[signal_index] if signal_index >= 0 else None
        target = strategy["allocation"]["when_true" if executed_signal else "when_false"] if executed_signal is not None else 0.0
        cash, units, traded, fee = rebalance(cash, units, close, target, cost_rate)
        equity = cash + units * close
        if cash < -1e-12 or units < -1e-12 or not math.isfinite(equity) or equity <= 0:
            raise ValueError("计算违反无杠杆现金/资产约束")
        traded_total += traded
        fees_total += fee
        trades += int(traded > 1e-12)
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1)
        records.append({
            "date": bar.date, "close": close, "signal": signals[i],
            "executed_signal_date": bars[signal_index].date if signal_index >= 0 and executed_signal is not None else None,
            "executed_signal": executed_signal, "target_weight": target,
            "weight_before_trade": weight_before, "weight_after_trade": units * close / equity,
            "cash": cash, "units": units, "traded_notional": traded,
            "fee": fee, "equity_before_trade": before_equity, "equity": equity,
            "period_return": equity / previous_equity - 1,
            "buy_hold_equity": base_cash + base_units * close,
            "drawdown": equity / peak - 1,
        })
        previous_equity = equity
    statistics = _return_statistics([record["equity"] for record in records],
                                    periods_per_year, annual_risk_free_rate)
    benchmark_statistics = _return_statistics([record["buy_hold_equity"] for record in records],
                                              periods_per_year, annual_risk_free_rate)
    return {
        "assumptions": {
            "initial_capital": "1 个归一化资本单位，不代表 1 元",
            "execution": f"第 t 条记录收盘生成信号，第 t+{execution_lag_bars} 条记录收盘交易；之后持仓承担收益",
            "execution_lag_bars": execution_lag_bars, "cost_bps": cost_bps,
            "cost_basis": "显式研究情景，每次买卖按绝对交易金额收取；非真实收费标准，未计独立滑点",
            "fractional_units": True, "leverage": False, "terminal_liquidation": False,
            "benchmark": "同一真实区间首日收盘买入持有，按相同成本情景；现金初值同为 1",
            "price_basis": "源收盘价，复权口径未外部核验；不声称分红再投资或实盘可执行",
            "gap_tolerance": gap_tolerance, "cash_interest": "研究假设为零",
            "periods_per_year": periods_per_year, "annual_risk_free_rate": annual_risk_free_rate,
            "annualization_basis": "按所选区间实际记录的相邻净值收益计算；每年区间数默认 252、无风险年利率默认 0 均为可编辑项目研究假设，非官方交易日历或真实无风险利率",
            "annualization_limitations": "夏普与波动率的平方根年化采用收益平稳、近似独立且无显著串行相关的常用缩放假设，本项目未验证这些条件；不构成未来绩效或实盘风险保证",
            "statistics_scope": "包括预热期空仓和区间内交易成本；排除首条之前虚拟收益。买入持有首日建仓成本保留在累计收益，但不纳入首条到末条的夏普和年化指标；策略与基准统计口径一致",
            "sharpe_formula": "mean(r - rf_period) / sample_std(r, ddof=1) * sqrt(periods_per_year)，r=equity[t]/equity[t-1]-1，rf_period=(1+annual_risk_free_rate)^(1/periods_per_year)-1；至少 2 个收益区间且标准差非零",
            "annualized_volatility_formula": "sample_std(r, ddof=1) * sqrt(periods_per_year)",
            "annualized_return_formula": "(last_equity/first_equity)^(periods_per_year/(rows-1))-1；按记录区间数年化，不按自然日推断期限",
            "not_modelled": ["整手交易", "完整T+1撮合", "涨跌停成交限制", "订单簿", "分红和企业行动", "实时成交"],
        },
        "strategy": strategy, "warmup_records": compiled.warmup, "quality": quality,
        "metrics": {"rows": len(bars), "start": bars[0].date, "end": bars[-1].date,
                    "total_return": records[-1]["equity"] - 1,
                    "buy_hold_return": records[-1]["buy_hold_equity"] - 1,
                    "max_drawdown": max_drawdown, "trade_records": trades,
                    "traded_notional": traded_total, "fees": fees_total,
                    **statistics,
                    "buy_hold_sharpe_ratio": benchmark_statistics["sharpe_ratio"],
                    "buy_hold_sharpe_unavailable_reason": benchmark_statistics["sharpe_unavailable_reason"]},
        "last_signal_not_executed_within_sample": signals[-execution_lag_bars:],
        "records": records,
    }
