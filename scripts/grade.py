#!/usr/bin/env python3
"""Validate a contextual review against a measured snapshot and calculate scores."""
import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from measure import RUBRIC_PATH, digest_file, new_output, write_csv, write_json


def require(condition, message):
    if not condition:
        raise ValueError(message)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def load_lines(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def grade(run, review, out):
    run = Path(run).resolve()
    measurement = json.loads((run / "measurement.json").read_text(encoding="utf-8"))
    rubric = json.loads(RUBRIC_PATH.read_text(encoding="utf-8"))
    require(digest_file(run / "inventory.jsonl") == measurement["inventory_sha256"], "inventory report was changed")
    require(digest_file(run / "findings.jsonl") == measurement["findings_sha256"], "scanner findings were changed")
    inventory = load_lines(run / "inventory.jsonl")
    scanner_findings = load_lines(run / "findings.jsonl")
    by_path = {row["path"]: row for row in inventory}
    require(len(by_path) == len(inventory), "inventory contains duplicate paths")
    review = json.loads(Path(review).read_text(encoding="utf-8"))
    require(isinstance(review, dict), "review must be an object")
    for field in ("source_fingerprint", "inventory_sha256"):
        require(review.get(field) == measurement[field], field + " does not match the measured snapshot")
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
        if rating is None:
            require(scope == "not_checked", "missing rating requires not_checked scope")
            all_scored = False
        else:
            require(scope != "not_checked" and paths, "a rating requires actual reviewed paths")
            observed += definition["weight"] * rating / 4
        if scope == "full":
            require(set(paths) == set(by_path), "full scope must cover every inventory path")
        else:
            all_full = False
        factors[name] = {"rating": rating, "score": None if rating is None else rating * 25,
                         "weight": definition["weight"], "scope": scope, "files_reviewed": len(paths)}

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
    critical = any(f["kind"] == "DEFECT" and f["severity"] == "critical" and
                   (s == "confirmed" or f in scanner_findings) for f, s in adjudicated)
    unresolved = any(f["kind"] == "DEFECT" or f["kind"] == "REVIEW" and s != "explained" for f, s in adjudicated)
    missing_checks = any(f["kind"] == "NOT_CHECKED" and s != "explained" for f, s in adjudicated)
    undisposed = len(handled) != len(scanner_findings)
    full = all_scored and all_full and measurement["status"] == "complete" and not missing_checks
    grade_status = ("fail" if critical else "needs_review" if unresolved or undisposed else
                    "partial" if not full else "pass_within_scope")

    mining = review.get("mining", {"status": "not_run", "search_scope": "", "tasks": []})
    require(isinstance(mining, dict) and mining.get("status") in {"not_run", "partial", "complete"}, "invalid mining status")
    tasks = mining.get("tasks")
    require(isinstance(tasks, list), "mining tasks must be a list")
    if mining["status"] == "not_run":
        require(not tasks, "not_run mining cannot include tasks")
    else:
        require(nonempty(mining.get("search_scope")), "mining needs its search scope and limits")
    keys, validated, candidates = set(), [], []
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
            require(nonempty(cutoff), "invalid temporal cutoff")
            datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
        evidence_paths(task.get("evidence"), required=task["status"] == "validated")
        if task["status"] == "validated":
            validation = task.get("validation")
            require(isinstance(validation, dict) and validation.get("passed") is True and
                    all(nonempty(validation.get(field)) for field in ("reviewer", "result", "method")), "validated task needs a passed, documented validation")
            validated.append(task)
        else:
            candidates.append(task)
    ran = mining["status"] != "not_run"
    mining_result = {"status": mining["status"], "search_scope": mining.get("search_scope", ""), "exhaustive": False,
                     "material_points": sum(rubric["task_depth_weights"][str(t["depth"])] for t in validated) if ran else None,
                     "validated_task_count": len(validated) if ran else None, "candidate_task_count": len(candidates) if ran else None,
                     "validated_by_family": dict(Counter(t["family"] for t in validated)) if ran else None,
                     "validated_by_depth": dict(Counter(t["depth"] for t in validated)) if ran else None, "tasks": tasks}
    result = {"schema_version": 1, "rubric_version": rubric["version"], "calibration_status": rubric["calibration_status"],
              "source_fingerprint": measurement["source_fingerprint"], "inventory_sha256": measurement["inventory_sha256"],
              "graded_at": datetime.now(timezone.utc).isoformat(), "reviewer": reviewer, "selection": review["selection"],
              "grade_status": grade_status, "score_comparable": False, "source_freshness": "measured_snapshot_only",
              "factors": factors, "observed_quality_score": round(observed, 2) if all_scored else None,
              "quality_score": round(observed, 2) if full else None,
              "material_points": mining_result["material_points"], "validated_task_count": mining_result["validated_task_count"],
              "dispositions": dispositions, "findings": manual,
              "limitations": ["Review evidence is structurally validated; reviewer correctness is not independently proven.",
                               "Rerun measurement to bind this grade to the current source folder.",
                               "The rubric and task-depth weights are provisional."]}
    require(not Path(out).expanduser().resolve().is_relative_to(run), "grade output must be outside the measurement directory")
    out = new_output(out, measurement["source_root"])
    write_json(out / "grade.json", result)
    write_json(out / "review.json", review)
    write_json(out / "artifact_mining.json", mining_result)
    summary = {"collection": measurement["source_name"], "grade_status": grade_status,
               **{name: factor["score"] for name, factor in factors.items()},
               "observed_quality_score": result["observed_quality_score"], "quality_score": result["quality_score"],
               "material_points": result["material_points"], "validated_task_count": result["validated_task_count"]}
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
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print("artifact-health: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
