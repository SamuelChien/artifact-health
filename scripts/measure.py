#!/usr/bin/env python3
"""Read-only documentary inventory and bounded format inspection. Python 3.10+."""
import argparse
import csv
import hashlib
import json
import math
import os
import posixpath
import re
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

if __package__:
    from .evidence import digest_file, write_json, evaluator_identity, run_receipt, source_fingerprint
    from .relations import analyze
    from .workers import ParserWorkers
    from .content_signals import IMAGE_EXTENSIONS, image_metrics, origin_signals, pdf_metadata, office_metadata, invalid_reference
else:
    from evidence import digest_file, write_json, evaluator_identity, run_receipt, source_fingerprint
    from relations import analyze
    from workers import ParserWorkers
    from content_signals import IMAGE_EXTENSIONS, image_metrics, origin_signals, pdf_metadata, office_metadata, invalid_reference

VERSION = "artifact-health-measure@4"
RUBRIC_PATH = Path(__file__).resolve().parents[1] / "references/rubric.json"
EXTENSIONS = {".pdf", ".docx", ".xlsx", ".pptx", ".odt", ".eml", ".txt", ".md",
              ".csv", ".tsv", ".json", ".html", ".htm", ".doc", ".xls", ".ppt",
              ".pages", ".numbers", ".key", ".rtf", ".msg", ".mbox", ".heic", ".heif"} | IMAGE_EXTENSIONS
LEGACY = {".doc", ".xls", ".ppt", ".pages", ".numbers", ".key", ".rtf", ".msg", ".mbox", ".heic", ".heif"}
SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv"}


def write_csv(path, rows, fields):
    def cell(value):
        if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + value
        return value
    with Path(path).open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: cell(value) for key, value in row.items()})


def new_output(path, source=None):
    path = Path(path).expanduser().absolute()
    if source and path.resolve().is_relative_to(Path(source).resolve()):
        raise ValueError("output must be outside the source tree")
    if path.exists():
        raise ValueError("output already exists; select a fresh directory")
    path.mkdir(parents=True, exist_ok=False)
    return path


def probe(path):
    """Runs in a disposable child process so parser timeouts are enforceable."""
    path = Path(path)
    ext = path.suffix.lower()
    result = {"status": "inspected", "metrics": {}, "observations": []}
    metrics = result["metrics"]

    def note(code, detail, kind="REVIEW", severity="minor"):
        result["observations"].append({"code": code, "detail": detail, "kind": kind, "severity": severity})

    try:
        if ext in LEGACY:
            result["status"] = "unavailable"
            note("EXTRACTOR_UNAVAILABLE", "Format is inventoried; conversion or a format-specific reader is required.", "NOT_CHECKED", "info")
        elif ext in IMAGE_EXTENSIONS:
            try:
                metrics.update(image_metrics(path))
            except ImportError:
                result["status"] = "unavailable"
                note("IMAGE_DEPENDENCY_MISSING", "Install Pillow for raster inspection.", "NOT_CHECKED", "info")
        elif ext == ".pdf":
            try:
                import pypdf
            except ImportError:
                result["status"] = "unavailable"
                note("PDF_DEPENDENCY_MISSING", "Install pypdf to inspect PDF files.", "NOT_CHECKED", "info")
                return result
            metrics["parser_version"] = pypdf.__version__
            reader = pypdf.PdfReader(str(path), strict=False)
            metrics["encrypted"] = reader.is_encrypted
            if reader.is_encrypted and not reader.decrypt(""):
                result["status"] = "unavailable"
                note("PDF_ENCRYPTED", "PDF needs a password; page content was not inspected.", "NOT_CHECKED", "info")
                return result
            metrics["pages"] = len(reader.pages)
            metrics["review_units"] = {"kind": "page", "count": metrics["pages"]}
            texts = [(page.extract_text() or "").strip() for page in reader.pages]
            lengths = [len(text) for text in texts]
            metrics["document_metadata"] = pdf_metadata(reader)
            metrics["origin_signals"] = origin_signals("\n".join(texts))
            metrics["page_geometry_points"] = [[float(p.mediabox.width), float(p.mediabox.height)] for p in reader.pages]
            metrics["native_text_characters"] = sum(lengths)
            metrics["pages_without_native_text"] = sum(length == 0 for length in lengths)
            if not lengths:
                note("PDF_NO_PAGES", "PDF contains no pages.", "DEFECT", "major")
            elif metrics["pages_without_native_text"]:
                note("PDF_TEXT_REVIEW", "Some pages have no extracted native text; inspect images and OCR needs. This does not establish blank pages.")
        elif ext in {".docx", ".xlsx", ".pptx", ".odt"}:
            with zipfile.ZipFile(path) as archive:
                if sum(item.file_size for item in archive.infolist()) > 200 * 1024 * 1024:
                    result["status"] = "unavailable"
                    note("EXPANDED_SIZE_LIMIT", "Expanded package exceeds the 200 MiB inspection limit.", "NOT_CHECKED", "info")
                    return result
                main = {".docx": "word/document.xml", ".xlsx": "xl/workbook.xml",
                        ".pptx": "ppt/presentation.xml", ".odt": "content.xml"}[ext]
                names = set(archive.namelist())
                metrics["document_metadata"] = office_metadata(archive)
                metrics["embedded_media_members"] = sum("/media/" in name for name in names)
                invalid_refs = []
                if ext == ".xlsx":
                    workbook = ET.fromstring(archive.read(main))
                    metrics["invalid_defined_names"] = [{"name": node.get("name"), "formula": node.text}
                        for node in workbook.iter() if node.tag.rsplit("}", 1)[-1] == "definedName" and invalid_reference(node.text)]
                    if metrics["invalid_defined_names"]:
                        note("SPREADSHEET_INVALID_DEFINED_NAME", "Workbook contains defined names with invalid references.", "DEFECT", "major")
                if len(names) != len(archive.namelist()):
                    note("OFFICE_DUPLICATE_PART", "Package contains duplicate member names.", "DEFECT", "major")
                if archive.testzip() is not None:
                    note("OFFICE_CRC_FAILED", "A package member failed its checksum.", "DEFECT", "major")
                external = 0
                for rel_name in sorted(n for n in names if n.endswith(".rels")):
                    relationships = ET.fromstring(archive.read(rel_name))
                    base = posixpath.dirname(posixpath.dirname(rel_name))
                    for relationship in relationships:
                        if relationship.get("TargetMode") == "External":
                            external += 1
                            continue
                        target = unquote(urlsplit(relationship.get("Target", "")).path)
                        resolved = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(base, target))
                        if not target or resolved == ".." or resolved.startswith("../") or resolved not in names:
                            note("OFFICE_RELATIONSHIP_MISSING", "Internal package relationship has no available target: " + rel_name,
                                 "DEFECT", "major")
                if ext != ".odt":
                    ET.fromstring(archive.read("[Content_Types].xml"))
                selected = [main]
                if ext == ".docx":
                    selected += sorted(n for n in names if re.fullmatch(r"word/(?:header\d+|footer\d+|footnotes|endnotes|comments)\.xml", n))
                    doc = ET.fromstring(archive.read(main))
                    tags = Counter(node.tag.rsplit("}", 1)[-1] for node in doc.iter())
                    metrics.update(paragraphs=tags["p"], tables=tags["tbl"], drawings=tags["drawing"], textboxes=tags["txbxContent"])
                if ext == ".xlsx":
                    selected += sorted(n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
                    if "xl/sharedStrings.xml" in names:
                        selected.append("xl/sharedStrings.xml")
                elif ext == ".pptx":
                    selected += sorted(n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n))
                text_chars = 0
                formula_count = 0
                errors = uncached = 0
                origin_text = []
                for name in selected:
                    document = ET.fromstring(archive.read(name))
                    origin_text.extend(node.text or "" for node in document.iter() if node.tag.rsplit("}", 1)[-1] in {"t", "p", "span"})
                    text_chars += sum(len(node.text or "") for node in document.iter() if node.tag.rsplit("}", 1)[-1] in {"t", "p", "span"})
                    formula_count += sum(node.tag.rsplit("}", 1)[-1] == "f" for node in document.iter())
                    if ext == ".xlsx":
                        for node in document.iter():
                            if node.tag.rsplit("}", 1)[-1] == "c":
                                children = {child.tag.rsplit("}", 1)[-1]: child for child in node}
                                errors += node.get("t") == "e"
                                uncached += "f" in children and ("v" not in children or not children["v"].text)
                                if "f" in children and invalid_reference(children["f"].text):
                                    invalid_refs.append({"part": name, "cell": node.get("r"), "formula": children["f"].text,
                                                         "shared_range": children["f"].get("ref")})
                metrics["origin_signals"] = origin_signals(" ".join(origin_text))
                metrics.update(xml_parts_inspected=len(selected), native_text_characters=text_chars,
                               formula_cells=formula_count, package_members=len(names), external_relationships=external,
                               review_units={"kind": "part", "count": len(selected), "labels": selected})
                if ext == ".xlsx":
                    metrics["invalid_reference_formulas"] = invalid_refs
                    if invalid_refs:
                        note("SPREADSHEET_INVALID_FORMULA_REFERENCE", "Formula definitions contain #REF!, including references masked by IFERROR.", "DEFECT", "major")
                    metrics.update(cached_error_cells=errors, formula_cells_without_cached_values=uncached)
                    if errors or uncached:
                        note("SPREADSHEET_CALCULATION_REVIEW", "Cached errors or missing formula values require recalculation and review.")
                if ext == ".pptx":
                    metrics["slides"] = len(selected) - 1
        elif ext == ".eml":
            message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
            if not any(message.get(field) for field in ("From", "Date", "Subject", "Message-ID", "Received")):
                note("EMAIL_NO_HEADERS", "No recognizable mail headers were found.", "DEFECT", "major")
            metrics["message_id"] = str(message.get("Message-ID", "")).strip() or None
            metrics["document_metadata"] = {"asserted": {field: str(message.get(field, "")) for field in ("From", "To", "Date", "Subject")},
                                             "authenticated_creator": None, "transport_authentication_verified": False}
            metrics["in_reply_to"] = str(message.get("In-Reply-To", "")).strip() or None
            metrics["reply_ids"] = re.findall(r"<[^<>\s]+>", metrics["in_reply_to"] or "")
            metrics["references"] = re.findall(r"<[^<>\s]+>", str(message.get("References", "")))
            try:
                date = parsedate_to_datetime(str(message.get("Date", "")))
                metrics["date_has_timezone"] = date.tzinfo is not None
                metrics["date_utc"] = date.astimezone(timezone.utc).isoformat() if date.tzinfo else None
                if date.tzinfo is None:
                    note("EMAIL_DATE_TIMEZONE_UNKNOWN", "Native Date has no timezone; chronology requires additional evidence.")
            except (ValueError, TypeError, OverflowError):
                metrics["date_utc"] = None
                note("EMAIL_DATE_UNKNOWN", "No usable native Date header; chronology requires other evidence.")
            attachments = []
            text_chars = 0
            origin_text = []
            defects = []
            content_parts = []
            leaf_number = 0
            for part in message.walk():
                if part.is_multipart():
                    defects.extend(type(d).__name__ for d in part.defects)
                    if part.get_content_disposition() == "attachment":
                        note("NESTED_MESSAGE_ATTACHMENT_UNCHECKED", "Attached multipart message requires separate content inspection.", "NOT_CHECKED", "info")
                    continue
                leaf_number += 1
                payload = part.get_payload(decode=True) or b""
                content_parts.append([part.get_content_type(), part.get_content_charset(), part.get_content_disposition(),
                                      part.get_filename(), str(part.get("Content-ID", "")),
                                      hashlib.sha256(payload.replace(b"\r\n", b"\n") if part.get_content_maintype() == "text" else payload).hexdigest()])
                defects.extend(type(d).__name__ for d in part.defects)
                if part.get_filename() or part.get_content_disposition() == "attachment":
                    attachments.append({"name": part.get_filename(), "content_type": part.get_content_type(),
                                        "part": leaf_number,
                                        "disposition": part.get_content_disposition(), "bytes": len(payload),
                                        "sha256": hashlib.sha256(payload).hexdigest()})
                elif part.get_content_maintype() == "text":
                    try:
                        decoded = payload.decode(part.get_content_charset() or "ascii")
                        text_chars += len(decoded)
                        origin_text.append(decoded)
                    except (UnicodeError, LookupError):
                        note("EMAIL_CHARSET_REVIEW", "A text part could not be decoded with its declared/default charset.")
            metrics.update(named_or_attachment_parts=attachments, body_characters_including_quotes=text_chars,
                           mime_defects=sorted(set(defects)), review_units={"kind": "part", "count": leaf_number})
            metrics["origin_signals"] = origin_signals("\n".join(origin_text))
            authored_headers = [str(message.get(name, "")) for name in ("From", "To", "Cc", "Subject", "Date", "In-Reply-To", "References")]
            metrics["content_sha256"] = hashlib.sha256(json.dumps([authored_headers, content_parts], ensure_ascii=False).encode()).hexdigest()
            if defects:
                note("EMAIL_MIME_DEFECTS", ", ".join(sorted(set(defects))), "REVIEW", "major")
        else:
            raw = path.read_bytes()
            encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
            try:
                text = raw.decode(encoding)
            except UnicodeError:
                result["status"] = "unavailable"
                note("TEXT_ENCODING_UNKNOWN", "Not UTF-8 or BOM-marked UTF-16; select the actual encoding before grading.", "NOT_CHECKED", "info")
                return result
            metrics["text_characters"] = len(text)
            metrics["origin_signals"] = origin_signals(text)
            metrics["review_units"] = {"kind": "file", "count": 1}
            if ext == ".json":
                data = json.loads(text)
                metrics["json_shape"] = {"type": type(data).__name__, "items": len(data) if isinstance(data, (dict, list)) else None,
                                         "top_level_keys": sorted(data)[:100] if isinstance(data, dict) else None,
                                         "schema_validation": "not_checked", "source_fidelity": "not_checked"}
                if path.name == "index.json" and isinstance(data, dict) and isinstance(data.get("messages"), list):
                    rows = data["messages"]
                    if any(isinstance(row, dict) and any(key in row for key in ("eml_file", "text_file", "original_file")) for row in rows):
                        result["index"] = {"path_base": data.get("path_base", "."), "rows": [
                            {k: row.get(k) for k in ("eml_file", "original_file", "text_file", "sha256", "original_sha256", "text_sha256")}
                            if isinstance(row, dict) else None for row in rows]}
                        metrics["indexed_mailbox_copies"] = len(rows)
            elif ext in {".csv", ".tsv"}:
                rows = csv.reader(text.splitlines(), delimiter="\t" if ext == ".tsv" else ",")
                widths = Counter(len(row) for row in rows)
                metrics.update(rows=sum(widths.values()), row_widths=dict(widths))
                if len(widths) > 1:
                    note("TABLE_WIDTH_VARIATION", "Delimited rows have varying field counts; inspect intended format.")
    except Exception as exc:
        format_error = isinstance(exc, (zipfile.BadZipFile, ET.ParseError, json.JSONDecodeError))
        format_error |= isinstance(exc, KeyError) and ext in {".docx", ".xlsx", ".pptx", ".odt"}
        format_error |= type(exc).__module__.startswith("pypdf.") and type(exc).__name__ in {
            "PdfReadError", "PdfStreamError", "ParseError", "EmptyFileError"}
        if format_error:
            result["status"] = "invalid"
            note("FORMAT_PARSE_FAILED", "Parser failed: " + type(exc).__name__, "DEFECT", "major")
        else:
            result["status"] = "unavailable"
            note("PARSER_UNAVAILABLE", "Inspection could not complete: " + type(exc).__name__, "NOT_CHECKED", "info")
    return result


def discover_files(root, scope, deadline=float("inf")):
    """One selection policy shared by measurement and source freshness checks."""
    files, errors = [], []
    excluded = Counter()
    complete = True
    profile = scope["profile"]
    exclusions = scope["exclude"]

    def walk_error(error):
        nonlocal complete
        complete = False
        errors.append(type(error).__name__)

    def explicit_exclusion(relative):
        return any(relative == value or relative.startswith(value + "/") for value in exclusions)

    for parent, dirs, names in os.walk(root, followlinks=False, onerror=walk_error):
        if time.monotonic() >= deadline:
            complete = False
            errors.append("discovery_budget_exhausted")
            break
        retained = []
        for name in sorted(dirs):
            path = Path(parent) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                excluded["symlink_directories"] += 1
            elif name in scope["skip_directory_names"] or explicit_exclusion(relative):
                excluded["excluded_directories"] += 1
            elif path.suffix.lower() in {".pages", ".numbers", ".key"}:
                if profile == "email":
                    excluded["profile:" + path.suffix.lower()] += 1
                else:
                    files.append((relative, path))
            else:
                retained.append(name)
        dirs[:] = retained
        for name in sorted(names):
            path = Path(parent) / name
            relative = path.relative_to(root).as_posix()
            ext = path.suffix.lower()
            if path.is_symlink():
                excluded["symlink_files"] += 1
            elif explicit_exclusion(relative):
                excluded["explicit_files"] += 1
            elif ext not in scope["extensions"]:
                excluded["extension:" + (ext or "none")] += 1
            elif (profile == "documents" and ext in {".eml", ".msg", ".mbox"}) or (profile == "email" and ext not in {".eml", ".msg", ".mbox", ".json", ".txt", ".html", ".htm"}):
                excluded["profile:" + ext] += 1
            else:
                files.append((relative, path))
    return sorted(files), dict(excluded), complete, errors


def snapshot_file(path, destination, deadline):
    """Hash exactly the bytes given to the parser, in a temporary local copy."""
    digest = hashlib.sha256()
    with path.open("rb") as source, destination.open("xb") as target:
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("run budget exhausted while copying parser input")
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            target.write(chunk)
    return digest.hexdigest()


def measure(root, out, profile="mixed", exclude=(), budget_seconds=900, file_timeout=30,
            max_files=None, max_file_mb=100, workers=1):
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("source must be a directory")
    if profile not in {"mixed", "documents", "email"}:
        raise ValueError("unknown measurement profile")
    if any(not math.isfinite(value) or value <= 0 for value in (budget_seconds, file_timeout, max_file_mb)) or (max_files is not None and max_files < 1):
        raise ValueError("budgets, file limits, and timeouts must be positive")
    if type(workers) is not int or not 1 <= workers <= 32:
        raise ValueError("workers must be an integer from 1 to 32")
    exclusions = []
    for item in exclude:
        value = Path(item)
        if value.is_absolute() or ".." in value.parts or str(value) in {"", "."}:
            raise ValueError("exclusions must be nonempty paths relative to the source")
        exclusions.append(value.as_posix().rstrip("/"))
    out = new_output(out, root)
    scope = {"profile": profile, "exclude": sorted(set(exclusions)), "skip_directory_names": sorted(SKIP_DIRS),
             "extensions": sorted(EXTENSIONS), "selection_version": 2}
    with run_receipt(out, "measure", source_root=str(root)) as receipt:
        result, code = _measure(root, out, scope, budget_seconds, file_timeout, max_files, max_file_mb, receipt["run_id"], workers)
        receipt.update(source_fingerprint=result["source_fingerprint"], measurement_status=result["status"], exit_code=code)
    return result, code


def _measure(root, out, scope, budget_seconds, file_timeout, max_files, max_file_mb, run_id, workers=1):
    started = time.monotonic()
    deadline = started + budget_seconds
    records, findings, indexes = [], [], []
    inspection_cache, cache_hits = {}, 0
    cache_locks = [threading.Lock() for _ in range(max(1, workers * 4))]

    def progress(phase, **details):
        with (out / "measurement.log").open("a", encoding="utf-8") as log:
            log.write(json.dumps({"phase": phase, "elapsed_seconds": round(time.monotonic() - started, 3), **details}) + "\n")

    progress("discovery_started")

    def finding(code, path, detail, kind="NOT_CHECKED", severity="info", sha256=None):
        findings.append({"id": f"F{len(findings) + 1:06d}", "kind": kind, "severity": severity,
                         "code": code, "path": path, "sha256": sha256, "detail": detail})

    files, excluded, discovery_complete, discovery_errors = discover_files(root, scope, deadline)
    progress("discovery_finished", files=len(files), complete=discovery_complete)
    for error in discovery_errors:
        finding("DISCOVERY_INCOMPLETE", ".", error)
    pool = ParserWorkers(workers, out)
    def inspect_one(item):
        number, (relative, path) = item
        parts = Path(relative).parts
        record = {"path": relative, "collection": parts[0] if len(parts) > 1 else ".",
                  "extension": path.suffix.lower(), "bytes": None, "sha256": None,
                  "status": "unavailable", "metrics": {}}
        notes = []
        index = None
        def finding(code, path, detail, kind="NOT_CHECKED", severity="info", sha256=None):
            notes.append({"kind": kind, "severity": severity, "code": code, "path": path, "sha256": sha256, "detail": detail})
        try:
            if not path.resolve().is_relative_to(root) or path.is_symlink():
                finding("SOURCE_PATH_CHANGED", relative, "Selected path now resolves outside the source or to a symlink.")
                return record, notes, index
            before = path.stat()
            if path.is_dir():
                record["kind"] = "package_directory"
                finding("PACKAGE_DIRECTORY_UNAVAILABLE", relative, "Document bundle is inventoried; a bundle-aware converter is required.")
                return record, notes, index
            record["bytes"] = before.st_size
            if max_files is not None and number >= max_files:
                finding("FILE_COUNT_LIMIT", relative, "Outside the configured inspection count.")
                return record, notes, index
            if before.st_size > max_file_mb * 1024 * 1024:
                finding("FILE_SIZE_LIMIT", relative, "Exceeds the configured file size limit.")
                return record, notes, index
            if before.st_size == 0:
                record["sha256"] = digest_file(path, deadline)
                record["status"] = "invalid"
                finding("EMPTY_FILE", relative, "File has zero bytes.", "DEFECT", "major", record["sha256"])
                return record, notes, index
            with tempfile.TemporaryDirectory(prefix=".probe-", dir=out) as temporary:
                snapshot = Path(temporary) / path.name
                record["sha256"] = snapshot_file(path, snapshot, deadline)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("run budget exhausted")
                cache_key = (record["extension"], path.name if record["extension"] == ".json" else "", record["sha256"])
                # Serialize copies of the same content while allowing unrelated files
                # to parse concurrently. Every copy is still independently hashed.
                lock = cache_locks[int(record["sha256"][:8], 16) % len(cache_locks)]
                with lock:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("run budget exhausted waiting for parser")
                    if cache_key in inspection_cache:
                        result = inspection_cache[cache_key]
                        record["_cache_hit"] = True
                    else:
                        if workers > 1:
                            result = pool.query(snapshot, min(file_timeout, remaining))
                        else:
                            child_env = {name: os.environ[name] for name in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "SYSTEMROOT") if name in os.environ}
                            child = subprocess.run([sys.executable, "-E", "-B", str(Path(__file__).resolve()), "--probe-file", str(snapshot)],
                                                   capture_output=True, text=True, encoding="utf-8", cwd=temporary, env=child_env,
                                                   timeout=min(file_timeout, remaining))
                            if child.returncode != 0:
                                progress("parser_process_failed", path=relative, returncode=child.returncode, stderr_tail=child.stderr[-2000:])
                                finding("PARSER_PROCESS_FAILED", relative, "Inspection process did not return a result.", sha256=record["sha256"])
                                return record, notes, index
                            result = json.loads(child.stdout)
                        if result["status"] in {"inspected", "invalid"}:
                            inspection_cache[cache_key] = result
            after = path.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                record["status"] = "changed"
                finding("SOURCE_CHANGED", relative, "Source changed during inspection; rerun on a stable snapshot.", sha256=record["sha256"])
                return record, notes, index
            record.update(status=result["status"], metrics=result["metrics"])
            for note in result["observations"]:
                finding(path=relative, sha256=record["sha256"], **note)
            if "index" in result:
                index = (relative, result["index"])
        except (TimeoutError, subprocess.TimeoutExpired):
            finding("INSPECTION_TIMEOUT", relative, "Run budget or per-file timeout prevented inspection.", sha256=record["sha256"])
        except (OSError, ValueError) as exc:
            finding("INSPECTION_UNAVAILABLE", relative, type(exc).__name__, sha256=record["sha256"])

        return record, notes, index

    try:
        results = pool.map(inspect_one, enumerate(files)) if workers > 1 else map(inspect_one, enumerate(files))
        for completed, (record, notes, index) in enumerate(results, 1):
            cache_hits += int(record.pop("_cache_hit", False))
            records.append(record)
            findings.extend(notes)
            if index is not None:
                indexes.append(index)
            if completed % 100 == 0:
                progress("inspection", completed=completed, total=len(files))
    finally:
        pool.close()
    records.sort(key=lambda row: row["path"])
    findings.sort(key=lambda row: (row["path"], row["code"], row["detail"]))
    for number, item in enumerate(findings, 1):
        item["id"] = f"F{number:06d}"
    indexes.sort(key=lambda item: item[0])

    by_path = {record["path"]: record for record in records}
    progress("inspection_finished", files=len(records))
    index_stats = Counter()
    for index_path, index in indexes:
        index_stats["indexes"] += 1
        index_stats["indexed_mailbox_copies"] += len(index["rows"])
        base = index["path_base"]
        for position, row in enumerate(index["rows"]):
            if time.monotonic() >= deadline:
                index_stats["rows_unchecked_budget"] += len(index["rows"]) - position
                finding("INDEX_BUDGET_EXHAUSTED", index_path, "Run budget prevented remaining index-reference checks.")
                break
            if not isinstance(row, dict) or not isinstance(base, str):
                finding("INDEX_ROW_INVALID", index_path, "Invalid mailbox row or path_base.", "DEFECT", "major")
                continue
            for field, hash_field in (("eml_file", "sha256"), ("original_file", "original_sha256"), ("text_file", "text_sha256")):
                reference = row.get(field)
                if reference is None:
                    continue
                index_stats["references"] += 1
                if not isinstance(reference, str) or not reference:
                    finding("INDEX_PATH_INVALID", index_path, "Invalid " + field + " reference.", "DEFECT", "major")
                    continue
                target = Path(os.path.abspath(root / Path(index_path).parent / base / reference))
                if not target.is_relative_to(root) or any(part.is_symlink() for part in [target, *list(target.parents)[:len(target.parts) - len(root.parts)]]):
                    finding("INDEX_PATH_OUTSIDE_SCOPE", index_path, "Reference escapes the root or traverses a symlink.", "DEFECT", "major")
                    continue
                relative = target.relative_to(root).as_posix()
                if not target.is_file():
                    finding("INDEX_FILE_MISSING", index_path, "Referenced file is missing: " + relative, "DEFECT", "major")
                    continue
                target_record = by_path.get(relative)
                expected = row.get(hash_field)
                if not target_record or not target_record["sha256"] or target_record["status"] == "changed":
                    index_stats["references_unchecked"] += 1
                    finding("INDEX_REFERENCE_UNCHECKED", index_path, "Reference was not stably hashed in this scope: " + relative)
                elif expected is None:
                    index_stats["hashes_absent"] += 1
                elif not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
                    finding("INDEX_HASH_INVALID", index_path, "Invalid expected checksum for " + relative, "DEFECT", "major")
                elif target_record["sha256"] != expected.lower():
                    index_stats["hash_mismatches"] += 1
                    finding("INDEX_HASH_MISMATCH", index_path, "Stored checksum differs from bytes for " + relative, "DEFECT", "major")
                else:
                    index_stats["hashes_matched"] += 1

    email_metrics, links = analyze(records, finding)
    with (out / "evidence_links.jsonl").open("w", encoding="utf-8") as stream:
        for link in links:
            stream.write(json.dumps(link, ensure_ascii=False) + "\n")
    if not records:
        finding("EMPTY_SCOPE", ".", "No documentary files were discovered in the selected scope.")
    status_counts = Counter(record["status"] for record in records)
    hash_counts = Counter(record["sha256"] for record in records if record["sha256"])
    message_ids = [record["metrics"].get("message_id") for record in records if record["extension"] == ".eml"]
    fingerprint = source_fingerprint(scope, records)
    with (out / "inventory.jsonl").open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
    with (out / "findings.jsonl").open("w", encoding="utf-8") as stream:
        for item in findings:
            stream.write(json.dumps(item, ensure_ascii=False) + "\n")
    groups = defaultdict(list)
    for record in records:
        groups[record["collection"]].append(record)
    collections = []
    for name, rows in sorted(groups.items()):
        counts = Counter(row["status"] for row in rows)
        hashes = [row["sha256"] for row in rows if row["sha256"]]
        collections.append({"collection": name, "files": len(rows), "bytes": sum(row["bytes"] or 0 for row in rows),
                            "inspected": counts["inspected"], "invalid": counts["invalid"],
                            "unavailable": counts["unavailable"] + counts["changed"],
                            "duplicate_copies": len(hashes) - len(set(hashes))})
    incomplete = not discovery_complete or not records or any(f["kind"] == "NOT_CHECKED" for f in findings)
    by_extension = defaultdict(Counter)
    for record in records:
        by_extension[record["extension"]]["files"] += 1
        by_extension[record["extension"]][record["status"]] += 1
    measurement = {"schema_version": 2, "run_id": run_id, "measurer_version": VERSION, "evaluator": evaluator_identity(),
                   "measured_at": datetime.now(timezone.utc).isoformat(), "source_name": root.name, "source_root": str(root),
                   "source_fingerprint": fingerprint, "inventory_sha256": digest_file(out / "inventory.jsonl"),
                   "findings_sha256": digest_file(out / "findings.jsonl"),
                    "links_sha256": digest_file(out / "evidence_links.jsonl"),
                   "status": "partial" if incomplete else "complete", "scope": scope,
                   "limits": {"budget_seconds": budget_seconds, "file_timeout": file_timeout, "max_files": max_files, "max_file_mb": max_file_mb, "workers": workers},
                   "coverage": {"discovery_complete": discovery_complete, "files_discovered": len(records),
                                "files_hashed": sum(hash_counts.values()), "parser_cache_hits": cache_hits,
                                "status_counts": dict(status_counts), "excluded": dict(excluded),
                                "by_extension": {ext: dict(counts) for ext, counts in sorted(by_extension.items())}},
                   "metrics": {"unique_file_hashes": len(hash_counts), "exact_duplicate_copies": sum(hash_counts.values()) - len(hash_counts),
                               "eml_files": len(message_ids), "distinct_rfc_message_ids": len({value for value in message_ids if value}),
                               "eml_without_measured_id": sum(not value for value in message_ids)},
                    "email_indexes": dict(index_stats), "email_continuity": email_metrics, "collections": collections,
                   "finding_counts": dict(Counter(f["kind"] for f in findings)),
                   "limitations": ["No visual, OCR, business-state, authorship, or semantic review was performed.",
                                    "Email index hashes do not establish raw/text semantic agreement.",
                                    "Embedded attachments are hashed, not document-reviewed.",
                                    "Parsers read hashed temporary copies; the collection is not an atomic filesystem snapshot. Use verify for current bytes.",
                                    "The budget is checked during discovery, copying, parsing and index checks; final report writing follows."]}
    write_json(out / "measurement.json", measurement)
    write_json(out / "artifact_mining.json", {"status": "not_run", "material_points": None, "validated_task_count": None, "candidate_task_count": None, "tasks": []})
    collection_fields = ["collection", "files", "bytes", "inspected", "invalid", "unavailable", "duplicate_copies"]
    write_csv(out / "artifact_collections.csv", collections, collection_fields)
    dimensions = list(json.loads(RUBRIC_PATH.read_text())["dimensions"])
    summary = {"collection": root.name, "grade_status": "unscored", **{d: None for d in dimensions},
               "quality_score": None, "material_points": None, "validated_task_count": None}
    write_csv(out / "score-summary.csv", [summary], list(summary))
    exit_code = 1 if any(f["kind"] == "DEFECT" for f in findings) else 2 if incomplete else 0
    progress("reports_written", measurement_status=measurement["status"], exit_code=exit_code)
    return measurement, exit_code


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--probe-stream":
        for request in sys.stdin:
            print(json.dumps(probe(json.loads(request)), ensure_ascii=False), flush=True)
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "--probe-file":
        print(json.dumps(probe(sys.argv[2]), ensure_ascii=False))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root")
    parser.add_argument("--out", required=True)
    parser.add_argument("--profile", choices=["documents", "email", "mixed"], default="mixed")
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--budget-seconds", type=float, default=900)
    parser.add_argument("--file-timeout", type=float, default=30)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--max-files", type=int)
    parser.add_argument("--max-file-mb", type=float, default=100)
    args = parser.parse_args()
    try:
        result, code = measure(**vars(args))
        print(json.dumps({"status": result["status"], "coverage": result["coverage"], "findings": result["finding_counts"], "out": args.out}))
        return code
    except (ValueError, OSError) as exc:
        print("artifact-health: " + str(exc), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
