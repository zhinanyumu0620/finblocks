"""受约束因子表达式与分区评价；不执行任意代码，不补造样本。"""

import ast
import math

from .data import require_valid
from .dsl import compile_strategy, evaluate
from .research import document_hash


REFERENCES = [
    {"id": "lee2000", "title": "Price Momentum and Trading Volume", "authors": "Charles M. C. Lee; Bhaskaran Swaminathan",
     "year": 2000, "identifier": "10.1111/0022-1082.00280", "url": "https://onlinelibrary.wiley.com/doi/10.1111/0022-1082.00280",
     "read_scope": "出版社摘要已核验；全文未核验", "verified_on": "2026-10-05",
     "relation": "量价与动量机制背景，换手率等定义不同；不证明当前公式等同原文或有效"},
    {"id": "pbo2015", "title": "The Probability of Backtest Overfitting",
     "authors": "David H. Bailey; Jonathan M. Borwein; Marcos López de Prado; Qiji Jim Zhu", "year": 2015,
     "identifier": "作者稿2015-02-27", "url": "https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf",
     "read_scope": "作者稿首页/摘要已核验；算法与实验未复验", "verified_on": "2026-10-05",
     "relation": "PBO/CSCV方法参考；本版不计算PBO，单次回测不足以给过拟合概率"},
    {"id": "dsr2014", "title": "The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting and Non-Normality",
     "authors": "David H. Bailey; Marcos López de Prado", "year": 2014, "identifier": "SSRN:2460551",
     "url": "https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf",
     "read_scope": "作者稿首页/摘要已核验；算法与实验未复验", "verified_on": "2026-10-05",
     "relation": "多重尝试和非正态收益的绩效检验参考；本版不计算DSR，原始IC不能直接代入"},
]


def compile_factor(expression, threshold=0.0, direction="gt", allocation=None):
    if not isinstance(expression, str) or not 1 <= len(expression.strip()) <= 1200:
        raise ValueError("因子表达式须为1–1200字符")
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, RecursionError):
        raise ValueError("因子表达式不是合法的受约束算式") from None
    if len(list(ast.walk(tree))) > 180:
        raise ValueError("表达式超过项目结构预算")
    nodes = []
    def add(op, **values):
        identifier = "f" + str(len(nodes))
        nodes.append({"id": identifier, "op": op, **values})
        if len(nodes) > 60:
            raise ValueError("因子最多60个计算节点，此为项目预算")
        return identifier
    def visit(node, depth=0):
        if depth > 12:
            raise ValueError("表达式深度超过12层项目预算")
        if isinstance(node, ast.Name) and node.id in {"open", "high", "low", "close", "volume"}:
            return add("field", field=node.id)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            try:
                if math.isfinite(node.value):
                    return add("const", value=node.value)
            except OverflowError:
                pass
            raise ValueError("因子常量必须在有限数值范围内")
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            child = visit(node.operand, depth + 1)
            return child if isinstance(node.op, ast.UAdd) else add("mul", left=add("const", value=-1), right=child)
        binary = {ast.Add: "add", ast.Sub: "sub", ast.Mult: "mul", ast.Div: "div"}
        if isinstance(node, ast.BinOp) and type(node.op) in binary:
            left = visit(node.left, depth + 1)
            right = visit(node.right, depth + 1)
            return add(binary[type(node.op)], left=left, right=right)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id.upper() in {"MA", "EMA", "RSI", "STD", "LAG"}
                and len(node.args) == 2 and not node.keywords):
            window = node.args[1]
            if not isinstance(window, ast.Constant) or type(window.value) is not int:
                raise ValueError("窗口/滞后必须显式写整数，不支持未指定参数")
            return add(node.func.id.lower(), input=visit(node.args[0], depth + 1), window=window.value)
        raise ValueError("非法或未开放因子结构；仅允许OHLCV、常量、加减乘除和MA/EMA/RSI/STD/LAG；未来字段、属性、任意代码和基本面未开放")
    factor_id = visit(tree.body)
    if direction not in {"gt", "lt"} or type(threshold) not in (int, float) or not math.isfinite(threshold):
        raise ValueError("策略转换须明确有限阈值及高于/低于方向")
    signal = add(direction, left=factor_id, right=add("const", value=threshold))
    strategy = {"version": 1, "name": "因子阈值策略", "nodes": nodes, "signal": signal,
                "allocation": {"when_true": 1.0, "when_false": 0.0} if allocation is None else allocation}
    compiled = compile_strategy(strategy)
    return compiled, factor_id, ast.unparse(tree)


def describe_factor(compiled, expression):
    nodes = compiled.ordered_nodes[:-2]
    fields = sorted({n["field"] for n in nodes if n["op"] == "field"})
    ops = {n["op"] for n in nodes}
    categories, keywords, findings = [], [], []
    table = {n["id"]: n for n in nodes}
    for node in nodes:
        if node["op"] == "div":
            left, right = table[node["left"]], table[node["right"]]
            if left.get("field") == "volume" and right["op"] == "ma" and table[right["input"]].get("field") == "volume":
                findings.append({"node_id": node["id"], "finding": "正成交量除以其正MA始终为正；这是量比，不是减去1的成交量偏离。该子项单独不会因低于均量而变负。"})
    if any(field in fields for field in ("open", "high", "low", "close")):
        categories.append({"type": "价格结构（趋势/反转方向待验证）", "evidence": [n["id"] for n in nodes if n["op"] in {"field", "ma", "ema", "lag"}], "status": "结构支持"})
        keywords += ["价格趋势", "动量", "反转", "price trend", "momentum", "reversal"]
    if "std" in ops:
        categories.append({"type": "波动率", "evidence": [n["id"] for n in nodes if n["op"] == "std"], "status": "结构支持"})
        keywords += ["波动率", "volatility"]
    if "volume" in fields:
        categories.append({"type": "成交量/交易活跃度", "evidence": [n["id"] for n in nodes if n.get("field") == "volume"], "status": "结构支持"})
        keywords += ["相对成交量", "量价交互", "trading volume", "volume-price interaction"]
    if len(categories) > 1:
        categories.append({"type": "复合因子", "evidence": [n["id"] for n in nodes if n["op"] in {"mul", "div", "add", "sub"}], "status": "结构支持；不是已验证的预测机制"})
    return {"expression": expression, "categories": categories or [{"type": "常量/无法确定", "evidence": [], "status": "待验证"}],
            "expression_sha256": document_hash(ast.dump(ast.parse(expression, mode="eval"), include_attributes=False)),
            "fields": fields, "keywords": keywords + ["样本外验证", "多重检验", "out-of-sample validation", "multiple testing"],
            "references": [r for r in REFERENCES if r["id"] != "lee2000" or "volume" in fields and "close" in fields],
            "complexity": {"nodes": len(nodes), "windows": [n["window"] for n in nodes if "window" in n]},
            "structural_findings": findings,
            "interpretation": "分类依据计算字段和算子；相关方向需真实实验支持，关联不证明因果。高成交量不自动等于低流动性风险。",
            "literature_mode": "人工核验参考库；本版无自动联网文献检索，不证明当前公式首次发现"}


def correlation(left, right):
    if len(left) < 3 or len(left) != len(right):
        return None
    # 缩放避免大成交量导致平方溢出；常数样本返回不可用。
    a_scale, b_scale = max(map(abs, left)) or 1, max(map(abs, right)) or 1
    a, b = [v / a_scale for v in left], [v / b_scale for v in right]
    a_mean, b_mean = math.fsum(a) / len(a), math.fsum(b) / len(b)
    a, b = [v - a_mean for v in a], [v - b_mean for v in b]
    da, db = math.fsum(v * v for v in a), math.fsum(v * v for v in b)
    return math.fsum(x * y for x, y in zip(a, b)) / math.sqrt(da * db) if da and db else None


def ranks(values):
    ordered = sorted(range(len(values)), key=lambda i: values[i])
    result, i = [0.0] * len(values), 0
    while i < len(ordered):
        j = i + 1
        while j < len(ordered) and values[ordered[j]] == values[ordered[i]]:
            j += 1
        for k in ordered[i:j]:
            result[k] = (i + j - 1) / 2 + 1
        i = j
    return result


def summarize(pairs, mode, minimum):
    days = sorted({p[0] for p in pairs})
    base = {"pairs": len(pairs), "dates": len(days), "correlation": None, "rank_correlation": None,
            "group_spread": None, "unavailable_reason": None}
    count = len(pairs) if mode == "time_series" else len(days)
    if count < minimum:
        base["unavailable_reason"] = "有效样本低于用户确认的项目门槛"
        return base
    if mode == "time_series":
        left, right = [p[2] for p in pairs], [p[3] for p in pairs]
        base["correlation"], base["rank_correlation"] = correlation(left, right), correlation(ranks(left), ranks(right))
    else:
        correlations, rank_correlations, spreads = [], [], []
        for day in days:
            samples = [p for p in pairs if p[0] == day]
            if len(samples) < 3:
                continue
            left, right = [p[2] for p in samples], [p[3] for p in samples]
            c, r = correlation(left, right), correlation(ranks(left), ranks(right))
            if c is None or r is None:
                continue
            correlations.append(c)
            rank_correlations.append(r)
            ordered = sorted(samples, key=lambda p: p[2])
            size = max(1, len(ordered) // 3)
            spreads.append(math.fsum(p[3] for p in ordered[-size:]) / size - math.fsum(p[3] for p in ordered[:size]) / size)
        base["valid_cross_sections"] = len(correlations)
        if len(correlations) >= minimum:
            base["correlation"] = math.fsum(correlations) / len(correlations)
            base["rank_correlation"] = math.fsum(rank_correlations) / len(rank_correlations)
            base["group_spread"] = math.fsum(spreads) / len(spreads)
        else:
            base["unavailable_reason"] = "非恒定且至少3标的的有效横截面不足"
    if base["correlation"] is None and not base["unavailable_reason"]:
        base["unavailable_reason"] = "常量因子或常量未来收益，相关系数无定义"
    return base


def evaluate_factor(panel, expression, protocol, reveal_test=False):
    if set(protocol) != {"mode", "horizon", "train_fraction", "validation_fraction", "min_samples"}:
        raise ValueError("因子实验协议不完整或含未知字段")
    mode, horizon, minimum = protocol["mode"], protocol["horizon"], protocol["min_samples"]
    train_fraction, validation_fraction = protocol["train_fraction"], protocol["validation_fraction"]
    if mode not in {"time_series", "cross_section"} or type(horizon) is not int or not 1 <= horizon <= 60:
        raise ValueError("请选择时间序列/横截面，预测跨度须为1–60条共同日历记录")
    if type(minimum) is not int or not 5 <= minimum <= 1000:
        raise ValueError("每区间样本门槛须为5–1000，此为项目研究协议")
    if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 < v < 1 for v in (train_fraction, validation_fraction)) or train_fraction + validation_fraction >= 1:
        raise ValueError("训练与验证占比须显式填写且之和小于1")
    if mode == "time_series" and len(panel) != 1 or mode == "cross_section" and len(panel) < 3:
        raise ValueError("时间序列模式需要1标的，横截面至少需要3个区间通过校验的标的")
    for bars in panel.values():
        require_valid(bars)
    compiled, factor_id, canonical = compile_factor(expression)
    dates = sorted({b.date for bars in panel.values() for b in bars})
    if len(dates) < minimum * 3 + horizon * 3 + compiled.warmup:
        raise ValueError("共同日历长度不足以覆盖预热、三段样本门槛与标签隔离；请扩大真实有效区间或明确调整协议")
    first_cut, second_cut = int(len(dates) * train_fraction), int(len(dates) * (train_fraction + validation_fraction))
    if first_cut == 0 or second_cut <= first_cut or second_cut >= len(dates):
        raise ValueError("分区长度不足")
    sections = {"train": [], "validation": [], "test": []}
    outputs, preview, missing = {}, [], 0
    for code, bars in panel.items():
        values = evaluate(compiled, bars)[factor_id]
        lookup = {bar.date: (bar.values["close"], value) for bar, value in zip(bars, values)}
        outputs[code] = lookup
        missing += sum(v is None for v in values)
        preview.extend({"symbol": code, "date": bar.date, "factor": value} for bar, value in list(zip(bars, values))[-8:])
    for i, day in enumerate(dates):
        section, upper = ("train", first_cut) if i < first_cut else ("validation", second_cut) if i < second_cut else ("test", len(dates))
        # 标签必须完全位于所属区间；对齐目标日期缺失时不前向填充。
        if i + horizon >= upper:
            continue
        target_day = dates[i + horizon]
        for code, lookup in outputs.items():
            current, target = lookup.get(day), lookup.get(target_day)
            if current and target and current[1] is not None:
                sections[section].append((day, code, current[1], target[0] / current[0] - 1))
    train, validation = summarize(sections["train"], mode, minimum), summarize(sections["validation"], mode, minimum)
    if train["unavailable_reason"] or validation["unavailable_reason"]:
        raise ValueError("训练或验证评价不可用：" + str(train["unavailable_reason"] or validation["unavailable_reason"]))
    test = summarize(sections["test"], mode, minimum) if reveal_test else {"status": "未执行；冻结候选后单独评估", "dates": len({p[0] for p in sections["test"]})}
    risks = [
        {"item": "未来字段与标签边界", "status": "未发现明显异常", "evidence": "表达式白名单只读历史；各段标签目标日不得越过分区边界"},
        {"item": "样本独立性", "status": "证据不足", "evidence": "时间相关与重叠收益未检验；行数不等于独立样本数"},
        {"item": "参数敏感性", "status": "未执行", "evidence": "尚未执行窗口邻域实验，不能宣称参数稳健"},
        {"item": "成本与可交易性", "status": "未执行", "evidence": "因子相关与分组差不是交易回测；尚无成本/换手账目"},
        {"item": "PBO / DSR", "status": "未执行", "evidence": "本版本没有实现进阶概率算法，不能由AI估计"},
        {"item": "复权口径", "status": "证据不足", "evidence": "源价格连续性通过不等于企业行动/复权已核验"},
        {"item": "量纲与经济机制", "status": "证据不足", "evidence": "本版结构分类不证明经济假说，尚未实施完整量纲一致性审计"},
    ]
    a, b = train["rank_correlation"], validation["rank_correlation"]
    risks.append({"item": "训练/验证方向一致性", "status": "发现风险信号" if a * b < 0 else "未发现明显异常",
                  "evidence": {"train_rank_correlation": a, "validation_rank_correlation": b},
                  "limitation": "符号变化不单独证明过拟合；同号也不证明未来有效"})
    if reveal_test:
        risks.append({"item": "最终测试", "status": "证据不足" if test.get("unavailable_reason") else "已执行",
                      "evidence": test, "limitation": "查看后再优化会污染测试集；不能反复选择最好的测试结果"})
    return {"expression": canonical, "description": describe_factor(compiled, canonical), "protocol": protocol,
            "intervals": {"train": [dates[0], dates[first_cut - 1]], "validation": [dates[first_cut], dates[second_cut - 1]], "test": [dates[second_cut], dates[-1]]},
            "statistics_scope": "单股时间序列相关" if mode == "time_series" else "逐日横截面IC/Rank IC平均，分组差为等权最高/最低三分组未来收益差，非组合回测；相同因子值按标的顺序分组存在边界歧义",
            "coverage": {"symbols": len(panel), "calendar_dates": len(dates), "warmup_records": compiled.warmup,
                         "unavailable_factor_values_including_warmup": missing},
            "train": train, "validation": validation, "test": test, "risks": risks, "preview": preview[:24],
            "protocol_hash": document_hash({"protocol": protocol, "symbols": sorted(panel), "dates": dates}),
            "test_revealed": reveal_test, "overfitting_conclusion": "证据不足；未发现异常不等于不存在过拟合"}
