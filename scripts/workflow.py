"""Snapshot-bound workflow evidence score; deliberately separate from v2 quality.

This validates review contracts, not the truth of a reviewer's interpretation.
Missing evidence retains unknown points. A high score cannot override defects.
"""
import hashlib
import html
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .evidence import digest_file, evaluator_identity, validate_measurement, write_json
from .measure import new_output
from .review_rules import canonical_units, coverage, overlaps, check_cutoff, timestamp

RUBRIC = Path(__file__).resolve().parents[1] / "references/workflow-rubric.json"
REPORTS = {"workflow.json", "protocol.json", "review.json", "report.html"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def covers_outputs(evidence, outputs, inventory, visual=False):
    required = coverage(outputs, inventory)
    observed = coverage(evidence, inventory, visual_only=visual)
    return all(any(c <= a and d >= b for c,d in observed.get(path, []))
               for path,ranges in required.items() for a,b in ranges)


def file_evidence(items, inventory):
    require(isinstance(items, list), "evidence must be a list")
    for item in items:
        require(isinstance(item, dict), "evidence must be an object")
        row = inventory.get(item.get("path"))
        require(row is not None and isinstance(row.get("sha256"), str) and len(row["sha256"]) == 64
                and row["sha256"] == item.get("sha256") and row["status"] != "changed", "evidence path/hash mismatch or unverified bytes")
        require(nonempty(item.get("locator")), "evidence locator required")
    coverage(items, inventory)
    return items


def validate_graph(nodes, edges, inputs, outputs, inventory, required_stages):
    require(isinstance(nodes, list) and isinstance(edges, list), "graph nodes and edges must be lists")
    by_id = {n["id"]: n for n in nodes}
    require(len(by_id) == len(nodes), "duplicate graph node")
    adjacency = defaultdict(set)
    indegree = {key: 0 for key in by_id}
    require(required_stages and len(set(required_stages)) == len(required_stages), "required stages must be distinct")
    order = {stage: i for i, stage in enumerate(required_stages)}
    for node in nodes:
        require(node.get("stage") in order, "graph node needs a frozen required stage")
        require(node.get("evidence"), "graph node needs evidence")
        file_evidence(node["evidence"], inventory)
        if node.get("occurred_at"):
            timestamp(node["occurred_at"])
    for edge in edges:
        left, right = edge.get("from"), edge.get("to")
        require(left in by_id and right in by_id and left != right, "invalid graph edge")
        require(right not in adjacency[left], "duplicate graph edge")
        require(edge.get("kind") in {"derived_from", "checks", "responds_to", "quotes", "orders", "revises", "handoff"}, "invalid edge kind")
        require(order[by_id[left]["stage"]] <= order[by_id[right]["stage"]], "workflow stages run backwards")
        require(nonempty(edge.get("basis")) and edge.get("evidence"), "edge needs concrete basis and evidence")
        file_evidence(edge["evidence"], inventory)
        a, b = by_id[left].get("occurred_at"), by_id[right].get("occurred_at")
        if a and b:
            require(timestamp(a) <= timestamp(b), "reverse workflow chronology")
        adjacency[left].add(right)
        indegree[right] += 1
    queue = [key for key, value in indegree.items() if not value]
    visited = []
    while queue:
        node = queue.pop()
        visited.append(node)
        for child in adjacency[node]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    require(len(visited) == len(nodes), "workflow graph contains a cycle")
    start_nodes = {n["id"] for n in nodes if n["stage"] == "source" and any(e["path"] in inputs for e in n["evidence"])}
    end_nodes = {n["id"] for n in nodes if n["stage"] == "output" and any(e["path"] in outputs for e in n["evidence"])}
    covered_inputs = {e["path"] for n in nodes if n["id"] in start_nodes for e in n["evidence"] if e["path"] in inputs}
    connected = bool(start_nodes and end_nodes) and covered_inputs == inputs
    all_reachable = set()
    for start in start_nodes:
        reachable, todo = {start}, [start]
        while todo:
            for child in adjacency[todo.pop()]:
                if child not in reachable:
                    reachable.add(child)
                    todo.append(child)
        connected = connected and bool(end_nodes & reachable)
        all_reachable |= reachable
    covered_outputs = {e["path"] for n in nodes if n["id"] in end_nodes & all_reachable for e in n["evidence"] if e["path"] in outputs}
    connected = connected and covered_outputs == outputs and all_reachable == set(by_id)
    # Each required stage must appear on a source-to-final-stage path; orphan labels earn no coverage.
    terminal = {n["id"] for n in nodes if n["stage"] == required_stages[-1]}
    stage_path = False
    for start in start_nodes:
        todo = [(start, frozenset([by_id[start]["stage"]]))]
        seen = set(todo)
        while todo:
            node, stages = todo.pop()
            if node in terminal and set(required_stages) <= stages:
                stage_path = True
            for child in adjacency[node]:
                state = (child, stages | {by_id[child]["stage"]})
                if state not in seen:
                    seen.add(state)
                    todo.append(state)
    return {"connected": connected, "stages": sorted({n["stage"] for n in nodes}),
            "ordered_stage_path": connected and stage_path, "nodes": len(nodes), "edges": len(edges)}


def grade_workflows(run, protocol, review, out):
    measurement, receipt, rows, findings = validate_measurement(run)
    inventory = {row["path"]: row for row in rows}
    protocol_path, review_path = Path(protocol), Path(review)
    protocol, review = json.loads(protocol_path.read_text()), json.loads(review_path.read_text())
    rubric = json.loads(RUBRIC.read_text())
    require(protocol.get("schema") == rubric["version"], "unsupported workflow protocol")
    require(nonempty(protocol.get("cohort_id")) and nonempty(protocol.get("selection")), "frozen cohort and selection required")
    require(review.get("protocol_sha256") == digest_file(protocol_path), "review protocol binding mismatch")
    require(review.get("measurement_run_id") == receipt["run_id"] and review.get("inventory_sha256") == measurement["inventory_sha256"], "review snapshot binding mismatch")
    reviewer = review.get("reviewer", {})
    require(reviewer.get("kind") in {"agent", "human"} and nonempty(reviewer.get("id")), "reviewer identity required")
    require(nonempty(reviewer.get("protocol")), "reviewer protocol required")
    if reviewer["kind"] == "agent":
        require(nonempty(reviewer.get("model")), "agent model/version required")
    cases, records = protocol.get("cases", []), review.get("cases", [])
    require(cases and isinstance(cases, list), "nonempty frozen cases required")
    case_ids = [c["id"] for c in cases]
    require(len(set(case_ids)) == len(cases), "duplicate case ID")
    outcomes = [(c.get("business_case"), c.get("outcome")) for c in cases]
    require(all(all(nonempty(x) for x in key) for key in outcomes) and len(set(outcomes)) == len(outcomes), "duplicate or missing logical case/outcome")
    require(len(records) == len(cases) and {c["id"] for c in records} == set(case_ids), "review must retain exactly the frozen cases")
    by_case = {case["id"]: case for case in records}
    checks = {check: dim for dim, definition in rubric["dimensions"].items() for check in definition["checks"]}
    blocked = {f["path"] for f in findings if f["kind"] in {"DEFECT", "NOT_CHECKED"}}
    counts = Counter()
    dimensions = {dim: {"weight": definition["weight"], "earned": 0.0, "failed": 0.0, "unknown": 0.0}
                  for dim, definition in rubric["dimensions"].items()}
    result_cases, canonical_outcomes, used_outputs = [], set(), []
    for case in cases:
        item = by_case[case["id"]]
        require(case.get("kind") in {"historical_employee_task", "reconstructed_analysis", "simulation"}, "task kind must distinguish actual work from reconstruction")
        require(nonempty(case.get("role")) and nonempty(case.get("value")) and nonempty(case.get("acceptance")), "task needs role, value and acceptance")
        inputs = file_evidence(case["inputs"], inventory)
        require(inputs, "task requires pinned inputs")
        logical = case.get("input_document_ids", {})
        require(set(logical) == {e["path"] for e in inputs} and all(nonempty(v) for v in logical.values()), "pin logical document identities for every input")
        input_key = tuple(sorted(set(logical.values())))
        require(input_key not in canonical_outcomes, "same canonical input set cannot multiply task count; split the analysis within one case")
        canonical_outcomes.add(input_key)
        outputs = file_evidence(item.get("outputs", []), inventory)
        require({e["path"] for e in inputs}.isdisjoint(e["path"] for e in outputs), "outputs overlap input paths")
        require({e["sha256"] for e in inputs}.isdisjoint(e["sha256"] for e in outputs), "output is a byte copy of an input")
        if outputs:
            output_units = canonical_units(outputs, inventory)
            require(not any(overlaps(output_units, previous) for previous in used_outputs), "outputs overlap another counted case")
            used_outputs.append(output_units)
        if case.get("kind") == "historical_employee_task":
            require(case.get("temporal_cutoff"), "historical task requires cutoff")
            check_cutoff(inputs, inventory, case["temporal_cutoff"])
        records_checks = item.get("checks", {})
        require(set(records_checks) == set(checks), "every case requires all fixed checks")
        graph = validate_graph(item.get("nodes", []), item.get("edges", []),
                               {e["path"] for e in inputs}, {e["path"] for e in outputs}, inventory, case["required_stages"])
        met = set()
        for key, record in records_checks.items():
            state = record.get("state")
            require(state in {"met", "failed", "unknown"}, "invalid check state; missing is unknown")
            require(nonempty(record.get("reason")), "every check needs an explanation")
            evidence = file_evidence(record.get("evidence", []), inventory)
            if state != "unknown":
                require(evidence, "met/failed check needs actual evidence")
            if state == "met":
                met.add(key)
                if key not in {"creator_attribution", "chronology", "grounded_need", "dependencies", "input_origin", "usable_inputs"}:
                    require(all(inventory[e["path"]]["status"] == "inspected" and e["path"] not in blocked for e in evidence), "positive readiness claim uses blocked or uninspected evidence")
            dim = checks[key]
            points = dimensions[dim]["weight"] / len(rubric["dimensions"][dim]["checks"]) / len(cases)
            dimensions[dim][{"met": "earned", "failed": "failed", "unknown": "unknown"}[state]] += points
        if "input_origin" in met:
            require(item.get("origin_basis") in {"generation_receipt", "authenticated_history", "signed_source_attestation"}, "metadata/style alone cannot prove origin")
        if "distinct_output" in met:
            require(outputs and all(inventory[e["path"]]["status"] == "inspected" and e["path"] not in blocked for e in outputs), "distinct output must be usable")
        if "usable_inputs" in met:
            # A repair task can use a broken source, but must explicitly identify the defect.
            require(all(inventory[e["path"]]["status"] == "inspected" for e in inputs), "required input was not inspected")
            defects = [f for f in findings if f["kind"] == "DEFECT" and f["path"] in {e["path"] for e in inputs}]
            dispositions = item.get("source_defects", [])
            require(all(any(d.get("finding_id") == f["id"] and nonempty(d.get("reason")) and d.get("disposition") == "retained_for_repair" for d in dispositions) for f in defects), "repair inputs need explicit source-defect dispositions")
        if "supported_edges" in met:
            require(graph["edges"] > 0, "supported graph has no edges")
        if "connected_path" in met:
            require(graph["connected"], "required inputs do not reach output")
        if "stage_coverage" in met:
            require(graph["ordered_stage_path"], "required workflow stages need a connected ordered path")
        if "presentation" in met:
            require(any(e["mode"] == "visual" for e in records_checks["presentation"]["evidence"]), "presentation requires actual visual review")
            visual_outputs = [e for e in outputs if inventory[e["path"]]["extension"] in {".pdf", ".xlsx", ".docx", ".pptx", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".gif", ".bmp"}]
            require(covers_outputs(records_checks["presentation"]["evidence"], visual_outputs, inventory, visual=True), "visual review does not cover declared output units")
        if "native_checks" in met:
            require(outputs and covers_outputs(records_checks["native_checks"]["evidence"], outputs, inventory), "native checks do not cover every output")
        validation = item.get("validation", {})
        if {"verified_result", "independent_acceptance"} & met:
            require(nonempty(item.get("author_id")), "independent acceptance needs an explicit artifact author identity")
            require(validation.get("passed") is True and nonempty(validation.get("method")) and validation.get("evidence"), "acceptance needs result, method and evidence")
            file_evidence(validation["evidence"], inventory)
            require(covers_outputs(validation["evidence"], outputs, inventory), "acceptance does not cover every output")
            require(validation.get("reviewer_id") == reviewer["id"] and validation["reviewer_id"] != item.get("author_id"), "acceptance must use an independent reviewer")
            require({e["path"] for e in validation["evidence"]} & {e["path"] for e in outputs}, "acceptance must inspect actual output")
        validated = {"grounded_need", "verified_result", "required_content", "usable_inputs", "distinct_output", "native_checks", "independent_acceptance"} <= met
        complete = validated and {"supported_edges", "connected_path", "stage_coverage", "chronology", "dependencies"} <= met
        counts["candidate_tasks"] += 1
        counts["validated_tasks"] += validated
        counts["complete_input_output_sets"] += validated and "distinct_output" in met
        counts["complete_analytical_workflows"] += complete
        counts["historical_employee_tasks"] += validated and case["kind"] == "historical_employee_task"
        counts["reconstructed_analyses"] += validated and case["kind"] == "reconstructed_analysis"
        result_cases.append({"id": case["id"], "title": case["title"], "kind": case["kind"], "role": case["role"],
                             "validated": validated, "complete_analytical_workflow": complete,
                             "business_completion": "not_established",
                             "input_files": len({e["sha256"] for e in inputs}), "output_files": len({e["sha256"] for e in outputs}),
                             "graph": graph, "checks": records_checks})
    for data in dimensions.values():
        for key in ("earned", "failed", "unknown"):
            data[key] = round(data[key], 6)
    output = new_output(out, measurement["source_root"])
    earned = round(sum(d["earned"] for d in dimensions.values()), 4)
    failed = round(sum(d["failed"] for d in dimensions.values()), 4)
    unknown = round(sum(d["unknown"] for d in dimensions.values()), 4)
    result = {"schema": rubric["version"], "run_id": uuid4().hex,
              "status": "failed" if failed else "incomplete" if unknown else "passed",
              "metric": rubric["metric"], "points": earned, "maximum_points": 100,
              "failed_points": failed, "unknown_points": unknown, "dimensions": dimensions,
              "counts": dict(counts), "cases": result_cases, "cohort_id": protocol["cohort_id"],
              "protocol_sha256": digest_file(protocol_path), "rubric_sha256": digest_file(RUBRIC),
              "review_sha256": digest_file(review_path),
              "measurement_run_id": receipt["run_id"], "measurement_sha256": receipt["reports"]["measurement.json"],
              "source_fingerprint": measurement["source_fingerprint"], "evaluator": evaluator_identity(),
              "measurement_evaluator": measurement["evaluator"], "measurement_scope": measurement["scope"],
              "measurement_limits": measurement.get("limits"), "measurement_complete": measurement.get("status") == "complete",
              "reviewer": reviewer, "defects_in_source_inventory": sum(f["kind"] == "DEFECT" for f in findings),
              "limitations": [rubric["calibration"], "Unknown points are missing proof, not observed artifact failures.",
                  "Counts cover only the frozen cohort; no extrapolation to the collection.",
                  "Independent agent review is not human review; the validator cannot prove reviewer understanding.",
                  "Analytical completion does not establish orders, approvals, payment, delivery or human authorship."],
              "ai_detector_validation": {"precision": None, "recall": None, "reason": "No held-out labeled controls or calibrated classifier."}}
    write_json(output / "workflow.json", result)
    (output / "protocol.json").write_bytes(protocol_path.read_bytes())
    (output / "review.json").write_bytes(review_path.read_bytes())
    rows_html = "".join(f"<tr><td>{html.escape(d)}</td><td>{v['earned']:.2f} / {v['weight']}</td><td>{v['failed']:.2f}</td><td>{v['unknown']:.2f}</td></tr>" for d,v in dimensions.items())
    details = "".join("<h2>" + html.escape(c["title"]) + "</h2><p>" + html.escape(c["kind"]) + "</p><ul>" +
                      "".join("<li><b>" + html.escape(k+": "+v["state"]) + "</b> — " + html.escape(v["reason"]) + "</li>" for k,v in c["checks"].items()) + "</ul>" for c in result_cases)
    (output / "report.html").write_text(f"<!doctype html><meta charset='utf-8'><title>Workflow evidence</title><style>body{{font:16px system-ui;max-width:1100px;margin:40px auto;padding:0 24px;line-height:1.5}}td,th{{padding:10px;border-bottom:1px solid #ddd;text-align:left}}table{{border-collapse:collapse}}li{{margin:8px 0}}</style><h1>Workflow evidence: {earned:.2f} / 100</h1><p>{failed:.2f} failed points; {unknown:.2f} unknown points. Frozen cohort: {len(cases)} cases. This is evidence readiness, not an authenticity probability or whole-archive grade.</p><table><tr><th>Dimension</th><th>Earned</th><th>Failed</th><th>Unknown</th></tr>{rows_html}</table>{details}", encoding="utf-8")
    write_json(output / "run.json", {"schema": "workflow-receipt@1", "state": "completed", "run_id": result["run_id"],
               "reports": {name: digest_file(output / name) for name in sorted(REPORTS)},
               "completed_at": datetime.now(timezone.utc).isoformat()})
    return result


def read_workflow(directory):
    root = Path(directory)
    receipt = json.loads((root / "run.json").read_text())
    require(receipt.get("schema") == "workflow-receipt@1" and receipt.get("state") == "completed", "workflow run not completed")
    require(set(receipt.get("reports", {})) == REPORTS, "incomplete workflow receipt")
    require(all(digest_file(root / name) == value for name, value in receipt["reports"].items()), "workflow report changed")
    result = json.loads((root / "workflow.json").read_text())
    require(result["run_id"] == receipt["run_id"], "workflow run mismatch")
    require(digest_file(root / "protocol.json") == result["protocol_sha256"] and digest_file(root / "review.json") == result["review_sha256"], "workflow input bytes do not match bindings")
    saved_protocol = json.loads((root / "protocol.json").read_text())
    saved_review = json.loads((root / "review.json").read_text())
    require(saved_protocol.get("cohort_id") == result["cohort_id"] and saved_review.get("protocol_sha256") == result["protocol_sha256"], "workflow protocol binding mismatch")
    require(saved_review.get("reviewer") == result["reviewer"] and saved_review.get("measurement_run_id") == result["measurement_run_id"], "workflow review binding mismatch")
    return result


def compare_workflows(before, after):
    left, right = read_workflow(before), read_workflow(after)
    mismatches = [key for key in ("protocol_sha256", "rubric_sha256", "evaluator", "reviewer", "measurement_evaluator", "measurement_scope", "measurement_limits") if left[key] != right[key]]
    if not left["measurement_complete"] or not right["measurement_complete"]:
        mismatches.append("incomplete_measurement")
    return {"status": "incomplete" if mismatches else "passed", "comparable": not mismatches,
            "incompatibilities": mismatches, "points_delta": None if mismatches else round(right["points"] - left["points"], 4),
            "before_points": left["points"], "after_points": right["points"],
            "count_deltas": None if mismatches else {key: right["counts"].get(key,0)-left["counts"].get(key,0) for key in set(left["counts"])|set(right["counts"])},
            "scope": left["cohort_id"], "source_bytes_changed": left["source_fingerprint"] != right["source_fingerprint"]}
