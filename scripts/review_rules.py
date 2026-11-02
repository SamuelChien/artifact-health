"""Evidence coverage, content identity and temporal checks for contextual reviews."""
from datetime import datetime

if __package__:
    from .relations import message_key
else:
    from relations import message_key


def require(condition, message):
    if not condition:
        raise ValueError(message)


def merge_ranges(ranges):
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    return merged


def unit_spec(row):
    spec = row["metrics"].get("review_units")
    return spec if spec and spec.get("count", 0) > 0 else {"kind": "file", "count": 1}


def coverage(evidence, by_path, *, usable=False, blocked=(), visual_only=False):
    selected = {}
    for item in evidence:
        row = by_path[item["path"]]
        if usable:
            require(row["status"] == "inspected" and row["path"] not in blocked,
                    "validated material/tasks need inspected evidence without blocking defects or missing checks")
        units = item.get("units")
        spec = unit_spec(row)
        require(isinstance(units, dict) and units.get("kind") == spec["kind"], "evidence needs measured unit kind: " + row["path"])
        ranges = units.get("ranges")
        require(isinstance(ranges, list) and ranges, "evidence needs nonempty unit ranges")
        for span in ranges:
            require(isinstance(span, list) and len(span) == 2 and all(type(n) is int for n in span)
                    and 1 <= span[0] <= span[1] <= spec["count"], "evidence range exceeds measured units")
        require(item.get("mode") in {"visual", "text", "structure"}, "evidence needs visual, text or structure review mode")
        if not visual_only or item["mode"] == "visual":
            selected.setdefault(row["path"], []).extend(ranges)
    return {path: merge_ranges(ranges) for path, ranges in selected.items()}


def canonical_units(evidence, by_path, *, blocked=()):
    selected = coverage(evidence, by_path, usable=True, blocked=blocked)
    canonical = {}
    for path, ranges in selected.items():
        row = by_path[path]
        attachments = []
        if row["extension"] == ".eml":
            for attachment in row["metrics"].get("named_or_attachment_parts", []):
                part = attachment["part"]
                if any(start <= part <= end for start, end in ranges):
                    matches = [other for other in selected if by_path[other]["extension"] != ".eml"
                               and by_path[other]["sha256"] == attachment["sha256"]
                               and selected[other] == [[1, unit_spec(by_path[other])["count"]]]]
                    require(matches, "attachment payload needs separate inspected document evidence covering its full contents")
                    attachments.append(part)
            # The separately inspected document already contributes these bytes.
            for part in attachments:
                ranges = [span for start, end in ranges for span in
                          ([[start, end]] if not start <= part <= end else
                           ([[start, part - 1]] if start < part else []) + ([[part + 1, end]] if part < end else []))]
        if not ranges:
            continue
        identity = message_key(row) if row["extension"] == ".eml" else row["sha256"]
        key = (identity, unit_spec(row)["kind"])
        canonical.setdefault(key, []).extend(ranges)
    return tuple((key, tuple(tuple(span) for span in merge_ranges(ranges)))
                 for key, ranges in sorted(canonical.items()))


def overlaps(left, right):
    right = dict(right)
    return any(max(a, c) <= min(b, d)
               for key, ranges in left for a, b in ranges for c, d in right.get(key, ()))


def timestamp(value):
    require(isinstance(value, str) and value.strip(), "temporal evidence needs an ISO timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(result.tzinfo is not None and result.utcoffset() is not None, "temporal timestamps require a timezone")
    return result


def check_cutoff(evidence, by_path, cutoff):
    if cutoff is None:
        return
    cutoff = timestamp(cutoff)
    for item in evidence:
        dates = [value for value in (by_path[item["path"]]["metrics"].get("date_utc"), item.get("available_at")) if value]
        require(dates, "historical task inputs need known availability times")
        require(max(timestamp(value) for value in dates) <= cutoff, "task input was unavailable at its temporal cutoff")
