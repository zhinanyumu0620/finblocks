"""实际验证密码哈希、持久化、会话与登录节流，不使用真实账号。"""

import hashlib
from contextlib import closing
import secrets
import sqlite3
from tempfile import TemporaryDirectory
from pathlib import Path
import unittest
from unittest.mock import patch

from finblocks.auth import Accounts, AuthError, COOKIE_NAME, SESSION_SECONDS


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "accounts.sqlite3"
        self.accounts = Accounts(self.path)
        self.password = secrets.token_urlsafe(18)

    def tearDown(self):
        self.directory.cleanup()

    def register(self, name="research_a"):
        return self.accounts.register({"username": name, "password": self.password})

    def test_registration_hashes_password_and_persists_login(self):
        first, first_token = self.register()
        second, _ = self.register("research_b")
        with closing(sqlite3.connect(self.path)) as connection:
            rows = connection.execute("SELECT username, salt, password_hash, iterations FROM users ORDER BY username").fetchall()
        self.assertNotEqual(rows[0][1], rows[1][1])
        self.assertNotEqual(rows[0][2], rows[1][2])
        self.assertEqual(rows[0][2], hashlib.pbkdf2_hmac("sha256", self.password.encode(), rows[0][1], rows[0][3]))
        self.assertNotIn(self.password.encode(), self.path.read_bytes())
        restarted = Accounts(self.path)
        self.assertIsNone(restarted.current(f"{COOKIE_NAME}={first_token}"))
        logged_in, _ = restarted.login({"username": "RESEARCH_A", "password": self.password})
        self.assertEqual(logged_in, first)
        self.assertNotEqual(first["id"], second["id"])

    def test_extra_identity_fields_invalid_names_and_duplicate_alias_rejected(self):
        self.register()
        with self.assertRaises(AuthError) as result:
            self.register("RESEARCH_A")
        self.assertEqual(result.exception.status, 409)
        for payload in ({"username": "' OR 1=1", "password": self.password},
                        {"username": "ok_name", "password": self.password, "email": "unused"},
                        {"username": "ok_name", "password": "short"},
                        {"username": [], "password": self.password}):
            with self.assertRaises(AuthError):
                self.accounts.register(payload)

    def test_session_expiry_logout_and_invalid_cookie(self):
        user, token = self.register()
        cookie = self.accounts.cookie_header(token)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertEqual(self.accounts.current(cookie), user)
        self.assertIsNone(self.accounts.current("finblocks_session=malformed"))
        self.accounts.logout(cookie)
        self.assertIsNone(self.accounts.current(cookie))
        _, token = self.accounts.login({"username": user["username"], "password": self.password})
        with patch("finblocks.auth.time.monotonic", return_value=10**15):
            self.assertIsNone(self.accounts.current(f"{COOKIE_NAME}={token}"))

    def test_unknown_and_wrong_password_same_error_and_source_throttle(self):
        self.register()
        for index in range(5):
            with self.assertRaises(AuthError) as result:
                self.accounts.login({"username": "research_a" if index % 2 else "unknown_user", "password": secrets.token_urlsafe(18)})
            self.assertEqual(result.exception.status, 401)
            self.assertEqual(str(result.exception), "用户名或密码错误")
        with self.assertRaises(AuthError) as result:
            self.accounts.login({"username": "research_a", "password": self.password})
        self.assertEqual(result.exception.status, 429)
