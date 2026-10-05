"""命令行入口，所有展示结果均由真实数据和实际调用产生。"""

import argparse
import csv
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import platform
import sys

from .ai import AIError, generate_strategy, list_models
from .backtest import run_backtest
from .data import DataError, load_daily, select_demo, sha256_file
from .dsl import CompileError, compile_strategy


def write_json(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def environment_report():
    packages = {}
    for name in ("numpy", "pandas", "pydantic", "fastapi", "flask", "pytest", "torch", "transformers"):
        available = importlib.util.find_spec(name) is not None
        try:
            version = importlib.metadata.version(name) if available else None
        except importlib.metadata.PackageNotFoundError:
            version = "unknown"
        packages[name] = {"available": available, "version": version}
    return {"python": platform.python_version(), "platform": platform.platform(),
            "packages": packages, "application_dependencies": "仅Python标准库，无需pip安装",
            "deepseek_key_configured": bool(os.environ.get("DEEPSEEK_API_KEY", "").strip()),
            "credentials_saved": False}


def main():
    parser = argparse.ArgumentParser(description="FinBlocks 策略研究内核与本机可视化工作台")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    sub = parser.add_subparsers(dest="command", required=True)
    env = sub.add_parser("environment", help="核查环境，不显示密钥")
    env.add_argument("--check-ai", action="store_true")
    env.add_argument("--output", type=Path)
    workbench = sub.add_parser("serve", help="启动本机可视化工作台")
    workbench.add_argument("--port", type=int, default=8765)
    select = sub.add_parser("select-demo", help="按审计和实际质量筛选演示数据")
    select.add_argument("--limit", type=int, default=3)
    select.add_argument("--min-bars", type=int, default=120)
    select.add_argument("--output", type=Path)
    generate = sub.add_parser("generate", help="实际AI生成策略，不上传金融数据")
    generate.add_argument("--prompt-file", type=Path, required=True)
    generate.add_argument("--model", default="deepseek-flash")
    generate.add_argument("--output", type=Path, required=True)
    backtest = sub.add_parser("backtest", help="真实日频研究回测，成本必须显式指定")
    backtest.add_argument("--strategy", type=Path, required=True)
    backtest.add_argument("--symbol", required=True)
    backtest.add_argument("--start")
    backtest.add_argument("--end")
    backtest.add_argument("--cost-bps", type=float, required=True)
    backtest.add_argument("--lag", type=int, default=1)
    backtest.add_argument("--gap-tolerance", type=float, default=0.0)
    backtest.add_argument("--periods-per-year", type=int, default=252, help="年化每年区间数，默认252为研究假设")
    backtest.add_argument("--annual-risk-free-rate", type=float, default=0.0, help="年化无风险利率小数，默认0为研究假设")
    backtest.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "serve":
            from .web import serve
            serve(args.root, args.port)
        elif args.command == "environment":
            report = environment_report()
            if args.check_ai:
                report["actual_api_models"] = list_models()
            if args.output:
                write_json(args.output, report)
            print(json.dumps(report, ensure_ascii=False, indent=2))
        elif args.command == "select-demo":
            manifest = select_demo(args.root, args.limit, args.min_bars)
            output = args.output or args.root / "data" / "demo_manifest.json"
            write_json(output, manifest)
            print(f"SELECT_DEMO: PASS; audited_candidates={manifest['audited_candidates']}; selected={len(manifest['selected'])}")
            for item in manifest["selected"]:
                print(item["code"], item["name"], item["source"]["rows"], item["source"]["start"], item["source"]["end"])
        elif args.command == "generate":
            result = generate_strategy(args.prompt_file.read_text(encoding="utf-8"), args.model)
            write_json(args.output, result)
            if result["status"] == "unsupported":
                print("AI_GENERATION: UNSUPPORTED; " + str(result["reason"]))
                return 2
            print("AI_GENERATION: PASS; model=" + str(result["evidence"]["model_returned"]))
            print("COMPILE: PASS; nodes=" + str(len(result["strategy"]["nodes"])) + "; warmup=" + str(result["warmup_records"]))
            print("AI_USAGE: " + json.dumps(result["evidence"]["usage"], ensure_ascii=False))
        else:
            document = json.loads(args.strategy.read_text(encoding="utf-8"))
            if isinstance(document, dict) and "status" in document:
                if document.get("status") != "ok" or not document.get("evidence", {}).get("generated_by_real_api"):
                    raise CompileError("策略文件不是成功且有执行证据的AI结果")
                strategy = document["strategy"]
                if document["evidence"].get("response", {}).get("strategy") != strategy:
                    raise CompileError("策略与保存的实际模型响应不一致")
                origin = {"type": "saved_actual_api_response", "model": document["evidence"].get("model_returned"),
                          "response_id": document["evidence"].get("response_id"), "evidence_file": args.strategy.name}
            else:
                strategy, origin = document, {"type": "manual_strategy", "evidence_file": args.strategy.name}
            compile_strategy(strategy)
            stats = json.loads((args.root / "docs/audit_evidence/data_statistics.json").read_text(encoding="utf-8"))
            archive = args.root / stats["archives"][0]["path"]
            inventory = json.loads((args.root / "docs/audit_evidence/workspace_inventory.json").read_text(encoding="utf-8"))
            actual_hash = sha256_file(archive)
            expected_hash = next(item["sha256"] for item in inventory if item["path"] == stats["archives"][0]["path"])
            if actual_hash != expected_hash:
                raise DataError("行情源文件已经变化，须重新审计")
            bars, source = load_daily(archive, args.symbol, args.start, args.end)
            source["archive_sha256"] = actual_hash
            result = run_backtest(bars, strategy, cost_bps=args.cost_bps, execution_lag_bars=args.lag, gap_tolerance=args.gap_tolerance,
                                  periods_per_year=args.periods_per_year, annual_risk_free_rate=args.annual_risk_free_rate)
            result["source"], result["strategy_origin"] = source, origin
            result["strategy_file_sha256"] = sha256_file(args.strategy)
            write_json(args.output, result)
            with args.output.with_suffix(".csv").open("w", newline="", encoding="utf-8-sig") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(result["records"][0]))
                writer.writeheader()
                writer.writerows(result["records"])
            print("BACKTEST: PASS; " + json.dumps(result["metrics"], ensure_ascii=False))
            print("EXECUTION: " + result["assumptions"]["execution"])
    except (AIError, DataError, CompileError, ValueError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return 2
    except OSError:
        print("ERROR: 本地文件无法读取或写入，请检查输入文件与输出目录；不显示账户绝对路径", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
