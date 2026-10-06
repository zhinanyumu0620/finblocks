"""独立CSV行情：实际字段校验、不可变快照和账号归属，不依赖原始大ZIP。"""

import base64
import binascii
import csv
from datetime import date
import hashlib
import io
import json
from pathlib import Path
import re
import threading
import uuid

from .data import Bar, DataError, require_valid


MAX_BYTES = 2_000_000  # 项目导入预算，不是比赛限制。
ALIASES = {"symbol": ("symbol", "code", "股票代码"), "name": ("name", "股票名称"),
           "date": ("date", "交易日期"), "open": ("open", "开盘价"),
           "high": ("high", "最高价"), "low": ("low", "最低价"),
           "close": ("close", "收盘价"), "volume": ("volume", "成交量"),
           "previous_close": ("previous_close", "前收盘价")}
BASIS = {"raw": "未复权", "forward_adjusted": "前复权", "back_adjusted": "后复权", "unknown": "口径未确认"}


def is_csv_source(source):
    return isinstance(source, str) and (source == "sample" or re.fullmatch(r"csv_[0-9a-f]{32}", source) is not None)


def parse_csv(raw):
    if not raw or len(raw) > MAX_BYTES:
        raise DataError("CSV不能为空，且须不超过2MB（项目预算）")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise DataError("CSV须使用UTF-8或GB18030编码")
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        headers = reader.fieldnames or []
        if not headers or len(headers) != len(set(headers)) or len(headers) > 40:
            raise DataError("首行须为CSV表头，不允许重复字段或超过40列")
        mapping = {}
        for field, aliases in ALIASES.items():
            matches = [h for h in headers if h.strip().lower() in aliases]
            if len(matches) > 1:
                raise DataError(f"{field}对应多个表头，请保留一个明确字段")
            if matches:
                mapping[field] = matches[0]
        missing = set(("symbol", "date", "open", "high", "low", "close", "volume")) - mapping.keys()
        if missing:
            raise DataError("CSV缺少必需字段：" + ", ".join(sorted(missing)))
        panel, dates, names = {}, set(), {}
        rows = 0
        for line, row in enumerate(reader, 2):
            rows += 1
            if rows > 30000 or None in row or any(v is None for v in row.values()):
                raise DataError(f"第{line}行列数不一致或超过30000行项目预算")
            code = row[mapping["symbol"]].strip()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,23}", code):
                raise DataError(f"第{line}行代码须为1–24位字母、数字、点、横线或下划线")
            day = row[mapping["date"]].strip()
            try:
                if date.fromisoformat(day).isoformat() != day:
                    raise ValueError
            except ValueError:
                raise DataError(f"第{line}行日期须为真实的YYYY-MM-DD日期") from None
            if (code, day) in dates:
                raise DataError(f"第{line}行股票代码与日期重复；不自动去重")
            dates.add((code, day))
            name = row[mapping["name"]].strip() if "name" in mapping else code
            if not name or len(name) > 80 or any(ord(c) < 32 for c in name):
                raise DataError(f"第{line}行名称为空、过长或含控制字符")
            if code in names and names[code] != name:
                raise DataError(f"第{line}行同一代码的名称不一致")
            names[code] = name
            values = {}
            for field in ("open", "high", "low", "close", "volume", "previous_close"):
                if field not in mapping:
                    continue
                try:
                    values[field] = float(row[mapping[field]])
                except (ValueError, TypeError):
                    raise DataError(f"第{line}行{field}为空或不是数值；不补零") from None
            panel.setdefault(code, []).append(Bar(code, name, day, values))
            if len(panel) > 300:
                raise DataError("单份CSV最多300个标的，此为项目预算")
        if not panel:
            raise DataError("CSV没有行情记录")
        for bars in panel.values():
            bars.sort(key=lambda b: b.date)
            for index, bar in enumerate(bars):
                if "previous_close" not in mapping:
                    # 第一条没有更早行情，沿用首条收盘只作校验占位；报告明确披露。
                    bar.values["previous_close"] = bars[index-1].values["close"] if index else bar.values["close"]
            require_valid(bars)
    except csv.Error:
        raise DataError("CSV结构无效或字段超长") from None
    return panel, {"encoding": encoding, "columns": mapping, "rows": rows,
                   "ignored_columns": [h for h in headers if h not in mapping.values()],
                   "normalization": "每个代码按日期升序排列；不删除记录、不补价格或成交量",
                   "previous_close_basis": "用户提供并校验" if "previous_close" in mapping else "同一CSV上一条收盘派生；首条沿用本条收盘，不是官方前收盘参考价"}


class CSVData:
    def __init__(self, root):
        self.root = Path(root)
        self.directory = self.root / "private/csv_datasets"
        self.lock = threading.RLock()

    def get(self, source, owner=None):
        if not is_csv_source(source):
            raise DataError("CSV数据源标识无效")
        if source == "sample":
            metadata_path = self.root / "data/sample/manifest.json"
            csv_path = self.root / "data/sample/market.csv"
        else:
            metadata_path = self.directory / (source + ".json")
            csv_path = self.directory / (source + ".csv")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if source != "sample" and metadata.get("owner") != owner:
                raise DataError("该CSV不属于当前身份，请重新导入自己的数据")
            raw = csv_path.read_bytes()
        except (OSError, json.JSONDecodeError):
            raise DataError("CSV数据源不存在或无法读取，请重新导入") from None
        digest = hashlib.sha256(raw).hexdigest()
        if digest != metadata["sha256"]:
            raise DataError("CSV快照已变化，请重新导入；不会复用旧报告或冻结样本")
        panel, audit = parse_csv(raw)
        symbols = {code: {"code": code, "name": bars[0].name, "pool": "custom", "local_data": True,
                          "source": {"start": bars[0].date, "end": bars[-1].date, "rows": len(bars)},
                          "quality": {"full_history_passed": True}} for code, bars in panel.items()}
        return metadata, panel, audit, symbols

    def public(self, source, owner=None):
        metadata, _, audit, symbols = self.get(source, owner)
        return {"label": metadata["label"], "members": {code: {**item["source"], "name": item["name"], "status": "READY"} for code, item in symbols.items()},
                "summary": {"ready": len(symbols), "rows": audit["rows"]}, "sha256": metadata["sha256"],
                "provenance": metadata["provenance"], "audit": audit}

    def sources(self, owner=None):
        sources = {}
        if (self.root / "data/sample/manifest.json").is_file():
            sources["sample"] = self.public("sample", owner)
        for path in sorted(self.directory.glob("csv_*.json")):
            try:
                metadata = json.loads(path.read_text(encoding="utf-8"))
                if metadata.get("owner") == owner:
                    sources[path.stem] = self.public(path.stem, owner)
            except (OSError, ValueError, KeyError):
                continue  # 损坏的数据不作为可用入口，直接调用仍明确报错。
        return sources

    def import_data(self, payload, owner=None):
        required = {"csv_base64", "label", "source_notice", "price_basis", "volume_unit", "currency", "confirmed"}
        if not isinstance(payload, dict) or set(payload) != required or payload["confirmed"] is not True:
            raise DataError("请填写完整数据来源、价格口径、单位，并确认有权使用导入数据")
        if payload["price_basis"] not in BASIS or payload["volume_unit"] != "shares":
            raise DataError("请选择价格口径；成交量须先换算为股，不能混用手或金额")
        for key in ("label", "source_notice", "currency"):
            value = payload[key]
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= 200 or any(ord(c) < 32 for c in value):
                raise DataError("名称、来源和币种须为不含控制字符的1–200字符文字")
        try:
            raw = base64.b64decode(payload["csv_base64"], validate=True)
        except (ValueError, TypeError, binascii.Error):
            raise DataError("CSV上传编码无效") from None
        parse_csv(raw)  # 全量通过后才保存，不留下部分成功的数据。
        source = "csv_" + uuid.uuid4().hex
        metadata = {"owner": owner, "label": "导入CSV · " + payload["label"].strip(),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "provenance": {"source_notice": payload["source_notice"].strip(), "price_basis": BASIS[payload["price_basis"]],
                                   "volume_unit": "股", "currency": payload["currency"].strip(),
                                   "verification": "用户声明来源与使用权限，平台未独立核验供应商、复权及许可"}}
        with self.lock:
            if sum(1 for item in self.sources(owner) if item != "sample") >= 30:
                raise DataError("当前身份最多保留30份CSV快照，此为项目预算")
            self.directory.mkdir(parents=True, exist_ok=True)
            (self.directory / (source + ".csv")).write_bytes(raw)
            (self.directory / (source + ".json")).write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        return {"source_id": source, "source": self.public(source, owner)}

    def load(self, source, code, start=None, end=None, owner=None, validate=True):
        metadata, panel, audit, _ = self.get(source, owner)
        if code not in panel:
            raise DataError("该代码不在所选CSV中，不回退到其他数据源")
        for day in (start, end):
            if day is not None and (not isinstance(day, str) or date.fromisoformat(day).isoformat() != day):
                raise DataError("日期须为YYYY-MM-DD")
        if start and end and start > end:
            raise DataError("开始日期晚于结束日期")
        bars = [b for b in panel[code] if (not start or b.date >= start) and (not end or b.date <= end)]
        if not bars:
            raise DataError("所选CSV区间没有真实记录")
        if validate:
            require_valid(bars)
        return bars, {"source_id": source, "member": code + ".csv", "member_sha256": metadata["sha256"],
                      "dataset_sha256": metadata["sha256"], "archive": metadata["label"], "rows": len(bars),
                      "start": bars[0].date, "end": bars[-1].date, "provenance": metadata["provenance"], "csv_audit": audit}
