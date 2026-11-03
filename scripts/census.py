"""Complete path inventory and explicit, unverified email-index claims.

No semantic quality score is assigned by directory enumeration or index fields.
"""
import json
import os
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from .evidence import digest_file, write_json
from .measure import EXTENSIONS, SKIP_DIRS, new_output


def census(root, folders, out):
    root = Path(root).resolve()
    if not folders or len(folders) != len(set(folders)):
        raise ValueError("select distinct source folders")
    selected = []
    for name in folders:
        path = root / name
        if Path(name).name != name or path.is_symlink() or not path.is_dir():
            raise ValueError("folders must be existing immediate directories without symlinks")
        selected.append(path)
    output_path = Path(out).resolve()
    if any(output_path.is_relative_to(p.resolve()) for p in selected):
        raise ValueError("census output must be outside every selected folder")
    output = new_output(output_path)
    counts, sizes = {}, {}
    errors, indexes, symlinks = [], [], []
    with (output / "paths.jsonl").open("x", encoding="utf-8") as stream:
        for folder in selected:
            extensions, byte_counts = Counter(), Counter()
            def error(exc):
                errors.append({"path": exc.filename, "error": type(exc).__name__})
            for parent, dirs, files in os.walk(folder, onerror=error):
                for name in sorted(dirs):
                    p = Path(parent) / name
                    if p.is_symlink():
                        symlinks.append(str(p.relative_to(root)))
                dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not (Path(parent) / d).is_symlink())
                for name in sorted(files):
                    path = Path(parent) / name
                    if path.is_symlink():
                        symlinks.append(str(path.relative_to(root)))
                        continue
                    try:
                        stat = path.stat()
                    except OSError as exc:
                        error(exc)
                        continue
                    ext = path.suffix.lower() or "[none]"
                    extensions[ext] += 1
                    byte_counts[ext] += stat.st_size
                    rel = str(path.relative_to(root))
                    stream.write(json.dumps({"path": rel, "extension": ext, "bytes": stat.st_size,
                        "scanner_supported": ext in EXTENSIONS, "temporary_office_lock": name.startswith("~$")}, ensure_ascii=False) + "\n")
                    if folder.name == "email" and name == "index.json" and path.parent.parent == folder:
                        indexes.append(path)
            counts[folder.name], sizes[folder.name] = dict(extensions), dict(byte_counts)
    email = {"mailbox_indexes": 0, "indexed_copies": 0, "distinct_message_ids": 0, "distinct_claimed_hashes": 0,
             "unknown_dates": 0, "future_dates": 0, "date_min": None, "date_max": None,
             "raw_eml_verified": False, "count_basis": "retained index claims; IDs/hashes not a canonical-message or task count", "index_bindings": []}
    ids, hashes, dates = set(), set(), []
    now = datetime.now(timezone.utc)
    for path in indexes:
        try:
            data = json.loads(path.read_text())
            messages = data.get("messages")
            if not isinstance(messages, list):
                continue
            email["mailbox_indexes"] += 1
            email["index_bindings"].append({"path": str(path.relative_to(root)), "sha256": digest_file(path), "copies": len(messages)})
            email["indexed_copies"] += len(messages)
            for msg in messages:
                mid = msg.get("headers", {}).get("Message-ID")
                if mid: ids.add(mid)
                if msg.get("sha256"): hashes.add(msg["sha256"])
                try:
                    date = parsedate_to_datetime(msg.get("headers", {}).get("Date", ""))
                    if not date.tzinfo: raise ValueError("no timezone")
                    date = date.astimezone(timezone.utc)
                    dates.append(date)
                    email["future_dates"] += date > now
                except (ValueError, TypeError, OverflowError):
                    email["unknown_dates"] += 1
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            errors.append({"path": str(path.relative_to(root)), "error": type(exc).__name__})
    email.update(distinct_message_ids=len(ids), distinct_claimed_hashes=len(hashes),
                 date_min=min(dates).isoformat() if dates else None, date_max=max(dates).isoformat() if dates else None)
    total = Counter()
    for c in counts.values(): total.update(c)
    summary = {"schema": "artifact-path-census@1", "status": "incomplete" if errors else "passed",
               "source_root": str(root), "folders": folders, "counts_by_folder": counts,
               "bytes_by_folder": sizes, "counts_by_extension": dict(total), "files": sum(total.values()),
               "format_count": len(total), "documentary_files": sum(n for e,n in total.items() if e in EXTENSIONS),
               "symlinks_skipped": symlinks, "errors": errors, "email_index_claims": email,
               "paths_sha256": digest_file(output / "paths.jsonl"), "measured_at": now.isoformat(),
               "limitations": ["Full path/size inventory; file bytes are not all hashed or parsed.",
                   "Semantic document shapes, task counts and photo quality require actual content review.",
                   "Email index metadata is not independently authenticated; future dates are clues, not proof of fabrication.",
                   "Operating-system and temporary lock files remain visible; they are not automatically business documents."]}
    write_json(output / "census.json", summary)
    write_json(output / "run.json", {"schema": "artifact-census-receipt@1", "state": "completed", "reports": {
        name: digest_file(output / name) for name in ("census.json", "paths.jsonl")}})
    return summary
