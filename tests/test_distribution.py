"""验证干净共享目录能打开工作台，缺数据和篡改仍阻断真实回测。"""

import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest

from finblocks.data import DataError
from finblocks.web import Workspace


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        actual = Path(__file__).resolve().parents[1]
        for relative in ("docs/audit_evidence/data_statistics.json", "docs/audit_evidence/workspace_inventory.json",
                         "data/demo_manifest.json", "data/csi300_manifest.json", "data/fundamental_capabilities.json",
                         "examples/ma_strategy.json"):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(actual / relative, target)

    def test_no_data_can_bootstrap_but_cannot_backtest(self):
        workspace = Workspace(self.root)
        self.assertEqual(workspace.bootstrap()["stock_pool"]["summary"]["official_members"], 300)
        self.assertTrue(workspace.validate(workspace.default_strategy)["valid"])
        with self.assertRaisesRegex(DataError, "尚未导入行情数据"):
            workspace.backtest({"strategy": workspace.default_strategy, "symbol": "sh600455",
                                "start": "2025-01-01", "end": "2026-08-19", "cost_bps": 10, "lag": 1})

    def test_no_financial_archive_has_actionable_error(self):
        workspace = Workspace(self.root)
        with self.assertRaisesRegex(DataError, "尚未导入财务数据"):
            workspace.financials({"symbol": "sh600455"})

    def test_existing_wrong_archive_still_blocks_startup(self):
        stats = json.loads((self.root / "docs/audit_evidence/data_statistics.json").read_text(encoding="utf-8"))
        (self.root / stats["archives"][0]["path"]).write_bytes(b"isolated invalid archive fixture")
        with self.assertRaisesRegex(DataError, "原始行情包已变化"):
            Workspace(self.root)
