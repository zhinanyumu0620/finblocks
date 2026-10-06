"""个人AI专测：真实本机HTTP与DPAPI；模型响应使用明确TEST_DOUBLE，不请求收费API。"""

from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from finblocks.ai import AIError, _request, generate_strategy, research_call, test_connection as check_connection
from finblocks.ai_settings import AIConnection, AIProfiles, ACTIVE_CONNECTION, PRESETS, PublicHTTPSConnection, normalize_url
from finblocks.auth import Accounts
from finblocks.web import Workspace, make_handler


ROOT = Path(__file__).resolve().parents[1]


def settings(provider="openai", key=None):
    preset = next(p for p in PRESETS if p["id"] == provider)
    return {"provider": provider, "base_url": preset["base_url"], "model": preset["model"],
            "api_key": key or secrets.token_urlsafe(32), "remember": False}


def model_response(document):
    return {"model": "TEST_DOUBLE", "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(document)}}]}


class AIProfileTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "profiles.sqlite3"
        self.profiles = AIProfiles(self.path)

    def tearDown(self):
        self.directory.cleanup()

    def test_account_isolation_plaintext_not_persisted_and_logout_expires(self):
        a, b = settings(), settings("qwen")
        public = self.profiles.save("a", a)
        self.profiles.save("b", b)
        self.assertNotIn(a["api_key"], json.dumps(public))
        self.assertNotIn(a["api_key"].encode(), self.path.read_bytes())
        self.assertEqual(self.profiles.connection("a").api_key, a["api_key"])
        self.assertEqual(self.profiles.connection("b").provider, "qwen")
        self.assertNotIn(a["api_key"], repr(self.profiles.connection("a")))
        self.profiles.forget_session("a")
        self.assertFalse(self.profiles.public("a")["configured"])
        self.assertTrue(self.profiles.public("b")["configured"])
        self.assertFalse(AIProfiles(self.path).public("b")["configured"])

    @unittest.skipUnless(os.name == "nt", "只有Windows支持实际DPAPI记住密钥")
    def test_real_dpapi_remember_restart_and_delete(self):
        payload = settings()
        payload["remember"] = True
        self.profiles.save("a", payload)
        self.assertNotIn(payload["api_key"].encode(), self.path.read_bytes())
        restarted = AIProfiles(self.path)
        self.assertEqual(restarted.connection("a").api_key, payload["api_key"])
        self.assertTrue(restarted.public("a")["remembered"])
        restarted.delete("a")
        self.assertIsNone(restarted.connection("a"))

    def test_empty_key_can_only_reuse_same_provider_and_address(self):
        payload = settings()
        self.profiles.save("a", payload)
        updated = {**payload, "api_key": "", "model": "another-model"}
        self.assertEqual(self.profiles.candidate("a", updated).api_key, payload["api_key"])
        for change in ({"base_url": "https://different.example.com/v1"}, {"provider": "custom"}):
            with self.assertRaises(ValueError):
                self.profiles.candidate("a", {**updated, **change})
        with self.assertRaises(ValueError):
            self.profiles.save(None, payload)

    def test_reject_private_urls_ports_credentials_and_non_ascii_keys(self):
        for url in ("http://api.example.com/v1", "https://127.0.0.1/v1", "https://169.254.169.254/v1",
                    "https://192.168.1.1/v1", "https://localhost/v1", "https://api.local/v1",
                    "https://api.example.com:8443/v1", "https://user:pass@api.example.com/v1", "https://api.example.com/v1?key=bad"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_url(url)
        self.assertEqual(normalize_url("https://api.openai.com/v1/chat/completions/"), "https://api.openai.com/v1")
        for key in ("Bearer wrong", "中文", "bad\nkey", "short"):
            with self.assertRaises(ValueError):
                self.profiles.save("a", settings(key=key))
        bad = settings()
        bad["model"] = bad["api_key"]
        with self.assertRaises(ValueError):
            self.profiles.save("a", bad)

    def test_dns_private_and_reserved_targets_rejected_before_socket(self):
        connection = PublicHTTPSConnection("api.example.com", timeout=1)
        for ip in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "203.0.113.1", "::1"):
            with patch("finblocks.ai_settings.socket.getaddrinfo", return_value=[(2, 1, 6, "", (ip, 443))]), patch("finblocks.ai_settings.socket.create_connection") as connect:
                with self.assertRaises(ValueError):
                    connection.connect()
                connect.assert_not_called()

    def test_concurrent_request_contexts_do_not_exchange_keys(self):
        self.profiles.save("a", settings())
        self.profiles.save("b", settings("qwen"))
        barrier = threading.Barrier(2)
        def read(owner):
            with self.profiles.use(owner):
                barrier.wait(timeout=3)
                return ACTIVE_CONNECTION.get().provider
        with ThreadPoolExecutor(2) as executor:
            a, b = executor.submit(read, "a"), executor.submit(read, "b")
            self.assertEqual((a.result(), b.result()), ("openai", "qwen"))
        self.assertIsNone(ACTIVE_CONNECTION.get())

    def test_presets_strategy_and_research_use_selected_model(self):
        strategy = json.loads((ROOT / "examples/ma_strategy.json").read_text(encoding="utf-8"))
        for provider in ("deepseek", "openai", "qwen", "kimi"):
            with self.subTest(provider=provider):
                self.profiles.save("a", settings(provider))
                with self.profiles.use("a"), patch("finblocks.ai._request", return_value=model_response({"status": "ok", "strategy": strategy})) as request:
                    generated = generate_strategy("收盘价MA5持续高于MA20")
                    self.assertEqual(request.call_count, 1)
                    self.assertEqual(request.call_args.args[1]["model"], self.profiles.connection("a").model)
                    payload = request.call_args.args[1]
                    self.assertEqual("thinking" in payload, provider in ("deepseek", "kimi"))
                    self.assertEqual("enable_thinking" in payload, provider == "qwen")
                    self.assertEqual(generated["evidence"]["provider"], self.profiles.connection("a").label)
                with self.profiles.use("a"), patch("finblocks.ai._request", return_value=model_response({"claims": []})):
                    parsed, evidence = research_call("返回JSON", {"facts": []})
                    self.assertEqual(parsed, {"claims": []})
                    self.assertNotIn(self.profiles.connection("a").api_key, json.dumps(evidence))

    def test_test_button_checks_json_and_resets_context(self):
        self.profiles.save("a", settings())
        with patch("finblocks.ai._request", return_value=model_response({"status": "ok"})):
            self.assertEqual(check_connection(self.profiles.connection("a"))["status"], "PASS")
        with patch("finblocks.ai._request", return_value=model_response({"status": "wrong"})), self.assertRaises(AIError):
            check_connection(self.profiles.connection("a"))
        self.assertIsNone(ACTIVE_CONNECTION.get())

    def test_transport_uses_personal_header_and_rejects_key_echo(self):
        payload = settings()
        self.profiles.save("a", payload)
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, _limit): return ('{"key":"' + ''.join('\\u%04x' % ord(c) for c in payload["api_key"]) + '"}').encode()
        with self.profiles.use("a"), patch("finblocks.ai.open_public_request", return_value=Response()) as opener:
            with self.assertRaises(AIError) as error:
                _request("/chat/completions", {})
            self.assertNotIn(payload["api_key"], str(error.exception))
            self.assertEqual(opener.call_args.args[0].get_header("Authorization"), "Bearer " + payload["api_key"])


class AISettingsHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = TemporaryDirectory()
        cls.workspace = Workspace(ROOT)
        cls.workspace.accounts = Accounts(Path(cls.directory.name)/"accounts.sqlite3")
        cls.workspace.ai_profiles = AIProfiles(Path(cls.directory.name)/"ai.sqlite3")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.workspace))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(); cls.directory.cleanup()

    def request(self, path, payload=None, cookie=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {"X-FinBlocks-Token": self.workspace.token}
        if cookie: headers["Cookie"] = cookie
        body = json.dumps(payload).encode() if payload is not None else None
        connection.request("POST" if body is not None else "GET", path, body, headers)
        response = connection.getresponse()
        result = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return result

    def register(self):
        status, body, headers = self.request("/api/auth/register", {"username": "ai_"+secrets.token_hex(5), "password": secrets.token_urlsafe(16)})
        self.assertEqual(status, 200)
        return json.loads(body)["user"]["id"], headers["Set-Cookie"].split(";", 1)[0]

    def test_login_configuration_isolated_and_logout_clears_key(self):
        self.assertEqual(self.request("/api/ai/config")[0], 401)
        self.assertEqual(self.request("/api/ai/config", settings())[0], 401)
        owner, cookie = self.register()
        _, other = self.register()
        payload = settings()
        status, body, _ = self.request("/api/ai/config", payload, cookie)
        self.assertEqual(status, 200)
        self.assertNotIn(payload["api_key"].encode(), body)
        own = json.loads(self.request("/api/bootstrap", cookie=cookie)[1])
        another = json.loads(self.request("/api/ai/config", cookie=other)[1])
        self.assertEqual(own["ai_profile"]["source"], "personal")
        self.assertEqual(another["profile"]["source"], "workspace")
        self.assertNotIn(payload["api_key"].encode(), json.dumps(own).encode())
        self.request("/api/auth/logout", {}, cookie)
        self.assertFalse(self.workspace.ai_profiles.public(owner)["configured"])

    def test_test_does_not_save_and_delete_restores_default(self):
        owner, cookie = self.register()
        payload = settings("qwen")
        with patch("finblocks.ai._request", return_value=model_response({"status": "ok"})):
            self.assertEqual(self.request("/api/ai/test", payload, cookie)[0], 200)
        self.assertIsNone(self.workspace.ai_profiles.connection(owner))
        self.assertEqual(self.request("/api/ai/config", payload, cookie)[0], 200)
        self.assertEqual(self.request("/api/ai/delete", {}, cookie)[0], 200)
        self.assertIsNone(self.workspace.ai_profiles.connection(owner))
