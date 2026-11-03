"""Native evidence adapters. Conversions operate only on retained copies."""
import csv
import hashlib
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET

from .contracts import digest, normalized
from .store import write_json
from scripts.content_signals import office_metadata, origin_signals, pdf_metadata, invalid_reference

IMAGES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp", ".gif", ".heic", ".heif"}
OFFICE = {".docx", ".xlsx", ".pptx", ".odt"}
CONVERTIBLE = {".doc", ".xls", ".ppt", ".rtf"}
TEXT = {".txt", ".md", ".csv", ".tsv", ".json", ".html", ".htm"}
SUPPORTED = IMAGES | OFFICE | CONVERTIBLE | TEXT | {".pdf", ".eml", ".pages", ".numbers", ".key"}
VERSION = "native-evidence@1"
MAIL_KEYS = ("From", "To", "Cc", "Date", "Subject", "Message-ID")


def mail_projection(headers, text):
    return digest([{k:headers.get(k,"") for k in MAIL_KEYS}, text.replace("\r\n","\n").rstrip()])


class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts = []; self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}: self.hidden += 1
        if tag in {"p", "div", "br", "tr", "li"}: self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}: self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden: self.parts.append(data)


def plain_html(text):
    parser = PlainHTML(); parser.feed(text)
    return "".join(parser.parts)


def clean_env():
    return {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "SYSTEMROOT", "TMPDIR") if k in os.environ}


def command(args, timeout=120):
    return subprocess.run([str(a) for a in args], capture_output=True, timeout=timeout, env=clean_env(), check=True)


def decode(raw):
    for encoding in (["utf-16"] if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else ["utf-8-sig"]):
        try: return raw.decode(encoding), encoding
        except UnicodeError: pass
    from charset_normalizer import from_bytes
    match = from_bytes(raw).best()
    if not match or match.chaos > 0.1:
        raise ValueError("text encoding could not be established")
    return str(match), match.encoding


def split_text(text, maximum=10000):
    if not text: return [(0, "")]
    result, start = [], 0
    while start < len(text):
        end = min(len(text), start + maximum)
        if end < len(text):
            boundary = text.rfind("\n", start + maximum // 2, end)
            if boundary > start: end = boundary + 1
        result.append((start, text[start:end])); start = end
    return result


def provenance(path, metadata):
    result = {"business_origin": "unknown", "ai_involvement": "unknown", "asserted_metadata": metadata,
              "authenticated_creator": None, "c2pa": {"state": "not_present_or_not_checked"},
              "pdf_signatures": {"state": "not_present_or_not_checked"}}
    # Embedded credentials only. Lack of a credential is never a human-origin verdict.
    try:
        from c2pa import Reader, Context
        context = Context.from_dict({"verify": {"remote_manifest_fetch": False, "verify_cert_anchors": True}})
        with Reader(str(path), context=context) as reader:
            manifest = json.loads(reader.json())
            result["c2pa"] = {"state": "inspected", "manifest": manifest,
                              "trust_basis": "SDK local trust configuration; no factual-truth claim"}
    except ImportError:
        result["c2pa"] = {"state": "unavailable", "reason": "c2pa-python not installed"}
    except Exception as exc:
        absent=any(marker in str(exc).lower() for marker in ('manifestnotfound','no jumbf data found','manifest not found'))
        result["c2pa"] = {"state": "absent" if absent else "unavailable",
                          "reason": str(exc)[:300]}
    if path.suffix.lower() == ".pdf":
        try:
            from pyhanko.pdf_utils.reader import PdfFileReader
            from pyhanko.sign.validation import validate_pdf_signature
            from pyhanko_certvalidator import ValidationContext
            with path.open("rb") as stream:
                reader = PdfFileReader(stream)
                signatures = []
                for signature in reader.embedded_signatures:
                    status = validate_pdf_signature(signature, signer_validation_context=ValidationContext(allow_fetching=False))
                    signatures.append({"field": signature.field_name, "intact": status.intact,
                                       "cryptographically_valid": status.valid, "chain_trusted": status.trusted,
                                       "signing_certificate_subject":signature.signer_cert.subject.human_friendly if signature.signer_cert else None,
                                       "revocation_status":"not_established_offline",
                                       "summary": status.summary()})
                result["pdf_signatures"] = {"state": "inspected" if signatures else "absent", "signatures": signatures,
                                             "trust_basis": "local certificates; unavailable revocation information is not assumed valid"}
        except ImportError:
            result["pdf_signatures"] = {"state": "unavailable", "reason": "pyHanko not installed"}
        except Exception as exc:
            result["pdf_signatures"] = {"state": "unavailable", "reason": str(exc)[:300]}
    return result


def extract(path, artifact_id, work, options=None):
    path, work, options = Path(path), Path(work), options or {}
    work.mkdir(parents=True, exist_ok=True)
    result = {"schema": VERSION, "id": artifact_id, "status": "inspected", "units": [], "metadata": {},
              "findings": [], "attachments": [], "links": [], "representation": "native", "coverage": {}}

    def finding(code, detail, kind="REVIEW"):
        result["findings"].append({"code": code, "detail": detail, "kind": kind})

    def add(locator, text, **extra):
        text = str(text)
        for offset, chunk in split_text(text):
            position = {**locator, "text_offset": offset}
            identity = digest([artifact_id, position])[:24]
            unit = {"id": identity, "canonical_id": identity, "artifact_id": artifact_id,
                    "locator": position, "text": chunk, "characters":len(chunk), "status": "inspected", **extra}
            result["units"].append(unit)

    def render_pdf(pdf, prefix="page"):
        if not options.get("render", True):
            finding('VISUAL_REVIEW_DISABLED','PDF rendering was disabled for this run.','NOT_CHECKED');return []
        if not shutil.which("pdftoppm"):
            finding("RENDERER_UNAVAILABLE", "PDF visual review is unavailable.", "NOT_CHECKED"); return []
        try:
            command(["pdftoppm", "-r", "110", "-png", pdf, work / prefix], timeout=options.get("render_timeout", 180))
            return sorted(work.glob(prefix + "-*.png"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))
        except (OSError, subprocess.SubprocessError) as exc:
            finding("PDF_RENDER_INCOMPLETE", type(exc).__name__, "NOT_CHECKED")
            return []

    def ocr(image):
        if not shutil.which("tesseract"):
            finding("OCR_UNAVAILABLE", "Tesseract not installed.", "NOT_CHECKED"); return ""
        args = ["tesseract", image, "stdout", "-l", options.get("ocr_languages", "eng")]
        if options.get("tessdata"): args += ["--tessdata-dir", options["tessdata"]]
        try: return command(args, timeout=90).stdout.decode("utf-8")
        except (OSError, subprocess.SubprocessError) as exc:
            finding("OCR_INCOMPLETE", type(exc).__name__, "NOT_CHECKED"); return ""

    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(path, strict=False)
            if reader.is_encrypted and not reader.decrypt(""):
                raise ValueError("encrypted PDF needs a password")
            result["metadata"] = pdf_metadata(reader)
            images = render_pdf(path)
            result["coverage"] = {"native_pages": len(reader.pages), "rendered_pages": len(images)}
            for i, page in enumerate(reader.pages):
                text = page.extract_text() or ""
                image = str(images[i]) if i < len(images) else None
                mode = "native"
                if not text.strip() and image:
                    text, mode = ocr(image), "ocr"
                add({"kind": "page", "page": i + 1}, text, image=image, extraction=mode,
                    dimensions=[float(page.mediabox.width), float(page.mediabox.height)])
            if not reader.pages: finding("PDF_NO_PAGES", "PDF has no pages.", "DEFECT")
        elif ext in IMAGES:
            from PIL import Image, ExifTags, ImageOps
            if ext in {".heic", ".heif"}:
                from pillow_heif import register_heif_opener
                register_heif_opener(thumbnails=False)
            with Image.open(path) as picture:
                metadata = {ExifTags.TAGS.get(k, str(k)): str(v) for k,v in picture.getexif().items()
                            if ExifTags.TAGS.get(k) in {"Artist", "Software", "DateTime", "DateTimeOriginal", "OffsetTimeOriginal", "Make", "Model", "Orientation"}}
                result["metadata"] = {"asserted": metadata}
                for i in range(getattr(picture, "n_frames", 1)):
                    picture.seek(i); picture.load()
                    target = work / f"frame-{i+1}.png"
                    ImageOps.exif_transpose(picture).convert("RGB").save(target)
                    add({"kind": "frame", "frame": i + 1}, ocr(target), image=str(target), extraction="ocr",
                        width=picture.width, height=picture.height, visual_quality=None)
                result["coverage"] = {"frames": len(result["units"])}
        elif ext == ".xlsx":
            from openpyxl import load_workbook
            formulas = load_workbook(path, data_only=False, read_only=False, keep_links=False)
            values = load_workbook(path, data_only=True, read_only=False, keep_links=False)
            with zipfile.ZipFile(path) as archive: result["metadata"] = office_metadata(archive)
            for sheet in formulas:
                for row in sheet:
                    for cell in row:
                        if cell.value is None and not cell.comment: continue
                        value = values[sheet.title][cell.coordinate].value
                        value = value.isoformat() if isinstance(value, datetime) else value
                        formula = str(cell.value) if cell.data_type == "f" else None
                        display = value if value is not None else cell.value
                        defects = []
                        if formula and invalid_reference(formula):
                            defects.append("INVALID_FORMULA_REFERENCE")
                            finding("INVALID_FORMULA_REFERENCE", f"{sheet.title}!{cell.coordinate}: {formula}", "DEFECT")
                        if formula and value is None:
                            finding("FORMULA_CACHE_MISSING", f"{sheet.title}!{cell.coordinate}")
                        add({"kind": "cell", "sheet": sheet.title, "cell": cell.coordinate}, display,
                            value=value, formula=formula, hidden_sheet=sheet.sheet_state != "visible",
                            number_format=cell.number_format, comment=cell.comment.text if cell.comment else None,
                            business_defects=defects)
            formulas.close(); values.close()
            result["coverage"]["cells"] = len(result["units"])
            if options.get("render", True) and shutil.which("soffice"):
                try:
                    recalculated = _convert(path, work, "xlsx")
                    checked = load_workbook(recalculated, data_only=True, read_only=True, keep_links=False)
                    for unit in list(result["units"]):
                        if unit.get("formula"):
                            locator = unit["locator"]
                            value = checked[locator["sheet"]][locator["cell"]].value
                            add({"kind":"calculated_cell","sheet":locator["sheet"],"cell":locator["cell"]},value,
                                value=value,derivation="LibreOffice recalculation of retained source copy",source_unit=unit["id"])
                            if value != unit.get("value"):
                                finding("CACHED_RECALCULATED_VALUE_DIFFERS", f"{locator['sheet']}!{locator['cell']}: {unit.get('value')} -> {value}")
                    checked.close()
                    result["coverage"]["recalculation"] = "executed"
                except Exception as exc:
                    finding("RECALCULATION_UNAVAILABLE",str(exc)[:300],"NOT_CHECKED")
            _office_visual(path, work, result, add, render_pdf, options)
        elif ext in {".docx", ".pptx", ".odt"}:
            with zipfile.ZipFile(path) as archive:
                if sum(x.file_size for x in archive.infolist()) > 512 * 1024 * 1024:
                    raise ValueError("expanded document exceeds inspection limit")
                bad = archive.testzip()
                if bad: finding("OFFICE_CRC_FAILED", bad, "DEFECT")
                result["metadata"] = office_metadata(archive)
                patterns = {".docx": r"word/(document|header\d+|footer\d+|footnotes|endnotes|comments)\.xml",
                            ".pptx": r"ppt/(slides/slide\d+|notesSlides/notesSlide\d+)\.xml", ".odt": r"content\.xml"}
                for name in sorted(archive.namelist()):
                    if re.fullmatch(patterns[ext], name):
                        doc = ET.fromstring(archive.read(name))
                        for n, paragraph in enumerate(doc.iter()):
                            if paragraph.tag.rsplit("}",1)[-1] in {"p", "h"}:
                                text = "".join(paragraph.itertext())
                                if text.strip(): add({"kind": "paragraph", "part": name, "paragraph": n+1}, text)
                    elif name.endswith(".rels"):
                        for rel in ET.fromstring(archive.read(name)):
                            if rel.get("TargetMode") == "External":
                                add({"kind": "relationship", "part": name, "relationship": rel.get("Id")}, rel.get("Target", ""))
            _office_visual(path, work, result, add, render_pdf, options)
        elif ext in CONVERTIBLE:
            target = _convert(path, work, {".doc":"docx", ".xls":"xlsx", ".ppt":"pptx", ".rtf":"docx"}[ext])
            converted = extract(target, artifact_id, work / "converted", options)
            converted["representation"] = "converted_copy"
            converted["conversion"] = {"source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                       "output_sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "tool": "LibreOffice"}
            return converted
        elif ext in {".pages", ".numbers", ".key"}:
            with zipfile.ZipFile(path) as archive:
                previews = [n for n in archive.namelist() if n.lower().endswith("preview.pdf")]
                if not previews: raise ValueError("native iWork decoder unavailable; no full PDF preview")
                target = work / "preview.pdf"; target.write_bytes(archive.read(previews[0]))
                converted = extract(target, artifact_id, work / "preview", options)
                converted["representation"] = "embedded_preview"
                converted["findings"].append({"code":"NATIVE_CONTENT_UNCHECKED", "kind":"NOT_CHECKED", "detail":"Embedded preview does not establish all editable source content."})
                return converted
        elif ext == ".eml":
            message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
            headers = {k: str(message.get(k, "")) for k in ("From", "To", "Cc", "Date", "Subject", "Message-ID", "In-Reply-To", "References")}
            result["metadata"] = {"asserted": headers}
            available = None
            try:
                date = parsedate_to_datetime(headers["Date"])
                if date.tzinfo: available = date.astimezone(timezone.utc).isoformat()
            except (ValueError, TypeError, OverflowError): pass
            authored = []
            for i, part in enumerate(p for p in message.walk() if not p.is_multipart()):
                payload = part.get_payload(decode=True) or b""
                if part.get_filename() or part.get_content_disposition() == "attachment":
                    suffix = Path(part.get_filename() or "").suffix.lower()
                    target = work / ("attachment-" + hashlib.sha256(payload).hexdigest() + suffix)
                    target.write_bytes(payload)
                    result["attachments"].append({"path": str(target), "name": part.get_filename(),
                                                  "sha256": hashlib.sha256(payload).hexdigest(), "available_at": available, "part": i+1})
                elif part.get_content_maintype() == "text":
                    text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    if part.get_content_type() == "text/html": text = plain_html(text)
                    text=text.replace("\r\n","\n").rstrip()
                    authored.append([part.get_content_type(), normalized(text)])
                    add({"kind": "mime_part", "part": i+1}, text, available_at=available, availability_basis="asserted_native_mail_date",
                        headers=headers, extraction="native")
                    if part.get_content_type()=="text/plain":
                        result.setdefault("mail_projections",[]).append(mail_projection(headers,text))
            result["logical_id"] = digest([headers, authored, [a["sha256"] for a in result["attachments"]]])
            for unit in result["units"]:
                unit["canonical_id"] = digest([result["logical_id"], unit["locator"]])
            result["links"] = [{"kind":"reply_to", "message_id":v} for v in re.findall(r"<[^<>]+>", headers["In-Reply-To"] or headers["References"])[-1:]]
        elif ext in TEXT:
            text, encoding = decode(path.read_bytes()); result["metadata"]["encoding"] = encoding
            if ext in {".html", ".htm"}: text = plain_html(text)
            if ext == ".json":
                data = json.loads(text)
                result["metadata"]["json_shape"] = {"type":type(data).__name__, "size":len(data) if isinstance(data,(dict,list)) else None}
                if isinstance(data, dict) and isinstance(data.get("messages"), list) and any(
                        isinstance(row,dict) and any(key in row for key in ("eml_file","text_file","original_file")) for row in data["messages"]):
                    result["administrative_index"] = True
                if isinstance(data,dict) and isinstance(data.get("files"),list) and data["files"] and all(
                        isinstance(r,dict) and "path" in r and "sha256" in r for r in data["files"]):
                    result["administrative_index"] = True
                if result.get("administrative_index"):
                    index=work/"index-records.jsonl"
                    with index.open('w') as stream:
                        for row in data.get("messages",data.get("files",[])):stream.write(json.dumps(row,ensure_ascii=False)+'\n')
                    result["index_records"]=str(index)
                if isinstance(data, (dict,list)):
                    for key, value in (data.items() if isinstance(data,dict) else enumerate(data)):
                        # Large export indexes are verified structurally and linked to their source records.
                        if result.get("administrative_index") and key in {"messages","files"}:
                            add({"kind":"json_pointer", "pointer":"/"+key}, json.dumps({"records":len(value),"content_review":"linked_source_records"})); continue
                        add({"kind":"json_pointer", "pointer":"/"+str(key).replace("~","~0").replace("/","~1")}, json.dumps(value,ensure_ascii=False))
                else: add({"kind":"json_pointer", "pointer":""}, text)
            elif ext in {".csv", ".tsv"}:
                for i,row in enumerate(csv.reader(io.StringIO(text),delimiter="\t" if ext==".tsv" else ",")):
                    add({"kind":"row", "row":i+1}, json.dumps(row,ensure_ascii=False), values=row)
            elif ext==".txt" and re.match(r"(?:Date|From|Subject|To|Message-ID):",text):
                message=BytesParser(policy=policy.default).parsebytes(text.encode('utf-8'))
                headers={k:str(message.get(k,'')) for k in MAIL_KEYS}
                if headers['Message-ID'] and headers['From'] and headers['Date'] and '\n\n' in text:
                    body=text.partition('\n\n')[2].replace('\r\n','\n').rstrip()
                    result['mail_text_projection']=mail_projection(headers,body)
                    result['metadata']['asserted']=headers
                    add({'kind':'mail_text_copy'},body,headers=headers)
                else:add({'kind':'text'},text)
            else: add({"kind":"text"},text)
        else:
            raise ValueError("unsupported native format")
        result["origin_signals"] = origin_signals("\n".join(u["text"] for u in result["units"]))
        result["provenance"] = provenance(path, result["metadata"]) if ext in IMAGES | {".pdf"} else {
            "business_origin":"unknown", "ai_involvement":"unknown", "asserted_metadata":result["metadata"], "authenticated_creator":None}
        result.setdefault("logical_id", artifact_id)
        if result['metadata'].get('metadata_date_order')=='modified_before_created':
            finding('METADATA_DATE_ORDER','Asserted modification date precedes creation date; provenance context is required.')
        for key in ('created_utc','modified_utc'):
            value=result['metadata'].get(key)
            if value:
                try:
                    if datetime.fromisoformat(value)>datetime.now(timezone.utc):
                        finding('METADATA_FUTURE_DATE',f'{key} is later than inspection time; a clock, metadata edit or provenance explanation is needed.')
                except ValueError:pass
    except Exception as exc:
        result["status"] = "unavailable"
        finding("NATIVE_EXTRACTION_UNAVAILABLE", type(exc).__name__ + ": " + str(exc)[:500], "NOT_CHECKED")
    return result


def _convert(path, work, extension):
    if not shutil.which("soffice"): raise ValueError("LibreOffice conversion is unavailable")
    target = work / "office-conversion"; target.mkdir(exist_ok=True)
    profile = work / "lo-profile"
    user=profile/'user';user.mkdir(parents=True,exist_ok=True)
    (user/'registrymodifications.xcu').write_text('''<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry"><item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item><item oor:path="/org.openoffice.Office.Calc/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>2</value></prop></item></oor:items>''')
    command(["soffice", "-env:UserInstallation=" + profile.as_uri(), "--headless", "--convert-to", extension,
             "--outdir", target, path], timeout=120)
    converted = target / (path.stem + "." + extension)
    if not converted.exists(): raise ValueError("Office conversion produced no output")
    return converted


def _office_visual(path, work, result, add, render_pdf, options):
    if not options.get("render", True):
        result['findings'].append({'code':'VISUAL_REVIEW_DISABLED','kind':'NOT_CHECKED','detail':'Office rendering was disabled for this run.'});return
    try:
        pdf = _convert(path, work, "pdf")
        images = render_pdf(pdf, "office-page")
        result["coverage"]["rendered_pages"] = len(images)
        for i,image in enumerate(images):
            add({"kind":"rendered_page", "page":i+1}, "", image=str(image), extraction="rendered_native_document")
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        result["findings"].append({"code":"OFFICE_VISUAL_UNAVAILABLE", "kind":"NOT_CHECKED", "detail":str(exc)[:300]})


if __name__ == "__main__":
    import contextlib
    for line in sys.stdin:
        try:
            request = json.loads(line)
            with contextlib.redirect_stdout(io.StringIO()):
                result = extract(**request)
        except Exception as exc:
            result = {"status":"unavailable", "units":[], "findings":[{"kind":"NOT_CHECKED","code":"WORKER_ERROR","detail":str(exc)}]}
        print(json.dumps(result,ensure_ascii=False,default=str), flush=True)
