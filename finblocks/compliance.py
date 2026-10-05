"""匿名材料扫描：只读显式范围，未知或未复核材料不能通过验收。"""

from bisect import bisect_right
from contextlib import contextmanager
import csv
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile


IDENTITY_GROUPS = (
    "school_names_and_aliases", "participant_names_and_aliases",
    "advisor_names_and_aliases", "college_names_and_aliases", "team_contacts",
    "school_logo_file_hashes", "school_logo_visual_references",
)
TEXT_GROUPS = IDENTITY_GROUPS[:5]
REQUIRED_COVERAGE = ("readme", "interface", "ppt_materials", "screenshots", "demo", "exports")
TEXT_SUFFIXES = {".txt", ".md", ".html", ".htm", ".css", ".js", ".json", ".csv", ".tsv", ".xml", ".svg",
                 ".srt", ".vtt", ".py", ".ps1", ".cjs", ".mjs"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
KEY_NAMES = ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY")


class ScopeError(ValueError):
    """错误信息不回显输入路径或操作系统账户。"""


def scoped_path(root, value):
    root = Path(root).resolve()
    if not isinstance(value, (str, Path)) or not str(value):
        raise ScopeError("范围条目无效")
    candidate = Path(value)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if not resolved.is_relative_to(root):
        raise ScopeError("范围条目超出工作区")
    return resolved


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _normalize(text, compact=False):
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(text.split()) if compact else text


def _mapped_text(text, compact):
    chars, offsets = [], []
    for index, char in enumerate(text):
        for normalized in unicodedata.normalize("NFKC", char).casefold():
            chars.append(normalized)
            offsets.append(index)
    individual = "".join(chars)
    whole = _normalize(text)
    if individual != whole:
        mapped = []
        for tag, a, b, c, d in SequenceMatcher(None, individual, whole).get_opcodes():
            if tag == "equal":
                mapped.extend(offsets[a:b])
            elif tag in {"replace", "insert"}:
                mapped.extend([offsets[min(a, len(offsets) - 1)]] * (d - c))
        offsets = mapped
    if compact:
        pairs = [(char, index) for char, index in zip(whole, offsets) if not char.isspace()]
        return "".join(char for char, _ in pairs), [index for _, index in pairs]
    return whole, offsets


def _prepare_text(text):
    # 仅保留当前片段的双通道映射，供本片段全部规则复用；不建立全局缓存。
    return ([i for i, char in enumerate(text) if char == "\n"],
            [(compact, *_mapped_text(text, compact)) for compact in (False, True)])


def _positions(text, pattern, literal=False, prepared=None):
    """双通道匹配后按原文位置去重；不返回匹配内容。"""
    spans = set()
    newlines, channels = _prepare_text(text) if prepared is None else prepared
    for compact, normalized, offsets in channels:
        expression = re.escape(_normalize(pattern, compact)) if literal else pattern
        if not expression:
            continue
        for hit in re.finditer(expression, normalized, re.IGNORECASE):
            if hit.end() > hit.start() and offsets:
                spans.add((offsets[hit.start()], offsets[hit.end() - 1] + 1))
    result = []
    for start, end in sorted(spans):
        line_index = bisect_right(newlines, start)
        previous = newlines[line_index - 1] if line_index else -1
        result.append({"line": line_index + 1, "column": start - previous, "end_offset": end})
    return result


def _dictionary(raw):
    raw = raw if isinstance(raw, dict) else {}
    groups_valid = all(isinstance(raw.get(group), list)
                       and all(isinstance(item, str) and _normalize(item, True) for item in raw[group])
                       for group in IDENTITY_GROUPS)
    terms = {group: [item for item in raw.get(group, []) if isinstance(item, str) and _normalize(item, True)]
             for group in TEXT_GROUPS if isinstance(raw.get(group, []), list)}
    absent = raw.get("confirmed_absent", [])
    absent = absent if isinstance(absent, list) else []
    declared = groups_valid and all(raw[group] or group in absent for group in IDENTITY_GROUPS)
    hashes = raw.get("school_logo_file_hashes", [])
    valid_hashes = isinstance(hashes, list) and all(isinstance(h, str) and re.fullmatch(r"[a-fA-F0-9]{64}", h) for h in hashes)
    hashes = {h.lower() for h in hashes if isinstance(h, str) and re.fullmatch(r"[a-fA-F0-9]{64}", h)} if isinstance(hashes, list) else set()
    complete = (raw.get("status") == "COMPLETE" and declared and valid_hashes
                and sum(len(values) for values in terms.values()) > 0
                and (terms.get("school_names_and_aliases") or "school_names_and_aliases" in absent))
    return terms, hashes, complete


def default_manifest(root):
    """公开候选范围固定为 README、静态界面与 reports；不扫描整工作区。"""
    root = Path(root)
    paths = [p for p in root.iterdir() if p.is_file() and (p.name == "README" or p.name.startswith("README."))]
    web = root / "web"
    if web.is_dir():
        paths.extend(p for p in web.rglob("*") if p.is_file() and p.suffix.lower() in {".html", ".css", ".js"})
    reports = root / "reports"
    if reports.is_dir():
        paths.extend(p for p in reports.rglob("*") if p.is_file())
    names = sorted({p.relative_to(root).as_posix() for p in paths})
    return {"files": names, "coverage": {"readme": [n for n in names if n.startswith("README")]}}


def _coverage_kind(category, evidence):
    """清单声明仍须对应实际材料类型；静态源不能替代运行界面验收。"""
    paths = [Path(name) for name in evidence]
    if category == "readme":
        return any(p.name == "README" or p.name.startswith("README.") for p in paths)
    if category == "interface":
        return any(p.suffix.lower() in IMAGE_SUFFIXES | {".json"} and p.parts[0] != "web" for p in paths)
    if category == "ppt_materials":
        return any(p.suffix.lower() in {".pptx", ".pdf"}
                   or (p.suffix.lower() == ".md" and any(tag in p.as_posix().lower() for tag in ("ppt", "slides", "deck"))) for p in paths)
    if category == "screenshots":
        return any(p.suffix.lower() in IMAGE_SUFFIXES for p in paths)
    if category == "demo":
        return any(p.suffix.lower() in VIDEO_SUFFIXES for p in paths)
    if category == "exports":
        return any(p.suffix.lower() in IMAGE_SUFFIXES | {".json", ".csv", ".pdf"} for p in paths)
    return False


def _decode(raw):
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = raw.decode(encoding)
            if "\x00" in text:
                raise ValueError("文本含二进制空字节")
            return text, encoding
        except UnicodeDecodeError:
            continue
    raise ValueError("文本无法完整解码")


def _string_values(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _string_values(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from _string_values(item)


@contextmanager
def _quiet_library_logs():
    # 第三方解析器的警告也可能携带原文；只保留本扫描器的安全状态码。
    logger = logging.getLogger("pypdf")
    handlers, propagate = logger.handlers[:], logger.propagate
    logger.handlers, logger.propagate = [logging.NullHandler()], False
    try:
        yield
    finally:
        logger.handlers, logger.propagate = handlers, propagate


def _image_metadata(raw):
    if importlib.util.find_spec("PIL") is None:
        raise ValueError("图像元数据读取库不可用")
    from PIL import Image
    with Image.open(io.BytesIO(raw)) as image:
        image.verify()
    with Image.open(io.BytesIO(raw)) as image:
        metadata = {"info": image.info, "exif": dict(image.getexif())}
    return str(metadata)


def _extract(path):
    """每个片段标明来源位置；全部文本和元数据读取状态分开记录。"""
    raw = path.read_bytes()
    suffix = path.suffix.lower()
    segments, visual_hashes = [("filename", path.name)], []
    info = {"text_status": "READ", "metadata_status": "READ", "visual_status": "NOT_APPLICABLE", "reader": "stdlib", "unit_count": 1}
    if suffix in TEXT_SUFFIXES or path.name == "README":
        text, info["encoding"] = _decode(raw)
        segments.append(("text", text))
        if suffix == ".json":
            for index, value in enumerate(_string_values(json.loads(text)), 1):
                segments.append((f"json_value:{index}", value))
        elif suffix in {".csv", ".tsv"}:
            reader = csv.reader(io.StringIO(text), delimiter="\t" if suffix == ".tsv" else ",", strict=True)
            for index, row in enumerate(reader, 1):
                segments.append((f"table_row:{index}", "\n".join(row)))
        if suffix == ".svg":
            info["visual_status"] = "MANUAL_REVIEW_REQUIRED"
    elif suffix == ".pdf":
        if importlib.util.find_spec("pypdf") is None:
            raise ValueError("PDF读取库不可用")
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            raise ValueError("PDF加密，不能确认完整读取")
        info.update(reader="pypdf", unit_count=len(reader.pages), visual_status="MANUAL_REVIEW_REQUIRED")
        for index, page in enumerate(reader.pages, 1):
            text = page.extract_text()
            if not text or not text.strip():
                info["text_status"] = "UNREADABLE"
            segments.append((f"page:{index}", text or ""))
            annotations = page.get("/Annots", [])
            for annotation_index, reference in enumerate(annotations, 1):
                annotation = reference.get_object()
                annotation_text = "\n".join(str(annotation.get(key, "")) for key in ("/T", "/Contents", "/Subj", "/RC"))
                segments.append((f"page:{index}:annotation:{annotation_index}", annotation_text))
        segments.append(("metadata", str(dict(reader.metadata or {}))))
        metadata_reference = reader.trailer["/Root"].get("/Metadata")
        if metadata_reference is not None:
            xmp = ET.fromstring(metadata_reference.get_object().get_data())
            values = []
            for child in xmp.iter():
                values.extend([child.text or "", child.tail or "", *child.attrib.keys(), *child.attrib.values()])
            segments.append(("metadata_xmp", "\n".join(values)))
        segments.append(("metadata_outline", str(reader.outline)))
        segments.append(("metadata_forms", str(reader.get_fields() or {})))
        if reader.attachments:
            info["metadata_status"] = "UNREADABLE"
    elif suffix in {".docx", ".pptx", ".xlsx"}:
        info.update(reader="stdlib_zip_xml", visual_status="MANUAL_REVIEW_REQUIRED")
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            xml_files = sorted(n for n in archive.namelist() if n.endswith((".xml", ".rels")))
            if not xml_files:
                raise ValueError("Office容器没有可读XML")
            info["unit_count"] = len(xml_files)
            for index, name in enumerate(xml_files, 1):
                node = ET.fromstring(archive.read(name))
                values = []
                for child in node.iter():
                    values.extend([child.text or "", child.tail or "", *child.attrib.keys(), *child.attrib.values()])
                segments.append((f"package_xml:{index}", "\n".join(values)))
            for index, name in enumerate(archive.namelist(), 1):
                extension = Path(name).suffix.lower()
                if extension in IMAGE_SUFFIXES:
                    content = archive.read(name)
                    visual_hashes.append(hashlib.sha256(content).hexdigest())
                    try:
                        segments.append((f"image_metadata:{index}", _image_metadata(content)))
                    except (OSError, ValueError, SyntaxError):
                        info["metadata_status"] = "UNREADABLE"
                elif extension in VIDEO_SUFFIXES or extension in {".bin", ".wmf", ".emf"}:
                    info["metadata_status"] = "UNREADABLE"
    elif suffix in IMAGE_SUFFIXES:
        info.update(reader="Pillow", text_status="NOT_APPLICABLE", visual_status="MANUAL_REVIEW_REQUIRED")
        segments.append(("image_metadata", _image_metadata(raw)))
        visual_hashes.append(hashlib.sha256(raw).hexdigest())
    elif suffix in VIDEO_SUFFIXES:
        info.update(reader="manual_required", text_status="UNREADABLE", metadata_status="UNREADABLE", visual_status="MANUAL_REVIEW_REQUIRED")
    else:
        raise ValueError("没有支持该格式的完整读取器")
    return segments, visual_hashes, info


def scan_workspace(root, *, rules_path="docs/anonymity_check_rules.json", identity_path="private/identity_dictionary.json", manifest_path=None, environ=None):
    root = Path(root).resolve()
    rules = _read_json(scoped_path(root, rules_path))
    identity_file = scoped_path(root, identity_path)
    identity_exists = identity_file.is_file()
    try:
        raw_identity = _read_json(identity_file) if identity_exists else rules.get("identity_dictionary", {})
    except (OSError, ValueError):
        raw_identity = {}
    terms, logo_hashes, complete = _dictionary(raw_identity)
    dictionary_status = "COMPLETE" if identity_exists and complete else ("INCOMPLETE" if identity_exists else "MISSING")
    manifest = _read_json(scoped_path(root, manifest_path)) if manifest_path else default_manifest(root)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise ValueError("材料清单必须包含files数组")
    environment = os.environ if environ is None else environ
    credential_values = [value for name in KEY_NAMES if isinstance(value := environment.get(name), str) and len(value) >= 8]
    checks = [("IDENTITY-" + group.upper(), "BLOCK", value, True) for group, values in terms.items() for value in values]
    checks.extend((rule["id"], rule["action"], rule["pattern"], False) for rule in rules.get("text_rules", []))
    checks.extend(("CREDENTIAL-ENV", "BLOCK", value, True) for value in credential_values)
    redactions = [value for values in terms.values() for value in values] + credential_values
    report = {
        "version": 1, "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope_policy": "explicit_manifest_or_readme_web_reports_only",
        "official_basis": {"source_file": rules["source"]["file"], "page": rules["source"]["page"],
                           "explicit_categories": rules["source"]["explicitly_forbidden"],
                           "conservative_extensions": rules["source"]["project_conservative_extensions"]},
        "identity_dictionary": {"status": dictionary_status, "text_term_count": sum(len(v) for v in terms.values()), "logo_hash_count": len(logo_hashes)},
        "credential_check": {"configured_key_count": len(credential_values), "hit_count": 0},
        "files": [], "findings": [], "invalid_scope_count": 0, "missing_coverage": [], "readiness_reasons": [],
    }
    seen, scanned = set(), {}
    text_reviews = manifest.get("text_reviews", [])
    visual_reviews = manifest.get("visual_reviews", [])
    for index, value in enumerate(manifest["files"], 1):
        try:
            path = scoped_path(root, value)
        except ScopeError:
            report["invalid_scope_count"] += 1
            continue
        relative = path.relative_to(root).as_posix()
        if relative in seen:
            continue
        seen.add(relative)
        safe_name = f"<redacted-name:{index}>" if any(_normalize(term, True) in _normalize(relative, True) for term in redactions) else relative
        item = {"path": safe_name, "file_id": f"file-{index}", "status": "UNREADABLE", "text_status": "UNREADABLE", "metadata_status": "UNREADABLE", "visual_status": "UNKNOWN"}
        report["files"].append(item)
        if not path.is_file():
            item["status"] = "MISSING"
            continue
        try:
            file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            item["sha256"] = file_hash
            with _quiet_library_logs():
                segments, visual_hashes, info = _extract(path)
            item.update(info, status="READ" if info["text_status"] != "UNREADABLE" and info["metadata_status"] == "READ" else "UNREADABLE")
            if item["visual_status"] == "MANUAL_REVIEW_REQUIRED":
                if any(isinstance(review, dict) and review.get("path") == relative and review.get("sha256") == file_hash
                       and review.get("status") == "APPROVED" and review.get("human_review") is True for review in visual_reviews):
                    item["visual_status"] = "APPROVED"
            grouped = {}
            for location, text in segments:
                prepared = _prepare_text(text)
                for rule_id, action, pattern, literal in checks:
                    matches = _positions(text, pattern, literal, prepared)
                    if not matches:
                        continue
                    key = (rule_id, action, location)
                    grouped.setdefault(key, []).extend(matches)
                prepared = None
            for (rule_id, action, location), positions in grouped.items():
                # 不输出片段、身份值、凭据值，也不输出包含账户名的绝对路径。
                unique = {(p["line"], p["column"], p["end_offset"]) for p in positions}
                entry = {"file_id": item["file_id"], "rule_id": rule_id, "action": action,
                         "location": location, "count": len(unique),
                         "positions": [{"line": p[0], "column": p[1]} for p in sorted(unique)[:200]], "review_status": "OPEN"}
                if action == "REVIEW" and any(isinstance(review, dict) and review.get("path") == relative
                                             and review.get("sha256") == file_hash and review.get("rule_id") == rule_id
                                             and review.get("location") == location and review.get("status") == "CLEARED"
                                             and review.get("human_review") is True for review in text_reviews):
                    entry["review_status"] = "CLEARED"
                report["findings"].append(entry)
                if rule_id == "CREDENTIAL-ENV":
                    report["credential_check"]["hit_count"] += entry["count"]
            if logo_hashes.intersection(visual_hashes):
                report["findings"].append({"file_id": item["file_id"], "rule_id": "ANON-LOGO-HASH", "action": "BLOCK",
                                           "location": "visual_asset", "count": len(logo_hashes.intersection(visual_hashes)), "positions": [], "review_status": "OPEN"})
            scanned[relative] = item
        except Exception:
            # 读取失败不得写入原始异常，异常可能含账户路径或材料原文。
            item["error_code"] = "FULL_READ_UNAVAILABLE"
    coverage = manifest.get("coverage", {})
    for category in REQUIRED_COVERAGE:
        evidence = coverage.get(category, []) if isinstance(coverage, dict) else []
        if (not isinstance(evidence, list) or not evidence
                or not all(isinstance(n, str) and n in scanned for n in evidence)
                or not _coverage_kind(category, evidence)):
            report["missing_coverage"].append(category)
    reasons = report["readiness_reasons"]
    if dictionary_status != "COMPLETE":
        reasons.append("IDENTITY_DICTIONARY_NOT_COMPLETE")
    if not report["files"]:
        reasons.append("EMPTY_MATERIAL_LIST")
    if report["invalid_scope_count"]:
        reasons.append("OUT_OF_WORKSPACE_SCOPE")
    if report["missing_coverage"]:
        reasons.append("DELIVERY_COVERAGE_INCOMPLETE")
    if any(item["status"] != "READ" for item in report["files"]):
        reasons.append("FULL_TEXT_OR_METADATA_NOT_READ")
    if any(item["visual_status"] not in {"NOT_APPLICABLE", "APPROVED"} for item in report["files"]):
        reasons.append("VISUAL_REVIEW_PENDING")
    blocked = sum(f["count"] for f in report["findings"] if f["action"] == "BLOCK")
    review = sum(f["count"] for f in report["findings"] if f["action"] == "REVIEW" and f["review_status"] != "CLEARED")
    if blocked:
        reasons.append("BLOCK_FINDINGS_PRESENT")
    if review:
        reasons.append("REVIEW_FINDINGS_PENDING")
    report["counts"] = {"files": len(report["files"]), "block_hits": blocked, "open_review_hits": review}
    report["status"] = "BLOCKED" if blocked else ("NOT_READY" if reasons else "PASS")
    return report
