"""只读接入已审计的真实行情，不修补或制造数据。"""

from dataclasses import dataclass
from datetime import date
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import re
import zipfile


FIELDS = {
    "open": "开盘价", "high": "最高价", "low": "最低价", "close": "收盘价",
    "previous_close": "前收盘价", "volume": "成交量", "amount": "成交额",
    "float_market_cap": "流通市值", "market_cap": "总市值",
}
PRICE_GAP_TOLERANCE = 0.0  # 执行默认要求逐行一致；原审计诊断阈值0.011保留在证据中。


class DataError(ValueError):
    """数据不满足研究执行门槛。"""


@dataclass(frozen=True)
class Bar:
    code: str
    name: str
    date: str
    values: dict


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_daily(archive: Path, code: str, start=None, end=None) -> tuple[list[Bar], dict]:
    if not re.fullmatch(r"(?:bj|sh|sz)[0-9]{6}", code):
        raise DataError("股票代码必须是本地数据的 bj/sh/sz 加六位数字标识")
    for bound in (start, end):
        if bound is not None:
            date.fromisoformat(bound)
    if start and end and start > end:
        raise DataError("开始日期不能晚于结束日期")
    member = code + ".csv"
    try:
        with zipfile.ZipFile(archive) as source:
            raw = source.read(member)
    except (KeyError, zipfile.BadZipFile) as exc:
        raise DataError("原始行情包没有该代码或 ZIP 无效") from exc
    # 第一行是来源说明；按实际结构重新建立第二行表头。
    lines = io.StringIO(raw.decode("gb18030"))
    next(lines)
    reader = csv.DictReader(lines)
    required = {"股票代码", "股票名称", "交易日期", *FIELDS.values()}
    if not required.issubset(reader.fieldnames or []):
        raise DataError("真实 CSV 字段结构与已审计结构不一致")
    bars = []
    for row in reader:
        day = row["交易日期"]
        try:
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError
        except ValueError as exc:
            raise DataError("存在无效交易日期") from exc
        if (start and day < start) or (end and day > end):
            continue
        if row["股票代码"] != code:
            raise DataError("文件名与内容股票代码不一致")
        values = {}
        for field, original in FIELDS.items():
            text = row[original].strip()
            if not text or text.lower() in {"nan", "null", "none", "na", "n/a"}:
                values[field] = None
                continue
            try:
                value = float(text)
            except ValueError as exc:
                raise DataError(f"{day} 的 {original} 不是数值") from exc
            if not math.isfinite(value):
                raise DataError(f"{day} 的 {original} 不是有限数值")
            values[field] = value
        bars.append(Bar(code, row["股票名称"], day, values))
    if not bars:
        raise DataError("所选区间没有真实记录")
    return bars, {
        "archive": archive.name, "member": member,
        "member_sha256": hashlib.sha256(raw).hexdigest(),
        "rows": len(bars), "start": bars[0].date, "end": bars[-1].date,
        "encoding": "gb18030", "source_notice_skipped": True,
        "price_basis": "源字段，复权口径未核验；本内核拒绝检测到的价差及包络异常",
    }


def validate_bars(bars: list[Bar], gap_tolerance=PRICE_GAP_TOLERANCE) -> dict:
    if not bars:
        raise DataError("行情不能为空")
    if not math.isfinite(gap_tolerance) or gap_tolerance < 0:
        raise DataError("价差诊断阈值必须为有限非负数")
    issues = []
    previous = None
    for bar in bars:
        v = bar.values
        reasons = []
        if previous and (bar.code != previous.code or bar.date <= previous.date):
            reasons.append("代码混合、日期重复或非递增")
        prices = [v.get(k) for k in ("open", "high", "low", "close", "previous_close")]
        if any(p is None or not math.isfinite(p) or p <= 0 for p in prices):
            reasons.append("价格缺失、非有限或非正")
        else:
            if v["high"] < max(v["open"], v["low"], v["close"]) or v["low"] > min(v["open"], v["high"], v["close"]):
                reasons.append("OHLC 包络不一致")
            if previous and previous.values.get("close") is not None and abs(v["previous_close"] - previous.values["close"]) > gap_tolerance:
                reasons.append("前收盘与上一记录收盘价差超阈值，价格口径待核验")
        volume = v.get("volume")
        if volume is None or not math.isfinite(volume) or volume <= 0:
            reasons.append("成交量缺失、非有限或非正，不推断为停牌")
        if reasons:
            issues.append({"date": bar.date, "reasons": reasons})
        previous = bar
    return {"passed": not issues, "issue_rows": len(issues), "issues": issues,
            "gap_tolerance": gap_tolerance, "basis": "项目质量门槛，不能证明完整复权或实盘可成交"}


def require_valid(bars: list[Bar], gap_tolerance=PRICE_GAP_TOLERANCE) -> dict:
    quality = validate_bars(bars, gap_tolerance)
    if not quality["passed"]:
        first = quality["issues"][0]
        raise DataError(f"质量门槛未通过，共 {quality['issue_rows']} 条；{first['date']}：{'；'.join(first['reasons'])}")
    return quality


def select_demo(root: Path, limit=3, min_bars=120) -> dict:
    if type(limit) is not int or limit < 1 or type(min_bars) is not int or min_bars < 2:
        raise DataError("候选数量必须为正整数，最少记录数至少为 2")
    evidence = root / "docs" / "audit_evidence"
    inventory = json.loads((evidence / "workspace_inventory.json").read_text(encoding="utf-8"))
    stats = json.loads((evidence / "data_statistics.json").read_text(encoding="utf-8"))
    details = json.loads((evidence / "data_file_details.json").read_text(encoding="utf-8"))
    archive = root / stats["archives"][0]["path"]
    expected = next(item["sha256"] for item in inventory if item["path"] == stats["archives"][0]["path"])
    if sha256_file(archive) != expected:
        raise DataError("行情源文件已经变化，必须重新审计")
    flags = ("core_missing_cells", "zero_volume_rows", "nonpositive_price_cells", "invalid_ohlc_rows", "previous_close_gap_gt_0_011_rows")
    candidates = [item for item in details if item["kind"] == "daily" and item["rows"] >= min_bars and all(item.get(k, 0) == 0 for k in flags)]
    latest = stats["summary"]["daily"]["dates"]["交易日期"]["max"]
    selected, rejected = [], []
    for item in sorted(candidates, key=lambda x: (-x["rows"], x["member"])):
        code = item["stock_codes"][0]
        if item["交易日期_max"] != latest:
            rejected.append({"code": code, "reason": "未覆盖当前数据快照末日"})
            continue
        bars, source = load_daily(archive, code)
        quality = validate_bars(bars)
        if not quality["passed"] or re.search(r"ST|PT|退", bars[-1].name, re.I):
            rejected.append({"code": code, "reason": "实际质量门槛或最新名称筛选未通过"})
            continue
        selected.append({"code": code, "name": bars[-1].name, "source": source, "quality": quality})
        if len(selected) == limit:
            break
    if not selected:
        raise DataError("没有通过筛选的演示数据")
    return {
        "selection_basis": "先质量后覆盖日期，按记录数降序和代码排序；未使用收益排序",
        "assumptions": {"min_bars": min_bars, "limit": limit, "gap_tolerance": PRICE_GAP_TOLERANCE,
                        "name_filter": "最新名称含 ST/PT/退则排除，仅为项目筛选，不证明历史证券状态"},
        "archive_sha256": expected, "audited_candidates": len(candidates),
        "selected": selected, "rejected_during_selection": rejected,
        "remaining_candidates_not_rechecked": len(candidates) - len(selected) - len(rejected),
    }
