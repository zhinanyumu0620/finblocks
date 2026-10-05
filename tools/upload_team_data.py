"""由管理员向组内HTTPS服务续传原始ZIP；上传前核对服务公布的审计SHA。"""

import argparse
import getpass
import hashlib
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit


def upload(url, kind, path, admin):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("上传地址须为不含用户名、参数的HTTPS网址")
    base = url.rstrip("/")
    with urllib.request.urlopen(base + "/api/bootstrap", timeout=30) as response:
        token = json.loads(response.read())["token"]

    def request(route, body, content_type="application/json", extra=None):
        headers = {"Content-Type": content_type, "X-FinBlocks-Token": token, "X-FinBlocks-Admin": admin, **(extra or {})}
        request = urllib.request.Request(base + route, data=body, headers=headers)
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.loads(response.read())

    state = request("/api/admin/data-status", json.dumps({"kind": kind}).encode())
    path = Path(path)
    if path.stat().st_size != state["bytes"]:
        raise ValueError("本地文件大小不符合审计清单，未上传")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != state["sha256"]:
        raise ValueError("本地文件SHA不符合审计清单，未上传")
    if state["ready"]:
        print("DATA_UPLOAD: ALREADY_VERIFIED", flush=True)
        return
    offset = state["uploaded_bytes"]
    if not 0 <= offset <= state["bytes"]:
        raise ValueError("服务器续传偏移无效")
    with path.open("rb") as stream:
        stream.seek(offset)
        index = 0
        while chunk := stream.read(1024 * 1024):
            state = request("/api/admin/upload-part", chunk, "application/octet-stream",
                            {"X-FinBlocks-Data-Kind": kind, "X-FinBlocks-Data-Offset": str(offset)})
            offset += len(chunk)
            if state["uploaded_bytes"] != offset:
                raise ValueError("上传响应与实际偏移不一致")
            index += 1
            if index % 20 == 0 or state["ready"]:
                print(f"DATA_UPLOAD: {offset}/{state['bytes']} bytes", flush=True)
    if not state["ready"]:
        raise ValueError("尚未完成服务器SHA核验，请继续上传")
    print("DATA_UPLOAD: PASS; server SHA verified", flush=True)


def main():
    parser = argparse.ArgumentParser(description="上传已获团队使用授权的原始金融数据")
    parser.add_argument("--url", required=True)
    parser.add_argument("--kind", choices=("prices", "financials"), required=True)
    parser.add_argument("--file", type=Path, required=True)
    args = parser.parse_args()
    try:
        upload(args.url, args.kind, args.file, getpass.getpass("管理员上传码（不显示）："))
        return 0
    except (ValueError, OSError, urllib.error.URLError):
        print("DATA_UPLOAD: FAIL; 请核对文件、HTTPS地址、上传码和存储空间。中断时可再次执行续传。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
