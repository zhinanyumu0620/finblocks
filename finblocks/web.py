"""本机工作台服务：复用真实内核，不公开原数据或环境凭据。"""

from collections import OrderedDict
import base64
import binascii
import csv
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import secrets
import threading
import uuid
import zlib

from .ai import AIError, generate_strategy, generate_factor, explain_research
from .auth import Accounts, AuthError
from .backtest import run_backtest, validate_metric_parameters
from .data import DataError, load_daily, sha256_file, require_valid
from .dsl import CompileError, compile_strategy, evaluate
from .factors import compile_factor, describe_factor, evaluate_factor
from .fundamentals import load_financial_reports
from .research import ResearchJournal, audit_intent, repair_strategy, evidence_pack, document_hash
from .portfolio import run_portfolio, validate_portfolio_options
from .public_data import SOURCE_ID, LIMITATIONS, public_manifest, load_public_daily


def encode_json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")


def records_csv(report):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(report["records"][0]))
    writer.writeheader()
    writer.writerows({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value
                     for key, value in row.items()} for row in report["records"])
    return b"\xef\xbb\xbf" + output.getvalue().encode("utf-8")


def require_png(body):
    """验证PNG块完整性，拒绝只有文件头、CRC损坏或截断的导出。"""
    if not body.startswith(b"\x89PNG\r\n\x1a\n") or len(body) > 750000:
        raise ValueError("PNG格式或大小不符合项目限制")
    offset, saw_header, saw_data = 8, False, False
    while offset + 12 <= len(body):
        length = int.from_bytes(body[offset:offset + 4], "big")
        kind = body[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(body):
            break
        content = body[offset + 8:end - 4]
        if zlib.crc32(kind + content) != int.from_bytes(body[end - 4:end], "big"):
            raise ValueError("PNG校验和不一致")
        if not saw_header:
            if kind != b"IHDR" or length != 13:
                raise ValueError("PNG图像头无效")
            width, height = int.from_bytes(content[:4], "big"), int.from_bytes(content[4:8], "big")
            if not 1 <= width <= 8192 or not 1 <= height <= 8192:
                raise ValueError("PNG图像尺寸超出项目范围")
            saw_header = True
        elif kind == b"IHDR":
            raise ValueError("PNG图像头重复")
        if kind == b"IDAT":
            saw_data = True
        if kind == b"IEND":
            if length == 0 and saw_data and end == len(body):
                return
            raise ValueError("PNG结束块无效")
        offset = end
    raise ValueError("PNG文件缺少数据或被截断")


class Workspace:
    def __init__(self, root):
        self.root = Path(root)
        stats = json.loads((self.root / "docs/audit_evidence/data_statistics.json").read_text(encoding="utf-8"))
        self.archive = self.root / stats["archives"][0]["path"]
        self.manifest = json.loads((self.root / "data/demo_manifest.json").read_text(encoding="utf-8"))
        self.pool = json.loads((self.root / "data/csi300_manifest.json").read_text(encoding="utf-8"))
        if self.pool["archive_sha256"] != self.manifest["archive_sha256"]:
            raise DataError("股票池与原始行情审计不一致")
        if len(self.pool["members"]) != 300 or len({item["code"] for item in self.pool["members"]}) != 300:
            raise DataError("沪深300名单不完整或有重复")
        self.symbols = {item["code"]: {**item, "pool": "demo"} for item in self.manifest["selected"]}
        self.symbols.update({item["code"]: item for item in self.pool["members"]})
        self.default_strategy = json.loads((self.root / "examples/ma_strategy.json").read_text(encoding="utf-8"))
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.cache, self.generations, self.runs, self.exports = OrderedDict(), OrderedDict(), OrderedDict(), OrderedDict()
        self.signature = None
        self.financial_archive = self.root / stats["archives"][1]["path"]
        inventory = json.loads((self.root / "docs/audit_evidence/workspace_inventory.json").read_text(encoding="utf-8"))
        self.financial_expected_sha = next(item["sha256"] for item in inventory if item["path"] == stats["archives"][1]["path"])
        self.financial_signature, self.financial_cache = None, {}
        # 共享源码首次启动可以浏览工作台；缺数据时真实回测仍明确阻断。
        if self.archive.exists():
            self.ensure_archive()
        self.accounts = Accounts(self.root / "private/test_accounts.sqlite3")
        self.run_owners, self.generation_owners, self.export_owners = {}, {}, {}
        self.journal = ResearchJournal(self.root / "private/research_history.sqlite3")

    def ensure_archive(self):
        with self.lock:
            if not self.archive.is_file():
                raise DataError("尚未导入行情数据，请按 TEAM_README.md 放入原始行情 ZIP；不会生成模拟行情")
            stat = self.archive.stat()
            signature = (stat.st_size, stat.st_mtime_ns)
            if signature != self.signature:
                if sha256_file(self.archive) != self.manifest["archive_sha256"]:
                    raise DataError("原始行情包已变化，请重新审计和筛选")
                self.cache.clear()
                self.signature = signature

    def bootstrap(self, user=None):
        return {"token": self.token, "symbols": [{"code": code, "name": item["name"], **item["source"],
                                                  "pool": item["pool"], "local_data": item.get("local_data", True),
                                                  "quality": item.get("quality", {"full_history_passed": True})}
                                                 for code, item in self.symbols.items()],
                "stock_pool": {key: self.pool[key] for key in ("index_code", "name", "as_of", "official_url", "official_sha256", "summary", "limitations")},
                "default_strategy": self.default_strategy, "ai_configured": bool(os.environ.get("DEEPSEEK_API_KEY", "").strip()),
                "user": user,
                "defaults": {"cost_bps": 10, "lag": 1, "periods_per_year": 252, "annual_risk_free_rate": 0.0}, "price_gap_tolerance": 0.0,
                "fundamentals": json.loads((self.root / "data/fundamental_capabilities.json").read_text(encoding="utf-8")),
                "data_sources": self.data_sources(),
                "limits": ["日频分数持仓研究", "原始价格复权口径未外部核验；公开快照单独审计", "基本面PIT、指数、分钟和实盘未开放"]}

    def data_sources(self):
        try:
            manifest = public_manifest(self.root)
            return {"public_hfq": {"label": "公开行情 · 后复权研究快照", "summary": manifest.get("summary", {}),
                    "members": {code: {k: entry[k] for k in ("status", "start", "end", "rows") if k in entry}
                                for code, entry in manifest["members"].items()}, "limitations": LIMITATIONS}}
        except DataError:
            return {}

    def data_source(self, payload):
        source = payload.get("data_source", "original")
        if source not in ("original", SOURCE_ID):
            raise DataError("请选择原始数据或已审计公开快照")
        return source

    def source_assumptions(self, report, source_id):
        if source_id == SOURCE_ID:
            report["assumptions"]["price_basis"] = LIMITATIONS
            report["assumptions"]["execution"] += " 公开模式成交价与股数均为复权研究单位，不能映射为真实订单。"

    def validate(self, strategy):
        compiled = compile_strategy(strategy)
        return {"valid": True, "warmup_records": compiled.warmup,
                "ordered_ids": [node["id"] for node in compiled.ordered_nodes]}

    def financials(self, payload):
        if set(payload) != {"symbol"} or not isinstance(payload["symbol"], str) or payload["symbol"] not in self.symbols:
            raise DataError("只能查看当前标的的原始财报")
        with self.lock:
            if not self.financial_archive.is_file():
                raise DataError("尚未导入财务数据，请按 TEAM_README.md 放入原始财务 ZIP")
            stat = self.financial_archive.stat()
            signature = (stat.st_size, stat.st_mtime_ns)
            if signature != self.financial_signature:
                if sha256_file(self.financial_archive) != self.financial_expected_sha:
                    raise DataError("原始财务包已变化，请重新审计")
                self.financial_cache.clear()
                self.financial_signature = signature
            code = payload["symbol"]
            if code not in self.financial_cache:
                self.financial_cache[code] = load_financial_reports(self.financial_archive, code)
            return self.financial_cache[code]

    def export_artifact(self, payload, owner=None):
        """导出先实际落盘；浏览器阻止下载时仍可从本地输出取得文件。"""
        kind = payload.get("kind")
        if kind == "research":
            document = self.journal.get(payload.get("research_id"), owner)
            body, filename, content_type = encode_json(document), "research.json", "application/json; charset=utf-8"
        elif kind == "workspace":
            document = payload.get("workspace")
            if not isinstance(document, dict) or document.get("format") != "finblocks-workspace" or document.get("version") != 1:
                raise ValueError("工作台文件格式无效")
            self.validate(document.get("strategy"))
            settings = document.get("config")
            if (not isinstance(settings, dict) or not {"symbol", "start", "end", "cost_bps", "lag"}.issubset(settings)
                    or set(settings) - {"symbol", "start", "end", "cost_bps", "lag", "periods_per_year", "annual_risk_free_rate", "mode", "symbols", "portfolio", "data_source"}):
                raise ValueError("工作台配置不完整")
            self.data_source(settings)
            if settings.get("mode", "single") == "portfolio":
                self.portfolio_spec({key: value for key, value in settings.items() if key not in {"symbol", "mode"}} | {"strategy": document["strategy"]})
            elif settings.get("mode", "single") != "single" or {"symbols", "portfolio"} & set(settings):
                raise ValueError("工作台回测模式或组合设置不一致")
            settings = {"periods_per_year": 252, "annual_risk_free_rate": 0.0, **settings}
            validate_metric_parameters(settings["periods_per_year"], settings["annual_risk_free_rate"])
            if settings["symbol"] not in self.symbols or type(settings["lag"]) is not int or settings["lag"] not in (1, 2):
                raise ValueError("标的或滞后配置无效")
            # 复用内核的数值门槛，不要求保存时区间内一定有足够回测数据。
            cost = settings["cost_bps"]
            if type(cost) not in (int, float) or not 0 <= cost < 10000:
                raise ValueError("成本配置无效")
            for day in (settings["start"], settings["end"]):
                if day is not None and (not isinstance(day, str) or date.fromisoformat(day).isoformat() != day):
                    raise ValueError("日期配置无效")
            if settings["start"] and settings["end"] and settings["start"] > settings["end"]:
                raise ValueError("开始日期晚于结束日期")
            clean = {"format": "finblocks-workspace", "version": 1, "strategy": document["strategy"],
                     "config": settings, "view": {"positions": {}}}
            view = document.get("view", {})
            if not isinstance(view, dict) or not isinstance(view.get("positions", {}), dict):
                raise ValueError("积木位置配置须为对象")
            positions = view.get("positions", {})
            for node in document["strategy"]["nodes"]:
                position = positions.get(node["id"])
                if isinstance(position, dict) and all(type(position.get(key)) in (int, float) and 0 <= position[key] <= limit for key, limit in (("x", 3000), ("y", 5000))):
                    clean["view"]["positions"][node["id"]] = {"x": position["x"], "y": position["y"]}
            body, filename, content_type = encode_json(clean), "strategy.json", "application/json; charset=utf-8"
        else:
            with self.lock:
                report = self.runs.get(payload.get("run_id"))
                if self.run_owners.get(payload.get("run_id")) != owner:
                    report = None
            if report is None:
                raise ValueError("该回测已过期，请重新运行")
            if kind == "report":
                body, filename, content_type = encode_json(report), "report.json", "application/json; charset=utf-8"
            elif kind == "records":
                body, filename, content_type = records_csv(report), "records.csv", "text/csv; charset=utf-8"
            elif kind == "chart":
                encoded = payload.get("image")
                if not isinstance(encoded, str) or not encoded.startswith("data:image/png;base64,"):
                    raise ValueError("净值图须为页面生成的PNG")
                try:
                    body = base64.b64decode(encoded.split(",", 1)[1], validate=True)
                except binascii.Error:
                    raise ValueError("PNG编码无效") from None
                require_png(body)
                body, filename, content_type = body, "equity.png", "image/png"
            else:
                raise ValueError("导出类型未开放")
        export_id = uuid.uuid4().hex
        destination = self.root / "artifacts/web" / (export_id + "_" + filename)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)
        with self.lock:
            self.exports[export_id] = (destination, filename, content_type)
            self.export_owners[export_id] = owner
            while len(self.exports) > 32:
                expired, _ = self.exports.popitem(last=False)
                self.export_owners.pop(expired, None)
        return {"url": "/api/file/" + export_id + "/" + filename, "filename": destination.name,
                "bytes": len(body), "saved_locally": True}

    def generate(self, prompt, owner=None):
        result = generate_strategy(prompt)
        generation_id = uuid.uuid4().hex
        with self.lock:
            self.generations[generation_id] = result
            self.generation_owners[generation_id] = owner
            while len(self.generations) > 32:
                expired, _ = self.generations.popitem(last=False)
                self.generation_owners.pop(expired, None)
        destination = self.root / "artifacts/web"
        destination.mkdir(parents=True, exist_ok=True)
        (destination / ("ai_" + generation_id + ".json")).write_bytes(encode_json(result))
        return {**result, "generation_id": generation_id}

    def backtest(self, payload, owner=None):
        if not isinstance(payload, dict) or set(payload) - {"strategy", "symbol", "start", "end", "cost_bps", "lag", "generation_id", "periods_per_year", "annual_risk_free_rate", "data_source"}:
            raise ValueError("回测请求包含未知字段或不是对象")
        if not {"strategy", "symbol", "cost_bps", "lag"}.issubset(payload):
            raise ValueError("必须明确策略、数据、成本情景和信号滞后")
        code = payload["symbol"]
        if not isinstance(code, str) or code not in self.symbols:
            raise DataError("请选择当前股票池或历史演示清单中的标的")
        if not self.symbols[code].get("local_data", True):
            raise DataError("该成分股的本地行情不可读取，请查看数据说明")
        start, end = payload.get("start"), payload.get("end")
        for boundary in (start, end):
            if boundary is not None and (not isinstance(boundary, str) or date.fromisoformat(boundary).isoformat() != boundary):
                raise ValueError("日期须为 YYYY-MM-DD")
        if start and end and start > end:
            raise ValueError("开始日期晚于结束日期")
        source_id = self.data_source(payload)
        bars, source = self.load_bars(code, start, end, source_id)
        with self.lock:
            generation_id = payload.get("generation_id")
            generation = self.generations.get(generation_id) if isinstance(generation_id, str) else None
            if generation and self.generation_owners.get(generation_id) != owner:
                generation = None
        report = run_backtest(bars, payload["strategy"], cost_bps=payload["cost_bps"], execution_lag_bars=payload["lag"],
                              periods_per_year=payload.get("periods_per_year", 252),
                              annual_risk_free_rate=payload.get("annual_risk_free_rate", 0.0))
        report["node_outputs"] = evaluate(compile_strategy(payload["strategy"]), bars)
        report["source"] = {**source,
                            "rows": len(bars), "start": bars[0].date, "end": bars[-1].date}
        self.source_assumptions(report, source_id)
        report["stock_pool"] = {"membership": self.symbols[code]["pool"],
                                "as_of": self.pool["as_of"] if self.symbols[code]["pool"] == "csi300" else None,
                                "official_url": self.pool["official_url"] if self.symbols[code]["pool"] == "csi300" else None,
                                "official_sha256": self.pool["official_sha256"] if self.symbols[code]["pool"] == "csi300" else None,
                                "limitations": self.pool["limitations"] if self.symbols[code]["pool"] == "csi300" else "历史演示标的，非沪深300成员"}
        report["strategy_origin"] = {"type": "manual_or_imported_workspace"}
        if generation and generation.get("status") == "ok":
            report["strategy_origin"] = {"type": "actual_api" if generation["strategy"] == payload["strategy"] else "actual_api_then_edited",
                                          "generation_id": generation_id, "model": generation["evidence"]["model_returned"],
                                          "response_id": generation["evidence"]["response_id"]}
        return self.store_backtest(report, owner)

    def store_backtest(self, report, owner):
        """组合与单股共用账号归属、落盘、研究历史及导出逻辑。"""
        run_id = uuid.uuid4().hex
        report["research_id"] = self.journal.append("backtest", {"kind": "backtest", "strategy": report["strategy"],
            "strategy_sha256": document_hash(report["strategy"]), "source": report["source"],
            "metrics": report["metrics"], "assumptions": report["assumptions"], "stock_pool": report["stock_pool"],
            "strategy_origin": report["strategy_origin"], **({"mode": "portfolio", "symbols": report["symbols"],
            "portfolio": report["portfolio"]} if report.get("kind") == "portfolio" else {})}, owner)
        destination = self.root / "artifacts/web"
        destination.mkdir(parents=True, exist_ok=True)
        (destination / ("run_" + run_id + ".json")).write_bytes(encode_json(report))
        with self.lock:
            self.runs[run_id] = report
            self.run_owners[run_id] = owner
            while len(self.runs) > 8:
                expired, _ = self.runs.popitem(last=False)
                self.run_owners.pop(expired, None)
        return {"run_id": run_id, "report": report}

    def portfolio_spec(self, payload):
        allowed = {"strategy", "symbols", "start", "end", "cost_bps", "lag", "portfolio", "periods_per_year", "annual_risk_free_rate", "generation_id", "data_source"}
        required = {"strategy", "symbols", "start", "end", "cost_bps", "lag", "portfolio"}
        if not isinstance(payload, dict) or set(payload) - allowed or not required.issubset(payload):
            raise ValueError("组合请求须明确策略、集合、区间、成本、滞后和仓位规则")
        self.data_source(payload)
        codes = payload["symbols"]
        if (not isinstance(codes, list) or not 1 <= len(codes) <= 300
                or any(not isinstance(code, str) or code not in self.symbols for code in codes) or len(set(codes)) != len(codes)):
            raise ValueError("请选择1–300个已审计且不重复的股票代码")
        for day in (payload["start"], payload["end"]):
            if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
                raise ValueError("组合回测须明确开始与结束日期")
        if payload["start"] > payload["end"]:
            raise ValueError("开始日期晚于结束日期")
        cost, lag = payload["cost_bps"], payload["lag"]
        if type(cost) not in (int, float) or not 0 <= cost < 10000 or type(lag) is not int or not 1 <= lag <= 10:
            raise ValueError("组合成本或执行滞后无效")
        validate_metric_parameters(payload.get("periods_per_year", 252), payload.get("annual_risk_free_rate", 0.0))
        validate_portfolio_options(payload["portfolio"])
        compiled = compile_strategy(payload["strategy"])
        if payload["strategy"]["allocation"]["when_false"] != 0:
            raise ValueError("股票池筛选模式要求否则持仓比例为0；满足条件比例是组合总仓位预算")
        return sorted(codes), compiled

    def portfolio_preflight(self, payload):
        codes, compiled = self.portfolio_spec(payload)
        self.ensure_archive()
        source_id = self.data_source(payload)
        public = public_manifest(self.root) if source_id == SOURCE_ID else None
        items, calendar, date_sets = [], set(), {}
        for code in codes:
            item = {"code": code, "name": self.symbols[code]["name"], "eligible": False}
            try:
                available = public["members"].get(code, {}) if public else self.symbols[code]["source"]
                if public and available.get("status") != "READY":
                    raise DataError("公开数据未通过审计：" + available.get("reason", "未下载"))
                if payload["start"] < available["start"] or payload["end"] > available["end"]:
                    raise DataError(f"请求区间超出本地覆盖{available['start']}至{available['end']}；不自动缩短区间")
                bars, source = self.load_bars(code, payload["start"], payload["end"], source_id, validate=False)
                if public and source["manifest_sha256"] != public["snapshot_sha256"]:
                    raise DataError("公开数据集在集合检查期间已更新，请重新检查")
                dates = {bar.date for bar in bars}
                calendar.update(dates)
                item.update(rows=len(bars), start=bars[0].date, end=bars[-1].date)
                require_valid(bars)
                if len(bars) < compiled.warmup + payload["lag"] + 1:
                    raise DataError("记录不足，不能覆盖预热、信号滞后及持仓收益")
                date_sets[code] = dates
                item["eligible"] = True
            except (DataError, ValueError) as exc:
                item["reason"] = str(exc)
            items.append(item)
        for item in items:
            if item["eligible"]:
                missing = sorted(calendar - date_sets[item["code"]])
                if missing:
                    item.update(eligible=False, reason=f"缺少共同日历{len(missing)}条记录，首个缺失日期{missing[0]}；不删日期或补价格")
                    event_path = self.root / "data/public_sources/security_events.json"
                    if event_path.exists():
                        events = json.loads(event_path.read_text(encoding="utf-8"))
                        for event in events["events"]:
                            if event["code"] == item["code"] and all(event["start"] <= day <= event["end"] for day in missing):
                                evidence = (self.root / event["path"]).resolve()
                                if evidence.is_relative_to((self.root / "data/public_sources/events").resolve()) and sha256_file(evidence) == event["sha256"]:
                                    item["security_event"] = event
                                    item["reason"] += f"；发行人公告{event['notice']}确认该段停牌，{event['resume']}复牌；当前内核未实现停牌冻结撮合，保留阻断"
        eligible = [item["code"] for item in items if item["eligible"]]
        return {"status": "PASS" if len(eligible) == len(codes) else "BLOCKED", "requested": len(codes),
                "eligible_symbols": eligible, "calendar_records": len(calendar), "items": items, "data_source": source_id,
                "snapshot_sha256": public["snapshot_sha256"] if public else self.manifest["archive_sha256"],
                "scope": "逐股来源SHA、价格质量、窗口长度及日历覆盖检查，不是收益筛选；不会自动缩减股票集合"}

    def portfolio_backtest(self, payload, owner=None):
        codes, _ = self.portfolio_spec(payload)
        checked = self.portfolio_preflight(payload)
        if checked["status"] != "PASS":
            first = next(item for item in checked["items"] if not item["eligible"])
            raise DataError(f"股票池检查未通过：{len(codes)-len(checked['eligible_symbols'])}个标的不符合；{first['code']}：{first['reason']}。请查看集合检查，不会自动删股")
        series, sources = {}, []
        source_id = self.data_source(payload)
        for code in codes:
            bars, source = self.load_bars(code, payload["start"], payload["end"], source_id)
            if source_id == SOURCE_ID and source["manifest_sha256"] != checked["snapshot_sha256"]:
                raise DataError("公开数据集在检查与执行之间已更新，请重新检查")
            series[code] = bars
            sources.append({"code": code, "name": self.symbols[code]["name"], **source})
        report = run_portfolio(series, payload["strategy"], cost_bps=payload["cost_bps"], execution_lag_bars=payload["lag"],
                               portfolio=payload["portfolio"], periods_per_year=payload.get("periods_per_year", 252),
                               annual_risk_free_rate=payload.get("annual_risk_free_rate", 0.0))
        report["source"] = {"archive": "腾讯公开行情快照" if source_id == SOURCE_ID else self.archive.name,
                            "archive_sha256": self.manifest["archive_sha256"], "data_source": source_id,
                            "members": sources, "start": report["metrics"]["start"], "end": report["metrics"]["end"]}
        self.source_assumptions(report, source_id)
        report["stock_pool"] = {"membership": "explicit_portfolio", "as_of": self.pool["as_of"],
                                "official_sha256": self.pool["official_sha256"], "limitations": self.pool["limitations"]}
        report["preflight"] = checked
        report["strategy_origin"] = {"type": "manual_or_imported_workspace"}
        generation_id = payload.get("generation_id")
        with self.lock:
            generated = self.generations.get(generation_id) if isinstance(generation_id, str) and self.generation_owners.get(generation_id) == owner else None
            if generated and generated.get("status") == "ok":
                report["strategy_origin"] = {"type": "actual_api" if generated["strategy"] == payload["strategy"] else "actual_api_then_edited",
                                              "generation_id": generation_id, "model": generated["evidence"]["model_returned"],
                                              "response_id": generated["evidence"]["response_id"]}
        return self.store_backtest(report, owner)

    def owned_run(self, run_id, owner):
        with self.lock:
            report = self.runs.get(run_id)
            if report is None or self.run_owners.get(run_id) != owner:
                raise ValueError("回测不存在、已过期或不属于当前身份")
            return report

    def research_evidence(self, payload, owner):
        if set(payload) - {"run_id", "date"}:
            raise ValueError("证据请求含未知字段")
        return evidence_pack(self.owned_run(payload.get("run_id"), owner), payload.get("date"))

    def explain(self, payload, owner):
        if set(payload) - {"run_id", "date", "confirmed"} or payload.get("confirmed") is not True:
            raise ValueError("请确认向已配置AI发送选定指标/积木/账目摘要；不上传原行情包")
        pack = self.research_evidence({k: v for k, v in payload.items() if k != "confirmed"}, owner)
        result = explain_research(pack)
        result["research_id"] = self.journal.append("explanation", {"kind": "explanation", "evidence_pack": pack, **result}, owner)
        return result

    def experiment(self, payload, owner):
        if set(payload) != {"run_id", "axis", "values", "confirmed"} or payload["confirmed"] is not True:
            raise ValueError("须确认单因素实验协议")
        axis, values = payload["axis"], payload["values"]
        if axis not in {"cost_bps", "lag"} or not isinstance(values, list) or not 1 <= len(values) <= 5:
            raise ValueError("首版支持成本或执行滞后，每次最多5个情景，此为可见项目预算")
        if any(type(v) not in (int, float) for v in values) or len(set(values)) != len(values):
            raise ValueError("实验值须为不重复数值")
        base = self.owned_run(payload["run_id"], owner)
        if base.get("kind") == "portfolio":
            raise ValueError("当前对照实验仅支持单股；组合请明确修改成本/滞后后重新回测，不自动改变集合或参数")
        bars, source = self.load_bars(base["source"]["member"].removesuffix(".csv"), base["source"]["start"], base["source"]["end"], base["source"].get("source_id", "original"))
        if source["member_sha256"] != base["source"]["member_sha256"]:
            raise DataError("实验数据与原回测不一致")
        results = []
        for value in values:
            try:
                report = run_backtest(bars, base["strategy"], cost_bps=value if axis == "cost_bps" else base["assumptions"]["cost_bps"],
                    execution_lag_bars=value if axis == "lag" else base["assumptions"]["execution_lag_bars"],
                    periods_per_year=base["assumptions"]["periods_per_year"], annual_risk_free_rate=base["assumptions"]["annual_risk_free_rate"])
                results.append({"value": value, "status": "DONE", "metrics": report["metrics"]})
            except ValueError as exc:
                results.append({"value": value, "status": "FAILED", "reason": str(exc)})
        document = {"kind": "experiment", "axis": axis, "values": values, "baseline": base["metrics"],
            "strategy": base["strategy"], "source": base["source"], "assumptions": base["assumptions"], "results": results,
            "scope": "单因素敏感性实验，不证明样本外有效；无自动择优、未实现窗口邻域或区间搜索"}
        document["research_id"] = self.journal.append("experiment", document, owner)
        return document

    def load_bars(self, code, start=None, end=None, source_id="original", validate=True):
        if not isinstance(code, str) or code not in self.symbols:
            raise DataError("标的不在已审计股票池中")
        for boundary in (start, end):
            if boundary is not None and (not isinstance(boundary, str) or date.fromisoformat(boundary).isoformat() != boundary):
                raise DataError("日期须为YYYY-MM-DD")
        if start and end and start > end:
            raise DataError("开始日期晚于结束日期")
        self.ensure_archive()
        if source_id == SOURCE_ID:
            bars, source = load_public_daily(self.root, code, start, end)
            return bars, {**source, "archive_sha256": self.manifest["archive_sha256"]}
        if source_id != "original":
            raise DataError("数据源不受支持")
        with self.lock:
            if code not in self.cache:
                bars, source = load_daily(self.archive, code)
                if source["member_sha256"] != self.symbols[code]["source"]["member_sha256"]:
                    raise DataError("行情成员与审计清单不一致")
                self.cache[code] = bars, source
                while len(self.cache) > 8:
                    self.cache.popitem(last=False)
            self.cache.move_to_end(code)
            full, source = self.cache[code]
        bars = [b for b in full if (not start or b.date >= start) and (not end or b.date <= end)]
        if validate:
            require_valid(bars)
        return bars, {**source, "archive_sha256": self.manifest["archive_sha256"], "rows": len(bars), "start": bars[0].date, "end": bars[-1].date}

    def factor(self, payload, owner, reveal_test=False):
        allowed = {"expression", "symbols", "start", "end", "protocol", "confirmed"}
        if set(payload) - (allowed | {"data_source"}) or not allowed.issubset(payload) or payload["confirmed"] is not True:
            raise ValueError("须明确确认表达式、股票、日期和完整实验协议")
        symbols = payload["symbols"]
        if not isinstance(symbols, list) or not 1 <= len(symbols) <= 300 or any(not isinstance(s, str) for s in symbols) or len(set(symbols)) != len(symbols):
            raise ValueError("研究标的须为1–300个不重复代码")
        panel, sources, rejected = {}, {}, []
        source_id = self.data_source(payload)
        for code in symbols:
            try:
                panel[code], sources[code] = self.load_bars(code, payload["start"], payload["end"], source_id)
            except ValueError as exc:
                rejected.append({"symbol": code, "reason": str(exc)})
        # 首版不静默缩减用户确认的研究集合；全部拒绝原因可查询。
        if rejected:
            failure = {"kind": "factor_rejected", "request": payload, "rejected": rejected,
                       "reason": "所选研究集合含不通过区间；未删除内部行或静默换标的。请明确修改协议后重试"}
            identifier = self.journal.append("factor_rejected", failure, owner)
            return {"status": "blocked", **failure, "research_id": identifier}
        result = evaluate_factor(panel, payload["expression"], payload["protocol"], reveal_test)
        family = document_hash({"symbols": sorted(symbols), "start": payload["start"], "end": payload["end"], "protocol": payload["protocol"]})
        # 同一实验跨数据源仍属于已查看测试；另外记录实际样本哈希，冻结时禁止换数据。
        result["sample_hash"] = document_hash({code: source["member_sha256"] for code, source in sources.items()})
        previous = self.journal.count_tests(family, owner)
        result.update(status="ok", kind="factor_test" if reveal_test else "factor_validation", source=sources, protocol_hash=family,
                      test_views_before_this=previous,
                      test_independence="已查看相同协议测试集，后续优化结果属探索，不能认领独立验证" if previous else "尚无本身份已记录的同协议测试查看；历史记录不完整时仍不能保证独立",
                      stock_pool_limitations="采用当前成员回看历史，非历史逐日池，有选样和存活偏差；历史演示标的也不是沪深300指数",
                      request=payload)
        recent = self.journal.list(owner)
        result["duplicate_check"] = {"matching_recent_research_ids": [r["id"] for r in recent
            if r["document"].get("description", {}).get("expression_sha256") == result["description"]["expression_sha256"]],
            "scope": "仅比较当前身份最近30条记录的规范表达式AST；未实现输出相关去重，不证明经济原创性"}
        result["risks"].append({"item": "多重尝试与测试集使用", "status": "发现风险信号" if previous else "证据不足",
                               "evidence": {"previous_test_views": previous}, "limitation": "平台外尝试未知，不能据记录数推定有效独立尝试数"})
        result["research_id"] = self.journal.append(result["kind"], result, owner)
        return result

    def factor_generate(self, payload, owner):
        if set(payload) != {"prompt", "confirmed"} or payload["confirmed"] is not True:
            raise ValueError("请确认调用已配置模型生成一个因子候选")
        result = generate_factor(payload["prompt"])
        result["research_id"] = self.journal.append("factor_candidate", {"kind": "factor_candidate", "question": payload["prompt"], **result}, owner)
        return result

    def freeze_factor(self, payload, owner):
        if set(payload) != {"research_id", "confirmed"} or payload["confirmed"] is not True:
            raise ValueError("请确认冻结已验证的候选，查看最终测试结果")
        document = self.journal.get(payload["research_id"], owner)
        if document.get("kind") != "factor_validation" or document.get("status") != "ok":
            raise ValueError("只有已完成训练/验证的因子记录可冻结测试")
        with self.lock:
            if document.get("sample_hash"):
                request = document["request"]
                hashes = {code: self.load_bars(code, request["start"], request["end"], self.data_source(request))[1]["member_sha256"]
                          for code in request["symbols"]}
                if document_hash(hashes) != document["sample_hash"]:
                    raise DataError("冻结验证使用的数据已变化，不能认领同一次独立测试")
            return self.factor(document["request"], owner, reveal_test=True)

    def factor_convert(self, payload, owner):
        if set(payload) - {"expression", "threshold", "direction", "allocation", "confirmed", "research_id"} or payload.get("confirmed") is not True:
            raise ValueError("请明确确认因子转换阈值、方向及持仓比例")
        if not {"expression", "threshold", "direction", "allocation"}.issubset(payload):
            raise ValueError("因子转换参数不完整")
        compiled, _, canonical = compile_factor(payload["expression"], payload["threshold"], payload["direction"], payload["allocation"])
        result = {"strategy": compiled.strategy, "warmup_records": compiled.warmup, "generation_id": None}
        if payload.get("research_id"):
            candidate = self.journal.get(payload["research_id"], owner)
            if candidate.get("kind") != "factor_candidate" or candidate.get("status") != "ok":
                raise ValueError("因子出处不属于可用AI候选")
            _, _, original = compile_factor(candidate["expression"])
            if original == canonical:
                identifier = uuid.uuid4().hex
                with self.lock:
                    self.generations[identifier] = {"status": "ok", "strategy": compiled.strategy, "evidence": candidate["evidence"]}
                    self.generation_owners[identifier] = owner
                    while len(self.generations) > 32:
                        expired, _ = self.generations.popitem(last=False)
                        self.generation_owners.pop(expired, None)
                result["generation_id"] = identifier
        return result

    def factor_explain(self, payload, owner):
        if set(payload) != {"research_id", "confirmed"} or payload["confirmed"] is not True:
            raise ValueError("请确认向AI发送表达式、训练/验证指标和风险摘要，不发送最终测试结果")
        document = self.journal.get(payload["research_id"], owner)
        if document.get("kind") != "factor_validation":
            raise ValueError("AI解读只接收训练/验证记录，不接收最终测试结果")
        facts = [{"id": "F-expression", "label": "表达式", "value": document["expression"]},
                 {"id": "F-classification", "label": "结构类别", "value": document["description"]["categories"]},
                 {"id": "F-train", "label": "训练评价", "value": document["train"]},
                 {"id": "F-validation", "label": "验证评价", "value": document["validation"]},
                 {"id": "F-risks", "label": "风险证据", "value": document["risks"]}]
        result = explain_research({"facts": facts, "scope": document["statistics_scope"]})
        result["research_id"] = self.journal.append("factor_explanation", {"kind": "factor_explanation", "facts": facts, **result}, owner)
        return result


def make_handler(workspace):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # 不将策略文本、文件路径或请求头写入访问日志。

        def respond(self, status, body, content_type="application/json; charset=utf-8", filename=None, cookie=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def json(self, value, status=200):
            self.respond(status, encode_json(value))

        def allowed_host(self):
            return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

        def current_user(self):
            return workspace.accounts.current(self.headers.get("Cookie", ""))

        def owner(self):
            user = self.current_user()
            return user["id"] if user else None

        def discard_body(self):
            """有界读取被拒绝的正文，避免未读数据导致Windows重置连接。"""
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return
            if not 0 < length <= 1048576:
                return
            previous_timeout = self.connection.gettimeout()
            try:
                self.connection.settimeout(2)
                self.rfile.read(length)
            except OSError:
                pass
            finally:
                self.connection.settimeout(previous_timeout)

        def do_GET(self):
            if not self.allowed_host():
                return self.json({"error": "仅允许本机访问"}, 403)
            static = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/experience.js": ("experience.js", "text/javascript; charset=utf-8"),
                      "/research.js": ("research.js", "text/javascript; charset=utf-8"),
                      "/styles.css": ("styles.css", "text/css; charset=utf-8"),
                      "/portfolio.js": ("portfolio.js", "text/javascript; charset=utf-8")}
            if self.path in static:
                filename, content_type = static[self.path]
                return self.respond(200, (workspace.root / "web" / filename).read_bytes(), content_type)
            if self.path == "/api/bootstrap":
                user = self.current_user()
                stale = workspace.accounts.cookie_token(self.headers.get("Cookie", "")) and user is None
                return self.respond(200, encode_json(workspace.bootstrap(user)), cookie=workspace.accounts.cookie_header() if stale else None)
            if self.path == "/api/auth/session":
                return self.json({"user": self.current_user()})
            exported = re.fullmatch(r"/api/file/([0-9a-f]{32})/(strategy.json|report.json|records.csv|equity.png|research.json)", self.path)
            if exported:
                with workspace.lock:
                    item = workspace.exports.get(exported[1])
                    if workspace.export_owners.get(exported[1]) != self.owner():
                        item = None
                if item is None or item[1] != exported[2]:
                    return self.json({"error": "导出链接已过期，本地文件仍保留"}, 404)
                return self.respond(200, item[0].read_bytes(), item[2], "finblocks-" + item[1])
            match = re.fullmatch(r"/api/export/([0-9a-f]{32})/(report.json|records.csv)", self.path)
            if match:
                with workspace.lock:
                    report = workspace.runs.get(match[1])
                    if workspace.run_owners.get(match[1]) != self.owner():
                        report = None
                if report is None:
                    return self.json({"error": "该结果已过期，请重新回测"}, 404)
                if match[2] == "report.json":
                    return self.respond(200, encode_json(report), filename="finblocks-report.json")
                return self.respond(200, records_csv(report), "text/csv; charset=utf-8", "finblocks-records.csv")
            self.json({"error": "页面或接口不存在"}, 404)

        def do_POST(self):
            origin = self.headers.get("Origin")
            if not self.allowed_host() or (origin and origin != "http://" + self.headers.get("Host", "")):
                self.discard_body()
                return self.json({"error": "只接受本机工作台请求"}, 403)
            token = self.headers.get("X-FinBlocks-Token", "")
            if not token.isascii() or not secrets.compare_digest(token, workspace.token):
                self.discard_body()
                return self.json({"error": "会话校验失败，请刷新页面"}, 403)
            user = self.current_user()
            owner = user["id"] if user else None
            if (workspace.accounts.cookie_token(self.headers.get("Cookie", "")) and user is None
                    and self.path not in ("/api/auth/register", "/api/auth/login", "/api/auth/logout")):
                self.discard_body()
                return self.json({"error": "登录会话已过期，请重新登录或刷新后使用游客模式"}, 401)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                limit = 1048576 if self.path == "/api/export-artifact" else 65536
                if not 0 < length <= limit:
                    self.discard_body()
                    raise ValueError("请求为空或超过项目大小上限")
                def invalid_constant(_value):
                    raise ValueError("请求包含非有限JSON常量")
                payload = json.loads(self.rfile.read(length), parse_constant=invalid_constant)
                if not isinstance(payload, dict):
                    raise ValueError("请求必须为JSON对象")
                if self.path in ("/api/auth/register", "/api/auth/login"):
                    if self.path.endswith("register"):
                        user, session = workspace.accounts.register(payload)
                    else:
                        user, session = workspace.accounts.login(payload, self.client_address[0])
                    # 登录切换时废弃当前会话，不延续旧身份。
                    workspace.accounts.logout(self.headers.get("Cookie", ""))
                    return self.respond(200, encode_json({"user": user}), cookie=workspace.accounts.cookie_header(session))
                if self.path == "/api/auth/logout":
                    if payload:
                        raise ValueError("退出请求不接受额外字段")
                    workspace.accounts.logout(self.headers.get("Cookie", ""))
                    return self.respond(200, encode_json({"user": None}), cookie=workspace.accounts.cookie_header())
                if self.path == "/api/validate":
                    return self.json(workspace.validate(payload.get("strategy")))
                if self.path == "/api/generate":
                    return self.json(workspace.generate(payload.get("prompt"), owner))
                if self.path == "/api/backtest":
                    return self.json(workspace.backtest(payload, owner))
                if self.path == "/api/portfolio/preflight":
                    return self.json(workspace.portfolio_preflight(payload))
                if self.path == "/api/portfolio/backtest":
                    return self.json(workspace.portfolio_backtest(payload, owner))
                if self.path == "/api/research/audit":
                    if set(payload) - {"prompt", "strategy", "contract"}:
                        raise ValueError("意图审计含未知字段")
                    result = audit_intent(payload.get("prompt", ""), payload.get("strategy"), payload.get("contract"))
                    result["research_id"] = workspace.journal.append("intent_audit", {"kind": "intent_audit", **result}, owner)
                    return self.json(result)
                if self.path == "/api/research/evidence":
                    return self.json(workspace.research_evidence(payload, owner))
                if self.path == "/api/research/repair":
                    if set(payload) != {"prompt", "strategy", "contract"}:
                        raise ValueError("修复预览须明确当前策略和意图契约")
                    return self.json(repair_strategy(payload["prompt"], payload["strategy"], payload["contract"]))
                if self.path == "/api/research/explain":
                    return self.json(workspace.explain(payload, owner))
                if self.path == "/api/research/experiment":
                    return self.json(workspace.experiment(payload, owner))
                if self.path == "/api/research/history":
                    if payload:
                        raise ValueError("历史查询不接受额外字段")
                    return self.json({"records": workspace.journal.list(owner)})
                if self.path == "/api/factor/inspect":
                    if set(payload) != {"expression"}:
                        raise ValueError("因子预检仅接受表达式")
                    compiled, _, canonical = compile_factor(payload["expression"])
                    return self.json(describe_factor(compiled, canonical))
                if self.path == "/api/factor/generate":
                    return self.json(workspace.factor_generate(payload, owner))
                if self.path == "/api/factor/evaluate":
                    return self.json(workspace.factor(payload, owner))
                if self.path == "/api/factor/test":
                    return self.json(workspace.freeze_factor(payload, owner))
                if self.path == "/api/factor/convert":
                    return self.json(workspace.factor_convert(payload, owner))
                if self.path == "/api/factor/explain":
                    return self.json(workspace.factor_explain(payload, owner))
                if self.path == "/api/financials":
                    return self.json(workspace.financials(payload))
                if self.path == "/api/export-artifact":
                    return self.json(workspace.export_artifact(payload, owner))
                return self.json({"error": "接口不存在"}, 404)
            except AuthError as exc:
                return self.json({"error": str(exc)}, exc.status)
            except (ValueError, KeyError, TypeError, OverflowError) as exc:
                message = str(exc)
                key = os.environ.get("DEEPSEEK_API_KEY", "")
                if key:
                    message = message.replace(key, "[已隐藏]")
                if self.path in ("/api/backtest", "/api/portfolio/backtest", "/api/factor/evaluate", "/api/factor/generate", "/api/factor/explain", "/api/research/explain", "/api/research/experiment"):
                    attempted = locals().get("payload")
                    safe_fields = {"strategy", "symbol", "symbols", "start", "end", "cost_bps", "lag", "portfolio", "protocol", "expression", "axis", "values", "run_id", "research_id"}
                    safe_request = {k: v for k, v in attempted.items() if k in safe_fields} if isinstance(attempted, dict) else None
                    try:
                        encode_json(safe_request)
                    except (ValueError, TypeError, OverflowError):
                        safe_request = {"recording_note": "非有限或无法序列化请求，仅保留拒绝原因"}
                    workspace.journal.append("failed_request", {"kind": "failed_request", "endpoint": self.path,
                        "reason": message, "request": safe_request}, owner)
                # 输入和编译错误可以说明原因，系统异常不回显账户路径。
                return self.json({"error": message or "请求未通过验证", "kind": type(exc).__name__}, 400)
            except OSError:
                return self.json({"error": "本地资料无法读取或写入，请检查源文件和输出目录"}, 500)
            except Exception:
                return self.json({"error": "执行出现异常，未生成成功结果"}, 500)

    return Handler


def serve(root, port=8765):
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("端口须为1–65535的整数")
    workspace = Workspace(root)
    with ThreadingHTTPServer(("127.0.0.1", port), make_handler(workspace)) as server:
        print(f"FINBLOCKS_WORKBENCH: http://127.0.0.1:{server.server_port}", flush=True)
        print("LOCAL_ONLY; " + ("source archive verified" if workspace.archive.is_file() else "data import pending") + "; no credentials exposed", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("工作台已停止", flush=True)
