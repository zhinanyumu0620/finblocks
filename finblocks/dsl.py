"""受约束策略编译器：不执行模型生成的任意代码。"""

from dataclasses import dataclass
import math
import re


class CompileError(ValueError):
    """策略类型、参数或图结构错误。"""


@dataclass(frozen=True)
class CompiledStrategy:
    strategy: dict
    ordered_nodes: tuple[dict, ...]
    warmup: int


def _finite_number(value):
    # bool 在 Python 中属于 int，但不能作为指标参数。
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def compile_strategy(strategy: dict) -> CompiledStrategy:
    if not isinstance(strategy, dict) or set(strategy) != {"version", "name", "nodes", "signal", "allocation"}:
        raise CompileError("根结构仅允许 version/name/nodes/signal/allocation，且必须完整")
    if type(strategy["version"]) is not int or strategy["version"] != 1:
        raise CompileError("仅支持 DSL version=1")
    if not isinstance(strategy["name"], str) or not 1 <= len(strategy["name"]) <= 80:
        raise CompileError("策略名称须为 1–80 个字符，此为软件字段上限而非参赛名称上限")
    allocation = strategy["allocation"]
    if not isinstance(allocation, dict) or set(allocation) != {"when_true", "when_false"}:
        raise CompileError("allocation 必须包含且仅包含 when_true/when_false")
    for value in allocation.values():
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
            raise CompileError("持仓目标必须是 0–1 的有限数值")
    nodes = strategy["nodes"]
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 64:
        raise CompileError("策略节点数须为 1–64，此为项目资源门槛")
    table, types, warmups, ordered = {}, {}, {}, []
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,39}", node["id"]):
            raise CompileError("节点 id 必须是有效标识符")
        if node["id"] in table:
            raise CompileError(f"节点 id 重复：{node['id']}")
        table[node["id"]] = node

    def visit(node_id, visiting):
        if not isinstance(node_id, str) or node_id not in table:
            raise CompileError("引用了不存在的节点")
        if node_id in visiting:
            raise CompileError(f"策略图有循环：{node_id}")
        if node_id in types:
            return types[node_id], warmups[node_id]
        node = table[node_id]
        op = node.get("op")
        if not isinstance(op, str):
            raise CompileError(f"{node_id}：操作名必须为字符串")
        visiting = visiting | {node_id}
        if op == "field":
            if set(node) != {"id", "op", "field"} or not isinstance(node["field"], str) or node["field"] not in {"open", "high", "low", "close", "volume"}:
                raise CompileError(f"{node_id}：字段不支持；基本面、指数、复权和未来字段未开放")
            kind, warmup = "number", 1
        elif op == "const":
            if set(node) != {"id", "op", "value"} or not _finite_number(node["value"]):
                raise CompileError(f"{node_id}：常数必须为有限数值")
            kind, warmup = "number", 1
        elif op in {"ma", "ema", "rsi", "std", "lag"}:
            minimum = 1 if op == "lag" else 2
            if set(node) != {"id", "op", "input", "window"} or type(node["window"]) is not int or not minimum <= node["window"] <= 500:
                raise CompileError(f"{node_id}：{op.upper()} window 必须是 {minimum}–500 的整数，此为项目参数约束")
            input_kind, w = visit(node["input"], visiting)
            if input_kind != "number":
                raise CompileError(f"{node_id}：{op.upper()} 输入必须为数值")
            kind, warmup = "number", w + node["window"] - (0 if op in {"rsi", "lag"} else 1)
        elif op == "bollinger":
            if (set(node) != {"id", "op", "input", "window", "multiplier", "band"}
                    or type(node["window"]) is not int or not 2 <= node["window"] <= 500
                    or not _finite_number(node["multiplier"]) or not 0 < node["multiplier"] <= 10
                    or not isinstance(node["band"], str) or node["band"] not in {"upper", "middle", "lower"}):
                raise CompileError(f"{node_id}：布林带须指定 2–500 整数窗口、0–10（不含0）倍数和 upper/middle/lower")
            input_kind, w = visit(node["input"], visiting)
            if input_kind != "number":
                raise CompileError(f"{node_id}：布林带输入必须为数值")
            kind, warmup = "number", w + node["window"] - 1
        elif op == "macd":
            if (set(node) != {"id", "op", "input", "fast", "slow", "signal", "component"}
                    or any(type(node[key]) is not int for key in ("fast", "slow", "signal"))
                    or not 2 <= node["fast"] < node["slow"] <= 500 or not 2 <= node["signal"] <= 500
                    or not isinstance(node["component"], str) or node["component"] not in {"line", "signal", "histogram"}):
                raise CompileError(f"{node_id}：MACD 须满足 2≤fast<slow≤500、2≤signal≤500，并指定 line/signal/histogram")
            input_kind, w = visit(node["input"], visiting)
            if input_kind != "number":
                raise CompileError(f"{node_id}：MACD 输入必须为数值")
            kind, warmup = "number", w + node["slow"] - 1
            if node["component"] != "line":
                warmup += node["signal"] - 1
        elif op in {"add", "sub", "mul", "div", "gt", "lt", "cross_up", "cross_down", "and", "or"}:
            if set(node) != {"id", "op", "left", "right"}:
                raise CompileError(f"{node_id}：操作字段不完整或含额外字段")
            left_kind, lw = visit(node["left"], visiting)
            right_kind, rw = visit(node["right"], visiting)
            required = "boolean" if op in {"and", "or"} else "number"
            if left_kind != required or right_kind != required:
                raise CompileError(f"{node_id}：输入类型需要 {required}")
            kind = "number" if op in {"add", "sub", "mul", "div"} else "boolean"
            warmup = max(lw, rw) + int(op.startswith("cross_"))
        else:
            raise CompileError(f"{node_id}：未知或未开放操作 {op}")
        types[node_id], warmups[node_id] = kind, warmup
        ordered.append(node)
        return kind, warmup

    signal_type, warmup = visit(strategy["signal"], set())
    if signal_type != "boolean":
        raise CompileError("signal 必须引用布尔条件节点")
    if len(ordered) != len(nodes):
        raise CompileError("存在未连接到信号的节点，须删除或接入；不允许隐藏无效积木")
    return CompiledStrategy(strategy, tuple(ordered), warmup)


def _ema_series(inputs, window):
    # 用首个连续有效窗口的 SMA 播种；缺失值后从头预热，禁止向未来取值。
    series, seed, previous = [], [], None
    alpha = 2 / (window + 1)
    for value in inputs:
        if value is None:
            seed, previous = [], None
        elif previous is None:
            seed.append(value)
            if len(seed) == window:
                previous = math.fsum(seed) / window
                seed = []
        else:
            previous = previous + alpha * (value - previous)
        series.append(previous)
    return series


def _rsi_series(inputs, window):
    # Wilder 平滑先取 window 个相邻差额；缺失值同时重置前值和涨跌种子。
    series, gains, losses = [], [], []
    previous = average_gain = average_loss = None
    for value in inputs:
        result = None
        if value is None:
            gains, losses = [], []
            previous = average_gain = average_loss = None
        else:
            if previous is not None:
                change = value - previous
                gain, loss = max(change, 0), max(-change, 0)
                if average_gain is None:
                    gains.append(gain)
                    losses.append(loss)
                    if len(gains) == window:
                        average_gain = math.fsum(gains) / window
                        average_loss = math.fsum(losses) / window
                        gains, losses = [], []
                else:
                    average_gain = ((window - 1) * average_gain + gain) / window
                    average_loss = ((window - 1) * average_loss + loss) / window
                if average_gain is not None:
                    if average_gain == average_loss == 0:
                        result = 50.0
                    elif average_loss == 0:
                        result = 100.0
                    else:
                        result = 100 - 100 / (1 + average_gain / average_loss)
            previous = value
        series.append(result)
    return series


def evaluate(compiled: CompiledStrategy, bars) -> dict[str, list]:
    values = {}
    count = len(bars)
    for node in compiled.ordered_nodes:
        op = node["op"]
        if op == "field":
            series = [bar.values[node["field"]] for bar in bars]
        elif op == "const":
            series = [node["value"]] * count
        elif op in {"ma", "std"}:
            inputs, window = values[node["input"]], node["window"]
            series = [None] * count
            for i in range(window - 1, count):
                segment = inputs[i - window + 1:i + 1]
                if all(v is not None for v in segment):
                    mean = math.fsum(segment) / window
                    series[i] = mean if op == "ma" else math.sqrt(math.fsum((v - mean) ** 2 for v in segment) / window)
        elif op == "lag":
            inputs, window = values[node["input"]], node["window"]
            series = [inputs[i - window] if i >= window else None for i in range(count)]
        elif op == "ema":
            series = _ema_series(values[node["input"]], node["window"])
        elif op == "rsi":
            series = _rsi_series(values[node["input"]], node["window"])
        elif op == "bollinger":
            inputs, window = values[node["input"]], node["window"]
            series = [None] * count
            for i in range(window - 1, count):
                segment = inputs[i - window + 1:i + 1]
                if all(value is not None for value in segment):
                    mean = math.fsum(segment) / window
                    # 总体标准差（ddof=0）；中轨与 MA 相同。
                    deviation = math.sqrt(math.fsum((value - mean) ** 2 for value in segment) / window)
                    offset = {"upper": 1, "middle": 0, "lower": -1}[node["band"]]
                    series[i] = mean + offset * node["multiplier"] * deviation
        elif op == "macd":
            inputs = values[node["input"]]
            fast, slow = _ema_series(inputs, node["fast"]), _ema_series(inputs, node["slow"])
            line = [None if a is None or b is None else a - b for a, b in zip(fast, slow)]
            if node["component"] == "line":
                series = line
            else:
                signal = _ema_series(line, node["signal"])
                # 柱值为 DIF−DEA；采用不乘 2 的明确约定。
                series = signal if node["component"] == "signal" else [
                    None if a is None or b is None else a - b for a, b in zip(line, signal)]
        else:
            left, right = values[node["left"]], values[node["right"]]
            series = []
            for i, (a, b) in enumerate(zip(left, right)):
                if a is None or b is None:
                    value = None
                elif op in {"add", "sub", "mul", "div"}:
                    try:
                        value = {"add": lambda: a + b, "sub": lambda: a - b,
                                 "mul": lambda: a * b, "div": lambda: a / b}[op]()
                        if not math.isfinite(value):
                            value = None
                    except (ZeroDivisionError, OverflowError):
                        value = None  # 除零/溢出保留不可用，不能补成0。
                elif op == "gt":
                    value = a > b
                elif op == "lt":
                    value = a < b
                elif op == "and":
                    value = a and b
                elif op == "or":
                    value = a or b
                elif i == 0 or left[i - 1] is None or right[i - 1] is None:
                    value = None
                elif op == "cross_up":
                    value = a > b and left[i - 1] <= right[i - 1]
                else:
                    value = a < b and left[i - 1] >= right[i - 1]
                series.append(value)
        values[node["id"]] = series
    return values
