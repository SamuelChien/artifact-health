#!/usr/bin/env python3
"""Reusable Artifact Health API and command line, with explicit evidence states."""
import argparse
import hashlib
import json
import math
import re
import shutil
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from scripts import measure as scanner
from scripts import grade as grader
from scripts.relations import message_key
from scripts.evidence import (digest_file, evaluator_identity, load_bundle, load_lines,
                              read_object, validate_measurement, write_json)

ROOT = Path(__file__).resolve().parent
EXIT_CODES = {"passed": 0, "failed": 1, "incomplete": 2, "error": 3}


def check_environment():
    """Read dependency/capability state without starting a provider or installing anything."""
    identity = evaluator_identity()
    version = identity["pypdf"]
    pdf_ok = bool(version and int(version.split(".")[0]) >= 5)
    checks = [{"name": "python", "ok": sys.version_info >= (3, 10), "installed": identity["python"]},
              {"name": "pypdf", "ok": pdf_ok, "installed": version, "required_for": "PDF inspection"}]
    return {"status": "passed" if all(c["ok"] for c in checks) else "incomplete",
            "ready": all(c["ok"] for c in checks), "checks": checks, "evaluator": identity,
            "optional_capabilities": {"pdf_rendering": bool(shutil.which("pdftoppm")),
                                      "office_conversion": bool(shutil.which("soffice")),
                                      "ocr": bool(shutil.which("tesseract"))},
            "model_calls": False,
            "limitations": ["Optional tools are detected, not executed; the scanner does not automatically render or OCR.",
                            "Formats without an installed reader remain explicitly unmeasured."]}


def _grade_for_measurement(measurement, receipt, grade_directory):
    result, _ = load_bundle(grade_directory, "grade")
    if (result.get("measurement_run_id") != receipt["run_id"] or
            result.get("measurement_sha256") != receipt["reports"]["measurement.json"] or
            result.get("source_fingerprint") != measurement["source_fingerprint"] or
            result.get("inventory_sha256") != measurement["inventory_sha256"]):
        raise ValueError("grade and measurement belong to different runs")
    mining = read_object(Path(grade_directory) / "artifact_mining.json")
    material = read_object(Path(grade_directory) / "artifact_material.json")
    if any(result.get(key) != mining.get(key) for key in ("material_points", "validated_task_count")):
        raise ValueError("grade and task-mining counts disagree")
    if any(result.get(key) != material.get(key) for key in ("material_points", "validated_material_count")):
        raise ValueError("grade and independent material assessment disagree")
    return result, mining, material


def read_artifact_health(directory, grade_directory=None, require="full"):
    """Read retained evidence. A clean scanner run alone cannot establish full health."""
    if require not in {"mechanical", "quality", "full"}:
        raise ValueError("require must be mechanical, quality, or full")
    measurement, receipt, _, findings = validate_measurement(directory)
    quality, mining = None, read_object(Path(directory) / "artifact_mining.json")
    material = {"status": "not_run", "material_points": None, "validated_material_count": None}
    if grade_directory is not None:
        quality, mining, material = _grade_for_measurement(measurement, receipt, grade_directory)
    issues = []

    def issue(check, status, reason, count=None):
        issues.append({"check": check, "status": status, "reason": reason, **({"count": count} if count is not None else {})})

    dispositions = {d["finding_id"]: d["status"] for d in quality["dispositions"]} if quality else {}
    groups = Counter((f["kind"], f["code"]) for f in findings if f["kind"] != "REVIEW" or dispositions.get(f["id"]) != "explained")
    for (kind, code), count in sorted(groups.items()):
        if kind == "DEFECT":
            issue(code, "failed", "The measured files have a confirmed defect.", count)
        elif kind == "NOT_CHECKED":
            issue(code, "incomplete", "Required mechanical evidence is unavailable.", count)
        elif require != "mechanical":
            issue(code, "incomplete", "A contextual finding remains unexplained.", count)
    if measurement["status"] != "complete":
        issue("measurement", "incomplete", "The selected file inventory was not fully measured.")
    if quality:
        for finding in quality["findings"]:
            if finding["kind"] == "DEFECT" and finding["status"] == "confirmed":
                issue(finding["code"], "failed", finding["detail"])
        if quality["grade_status"] == "fail":
            issue("quality_gate", "failed", "The contextual grade retains a confirmed defect.")
    if require in {"quality", "full"}:
        if quality is None:
            issue("contextual_review", "incomplete", "No completed contextual grade was supplied.")
        elif quality["grade_status"] not in {"pass_within_scope", "fail"}:
            issue("contextual_review", "incomplete", "The contextual review is sampled, incomplete, or has unresolved findings.")
    if require == "full" and mining.get("status") != "complete":
        issue("task_mining", "incomplete", "The declared task-mining search has not completed.")
    if require == "full" and material.get("status") != "complete":
        issue("material_assessment", "incomplete", "The independent material assessment has not completed.")
    status = "failed" if any(i["status"] == "failed" for i in issues) else "incomplete" if issues else "passed"
    return {"schema": "artifact-health@2", "status": status, "required_evidence": require, "issues": issues,
            "run_id": receipt["run_id"], "source_fingerprint": measurement["source_fingerprint"],
            "source_name": measurement["source_name"], "source_freshness": "measured_snapshot_only",
            "measured_at": measurement["measured_at"], "evaluator": measurement["evaluator"],
            "scope": measurement["scope"], "limits": measurement["limits"], "measurement_status": measurement["status"],
            "coverage": measurement["coverage"], "metrics": measurement["metrics"], "email_indexes": measurement["email_indexes"],
            "email_continuity": measurement.get("email_continuity"),
            "finding_counts": measurement["finding_counts"], "quality": quality,
            "tasks": {key: mining.get(key) for key in ("status", "search_scope", "material_points", "validated_task_count", "candidate_task_count", "validated_by_family", "validated_by_depth", "exhaustive")},
            "material": {key: material.get(key) for key in ("status", "search_scope", "material_points", "validated_material_count", "candidate_material_count", "exhaustive")},
            "output_directory": str(Path(directory).resolve())}


def measure_collection(root, output=None, **options):
    root = Path(root).expanduser().resolve()
    if output is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output = ROOT.parent / "artifact-health-results" / root.name / f"{stamp}-{uuid4().hex[:8]}"
    scanner.measure(root, output, **options)
    return read_artifact_health(output)


def verify_source(directory, source=None, budget_seconds=900):
    """Compare current selected paths and bytes with a sealed measurement snapshot."""
    if not math.isfinite(budget_seconds) or budget_seconds <= 0:
        raise ValueError("verification budget must be finite and positive")
    measurement, receipt, records, _ = validate_measurement(directory)
    root = Path(source or measurement["source_root"]).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("source root is unavailable; supply its current location")
    deadline = time.monotonic() + budget_seconds
    files, _, complete, errors = scanner.discover_files(root, measurement["scope"], deadline)
    expected = {r["path"]: r for r in records}
    actual = dict(files)
    added = sorted(set(actual) - set(expected))
    removed = sorted(set(expected) - set(actual)) if complete else []
    changed, unknown = [], []
    for relative in sorted(set(expected) & set(actual)):
        record, path = expected[relative], actual[relative]
        if not record["sha256"] or record["status"] == "changed":
            unknown.append(relative)
            continue
        try:
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                changed.append(relative)
                continue
            before = path.stat()
            digest = digest_file(path, deadline)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                unknown.append(relative)
            elif digest != record["sha256"]:
                changed.append(relative)
        except (OSError, TimeoutError):
            unknown.append(relative)
    mismatch = bool(added or removed or changed)
    incomplete = bool(unknown or not complete or not measurement["coverage"]["discovery_complete"])
    status = "failed" if mismatch else "incomplete" if incomplete else "passed"
    return {"schema": "artifact-health-source-verification@2", "status": status,
            "measurement_run_id": receipt["run_id"], "source_fingerprint": measurement["source_fingerprint"],
            "matches": False if mismatch else None if incomplete else True,
            "added": added, "removed": removed, "changed": changed, "unverified": unknown,
            "discovery_errors": errors, "verified_at": datetime.now(timezone.utc).isoformat(),
            "limitations": ["Checks observed file bytes; this is not an atomic filesystem snapshot or a contextual re-review."]}


def prepare_review(directory, output, seed="artifact-health-v2", per_stratum=3):
    """Create a reproducible review queue; never prefill a passing rating."""
    if type(per_stratum) is not int or per_stratum < 1:
        raise ValueError("per_stratum must be a positive integer")
    measurement, receipt, records, findings = validate_measurement(directory)
    by_path = {r["path"]: r for r in records}
    strata = defaultdict(list)
    for row in records:
        strata[(row["collection"], row["extension"])].append(row["path"])
    selected, allocations = set(), []
    for (collection, extension), paths in sorted(strata.items()):
        groups = {}
        for path in sorted(paths):
            row = by_path[path]
            key = message_key(row) if extension == ".eml" and row["status"] == "inspected" else row["sha256"]
            groups.setdefault(key or path, path)
        ordered = sorted(groups, key=lambda key: hashlib.sha256((seed + "\0" + key).encode()).hexdigest())
        chosen = [groups[key] for key in ordered[:per_stratum]]
        selected.update(chosen)
        allocations.append({"collection": collection, "extension": extension, "eligible_files": len(paths),
                            "eligible_content_groups": len(groups), "selected_files": len(chosen)})
    representative = set(selected)
    suspicious = {f["path"] for f in findings if f["path"] in by_path}
    selected |= suspicious
    # Union all available email copies by RFC ID and reply/reference edges. Missing
    # parents remain explicit; complete context within this export is not a claim
    # that the original conversation is complete.
    parents = {}

    def find(node):
        parents.setdefault(node, node)
        root = node
        while parents[root] != root:
            root = parents[root]
        while parents[node] != node:
            previous = node
            node = parents[node]
            parents[previous] = root
        return root

    def union(a, b):
        parents[find(b)] = find(a)

    known_ids = {r["metrics"].get("message_id") for r in records if r["extension"] == ".eml" and r["metrics"].get("message_id")}
    missing = set()
    for row in records:
        if row["extension"] != ".eml":
            continue
        node = "file:" + row["path"]
        find(node)
        identifier = row["metrics"].get("message_id")
        if identifier:
            union(node, "id:" + identifier)
        refs = list(row["metrics"].get("references", []))
        reply = row["metrics"].get("in_reply_to")
        if reply:
            refs.extend(re.findall(r"<[^<>\s]+>", reply))
        for ref in refs:
            union(node, "id:" + ref)
            if ref not in known_ids:
                missing.add(ref)
    chosen_components = {find("file:" + path) for path in selected if by_path[path]["extension"] == ".eml"}
    selected |= {r["path"] for r in records if r["extension"] == ".eml" and find("file:" + r["path"]) in chosen_components}
    context = selected - representative - suspicious
    protocol = {"version": "artifact-health-selection@3", "method": "canonical_content_by_collection_and_extension_then_email_components",
                "seed": seed, "per_stratum": per_stratum, "include_findings": True}
    queue = [{"path": path, "sha256": by_path[path]["sha256"], "status": by_path[path]["status"],
              "review_units": by_path[path]["metrics"].get("review_units"),
              "reasons": [reason for reason, paths in (("representative", representative), ("finding", suspicious), ("conversation_context", context)) if path in paths]}
             for path in sorted(selected)]
    plan = {"schema": "artifact-health-review-plan@2", "measurement_run_id": receipt["run_id"],
            "source_fingerprint": measurement["source_fingerprint"], "selection_protocol": protocol,
            "strata": allocations, "queue": queue,
            "context_expansion_files": len(context),
            "missing_parent_ids": sorted(ref for ref in missing if find("id:" + ref) in chosen_components),
            "limitations": ["The sample is not a population prevalence estimate.",
                            "Email expansion covers available linked raw messages; unparsed messages and absent parents still need review.",
                            "Language, purpose, date, and native/scanned strata require reviewer annotation.",
                            "Selecting a path does not establish that its pages or parts were reviewed."]}
    rubric = read_object(scanner.RUBRIC_PATH)
    review = {"rubric_version": rubric["version"], "measurement_run_id": receipt["run_id"],
              "measurement_sha256": receipt["reports"]["measurement.json"], "findings_sha256": measurement["findings_sha256"],
              "source_fingerprint": measurement["source_fingerprint"], "inventory_sha256": measurement["inventory_sha256"],
              "reviewer": {"kind": "agent", "name": None, "model": None},
              "selection": "Reproducible review queue; reading and contextual review have not run.", "selection_protocol": protocol,
              "dimensions": {name: {"rating": None, "scope": "not_checked", "reviewed_paths": [], "evidence": [],
                                    "reason": "Review has not run."} for name in rubric["dimensions"]},
              "dispositions": [], "findings": [], "mining": {"status": "not_run", "search_scope": "", "tasks": []},
              "material": {"status": "not_run", "search_scope": "", "items": []}}
    out = scanner.new_output(output, measurement["source_root"])
    write_json(out / "plan.json", plan)
    write_json(out / "review.json", review)
    return {"status": "incomplete", "review_performed": False, "selected_files": len(queue),
            "context_expansion_files": len(context), "output_directory": str(out)}


def compare_runs(before, after, before_grade=None, after_grade=None):
    left = read_artifact_health(before, before_grade)
    right = read_artifact_health(after, after_grade)
    reasons = []
    for key in ("evaluator", "scope", "limits"):
        if left[key] != right[key]:
            reasons.append(key + " differs")
    if left["measurement_status"] != "complete" or right["measurement_status"] != "complete":
        reasons.append("measurement coverage is incomplete")
    mechanical_comparable = not reasons

    def deltas(a, b):
        return {key: {"before": a.get(key), "after": b.get(key),
                      "delta": b[key] - a[key] if type(a.get(key)) in (int, float) and type(b.get(key)) in (int, float) else None}
                for key in sorted(set(a) | set(b))}

    metrics = deltas(left["metrics"], right["metrics"])
    quality_reasons = list(reasons)
    lq, rq = left["quality"], right["quality"]
    if lq is None or rq is None:
        quality_reasons.append("one or both contextual grades are absent")
    else:
        for key in ("rubric_version", "acceptance_policy", "reviewer", "selection_protocol", "grader_evaluator"):
            if not lq.get(key) or not rq.get(key) or lq[key] != rq[key]:
                quality_reasons.append(key + " is missing or differs")
        if lq["quality_score"] is None or rq["quality_score"] is None:
            quality_reasons.append("whole-collection grade coverage is incomplete")
    quality_deltas = deltas({k: v["score"] for k, v in lq["factors"].items()} if lq else {},
                           {k: v["score"] for k, v in rq["factors"].items()} if rq else {})
    task_reasons = list(quality_reasons)
    if left["tasks"]["status"] != "complete" or right["tasks"]["status"] != "complete":
        task_reasons.append("task searches are incomplete")
    if not left["tasks"].get("search_scope") or left["tasks"].get("search_scope") != right["tasks"].get("search_scope"):
        task_reasons.append("task search scope differs or is absent")
    tasks = deltas({key: left["tasks"].get(key) for key in ("validated_task_count", "candidate_task_count")},
                   {key: right["tasks"].get(key) for key in ("validated_task_count", "candidate_task_count")})
    material_reasons = list(quality_reasons)
    if any(result["material"]["status"] != "complete" for result in (left, right)):
        material_reasons.append("material assessments are incomplete")
    if not left["material"].get("search_scope") or left["material"].get("search_scope") != right["material"].get("search_scope"):
        material_reasons.append("material search scope differs or is absent")
    material = deltas({key: left["material"].get(key) for key in ("material_points", "validated_material_count")},
                      {key: right["material"].get(key) for key in ("material_points", "validated_material_count")})
    for records, blocked in ((metrics, reasons), (quality_deltas, quality_reasons), (tasks, task_reasons), (material, material_reasons)):
        if blocked:
            for row in records.values():
                row["delta"] = None
    return {"schema": "artifact-health-comparison@2", "status": "passed" if mechanical_comparable else "incomplete",
            "before_status": left["status"], "after_status": right["status"],
            "mechanical": {"comparable": mechanical_comparable, "reasons": reasons, "metrics": metrics},
            "quality": {"protocol_matched": not quality_reasons, "score_comparable": False,
                        "reasons": quality_reasons, "descriptive_deltas": quality_deltas},
            "tasks": {"protocol_matched": not task_reasons, "reasons": task_reasons, "descriptive_deltas": tasks},
            "material": {"protocol_matched": not material_reasons, "reasons": material_reasons, "descriptive_deltas": material},
            "limitations": ["Quality weights remain provisional; matching protocol does not establish calibrated ranking validity.",
                            "Declining measurements are retained. Changed collection size and composition still need interpretation."]}


def export_summary(directory, grade_directory=None, output=None):
    """Allowlisted numeric/enum export. No paths, IDs, names, prose, or task descriptions."""
    health = read_artifact_health(directory, grade_directory)
    numeric = lambda value: value if type(value) in (int, float) and math.isfinite(value) else None
    quality = health["quality"]
    dimensions = read_object(scanner.RUBRIC_PATH)["dimensions"]
    result = {"schema": "artifact-health-external@2", "status": health["status"],
              "files_discovered": numeric(health["coverage"].get("files_discovered")),
              "files_hashed": numeric(health["coverage"].get("files_hashed")),
              "metrics": {key: numeric(health["metrics"].get(key)) for key in ("unique_file_hashes", "exact_duplicate_copies", "eml_files", "distinct_rfc_message_ids")},
              "quality_score": numeric(quality["quality_score"]) if quality else None,
              "factors": {key: numeric(quality["factors"][key]["score"]) if quality else None for key in dimensions},
              "material_points": numeric(health["material"].get("material_points")),
              "validated_material_count": numeric(health["material"].get("validated_material_count")),
              "validated_task_count": numeric(health["tasks"].get("validated_task_count")),
              "candidate_task_count": numeric(health["tasks"].get("candidate_task_count"))}
    if output is not None:
        target = Path(output).expanduser().absolute()
        measurement, _, _, _ = validate_measurement(directory)
        if target.resolve().is_relative_to(Path(measurement["source_root"]).resolve()):
            raise ValueError("external summary must be outside the source tree")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(result, indent=2, allow_nan=False) + "\n")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    measure = commands.add_parser("measure")
    measure.add_argument("root")
    measure.add_argument("--out", dest="output")
    measure.add_argument("--profile", choices=["mixed", "documents", "email"], default="mixed")
    measure.add_argument("--exclude", action="append", default=[])
    for flag, default in (("budget-seconds", 900), ("file-timeout", 30), ("max-file-mb", 100)):
        measure.add_argument("--" + flag, type=float, default=default)
    measure.add_argument("--max-files", type=int)
    measure.add_argument("--workers", type=int, default=1)
    batch = commands.add_parser("batch")
    batch.add_argument("root")
    batch.add_argument("--out", dest="output", required=True)
    batch.add_argument("--folders", nargs="+", required=True)
    batch.add_argument("--workers", type=int, default=6)
    batch.add_argument("--budget-seconds", type=float, default=7200)
    batch.add_argument("--file-timeout", type=float, default=30)
    batch.add_argument("--max-file-mb", type=float, default=512)
    refresh = commands.add_parser("batch-refresh")
    refresh.add_argument("output")
    census_parser = commands.add_parser("census")
    census_parser.add_argument("root")
    census_parser.add_argument("--folders", nargs="+", required=True)
    census_parser.add_argument("--out", required=True)
    workflow = commands.add_parser("workflow-grade")
    workflow.add_argument("run")
    workflow.add_argument("--protocol", required=True)
    workflow.add_argument("--review", required=True)
    workflow.add_argument("--out", required=True)
    workflow_compare = commands.add_parser("workflow-compare")
    workflow_compare.add_argument("before")
    workflow_compare.add_argument("after")
    summary = commands.add_parser("summarize")
    summary.add_argument("directory")
    summary.add_argument("--grade", dest="grade_directory")
    summary.add_argument("--require", choices=["mechanical", "quality", "full"], default="full")
    verify = commands.add_parser("verify")
    verify.add_argument("directory")
    verify.add_argument("--source")
    verify.add_argument("--budget-seconds", type=float, default=900)
    prepare = commands.add_parser("prepare-review")
    prepare.add_argument("directory")
    prepare.add_argument("--out", dest="output", required=True)
    prepare.add_argument("--seed", default="artifact-health-v2")
    prepare.add_argument("--per-stratum", type=int, default=3)
    grade = commands.add_parser("grade")
    grade.add_argument("run")
    grade.add_argument("--review", required=True)
    grade.add_argument("--out", required=True)
    compare = commands.add_parser("compare")
    compare.add_argument("before")
    compare.add_argument("after")
    compare.add_argument("--before-grade")
    compare.add_argument("--after-grade")
    export = commands.add_parser("export")
    export.add_argument("directory")
    export.add_argument("--grade", dest="grade_directory")
    export.add_argument("--out", dest="output")
    args = vars(parser.parse_args(argv))
    command = args.pop("command")
    from scripts.batch import measure_folders, refresh_batch
    from scripts.workflow import grade_workflows, compare_workflows
    from scripts.census import census
    handlers = {"doctor": check_environment, "measure": measure_collection, "summarize": read_artifact_health,
                "verify": verify_source, "prepare-review": prepare_review, "compare": compare_runs, "export": export_summary,
                "batch": measure_folders, "batch-refresh": refresh_batch,
                "workflow-grade": grade_workflows, "workflow-compare": compare_workflows, "census": census}
    try:
        if command == "grade":
            grader.grade(**args)
            result = read_artifact_health(args["run"], args["out"])
        else:
            result = handlers[command](**args)
        code = EXIT_CODES[result["status"]]
    except (ValueError, OSError, KeyError, TypeError) as exc:
        result, code = {"status": "error", "error": str(exc)}, 3
    print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
