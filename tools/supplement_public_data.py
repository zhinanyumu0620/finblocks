"""有界下载公开行情并建立可复现快照；失败标的保留阻断原因。"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, date
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from finblocks.data import load_daily, sha256_file
from finblocks.public_data import SOURCE_ID, LIMITATIONS, fetch_snapshot, normalize_snapshot


def encoded(document):
    return json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")


def main():
    global ROOT
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT, help="线上持久化资料目录；默认本工作区")
    parser.add_argument("--end", default="2026-09-30")
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    ROOT = args.root.resolve()
    if date.fromisoformat(args.end) > date.today() or not 1 <= args.limit <= 300:
        parser.error("不请求未来日期，数量须为1–300")
    stats = json.loads((ROOT / "docs/audit_evidence/data_statistics.json").read_text(encoding="utf-8"))
    archive = ROOT / stats["archives"][0]["path"]
    pool = json.loads((ROOT / "data/csi300_manifest.json").read_text(encoding="utf-8"))
    if sha256_file(archive) != pool["archive_sha256"]:
        raise ValueError("原始行情SHA变化，停止公开补充")
    destination = ROOT / "data/public_sources/manifest.json"
    old = json.loads(destination.read_text(encoding="utf-8")) if destination.exists() else {"members": {}}
    manifest = {"version": 1, "source_id": SOURCE_ID, "created_at": datetime.now(timezone.utc).isoformat(),
                "requested_end": args.end, "original_archive_sha256": pool["archive_sha256"],
                "membership_as_of": pool["as_of"], "limitations": LIMITATIONS, "members": old["members"]}
    members = sorted(pool["members"], key=lambda x: x["code"])[:args.limit]

    def fetch(member):
        code = member["code"]
        if not args.refresh and manifest["members"].get(code, {}).get("requested_end") == args.end:
            return code, manifest["members"][code]
        entry = {"status": "BLOCKED", "name": member["name"], "requested_end": args.end, "responses": []}
        try:
            plain, plain_evidence = fetch_snapshot(ROOT, code, "", args.end)
            entry["responses"].append(plain_evidence)
            time.sleep(0.35)
            adjusted, adjusted_evidence = fetch_snapshot(ROOT, code, "hfq", args.end)
            entry["responses"].append(adjusted_evidence)
            local, original = load_daily(archive, code, plain[0]["date"], args.end)
            bars, metadata = normalize_snapshot(code, member["name"], plain, adjusted,
                                                 adjusted_evidence["returned_key"], local, args.end)
            metadata["original_member_sha256"] = original["member_sha256"]
            document = {"code": code, "name": member["name"], "metadata": metadata,
                        "records": [{"date": b.date, "values": b.values} for b in bars]}
            raw = encoded(document)
            digest = hashlib.sha256(raw).hexdigest()
            path = ROOT / "data/public_sources/normalized" / (code + "_" + digest + ".json")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            entry.update(status="READY", rows=len(bars), start=bars[0].date, end=bars[-1].date,
                         path=path.relative_to(ROOT).as_posix(), sha256=digest, metadata=metadata)
        except Exception as exc:
            entry["reason"] = str(exc)
        return code, entry

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(fetch, member) for member in members]
        for index, future in enumerate(as_completed(futures), 1):
            code, entry = future.result()
            manifest["members"][code] = entry
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(".tmp")
            temporary.write_bytes(encoded(manifest))
            temporary.replace(destination)
            if index % 20 == 0 or entry["status"] != "READY":
                print(f"{index}/{len(members)} {code} {entry['status']} {entry.get('reason', '')}", flush=True)
    ready = [x for x in manifest["members"].values() if x["status"] == "READY"]
    manifest["summary"] = {"requested": len(manifest["members"]), "ready": len(ready),
                           "blocked": len(manifest["members"]) - len(ready),
                           "records": sum(x["rows"] for x in ready),
                           "ohlc_conflict_cells": sum(len(x["metadata"]["local_comparison"]["ohlc_conflicts"]) for x in ready)}
    destination.write_bytes(encoded(manifest))
    print(json.dumps(manifest["summary"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
