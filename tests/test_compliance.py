"""匿名检查使用隔离测试身份；不读取或伪造参赛者真实身份。"""

import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from finblocks.compliance import IDENTITY_GROUPS, scan_workspace, scoped_path


class AnonymityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        actual = Path(__file__).resolve().parents[1] / "docs/anonymity_check_rules.json"
        self.write("docs/anonymity_check_rules.json", actual.read_text(encoding="utf-8"))
        self.write("README.md", "仅用于隔离测试的中性产品说明。")

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def dictionary(self, status="COMPLETE", empty=False):
        raw = {group: [] for group in IDENTITY_GROUPS}
        if not empty:
            raw["school_names_and_aliases"] = ["匿名测试Ａ学院"]
            raw["participant_names_and_aliases"] = ["测试甲乙"]
        raw["status"] = status
        raw["confirmed_absent"] = [group for group in IDENTITY_GROUPS if not raw[group]]
        self.write("private/identity_dictionary.json", json.dumps(raw, ensure_ascii=False))

    def manifest(self, files, full_coverage=False, **kwargs):
        manifest = {"files": files, "coverage": {"readme": ["README.md"]}, **kwargs}
        if full_coverage:
            manifest["coverage"] = {category: files for category in ("readme", "interface", "ppt_materials", "screenshots", "demo", "exports")}
        self.write("manifest.json", json.dumps(manifest, ensure_ascii=False))
        return scan_workspace(self.root, manifest_path="manifest.json", environ={})

    def test_subtitles_and_source_text_are_scanned(self):
        self.dictionary()
        files = ["README.md"]
        for suffix in ("srt", "vtt", "py", "ps1", "cjs", "mjs"):
            relative = f"public/sample.{suffix}"
            self.write(relative, "仅用于隔离扫描测试：测试甲乙")
            files.append(relative)
        report = self.manifest(files)
        self.assertEqual(report["status"], "BLOCKED")
        self.assertTrue(all(item["status"] == "READ" for item in report["files"]))
        self.assertEqual(report["counts"]["block_hits"], 6)

    def test_missing_and_empty_dictionary_never_pass(self):
        report = scan_workspace(self.root, environ={})
        self.assertEqual(report["status"], "NOT_READY")
        self.assertEqual(report["identity_dictionary"]["status"], "MISSING")
        self.dictionary(empty=True)
        report = self.manifest(["README.md"], full_coverage=True)
        self.assertEqual(report["status"], "NOT_READY")
        self.assertIn("IDENTITY_DICTIONARY_NOT_COMPLETE", report["readiness_reasons"])

    def test_readme_cannot_impersonate_every_submission_category(self):
        self.dictionary()
        report = self.manifest(["README.md"], full_coverage=True)
        self.assertEqual(report["status"], "NOT_READY")
        self.assertEqual(report["missing_coverage"], ["interface", "ppt_materials", "screenshots", "demo", "exports"])

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "已有Pillow不可用")
    def test_manual_visual_clearance_is_bound_to_content_hash(self):
        from PIL import Image
        import hashlib
        self.dictionary()
        path = self.root / "sample.png"
        Image.new("RGB", (8, 8), "white").save(path)
        review = {"path": "sample.png", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "status": "APPROVED", "human_review": True}
        report = self.manifest(["README.md", "sample.png"], visual_reviews=[review])
        self.assertEqual(report["files"][1]["visual_status"], "APPROVED")
        Image.new("RGB", (8, 8), "black").save(path)
        report = self.manifest(["README.md", "sample.png"], visual_reviews=[review])
        self.assertEqual(report["files"][1]["visual_status"], "MANUAL_REVIEW_REQUIRED")

    def test_dictionary_normalizes_and_masks_original_and_filename(self):
        self.dictionary()
        self.write("reports/测试甲乙.md", "学校名称：匿名测试 a 学院\n测试 甲乙")
        report = self.manifest(["README.md", "reports/测试甲乙.md"])
        self.assertEqual(report["status"], "BLOCKED")
        self.assertGreaterEqual(report["counts"]["block_hits"], 3)
        serialized = json.dumps(report, ensure_ascii=False)
        for term in ("测试甲乙", "匿名测试", "测试 甲乙", str(self.root)):
            self.assertNotIn(term, serialized)
        self.assertTrue(any(f["path"].startswith("<redacted-name:") for f in report["files"]))

    def test_absolute_account_path_block_has_location_only(self):
        self.write("README.md", "错误来自 C:\\Users\\ExamplePrivateUser\\Documents\\file.json")
        report = scan_workspace(self.root, environ={})
        hit = next(f for f in report["findings"] if f["rule_id"] == "ANON-LOCAL-PATH")
        self.assertEqual(hit["action"], "BLOCK")
        self.assertEqual(hit["positions"][0]["line"], 1)
        self.assertNotIn("ExamplePrivateUser", json.dumps(report))

    def test_third_party_school_reference_requires_review_only(self):
        self.dictionary()
        self.write("README.md", "必要来源：Third Party University 的公开技术文献。")
        report = scan_workspace(self.root, environ={})
        self.assertEqual(report["counts"]["block_hits"], 0)
        self.assertGreater(report["counts"]["open_review_hits"], 0)

    def test_credentials_do_not_leak_in_reports(self):
        secret = "test-secret-value-do-not-export-9284"
        self.write("README.md", "错误凭据=" + secret)
        report = scan_workspace(self.root, environ={"DEEPSEEK_API_KEY": secret})
        self.assertGreater(report["credential_check"]["hit_count"], 0)
        self.assertEqual(report["status"], "BLOCKED")
        self.assertNotIn(secret, json.dumps(report))

    def test_nfkc_combining_characters_and_casefold(self):
        self.dictionary()
        path = self.root / "private/identity_dictionary.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["participant_names_and_aliases"] = ["ÉMILIE"]
        path.write_text(json.dumps(raw), encoding="utf-8")
        self.write("README.md", "e\u0301milie")
        report = scan_workspace(self.root, environ={})
        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual(report["counts"]["block_hits"], 1)

    def test_json_unicode_escapes_are_decoded_before_checks(self):
        self.dictionary()
        self.write("record.json", json.dumps({"author": "测试甲乙"}, ensure_ascii=True))
        report = self.manifest(["README.md", "record.json"])
        self.assertGreater(report["counts"]["block_hits"], 0)
        self.assertTrue(any(f["location"].startswith("json_value:") for f in report["findings"]))

    def test_scope_escape_is_never_read_or_logged(self):
        with self.assertRaises(ValueError):
            scoped_path(self.root, "../private-identity.txt")
        report = self.manifest(["README.md", "../private-identity.txt"])
        self.assertEqual(report["invalid_scope_count"], 1)
        self.assertNotIn("private-identity", json.dumps(report))
        self.assertNotEqual(report["status"], "PASS")

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "已有Pillow不可用")
    def test_image_metadata_read_does_not_replace_visual_review(self):
        from PIL import Image, PngImagePlugin
        self.dictionary()
        path = self.root / "sample.png"
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("Description", "测试甲乙")
        Image.new("RGB", (8, 8), "white").save(path, pnginfo=metadata)
        report = self.manifest(["README.md", "sample.png"], full_coverage=True)
        image = report["files"][1]
        self.assertEqual(image["metadata_status"], "READ")
        self.assertEqual(image["visual_status"], "MANUAL_REVIEW_REQUIRED")
        self.assertGreater(report["counts"]["block_hits"], 0)
        self.assertNotIn("测试甲乙", json.dumps(report, ensure_ascii=False))

    def test_video_unreadable_is_not_pass(self):
        self.dictionary()
        (self.root / "sample.mp4").write_bytes(b"not-a-real-video")
        report = self.manifest(["README.md", "sample.mp4"], full_coverage=True)
        self.assertEqual(report["status"], "NOT_READY")
        self.assertIn("VISUAL_REVIEW_PENDING", report["readiness_reasons"])
        self.assertIn("FULL_TEXT_OR_METADATA_NOT_READ", report["readiness_reasons"])

    @unittest.skipUnless(importlib.util.find_spec("pypdf"), "已有pypdf不可用")
    def test_pdf_author_metadata_and_blank_page_are_audited(self):
        from pypdf import PdfWriter
        self.dictionary()
        writer = PdfWriter()
        writer.add_blank_page(80, 80)
        writer.add_metadata({"/Author": "测试甲乙", "/Title": "metadata fixture"})
        with (self.root / "sample.pdf").open("wb") as handle:
            writer.write(handle)
        report = self.manifest(["README.md", "sample.pdf"])
        pdf = report["files"][1]
        self.assertEqual(pdf["metadata_status"], "READ")
        self.assertEqual(pdf["text_status"], "UNREADABLE")
        self.assertTrue(any(f["location"] == "metadata" and f["action"] == "BLOCK" for f in report["findings"]))
        self.assertNotIn("测试甲乙", json.dumps(report, ensure_ascii=False))

    def test_corrupt_pdf_does_not_crash_or_pass(self):
        (self.root / "broken.pdf").write_bytes(b"%PDF-1.4\ntruncated")
        report = self.manifest(["README.md", "broken.pdf"])
        self.assertEqual(report["files"][1]["status"], "UNREADABLE")
        self.assertNotEqual(report["status"], "PASS")

    @unittest.skipUnless(importlib.util.find_spec("pypdf"), "已有pypdf不可用")
    def test_pdf_xmp_metadata_is_read(self):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, NameObject
        self.dictionary()
        writer = PdfWriter()
        writer.add_blank_page(80, 80)
        stream = DecodedStreamObject()
        stream.set_data('<metadata><creator>测试甲乙</creator></metadata>'.encode("utf-8"))
        stream[NameObject("/Type")] = NameObject("/Metadata")
        stream[NameObject("/Subtype")] = NameObject("/XML")
        writer._root_object[NameObject("/Metadata")] = writer._add_object(stream)
        with (self.root / "xmp.pdf").open("wb") as handle:
            writer.write(handle)
        report = self.manifest(["README.md", "xmp.pdf"])
        self.assertTrue(any(f["location"] == "metadata_xmp" and f["action"] == "BLOCK" for f in report["findings"]))

    def test_office_notes_comments_and_metadata_are_read(self):
        self.dictionary()
        path = self.root / "sample.pptx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("docProps/core.xml", '<root><creator>测试甲乙</creator></root>')
            archive.writestr("ppt/slides/slide1.xml", '<root hidden="true">neutral slide</root>')
            archive.writestr("ppt/notesSlides/notesSlide1.xml", '<root><text>测试 甲乙</text></root>')
        report = self.manifest(["README.md", "sample.pptx"])
        self.assertEqual(report["files"][1]["reader"], "stdlib_zip_xml")
        self.assertEqual(report["files"][1]["unit_count"], 3)
        self.assertGreaterEqual(report["counts"]["block_hits"], 2)

    def test_original_data_and_internal_docs_are_not_implicitly_packaged(self):
        self.write("docs/data_audit.md", "C:\\Users\\PrivateAccount\\note")
        (self.root / "original.zip").write_bytes(b"fixture")
        report = scan_workspace(self.root, environ={})
        self.assertEqual([f["path"] for f in report["files"]], ["README.md"])
        self.assertEqual(report["counts"]["block_hits"], 0)


if __name__ == "__main__":
    unittest.main()
