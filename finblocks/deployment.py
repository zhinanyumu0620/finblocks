"""组内网页版配置与数据上传；保留本机模式，不公开原始数据下载路由。"""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import threading
from urllib.parse import urlsplit

from .data import DataError, sha256_file


@dataclass(frozen=True)
class OnlinePolicy:
    origin: str
    invite: str
    admin: str

    def __post_init__(self):
        parsed = urlsplit(self.origin)
        if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment):
            raise ValueError("网页版必须配置不带路径的HTTPS地址")
        for key in (self.invite, self.admin):
            if not isinstance(key, str) or not key.isascii() or not 20 <= len(key) <= 256:
                raise ValueError("组员邀请码和管理员上传码须分别配置至少20位ASCII字符")
        if secrets.compare_digest(self.invite, self.admin):
            raise ValueError("组员邀请码不能与管理员上传码相同")

    @property
    def host(self):
        return urlsplit(self.origin).netloc

    def matches(self, header, key):
        return isinstance(header, str) and header.isascii() and secrets.compare_digest(header, key)


class DataUploads:
    chunk_limit = 1024 * 1024

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.specs = json.loads((self.root / "data/data_upload_spec.json").read_text(encoding="utf-8"))
        self.lock = threading.RLock()
        self.verified = {}
        for kind, spec in self.specs.items():
            if kind not in ("prices", "financials") or type(spec["bytes"]) is not int or spec["bytes"] <= 0:
                raise ValueError("上传资料清单无效")
            if not (self.root / spec["path"]).resolve().is_relative_to(self.root):
                raise ValueError("上传目标不能超出资料目录")

    def spec(self, kind):
        if kind not in self.specs:
            raise DataError("仅接受审计过的行情或财务原始ZIP")
        return self.specs[kind]

    def paths(self, kind):
        spec = self.spec(kind)
        target = self.root / spec["path"]
        part = self.root / "private/uploads" / (kind + ".part")
        return target, part

    def status(self, kind):
        with self.lock:
            spec = self.spec(kind)
            target, part = self.paths(kind)
            ready = False
            if target.is_file():
                stat = target.stat()
                signature = (stat.st_size, stat.st_mtime_ns)
                if self.verified.get(kind) != signature:
                    if stat.st_size != spec["bytes"] or sha256_file(target) != spec["sha256"]:
                        raise DataError("服务器原始数据与审计SHA不一致")
                    self.verified[kind] = signature
                ready = True
            return {"kind": kind, "ready": ready, "bytes": spec["bytes"], "sha256": spec["sha256"],
                    "uploaded_bytes": spec["bytes"] if ready else part.stat().st_size if part.is_file() else 0}

    def append(self, kind, offset, body):
        with self.lock:
            if type(offset) is not int or offset < 0 or not 0 < len(body) <= self.chunk_limit:
                raise DataError("上传偏移或分片大小无效")
            state = self.status(kind)
            if state["ready"] or offset != state["uploaded_bytes"]:
                raise DataError("上传进度已变化，请读取状态后续传；不覆盖已核验数据")
            if offset + len(body) > state["bytes"]:
                raise DataError("上传文件超出审计记录大小")
            target, part = self.paths(kind)
            part.parent.mkdir(parents=True, exist_ok=True)
            with part.open("ab") as stream:
                stream.write(body)
            if part.stat().st_size == state["bytes"]:
                if sha256_file(part) != state["sha256"]:
                    part.unlink()
                    raise DataError("上传文件SHA与原审计不一致，已丢弃该错误副本")
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(part, target)
            return self.status(kind)
