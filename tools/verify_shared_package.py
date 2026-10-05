"""校验组员共享包文件哈希，不读取凭据、不联网。"""

import hashlib
import json
from pathlib import Path
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "PACKAGE_MANIFEST.json").read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        path = (root / entry["path"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            print("PACKAGE_CHECK: FAIL; file missing or outside package")
            return 2
        raw = path.read_bytes()
        if len(raw) != entry["bytes"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            print("PACKAGE_CHECK: FAIL; " + entry["path"])
            return 2
    print(f"PACKAGE_CHECK: PASS; files={len(manifest['files'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
