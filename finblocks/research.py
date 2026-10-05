"""意图审计、真实证据与私有实验记录；不将模型解释当成计算证明。"""

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid

from .dsl import compile_strategy


def audit_intent(prompt, strategy, contract=None):
    if not isinstance(prompt, str) or len(prompt) > 4000:
        raise ValueError("研究问题须为最多4000字的文本")
    compiled = compile_strategy(strategy)
    checks = []
    def check(slot, expected, actual, node_ids, matched):
        checks.append({"slot": slot, "expected": expected, "actual": actual,
                       "node_ids": node_ids, "status": "已核对" if matched else "存在差异"})
    # 自动提取仅覆盖明确表达；未知需求不能被默认认领为通过。
    parsed = {}
    mapping = {"上穿": "cross_up", "向上穿越": "cross_up", "下穿": "cross_down",
               "向下穿越": "cross_down", "持续高于": "gt", "严格高于": "gt",
               "持续低于": "lt", "严格低于": "lt"}
    found = {value for text, value in mapping.items() if text in prompt}
    if len(found) == 1:
        parsed["relation"] = found.pop()
    field_map = {"收盘价": "close", "开盘价": "open", "最高价": "high", "最低价": "low", "成交量": "volume"}
    fields = {value for text, value in field_map.items() if text in prompt}
    if len(fields) == 1:
        parsed["field"] = fields.pop()
    windows = re.findall(r"(?<![A-Za-z])MA\s*([0-9]{1,3})(?![0-9])", prompt, re.I)
    if len(windows) == 2:
        parsed["ma_windows"] = [int(value) for value in windows]
    explicit = contract if contract is not None else parsed
    if not isinstance(explicit, dict) or set(explicit) - {"relation", "field", "ma_windows", "when_true", "when_false"}:
        raise ValueError("意图契约含未知字段")
    root = next(node for node in compiled.ordered_nodes if node["id"] == strategy["signal"])
    if "relation" in explicit:
        expected = explicit["relation"]
        if expected not in {"gt", "lt", "cross_up", "cross_down"}:
            raise ValueError("请明确选择高于/低于状态或上穿/下穿事件")
        check("根条件（事件/状态）", expected, root["op"], [root["id"]], root["op"] == expected)
    inputs = [node for node in compiled.ordered_nodes if node["op"] == "field"]
    if "field" in explicit:
        expected = explicit["field"]
        if expected not in {"open", "high", "low", "close", "volume"}:
            raise ValueError("意图字段尚不支持")
        check("行情字段集合", [expected], sorted({n["field"] for n in inputs}),
              [n["id"] for n in inputs], {n["field"] for n in inputs} == {expected})
    if "ma_windows" in explicit:
        expected = explicit["ma_windows"]
        if not isinstance(expected, list) or len(expected) != 2 or any(type(v) is not int or not 2 <= v <= 500 for v in expected):
            raise ValueError("MA意图须明确两个2–500的窗口")
        table = {n["id"]: n for n in compiled.ordered_nodes}
        nodes = [table.get(root.get(side), {}) for side in ("left", "right")]
        actual = [n.get("window") if n.get("op") == "ma" else None for n in nodes]
        check("根条件左右MA窗口", expected, actual, [n.get("id") for n in nodes], actual == expected)
    for key in ("when_true", "when_false"):
        if key in explicit:
            value = explicit[key]
            if type(value) not in (int, float) or not 0 <= value <= 1:
                raise ValueError("意图持仓比例须为0–1")
            check(key, value, strategy["allocation"][key], [], strategy["allocation"][key] == value)
    blockers = []
    if re.search(r"(金叉|上穿|买入).*(死叉|下穿|卖出)", prompt) and re.search(r"持有|期间|直到|卖出", prompt):
        blockers.append("需求包含买入后持续持有、直到另一事件卖出的状态记忆；现有真/假目标比例不能等价表达，不能用上穿当天持仓代替")
    if re.search(r"市盈率|市净率|\bPE\b|\bPB\b|\bROE\b|财报|基本面", prompt, re.I):
        blockers.append("可靠历史基本面PIT未开放；不能以最新财报回填历史")
    mismatches = any(item["status"] == "存在差异" for item in checks)
    status = "不支持" if blockers else "存在差异" if mismatches else "已核对明确项" if checks else "待确认"
    return {"status": status, "contract": explicit, "checks": checks, "blockers": blockers,
            "application_blocked": bool(blockers or mismatches), "warmup_records": compiled.warmup,
            "scope": "只核对所列明确字段，不证明整段自然语言与策略完全等价；未列参数、复合语义和执行配置仍需人工确认",
            "execution_note": "穿越是单次事件；信号滞后及成本由回测配置决定，未自行更改"}


def evidence_pack(report, day=None):
    metrics = report["metrics"]
    facts = [{"id": "M-" + key, "label": label, "value": metrics[key]} for key, label in (
        ("total_return", "累计收益率（小数）"), ("max_drawdown", "最大回撤（小数）"),
        ("sharpe_ratio", "夏普比率"), ("buy_hold_return", "买入并持有收益率（小数）"),
        ("rows", "真实行情记录数"))]
    facts.append({"id": "A-execution", "label": "执行规则", "value": report["assumptions"]["execution"]})
    if day is not None:
        index = next((i for i, row in enumerate(report["records"]) if row["date"] == day), None)
        if index is None:
            raise ValueError("该日期不在本次真实回测中")
        row = report["records"][index]
        for key in ("date", "executed_signal_date", "executed_signal", "target_weight", "fee", "equity", "drawdown"):
            facts.append({"id": "R-" + key, "label": key, "value": row[key]})
        signal_index = index - report["assumptions"]["execution_lag_bars"]
        if report.get("kind") == "portfolio":
            for key in ("rebalance", "matched_symbols", "selected_symbols", "capacity_excluded", "cash", "holdings", "trades"):
                facts.append({"id": "P-" + key, "label": "组合执行 " + key, "value": row[key]})
        elif signal_index >= 0 and row["executed_signal_date"] is not None:
            for node in report["strategy"]["nodes"]:
                facts.append({"id": "N-" + node["id"], "label": "信号来源日积木 " + node["id"],
                              "value": report["node_outputs"][node["id"]][signal_index], "node": node})
    return {"facts": facts, "source": report["source"], "day": day,
            "limitations": ["归一化资本单位，不等于真实人民币账户", "账目可说明规则触发，不能证明市场涨跌的新闻或宏观原因",
                            report["assumptions"]["price_basis"]]}


def repair_strategy(prompt, strategy, contract):
    """仅对明确的双MA规则做可见补丁，复杂图不擅自改写。"""
    audit_intent(prompt, strategy, contract)
    copied = deepcopy(strategy)
    table = {n["id"]: n for n in copied["nodes"]}
    root = table[copied["signal"]]
    if root["op"] not in {"gt", "lt", "cross_up", "cross_down"}:
        raise ValueError("首版修复预览仅支持双MA根比较；复杂图请按审计位置手动修改")
    left, right = table[root["left"]], table[root["right"]]
    if left["op"] != "ma" or right["op"] != "ma" or left["id"] == right["id"]:
        raise ValueError("首版补丁需要两个独立MA节点")
    if "relation" in contract:
        root["op"] = contract["relation"]
    if "ma_windows" in contract:
        left["window"], right["window"] = contract["ma_windows"]
    if "field" in contract:
        fields = [table[left["input"]], table[right["input"]]]
        if any(n["op"] != "field" for n in fields):
            raise ValueError("MA输入不是直接行情字段，不能自动修复")
        for node in fields:
            node["field"] = contract["field"]
    for key in ("when_true", "when_false"):
        if key in contract:
            copied["allocation"][key] = contract[key]
    audit = audit_intent(prompt, copied, contract)
    return {"strategy": copied, "audit": audit, "diff": [{"node_id": n["id"], "before": old, "after": n}
        for old, n in zip(strategy["nodes"], copied["nodes"]) if old != n],
        "allocation_diff": {"before": strategy["allocation"], "after": copied["allocation"]},
        "scope": "仅预览，不自动应用；不修改成本或执行时点"}


class ResearchJournal:
    """账号归属只在私有库内保存；公开研究内容不写用户名。"""
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("CREATE TABLE IF NOT EXISTS research (id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL, created TEXT NOT NULL, document TEXT NOT NULL)")

    def append(self, kind, document, owner):
        identifier = uuid.uuid4().hex
        encoded = json.dumps(document, ensure_ascii=False, allow_nan=False)
        with closing(sqlite3.connect(self.path, timeout=15)) as connection, connection:
            connection.execute("INSERT INTO research VALUES (?,?,?,?,?)", (identifier, str(owner or "guest"), kind,
                               datetime.now(timezone.utc).isoformat(), encoded))
        return identifier

    def list(self, owner, limit=30):
        with closing(sqlite3.connect(self.path)) as connection:
            rows = connection.execute("SELECT id, kind, created, document FROM research WHERE owner=? ORDER BY rowid DESC LIMIT ?",
                                      (str(owner or "guest"), limit)).fetchall()
        return [{"id": r[0], "kind": r[1], "created_at_utc": r[2], "document": json.loads(r[3])} for r in rows]

    def get(self, identifier, owner):
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute("SELECT document FROM research WHERE id=? AND owner=?", (identifier, str(owner or "guest"))).fetchone()
        if not row:
            raise ValueError("研究记录不存在或不属于当前身份")
        return json.loads(row[0])

    def count_tests(self, protocol_hash, owner):
        with closing(sqlite3.connect(self.path)) as connection:
            rows = connection.execute("SELECT document FROM research WHERE owner=? AND kind='factor_test'", (str(owner or "guest"),)).fetchall()
        return sum(json.loads(row[0]).get("protocol_hash") == protocol_hash for row in rows)


def document_hash(document):
    return hashlib.sha256(json.dumps(document, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
