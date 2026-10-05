"""公开行情快照：保留原响应，显式标注研究价格口径，不覆盖原始 ZIP。"""

from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re
import urllib.parse
import urllib.request

from .data import Bar, DataError, FIELDS, require_valid, sha256_file


SOURCE_ID = "public_hfq"
SOURCE_DOCUMENT = "https://github.com/akfamily/akshare/blob/main/akshare/stock_feature/stock_hist_tx.py"
LIMITATIONS = ("腾讯公开行情单供应商快照，非交易所逐笔认证；后复权为研究价格，不能作为实际订单价格。"
               "前收盘字段为同一快照上一条研究收盘价的派生值，不是官方除权参考价。"
               "未证明历史时点可获得性；当前股票池仍有存活偏差；基本面PIT未开放。")


def parse_response(raw, code, adjustment):
    """只解析 JSON/固定变量赋值，不执行供应商脚本。"""
    text = raw.decode("utf-8").strip()
    if not text.startswith("{"):
        match = re.fullmatch(r"kline_day(?:hfq)?[0-9]{4}=(\{.*\});?", text, re.S)
        if not match:
            raise DataError("公开行情不是预期 JSON 或固定变量赋值")
        text = match.group(1)
    document = json.loads(text)
    if document.get("code") != 0:
        raise DataError("供应商没有返回成功状态")
    payload = document.get("data", {}).get(code)
    if not isinstance(payload, dict):
        raise DataError("公开行情没有请求代码的数据")
    key = "hfqday" if adjustment == "hfq" and "hfqday" in payload else "day"
    rows = payload.get(key)
    if not isinstance(rows, list) or not rows:
        raise DataError("公开行情为空，不能填充")
    normalized = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 9:
            raise DataError("供应商字段结构发生变化")
        day = row[0]
        if date.fromisoformat(day).isoformat() != day:
            raise DataError("公开行情日期无效")
        fields = ("open", "close", "high", "low", "volume_raw", "turnover_raw", "amount_raw")
        values = {}
        for field, index in zip(fields, (1, 2, 3, 4, 5, 7, 8)):
            value = float(row[index])
            if not math.isfinite(value):
                raise DataError("公开行情存在非有限数值")
            values[field] = value
        item = {"date": day, **values}
        if day in normalized and normalized[day] != item:
            raise DataError("供应商重复日期的内容存在冲突")
        normalized[day] = item
    return [normalized[k] for k in sorted(normalized)], key


def fetch_snapshot(root, code, adjustment, end):
    if not re.fullmatch(r"(?:sh|sz)[0-9]{6}", code) or adjustment not in ("", "hfq"):
        raise DataError("公开行情仅接受沪深代码及不复权/后复权")
    day = date.fromisoformat(end)
    year = day.year - 1
    # 供应商可能忽略开始日期或向前截取640条；实际覆盖以响应为准。
    params = {"_var": f"kline_day{adjustment}{year}",
              "param": f"{code},day,{year}-01-01,{day.year}-12-31,640,{adjustment}",
              "r": "0.8205512681390605"}
    url = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=20) as response:
        raw = response.read(4000001)
        if len(raw) > 4000000:
            raise DataError("公开响应超出大小限制")
    rows, key = parse_response(raw, code, adjustment)
    digest = hashlib.sha256(raw).hexdigest()
    destination = Path(root) / "data/public_sources/raw" / (digest + ".json.txt")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and sha256_file(destination) != digest:
        raise DataError("公开快照存储发生哈希冲突")
    destination.write_bytes(raw)
    return rows, {"provider": "Tencent", "url": url, "retrieved_at": datetime.now(timezone.utc).isoformat(),
                  "path": destination.relative_to(root).as_posix(), "sha256": digest,
                  "requested_adjustment": adjustment or "none", "returned_key": key,
                  "response_rows": len(rows), "response_start": rows[0]["date"], "response_end": rows[-1]["date"],
                  "implementation_reference": SOURCE_DOCUMENT}


def normalize_snapshot(code, name, plain, adjusted, returned_key, local_bars, end):
    """单位换算须得到本地逐行交叉证据；没有证据则阻断。"""
    plain_by_date = {r["date"]: r for r in plain if r["date"] <= end}
    local = {b.date: b for b in local_bars}
    overlap = [(r, local[day]) for day, r in plain_by_date.items()
               if day in local and local[day].values.get("volume") and r["volume_raw"] > 0]
    if len(overlap) < 20:
        raise DataError("成交量单位交叉证据少于20条，不自动猜测换算")
    # 腾讯科创板通常为股，其他股票通常为手；000开头的深市股票也逐行验证。
    scale = 1 if code.startswith("sh688") else 100
    errors = [abs(r["volume_raw"] * scale - b.values["volume"]) for r, b in overlap]
    if max(errors) > scale / 2 + 1:
        raise DataError("成交量与本地精确记录不符合股/手舍入关系，单位待人工核验")
    conflicts = []
    for r, b in overlap:
        for field in ("open", "high", "low", "close"):
            if b.values.get(field) != r[field]:
                conflicts.append({"date": b.date, "field": field, "original": b.values.get(field), "public": r[field]})
    rows = [r for r in adjusted if r["date"] <= end]
    if len(rows) < 2:
        raise DataError("公开研究价格不足两条")
    if set(plain_by_date) != {r["date"] for r in rows}:
        raise DataError("不复权与研究价格日历不一致，不能混合快照")
    if returned_key == "day":
        if rows != [plain_by_date[r["date"]] for r in rows]:
            raise DataError("后复权请求返回day，但与不复权响应不一致")
        # 无复权字段时，仅接受本地除权参考价未出现断点的重叠区间，禁止默认声称已复权。
        for previous, current in zip(rows, rows[1:]):
            b = local.get(current["date"])
            if b and b.values.get("previous_close") != previous["close"]:
                raise DataError("供应商回退day且本地参考价存在断点，不能认领后复权")
    output = []
    for previous, current in zip(rows, rows[1:]):
        values = {field: None for field in FIELDS}
        values.update({field: current[field] for field in ("open", "high", "low", "close")})
        values.update(previous_close=previous["close"], volume=current["volume_raw"] * scale,
                      amount=float(Decimal(str(current["amount_raw"])) * 10000))
        output.append(Bar(code, name, current["date"], values))
    require_valid(output)
    return output, {"returned_key": returned_key, "volume_scale": scale,
                    "volume_unit": "股（按公开实现的市场规则换算，并经本地重叠记录验证）",
                    "volume_unit_evidence_rows": len(overlap), "volume_max_rounding_difference_shares": max(errors),
                    "volume_precision_shares": scale, "amount_unit": "元", "amount_precision_yuan": 100,
                    "price_basis": "腾讯后复权研究序列" if returned_key == "hfqday" else "腾讯day回退序列；重叠除权参考价连续性通过，未认领后复权",
                    "previous_close_basis": "从同一完整快照上一条收盘价派生；首条响应不进入可执行数据",
                    "local_comparison": {"overlap_rows": len(overlap), "ohlc_conflicts": conflicts},
                    "market_cap_basis": "该接口未提供可靠历史市值，留空；不移植原口径市值", "limitations": LIMITATIONS}


def public_manifest(root):
    path = Path(root) / "data/public_sources/manifest.json"
    if not path.exists():
        raise DataError("尚无已审计公开数据集")
    raw = path.read_bytes()
    document = json.loads(raw)
    if document.get("source_id") != SOURCE_ID or document.get("version") != 1:
        raise DataError("公开数据集清单格式不受支持")
    document["snapshot_sha256"] = hashlib.sha256(raw).hexdigest()
    return document


def load_public_daily(root, code, start=None, end=None):
    manifest = public_manifest(root)
    entry = manifest["members"].get(code)
    if not entry or entry.get("status") != "READY":
        raise DataError("该股票的公开数据未通过审计，不回退原数据")
    for bound in (start, end):
        if bound is not None and date.fromisoformat(bound).isoformat() != bound:
            raise DataError("日期须为YYYY-MM-DD")
    if start and end and start > end:
        raise DataError("开始日期不能晚于结束日期")
    if (start and start < entry["start"]) or (end and end > entry["end"]):
        raise DataError(f"公开快照仅覆盖{entry['start']}至{entry['end']}；不补价格或缩短请求")
    root = Path(root).resolve()
    def checked_file(relative, expected):
        path = (root / relative).resolve()
        if not path.is_relative_to(root / "data/public_sources") or sha256_file(path) != expected:
            raise DataError("公开快照路径或SHA校验失败")
        return path
    path = checked_file(entry["path"], entry["sha256"])
    for evidence in entry["responses"]:
        checked_file(evidence["path"], evidence["sha256"])
    document = json.loads(path.read_text(encoding="utf-8"))
    if document["code"] != code or document["metadata"] != entry["metadata"]:
        raise DataError("公开数据内容与清单不一致")
    all_bars = [Bar(code, document["name"], r["date"], r["values"]) for r in document["records"]]
    if len(all_bars) != entry["rows"] or all_bars[0].date != entry["start"] or all_bars[-1].date != entry["end"]:
        raise DataError("公开数据覆盖与清单不一致")
    bars = [b for b in all_bars if (not start or b.date >= start) and (not end or b.date <= end)]
    require_valid(bars)
    return bars, {"archive": "腾讯公开行情快照", "member": code + ".csv", "source_id": SOURCE_ID,
                  "member_sha256": entry["sha256"], "manifest_sha256": manifest["snapshot_sha256"],
                  "rows": len(bars), "start": bars[0].date, "end": bars[-1].date,
                  "responses": entry["responses"], **entry["metadata"]}
