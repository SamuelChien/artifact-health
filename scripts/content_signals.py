"""Descriptive native-format evidence. No inference of human authorship."""
import re
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp", ".gif"}
AI_DECLARATION = re.compile(r"\b(?:AI[- ]generated|generated (?:by|with) (?:ChatGPT|GPT[- ]?\d|Claude|Codex)|synthetic (?:data|document|scenario)|AI[- ]assisted)\b", re.I)


def invalid_reference(formula):
    # Excel strings and quoted worksheet names may legitimately contain '#REF!'.
    unquoted = re.sub(r'"(?:[^"]|"")*"|\'(?:[^\']|\'\')*\'', "", formula or "")
    return "#REF!" in unquoted.upper()


def origin_signals(text, metadata=None):
    """Declarations are evidence to review, never a calibrated classifier."""
    text = text or ""
    hits = [{"offset": m.start(), "text": m.group(0)} for m in AI_DECLARATION.finditer(text)]
    return {"explicit_declaration_hits": hits[:30], "declaration_count": len(hits),
            "characters_checked": len(text), "authorship_verdict": "unknown",
            "metadata_identity_is_asserted": True,
            "limitation": "Declarations and metadata need provenance review; absence is not evidence of human origin."}


def pdf_metadata(reader):
    info = reader.metadata or {}
    result = {key.lstrip("/"): str(info[key]) for key in
              ("/Author", "/Creator", "/Producer", "/CreationDate", "/ModDate", "/Title") if info.get(key)}
    dates = {}
    for key, attribute in (("created_utc", "creation_date"), ("modified_utc", "modification_date")):
        try:
            value = getattr(reader.metadata, attribute, None)
            dates[key] = value.astimezone(timezone.utc).isoformat() if value and value.tzinfo else None
        except (ValueError, TypeError, OverflowError):
            dates[key] = None
    order = "unknown" if not all(dates.values()) else "modified_before_created" if dates["modified_utc"] < dates["created_utc"] else "ordered"
    return {"asserted": result, **dates, "metadata_date_order": order, "authenticated_creator": None, "signature_verified": False,
            "timestamp_basis": "document metadata; not a business event or availability proof"}


def office_metadata(archive):
    result = {}
    for part in ("docProps/core.xml", "docProps/app.xml"):
        if part not in archive.namelist():
            continue
        node = ET.fromstring(archive.read(part))
        for child in node:
            name = child.tag.rsplit("}", 1)[-1]
            if name in {"creator", "lastModifiedBy", "created", "modified", "title", "Application", "AppVersion"}:
                result[name] = child.text
    temporal = "unknown"
    try:
        start = datetime.fromisoformat(result["created"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(result["modified"].replace("Z", "+00:00"))
        if start.tzinfo and end.tzinfo:
            temporal = "modified_before_created" if end < start else "ordered"
    except (KeyError, AttributeError, ValueError, TypeError):
        pass
    return {"asserted": result, "authenticated_creator": None, "signature_verified": False,
            "metadata_date_order": temporal, "timestamp_basis": "save metadata; not business event time"}


def image_metrics(path):
    from PIL import Image, ExifTags
    with Image.open(path) as picture:
        picture.verify()
    with Image.open(path) as picture:
        # Decode every frame, not only a header. Pillow's decompression-bomb guard remains active.
        frames = getattr(picture, "n_frames", 1)
        primary = {"width_pixels": picture.width, "height_pixels": picture.height,
                   "mode": picture.mode, "image_format": picture.format}
        exif = {ExifTags.TAGS.get(key, str(key)): str(value) for key, value in picture.getexif().items()
                if ExifTags.TAGS.get(key) in {"Artist", "Software", "DateTime", "DateTimeOriginal", "Make", "Model"}}
        frame_details = []
        for frame in range(frames):
            picture.seek(frame)
            picture.load()
            frame_details.append({"frame": frame + 1, "width_pixels": picture.width,
                                  "height_pixels": picture.height, "mode": picture.mode})
        return {**primary, "frames": frames, "frame_details": frame_details, "asserted_exif": exif,
                "review_units": {"kind": "frame", "count": frames},
                "visual_quality": None, "subject_accuracy": None,
                "quality_note": "Successful decoding and pixel dimensions do not establish legibility, subject accuracy or photographic origin."}
