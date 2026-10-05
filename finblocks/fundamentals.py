"""只读原始财报发布记录；未经核验的字段不作为历史策略因子。"""

import csv
import hashlib
import io
import re
import zipfile

from .data import DataError


RAW_FIELDS = ("R_np_atoopc@xbx", "B_total_equity_atoopc@xbx", "R_basic_eps@xbx",
              "R_operating_total_revenue@xbx", "R_revenue@xbx", "B_total_assets@xbx")


def load_financial_reports(archive, code):
    if not isinstance(code, str) or not re.fullmatch(r"(?:bj|sh|sz)[0-9]{6}", code):
        raise DataError("财报股票代码无效")
    member = f"{code}/{code}_一般企业.csv"
    try:
        with zipfile.ZipFile(archive) as source:
            raw = source.read(member)
    except (KeyError, zipfile.BadZipFile) as exc:
        raise DataError("本地财务包没有该格式的原始财报") from exc
    lines = io.StringIO(raw.decode("gb18030"))
    next(lines)  # 跳过推广/来源首行，不把联系方式暴露到页面。
    reader = csv.DictReader(lines)
    required = {"stock_code", "statement_format", "report_date", "publish_date", *RAW_FIELDS}
    if not required.issubset(reader.fieldnames or []):
        raise DataError("财报字段与实际审计结构不一致，不能自动猜测字段")
    reports = []
    for row in reader:
        if row["stock_code"] != code:
            raise DataError("财报文件与内部股票代码不一致")
        reports.append({"report_date": row["report_date"], "publish_date": row["publish_date"],
                        "statement_format": row["statement_format"],
                        "values": {field: row[field] for field in RAW_FIELDS}})
    reports.sort(key=lambda row: (row["publish_date"], row["report_date"]))
    return {"symbol": code, "rows": len(reports), "fields": list(RAW_FIELDS), "reports": reports,
            "source": {"archive": archive.name, "member": member,
                       "member_sha256": hashlib.sha256(raw).hexdigest(), "encoding": "gb18030"},
            "unit": "UNKNOWN", "historical_factor_supported": False,
            "limitations": "仅浏览原始发布记录，保留不同发布版本；字段语义、单位、累计口径和历史修订可得时点未核验，不能据此计算历史PE/PB/ROE或回填策略。"}
