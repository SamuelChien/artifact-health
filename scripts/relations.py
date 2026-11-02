"""Conservative email identity, reply continuity and attachment evidence links."""
import hashlib
import json
from collections import defaultdict, deque
from datetime import datetime


def message_key(record):
    metrics = record["metrics"]
    if metrics.get("message_id") and metrics.get("content_sha256"):
        return hashlib.sha256(json.dumps([metrics["message_id"], metrics["content_sha256"]]).encode()).hexdigest()
    return record["sha256"]


def analyze(records, finding):
    mails = [r for r in records if r["extension"] == ".eml" and r["status"] == "inspected"]
    groups, by_id, by_hash = {}, defaultdict(list), defaultdict(list)
    links = []
    for record in records:
        if record["sha256"] and record["status"] != "changed" and record["extension"] != ".eml":
            by_hash[record["sha256"]].append(record)
    for record in mails:
        groups.setdefault(message_key(record), record)
    for key, record in groups.items():
        identifier = record["metrics"].get("message_id")
        if identifier:
            by_id[identifier].append(key)
    variants = sum(len(keys) > 1 for keys in by_id.values())
    for keys in by_id.values():
        if len(keys) > 1:
            record = groups[keys[0]]
            finding("EMAIL_ID_CONTENT_VARIANTS", record["path"],
                    "One Message-ID has multiple authored-content variants; preserve and review them.",
                    "REVIEW", "major", record["sha256"])
    parent = {key: key for key in groups}

    def root(key):
        parent.setdefault(key, key)
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    edges, missing = defaultdict(set), set()
    chronology = attachment_matches = 0
    for key, record in groups.items():
        metrics = record["metrics"]
        reply_ids = metrics.get("reply_ids") or metrics.get("references", [])[-1:]
        for identifier in sorted(set(reply_ids)):
            targets = by_id.get(identifier, [])
            if not targets:
                parent[root(key)] = root("missing:" + identifier)
                missing.add(key)
                finding("EMAIL_PARENT_NOT_IN_SCOPE", record["path"],
                        "A referenced parent is absent from inspected messages; the conversation may extend beyond this scope.",
                        "REVIEW", "minor", record["sha256"])
            for target_key in targets:
                target = groups[target_key]
                parent[root(key)] = root(target_key)
                edges[target_key].add(key)
                links.append({"kind": "reply_to", "source": record["path"], "source_sha256": record["sha256"],
                              "target": target["path"], "target_sha256": target["sha256"]})
                child_date, parent_date = metrics.get("date_utc"), target["metrics"].get("date_utc")
                if child_date and parent_date and datetime.fromisoformat(child_date) < datetime.fromisoformat(parent_date):
                    chronology += 1
                    finding("EMAIL_REPLY_PRECEDES_PARENT", record["path"],
                            "Native dates put this reply before its parent; review clocks, exports and context.",
                            "REVIEW", "major", record["sha256"])
        seen = set()
        for attachment in metrics.get("named_or_attachment_parts", []):
            digest = attachment["sha256"]
            if digest in seen or digest not in by_hash:
                continue
            seen.add(digest)
            target = by_hash[digest][0]
            attachment_matches += 1
            links.append({"kind": "attachment_matches_document", "source": record["path"],
                          "source_sha256": record["sha256"], "part": attachment["part"],
                          "attachment_sha256": digest, "target": target["path"],
                          "target_sha256": target["sha256"], "document_copy_count": len(by_hash[digest])})
    indegree = dict.fromkeys(groups, 0)
    for targets in edges.values():
        for key in targets:
            indegree[key] += 1
    ready = deque(key for key, count in indegree.items() if count == 0)
    removed = 0
    while ready:
        key = ready.popleft()
        removed += 1
        for child in edges.get(key, ()):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    cycle = removed != len(groups)
    if cycle:
        finding("EMAIL_REPLY_CYCLE", ".", "The observed reply graph contains a cycle; inspect identity variants and reply references.",
                "REVIEW", "major")
    return {
        "inspected_message_copies": len(mails), "message_content_groups": len(groups),
        "duplicate_message_copies": len(mails) - len(groups), "same_id_variant_groups": variants,
        "observed_conversations": len({root(key) for key in groups}),
        "conversations_with_missing_parents": len({root(key) for key in missing}),
        "reply_chronology_flags": chronology, "reply_cycle_detected": cycle,
        "attachment_document_matches": attachment_matches,
        "identity_basis": "Message-ID plus decoded authored content; exact bytes when ID is absent",
        "conversation_completeness": "not_established",
    }, links
