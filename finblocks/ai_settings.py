"""个人AI连接：账号隔离、可选Windows凭据加密、公开HTTPS接口。"""

from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import ctypes
import http.client
import ipaddress
import os
from pathlib import Path
import re
import socket
import sqlite3
import threading
import urllib.parse
import urllib.request


PRESETS = (
    {"id": "deepseek", "label": "DeepSeek", "base_url": "https://api.deepseek.com", "model": "deepseek-flash", "docs_url": "https://api-docs.deepseek.com/guides/codex"},
    {"id": "openai", "label": "OpenAI", "base_url": "https://api.openai.com/v1", "model": "gpt-4.1-mini", "docs_url": "https://developers.openai.com/api/docs/models/gpt-4.1-mini"},
    {"id": "qwen", "label": "通义千问 · 百炼", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus", "docs_url": "https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope"},
    {"id": "kimi", "label": "Kimi · 月之暗面", "base_url": "https://api.moonshot.cn/v1", "model": "kimi-k2.6", "docs_url": "https://platform.kimi.com/docs/api/chat"},
    {"id": "custom", "label": "自定义 OpenAI 兼容接口", "base_url": "", "model": "", "docs_url": ""},
)
ACTIVE_CONNECTION = ContextVar("finblocks_ai_connection", default=None)


@dataclass(frozen=True)
class AIConnection:
    provider: str
    base_url: str
    model: str
    api_key: str = field(repr=False)

    @property
    def label(self):
        return next(p["label"] for p in PRESETS if p["id"] == self.provider)


def normalize_url(value):
    if not isinstance(value, str) or len(value) > 512 or re.search(r"[\s\x00-\x1f\\]", value):
        raise ValueError("API地址格式无效")
    parsed = urllib.parse.urlsplit(value)
    try:
        valid_port = parsed.port in (None, 443)
    except ValueError:
        valid_port = False
    if (parsed.scheme != "https" or not parsed.hostname or not valid_port or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("请填写公开HTTPS API根地址，不含账号、查询参数或自定义端口")
    host = parsed.hostname.lower()
    if "." not in host or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("不能使用本机或内网API地址")
    try:
        if not ipaddress.ip_address(host).is_global:
            raise ValueError("不能使用本机或内网API地址")
    except ValueError as exc:
        if str(exc) == "不能使用本机或内网API地址":
            raise
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        path = path[:-len("/chat/completions")]
    return "https://" + host + path


def parse_settings(payload):
    if (not isinstance(payload, dict) or set(payload) != {"provider", "base_url", "model", "api_key", "remember"}
            or type(payload["remember"]) is not bool):
        raise ValueError("AI配置须包含服务商、地址、模型、API Key和记住选项")
    if payload["provider"] not in {p["id"] for p in PRESETS}:
        raise ValueError("AI服务商不受支持")
    model, key = payload["model"], payload["api_key"]
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,119}", model):
        raise ValueError("请填写服务商实际支持的模型ID")
    if not isinstance(key, str) or len(key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ValueError("API Key格式无效；请勿粘贴Bearer前缀或空白")
    if key and (len(key) < 8 or key in model or key in payload["base_url"]):
        raise ValueError("请填写有效API Key，且不要将密钥放入模型ID或地址")
    return AIConnection(payload["provider"], normalize_url(payload["base_url"]), model, key)


def protect_key(raw, decrypt=False):
    """只用Windows DPAPI，其他平台不以明文替代。"""
    if os.name != "nt":
        raise ValueError("当前系统不能加密记住API Key，请使用本次登录配置")
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    data = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    source, target = Blob(len(raw), data), Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p if decrypt else ctypes.c_wchar_p,
                         ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ValueError("系统无法读取或加密该API Key，请重新填写")
    free = ctypes.WinDLL("kernel32", use_last_error=True).LocalFree
    free.argtypes, free.restype = [ctypes.c_void_p], ctypes.c_void_p
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        free(ctypes.cast(target.data, ctypes.c_void_p))


class AIProfiles:
    def __init__(self, database):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.lock, self.keys = threading.RLock(), {}
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS ai_profiles (
                owner TEXT PRIMARY KEY, provider TEXT NOT NULL, base_url TEXT NOT NULL,
                model TEXT NOT NULL, sealed_key BLOB)""")

    def row(self, owner):
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute("SELECT provider, base_url, model, sealed_key FROM ai_profiles WHERE owner=?", (owner,)).fetchone()

    def connection(self, owner):
        if owner is None:
            return None
        with self.lock:
            row = self.row(owner)
            if row is None:
                return None
            key = self.keys.get(owner)
            if not key and row[3]:
                key = protect_key(row[3], decrypt=True).decode("ascii")
            return AIConnection(*row[:3], key or "")

    def public(self, owner):
        try:
            connection = self.connection(owner)
        except ValueError:
            connection = None
        row = self.row(owner) if owner else None
        if row:
            return {"source": "personal", "provider": row[0], "base_url": row[1], "model": row[2],
                    "configured": bool(connection and connection.api_key), "remembered": bool(row[3])}
        return {"source": "workspace", "provider": "deepseek", "base_url": "https://api.deepseek.com", "model": "deepseek-flash",
                "configured": bool(os.environ.get("DEEPSEEK_API_KEY", "").strip()), "remembered": False}

    def candidate(self, owner, payload):
        if owner is None:
            raise ValueError("请先登录后配置个人AI")
        candidate = parse_settings(payload)
        if candidate.api_key:
            return candidate
        current = self.connection(owner)
        if (current and current.api_key and current.provider == candidate.provider
                and current.base_url == candidate.base_url):
            return AIConnection(candidate.provider, candidate.base_url, candidate.model, current.api_key)
        raise ValueError("请填写API Key；更换服务商或地址时不能沿用原密钥")

    def save(self, owner, payload):
        with self.lock:
            connection = self.candidate(owner, payload)
            sealed = protect_key(connection.api_key.encode("ascii")) if payload["remember"] else None
            with closing(sqlite3.connect(self.database)) as database, database:
                database.execute("INSERT OR REPLACE INTO ai_profiles VALUES (?, ?, ?, ?, ?)",
                                 (owner, connection.provider, connection.base_url, connection.model, sealed))
            self.keys[owner] = connection.api_key
            return self.public(owner)

    def forget_session(self, owner):
        with self.lock:
            self.keys.pop(owner, None)

    def delete(self, owner):
        with self.lock:
            self.forget_session(owner)
            with closing(sqlite3.connect(self.database)) as connection, connection:
                connection.execute("DELETE FROM ai_profiles WHERE owner=?", (owner,))
        return self.public(owner)

    @contextmanager
    def use(self, owner):
        token = ACTIVE_CONNECTION.set(self.connection(owner))
        try:
            yield
        finally:
            ACTIVE_CONNECTION.reset(token)


class PublicHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        # 将TLS连接固定到已经检查的公开IP，避免二次DNS解析与凭据重定向。
        addresses = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError("API域名解析到内网或保留地址，连接已拒绝")
        self.sock = self._context.wrap_socket(socket.create_connection(addresses[0][4][:2], self.timeout), server_hostname=self.host)


class PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(PublicHTTPSConnection, request, context=self._context)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def open_public_request(request, timeout):
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), PublicHTTPSHandler(), NoRedirect()).open(request, timeout=timeout)
