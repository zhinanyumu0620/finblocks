"""本机测试账号：不收集邮箱/手机号，使用标准库哈希与SQLite。"""

from collections import OrderedDict
from contextlib import closing, contextmanager
import hashlib
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
import uuid

COOKIE_NAME = "finblocks_session"
SESSION_SECONDS = 8 * 60 * 60  # 本机测试会话期限，非比赛参数。
PASSWORD_ITERATIONS = 600000


class AuthError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Accounts:
    def __init__(self, database):
        self.database = Path(database)
        self.lock = threading.RLock()
        self.sessions, self.failures = OrderedDict(), OrderedDict()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                salt BLOB NOT NULL, password_hash BLOB NOT NULL,
                iterations INTEGER NOT NULL, created_at REAL NOT NULL)""")

    @contextmanager
    def connection(self):
        # SQLite的事务上下文不负责关闭连接，显式关闭以释放Windows文件句柄。
        with closing(sqlite3.connect(self.database)) as connection:
            with connection:
                yield connection

    @staticmethod
    def credentials(payload):
        if not isinstance(payload, dict) or set(payload) != {"username", "password"}:
            raise AuthError("只需用户名和密码，不提交手机号、邮箱或其他字段")
        username, password = payload["username"], payload["password"]
        if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_]{3,32}", username):
            raise AuthError("用户名须为3–32位字母、数字或下划线")
        if not isinstance(password, str) or not 8 <= len(password) <= 128:
            raise AuthError("密码须为8–128个字符")
        return username.lower(), password

    @staticmethod
    def digest(password, salt, iterations=PASSWORD_ITERATIONS):
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)

    def register(self, payload):
        username, password = self.credentials(payload)
        salt, user_id = secrets.token_bytes(32), uuid.uuid4().hex
        password_hash = self.digest(password, salt)
        with self.lock:
            try:
                with self.connection() as connection:
                    connection.execute("INSERT INTO users VALUES (?, ?, ?, ?, ?, ?)",
                                       (user_id, username, salt, password_hash, PASSWORD_ITERATIONS, time.time()))
            except sqlite3.IntegrityError:
                raise AuthError("该用户名已注册，请登录或换一个用户名", 409) from None
        return self.new_session({"id": user_id, "username": username})

    def login(self, payload, client="local"):
        username, password = self.credentials(payload)
        # 按本机来源累计失败，避免通过更换用户名绕过节流；不记录输入密码。
        now = time.monotonic()
        with self.lock:
            attempts = [stamp for stamp in self.failures.get(client, []) if now - stamp < 60]
            if len(attempts) >= 5:
                raise AuthError("登录失败次数过多，请稍后重试", 429)
            with self.connection() as connection:
                row = connection.execute("SELECT id, username, salt, password_hash, iterations FROM users WHERE username = ?", (username,)).fetchone()
            salt, expected, iterations = (row[2], row[3], row[4]) if row else (b"\x00" * 32, b"\x00" * 32, PASSWORD_ITERATIONS)
            actual = self.digest(password, salt, iterations)
            if not row or not secrets.compare_digest(actual, expected):
                self.failures[client] = attempts + [now]
                self.failures.move_to_end(client)
                while len(self.failures) > 128:
                    self.failures.popitem(last=False)
                raise AuthError("用户名或密码错误", 401)
            self.failures.pop(client, None)
            return self.new_session({"id": row[0], "username": row[1]})

    def new_session(self, user):
        token = secrets.token_hex(32)
        with self.lock:
            self.sessions[hashlib.sha256(token.encode()).hexdigest()] = (user, time.monotonic() + SESSION_SECONDS)
            while len(self.sessions) > 128:
                self.sessions.popitem(last=False)
        return user, token

    @staticmethod
    def cookie_token(header):
        if not isinstance(header, str) or len(header) > 8192:
            return None
        try:
            cookie = SimpleCookie()
            cookie.load(header)
            token = cookie[COOKIE_NAME].value if COOKIE_NAME in cookie else None
        except CookieError:
            return None
        return token if token and re.fullmatch(r"[0-9a-f]{64}", token) else None

    def current(self, header):
        token = self.cookie_token(header)
        if token is None:
            return None
        key = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            item = self.sessions.get(key)
            if item is None:
                return None
            if time.monotonic() >= item[1]:
                del self.sessions[key]
                return None
            return dict(item[0])

    def logout(self, header):
        token = self.cookie_token(header)
        if token:
            with self.lock:
                self.sessions.pop(hashlib.sha256(token.encode()).hexdigest(), None)

    @staticmethod
    def cookie_header(token=None, secure=False):
        value = token or ""
        lifetime = SESSION_SECONDS if token else 0
        # 服务仅监听loopback HTTP；HttpOnly会话不写入页面脚本或localStorage。
        return f"{COOKIE_NAME}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={lifetime}" + ("; Secure" if secure else "")
