"""验证组内域名、邀请码、HTTPS会话与数据上传边界；小字节串仅为隔离测试。"""

import hashlib
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import shutil
from tempfile import TemporaryDirectory
import threading
import unittest

from finblocks.deployment import OnlinePolicy, DataUploads
from finblocks.data import DataError
from finblocks.web import Workspace, make_handler


class OnlineTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        original = Path(__file__).resolve().parents[1]
        for relative in ("docs/audit_evidence/data_statistics.json", "docs/audit_evidence/workspace_inventory.json",
                         "data/demo_manifest.json", "data/csi300_manifest.json", "data/fundamental_capabilities.json",
                         "data/data_upload_spec.json", "examples/ma_strategy.json"):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original / relative, target)
        shutil.copytree(original / "web", self.root / "web")
        self.policy = OnlinePolicy("https://team.example.invalid", secrets.token_hex(20), secrets.token_hex(20))
        self.workspace = Workspace(self.root, self.policy)
        self.workspace.uploads = DataUploads(self.root)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.workspace))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, route, payload=None, extra=None, raw=None):
        headers = {"Host": self.policy.host, "Origin": self.policy.origin,
                   "X-FinBlocks-Token": self.workspace.token, "Content-Type": "application/json", **(extra or {})}
        body = raw if raw is not None else json.dumps(payload).encode() if payload is not None else None
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        connection.request("POST" if body is not None else "GET", route, body, headers)
        response = connection.getresponse()
        result = response.status, json.loads(response.read()), dict(response.getheaders())
        connection.close()
        return result

    def register(self):
        status, result, headers = self.request("/api/auth/register", {"username": "team_" + secrets.token_hex(4),
                                                   "password": secrets.token_urlsafe(20)}, {"X-FinBlocks-Invite": self.policy.invite})
        self.assertEqual(status, 200)
        self.assertIn("; Secure", headers["Set-Cookie"])
        return {"Cookie": headers["Set-Cookie"].split(";", 1)[0]}

    def test_reject_invalid_origin_and_secrets(self):
        for origin in ("http://team.example.invalid", "https://team.example.invalid/path", "https://user@team.example.invalid"):
            with self.assertRaises(ValueError):
                OnlinePolicy(origin, self.policy.invite, self.policy.admin)
        with self.assertRaises(ValueError):
            OnlinePolicy(self.policy.origin, self.policy.invite, self.policy.invite)

    def test_domain_origin_and_guest_financial_access(self):
        self.assertEqual(self.request("/api/bootstrap")[0], 200)
        self.assertTrue(self.request("/api/bootstrap")[1]["online"])
        self.assertEqual(self.request("/api/bootstrap", extra={"Host": "unexpected.example.invalid"})[0], 403)
        self.assertEqual(self.request("/api/validate", {"strategy": self.workspace.default_strategy},
                                      {"Origin": "https://unexpected.example.invalid"})[0], 403)
        self.assertEqual(self.request("/api/financials", {"symbol": "sh600455"})[0], 401)
        self.assertEqual(self.request("/api/generate", {"prompt": "隔离测试，不调用模型"})[0], 401)
        self.assertEqual(self.request("/api/research/history", {})[0], 401)

    def test_invite_register_and_authorized_request(self):
        self.assertEqual(self.request("/api/auth/register", {"username": "isolated_team", "password": secrets.token_urlsafe(20)})[0], 403)
        cookie = self.register()
        status, body, _ = self.request("/api/financials", {"symbol": "sh600455"}, cookie)
        self.assertEqual(status, 400)
        self.assertIn("尚未导入财务数据", body["error"])
        self.assertEqual(self.request("/data/data_upload_spec.json", extra=cookie)[0], 404)

    def test_admin_role_not_given_to_group_members(self):
        cookie = self.register()
        self.assertEqual(self.request("/api/admin/data-status", {"kind": "prices"}, cookie)[0], 403)
        self.assertEqual(self.request("/api/admin/data-status", {"kind": "prices"}, {"X-FinBlocks-Admin": self.policy.invite})[0], 403)
        self.assertEqual(self.request("/api/admin/data-status", {"kind": "prices"}, {"X-FinBlocks-Admin": self.policy.admin})[0], 200)

    def test_chunks_offset_sha_and_no_replacement(self):
        data = b"isolated upload fixture; not financial market data"
        store = self.workspace.uploads
        store.specs["financials"] = {"path": "fixture/financials.zip", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        self.assertEqual(store.append("financials", 0, data[:10])["uploaded_bytes"], 10)
        with self.assertRaises(DataError):
            store.append("financials", 0, data[10:])
        with self.assertRaises(DataError):
            store.append("../prices", 0, data)
        self.assertTrue(store.append("financials", 10, data[10:])["ready"])
        self.assertEqual((self.root / "fixture/financials.zip").read_bytes(), data)
        with self.assertRaises(DataError):
            store.append("financials", 0, data)

    def test_bad_digest_does_not_publish_fixture(self):
        store = self.workspace.uploads
        store.specs["financials"] = {"path": "fixture/financials.zip", "bytes": 4, "sha256": hashlib.sha256(b"GOOD").hexdigest()}
        with self.assertRaises(DataError):
            store.append("financials", 0, b"FAIL")
        self.assertFalse((self.root / "fixture/financials.zip").exists())
        self.assertEqual(store.status("financials")["uploaded_bytes"], 0)

    def test_http_binary_chunk_has_admin_auth_and_digest(self):
        data = b"isolated bytes, not a financial dataset"
        self.workspace.uploads.specs["financials"] = {"path": "fixture/financials.zip", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        headers = {"X-FinBlocks-Admin": self.policy.admin, "Content-Type": "application/octet-stream",
                   "X-FinBlocks-Data-Kind": "financials", "X-FinBlocks-Data-Offset": "0"}
        status, body, _ = self.request("/api/admin/upload-part", raw=data, extra=headers)
        self.assertEqual(status, 200)
        self.assertTrue(body["ready"])
