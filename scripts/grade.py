#!/usr/bin/env python3
"""Validate a contextual review against a measured snapshot and calculate scores."""
import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

if __package__:
    from .measure import RUBRIC_PATH, digest_file, new_output, write_csv, write_json
    from .evidence import load_lines, validate_measurement, run_receipt, evaluator_identity
    from .review_rules import coverage, unit_spec, canonical_units, overlaps, check_cutoff, timestamp
else:
    from measure import RUBRIC_PATH, digest_file, new_output, write_csv, write_json
    from evidence import load_lines, validate_measurement, run_receipt, evaluator_identity
    from review_rules import coverage, unit_spec, canonical_units, overlaps, check_cutoff, timestamp


def require(condition, message):
    if not condition:
        raise ValueError(message)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def grade(run, review, out):
    run = Path(run).resolve()
    measurement, receipt, inventory, scanner_findings = validate_measurement(run)
    rubric = json.loads(RUBRIC_PATH.read_text(encoding="utf-8"))
    require(digest_file(RUBRIC_PATH) == measurement["evaluator"]["rubric_sha256"], "rubric changed after measurement; remeasure with the current rubric")
    by_path = {row["path"]: row for row in inventory}
    require(len(by_path) == len(inventory), "inventory contains duplicate paths")
    review = json.loads(Path(review).read_text(encoding="utf-8"))
    require(isinstance(review, dict), "review must be an object")
    for field in ("source_fingerprint", "inventory_sha256"):
        require(review.get(field) == measurement[field], field + " does not match the measured snapshot")
    require(review.get("measurement_run_id") == receipt["run_id"], "review belongs to a different measurement run")
    require(review.get("measurement_sha256") == receipt["reports"]["measurement.json"], "review does not bind the measurement report")
    require(review.get("findings_sha256") == measurement["findings_sha256"], "review does not bind the measured findings")
    require(review.get("rubric_version") == rubric["version"], "rubric version does not match")
    reviewer = review.get("reviewer", {})
    require(isinstance(reviewer, dict) and reviewer.get("kind") in {"agent", "human"} and nonempty(reviewer.get("name")), "reviewer needs kind and name")
    if reviewer["kind"] == "agent":
        require(nonempty(reviewer.get("model")), "agent reviewer needs its model/version")
    require(nonempty(review.get("selection")), "review selection method is required")

    def evidence_paths(evidence, required=True):
        require(isinstance(evidence, list), "evidence must be a list")
        require(not required or evidence, "evidence must be nonempty")
        paths = set()
        for item in evidence:
            require(isinstance(item, dict), "evidence entries must be objects")
            path = item.get("path")
            require(isinstance(path, str) and path in by_path, "evidence path is absent from the inventory")
            row = by_path[path]
            require(row["sha256"] is not None and item.get("sha256") == row["sha256"], "evidence hash does not match")
            require(row["status"] != "changed", "evidence changed during measurement")
            require(nonempty(item.get("locator")), "evidence needs a page, part, or other specific locator")
            paths.add(path)
        return paths

    dimensions = review.get("dimensions")
    require(isinstance(dimensions, dict) and set(dimensions) == set(rubric["dimensions"]), "provide exactly the six rubric dimensions")
    all_scored, all_full = True, bool(inventory)
    observed = 0.0
    factors = {}
    for name, definition in rubric["dimensions"].items():
        dimension = dimensions[name]
        require(isinstance(dimension, dict), "dimension must be an object: " + name)
        rating = dimension.get("rating")
        scope = dimension.get("scope")
        require(scope in {"full", "sample", "not_checked"}, "invalid dimension scope: " + name)
        require(rating is None or type(rating) is int and 0 <= rating <= 4, "rating must be null or an integer from 0 to 4")
        require(nonempty(dimension.get("reason")), "dimension needs a reason: " + name)
        paths = dimension.get("reviewed_paths")
        require(isinstance(paths, list) and all(isinstance(path, str) for path in paths), "reviewed_paths must be a string list")
        require(len(set(paths)) == len(paths) and set(paths) <= set(by_path), "invalid or duplicate reviewed paths")
        referenced = evidence_paths(dimension.get("evidence"), required=rating is not None)
        require(set(paths) == referenced, "reviewed_paths must exactly match the evidence paths")
        covered = coverage(dimension["evidence"], by_path)
        if rating is None:
            require(scope == "not_checked", "missing rating requires not_checked scope")
            all_scored = False
        else:
            require(scope != "not_checked" and paths, "a rating requires actual reviewed paths")
            observed += definition["weight"] * rating / 4
        if scope == "full":
            require(set(paths) == set(by_path), "full scope must cover every inventory path")
            require(all(covered.get(path) == [[1, unit_spec(row)["count"]]] for path, row in by_path.items()),
                    "full scope must cover every measured page or part, not just every filename")
        else:
            all_full = False
        if name == "communication" and rating is not None:
            visual = coverage(dimension["evidence"], by_path, visual_only=True)
            for path in paths:
                if by_path[path]["extension"] in {".pdf", ".docx", ".xlsx", ".pptx", ".odt", ".html", ".htm"}:
                    require(visual.get(path) == covered[path], "presentation ratings need visual evidence for the reviewed units")
        factors[name] = {"rating": rating, "score": None if rating is None else rating * 25,
                         "weight": definition["weight"], "scope": scope, "files_reviewed": len(paths),
                         "unit_coverage": {path: {"kind": unit_spec(by_path[path])["kind"],
                              "reviewed": sum(end - start + 1 for start, end in spans),
                              "eligible": unit_spec(by_path[path])["count"], "ranges": spans}
                              for path, spans in covered.items()}}
        reviewed_counts, eligible_counts = Counter(), Counter()
        for path, spans in covered.items():
            spec = unit_spec(by_path[path])
            reviewed_counts[spec["kind"] + "s"] += sum(end - start + 1 for start, end in spans)
            eligible_counts[spec["kind"] + "s"] += spec["count"]
        factors[name].update(units_reviewed=dict(reviewed_counts), units_eligible_in_reviewed_files=dict(eligible_counts))

    dispositions = review.get("dispositions", [])
    require(isinstance(dispositions, list), "dispositions must be a list")
    scanner_by_id = {finding["id"]: finding for finding in scanner_findings}
    handled = {}
    for item in dispositions:
        require(isinstance(item, dict), "disposition must be an object")
        identifier = item.get("finding_id")
        require(identifier in scanner_by_id and identifier not in handled, "unknown or duplicate finding disposition")
        require(item.get("status") in {"confirmed", "explained", "unresolved"} and nonempty(item.get("reason")), "invalid finding disposition")
        require(not (scanner_by_id[identifier]["kind"] == "DEFECT" and item["status"] == "explained"), "a mechanical defect requires a corrected scan, not an explained disposition")
        handled[identifier] = item["status"]
    manual = review.get("findings", [])
    require(isinstance(manual, list), "findings must be a list")
    for item in manual:
        require(isinstance(item, dict), "finding must be an object")
        require(item.get("kind") in {"DEFECT", "REVIEW", "NOT_CHECKED"}, "invalid finding kind")
        require(item.get("severity") in {"critical", "major", "minor", "info"}, "invalid finding severity")
        require(item.get("status") in {"confirmed", "explained", "unresolved"}, "invalid finding status")
        require(nonempty(item.get("code")) and nonempty(item.get("detail")), "finding needs code and detail")
        require(not (item["kind"] == "DEFECT" and item["status"] == "explained"), "a defect cannot be marked explained")
        evidence_paths(item.get("evidence"))
    adjudicated = [(f, handled.get(f["id"], "unresolved")) for f in scanner_findings]
    adjudicated += [(f, f["status"]) for f in manual]
    confirmed_defect = any(f["kind"] == "DEFECT" and (s == "confirmed" or f in scanner_findings) for f, s in adjudicated)
    unresolved = any(f["kind"] == "DEFECT" or f["kind"] == "REVIEW" and s != "explained" for f, s in adjudicated)
    missing_checks = any(f["kind"] == "NOT_CHECKED" and s != "explained" for f, s in adjudicated)
    undisposed = len(handled) != len(scanner_findings)
    full = all_scored and all_full and measurement["status"] == "complete" and not missing_checks
    grade_status = ("fail" if confirmed_defect else "needs_review" if unresolved or undisposed else
                    "partial" if not full else "pass_within_scope")

    blocked = set()
    for finding, disposition in adjudicated:
        if (finding["kind"] == "NOT_CHECKED" and disposition != "explained"
                or finding["kind"] == "DEFECT" and finding["severity"] in {"major", "critical"}):
            if "path" in finding:
                blocked.add(finding["path"])
            else:
                blocked.update(e["path"] for e in finding["evidence"])

    material = review.get("material", {"status": "not_run", "search_scope": "", "items": []})
    require(isinstance(material, dict) and material.get("status") in {"not_run", "partial", "complete"}, "invalid material assessment status")
    items = material.get("items")
    require(isinstance(items, list), "material items must be a list")
    require(material["status"] != "not_run" or not items, "not_run material assessment cannot include items")
    require(material["status"] == "not_run" or nonempty(material.get("search_scope")), "material assessment needs its search scope")
    material_keys, material_units, usable_items = set(), [], []
    for item in items:
        require(isinstance(item, dict), "material item must be an object")
        require(all(nonempty(item.get(field)) for field in ("dedupe_key", "summary", "reason")), "material item needs a key, summary and usability reason")
        require(item["dedupe_key"] not in material_keys, "duplicate material key")
        material_keys.add(item["dedupe_key"])
        require(item.get("status") in {"candidate", "validated"}, "invalid material item status")
        require(type(item.get("depth")) is int and str(item["depth"]) in rubric["material_depth_weights"], "invalid material depth")
        evidence_paths(item.get("evidence"), required=item["status"] == "validated")
        if item["status"] == "validated":
            units = canonical_units(item["evidence"], by_path, blocked=blocked)
            require(not any(overlaps(units, previous) for previous in material_units), "material evidence overlaps an already counted item or copy")
            material_units.append(units)
            usable_items.append(item)
    material_ran = material["status"] == "complete" or bool(usable_items)
    material_result = {"status": material["status"], "search_scope": material.get("search_scope", ""),
                       "exhaustive": False, "basis": "distinct reviewed evidence units",
                       "material_points": sum(rubric["material_depth_weights"][str(item["depth"])] for item in usable_items) if material_ran else None,
                       "validated_material_count": len(usable_items) if material_ran else None,
                       "candidate_material_count": len(items) - len(usable_items)
                           if material["status"] == "complete" or len(items) > len(usable_items) else None, "items": items}

    mining = review.get("mining", {"status": "not_run", "search_scope": "", "tasks": []})
    require(isinstance(mining, dict) and mining.get("status") in {"not_run", "partial", "complete"}, "invalid mining status")
    tasks = mining.get("tasks")
    require(isinstance(tasks, list), "mining tasks must be a list")
    if mining["status"] == "not_run":
        require(not tasks, "not_run mining cannot include tasks")
    else:
        require(nonempty(mining.get("search_scope")), "mining needs its search scope and limits")
    keys, instance_fingerprints, validated, candidates = set(), set(), [], []
    for task in tasks:
        require(isinstance(task, dict), "task must be an object")
        for field in ("dedupe_key", "summary", "expected_output", "acceptance_criteria"):
            require(nonempty(task.get(field)), "task needs " + field)
        require(task["dedupe_key"] not in keys, "duplicate task dedupe_key")
        keys.add(task["dedupe_key"])
        require(task.get("family") in rubric["task_families"], "invalid task family")
        require(type(task.get("depth")) is int and str(task["depth"]) in rubric["task_depth_weights"], "invalid task depth")
        require(task.get("status") in {"candidate", "validated"}, "invalid task status")
        cutoff = task.get("temporal_cutoff")
        if cutoff is not None:
            timestamp(cutoff)
        evidence_paths(task.get("evidence"), required=task["status"] == "validated")
        if task["status"] == "validated":
            units = canonical_units(task["evidence"], by_path, blocked=blocked)
            instance = [task["family"], units]
            instance_digest = hashlib.sha256(json.dumps(instance, sort_keys=True).encode()).hexdigest()
            require(instance_digest not in instance_fingerprints, "duplicate validated instance under different keys or copied paths")
            instance_fingerprints.add(instance_digest)
            check_cutoff(task["evidence"], by_path, cutoff)
        if task["status"] == "validated":
            validation = task.get("validation")
            require(isinstance(validation, dict) and validation.get("passed") is True and
                    all(nonempty(validation.get(field)) for field in ("reviewer", "result", "method")), "validated task needs a passed, documented validation")
            evidence_paths(validation.get("output_evidence"))
            output_units = canonical_units(validation["output_evidence"], by_path, blocked=blocked)
            require(not overlaps(units, output_units), "task inputs overlap the demonstrated output; separate answer evidence from inputs")
            validated.append(task)
        else:
            candidates.append(task)
    ran = mining["status"] == "complete" or bool(validated)
    mining_result = {"status": mining["status"], "search_scope": mining.get("search_scope", ""), "exhaustive": False,
                     "material_points": material_result["material_points"],
                     "validation_basis": "reviewer-recorded output evidence and acceptance result",
                     "validated_task_count": len(validated) if ran else None,
                     "candidate_task_count": len(candidates) if mining["status"] == "complete" or candidates else None,
                     "validated_by_family": dict(Counter(t["family"] for t in validated)) if ran else None,
                     "validated_by_depth": dict(Counter(t["depth"] for t in validated)) if ran else None, "tasks": tasks}
    result = {"schema_version": 2, "rubric_version": rubric["version"], "calibration_status": rubric["calibration_status"],
              "acceptance_policy": "artifact-health-gates@2", "measurement_run_id": receipt["run_id"],
              "measurement_sha256": receipt["reports"]["measurement.json"], "evaluator": measurement["evaluator"],
              "grader_evaluator": evaluator_identity(),
              "source_fingerprint": measurement["source_fingerprint"], "inventory_sha256": measurement["inventory_sha256"],
              "graded_at": datetime.now(timezone.utc).isoformat(), "reviewer": reviewer, "selection": review["selection"],
              "selection_protocol": review.get("selection_protocol"),
              "grade_status": grade_status, "score_comparable": False, "source_freshness": "measured_snapshot_only",
              "factors": factors, "observed_quality_score": round(observed, 2) if all_scored else None,
              "quality_score": round(observed, 2) if full else None,
              "material_points": mining_result["material_points"], "validated_task_count": mining_result["validated_task_count"],
              "validated_material_count": material_result["validated_material_count"],
              "material_status": material["status"], "mining_status": mining["status"],
              "dispositions": dispositions, "findings": manual,
              "limitations": ["Review evidence is structurally validated; reviewer correctness is not independently proven.",
                               "Rerun measurement to bind this grade to the current source folder.",
                               "The rubric and task-depth weights are provisional."]}
    require(not Path(out).expanduser().resolve().is_relative_to(run), "grade output must be outside the measurement directory")
    out = new_output(out, measurement["source_root"])
    summary = {"collection": measurement["source_name"], "grade_status": grade_status,
               **{name: factor["score"] for name, factor in factors.items()},
               "observed_quality_score": result["observed_quality_score"], "quality_score": result["quality_score"],
               "material_points": result["material_points"], "validated_task_count": result["validated_task_count"]}
    with run_receipt(out, "grade", source_fingerprint=measurement["source_fingerprint"], measurement_run_id=receipt["run_id"]) as grade_receipt:
        result["run_id"] = grade_receipt["run_id"]
        write_json(out / "grade.json", result)
        write_json(out / "review.json", review)
        write_json(out / "artifact_mining.json", mining_result)
        write_json(out / "artifact_material.json", material_result)
        write_csv(out / "score-summary.csv", [summary], list(summary))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run")
    parser.add_argument("--review", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    try:
        result = grade(**vars(args))
        print(json.dumps({key: result[key] for key in ("grade_status", "quality_score", "observed_quality_score", "material_points", "validated_task_count")}))
        return 1 if result["grade_status"] == "fail" else 0 if result["grade_status"] == "pass_within_scope" else 2
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print("artifact-health: " + str(exc), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
