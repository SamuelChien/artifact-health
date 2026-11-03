"""Evidence identity and executable task acceptance; no model or filesystem writes."""
import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from . import VERSION

ROLES = ("accounts_payable", "billing_receivables", "sales", "procurement", "logistics",
         "quality_compliance", "facilities_maintenance", "corporate_administration", "unknown")
FAMILIES = ("extract_and_structure", "reconcile_and_audit", "compare_and_decide",
            "reconstruct_timeline", "create_or_revise", "workflow_followthrough")
KINDS = ("historical_employee_task", "reconstructed_analysis", "simulation")
STATES = ("ready", "not_ready", "unknown")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def normalized(value):
    return " ".join(str(value).casefold().split())


def require(condition, message):
    if not condition:
        raise ValueError(message)


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(result.tzinfo is not None and result.utcoffset() is not None, "timestamp has no timezone")
    return result


def resolve(ref, units):
    """A reference selects an exact interval in a measured native evidence unit."""
    require(isinstance(ref, dict) and ref.get("unit_id") in units, "unknown evidence unit")
    unit = units[ref["unit_id"]]
    text = unit.get("text", "")
    start, end = ref.get("start", 0), ref.get("end", len(text))
    require(type(start) is int and type(end) is int and 0 <= start <= end <= len(text), "invalid evidence range")
    if ref.get("quote") is not None:
        require(ref["quote"] == text[start:end], "evidence quotation does not match source")
    require(unit.get("status", "inspected") == "inspected", "evidence was not inspected")
    return unit, text[start:end], (unit.get("canonical_id", unit["id"]), start, end)


def references_overlap(left, right, units):
    for a in left:
        ua, ta, (ka, sa, ea) = resolve(a, units)
        for b in right:
            ub, tb, (kb, sb, eb) = resolve(b, units)
            if ka == kb and (max(sa, sb) < min(ea, eb) or sa == ea == sb == eb):
                return True
            # A supplied page image exposes all of that page, including withheld text.
            if ua.get("image") and (ua.get("image") == ub.get("image") or
                    ua["artifact_id"] == ub["artifact_id"] and not ub.get("image")
                    and ub.get("locator", {}).get("kind") not in {"page", "frame"}):
                return True
    return False


def contained(ref, allowed, units):
    _, _, (key, start, end) = resolve(ref, units)
    intervals = sorted((a, b) for r in allowed for k, a, b in [resolve(r, units)[2]] if k == key)
    cursor = start
    for a, b in intervals:
        if a <= cursor <= b:
            cursor = max(cursor, b)
            if cursor >= end:
                return True
    return False


def task_identity(task):
    """Family labels, prompt wording and the partition of material are not identity."""
    for key in ("business_case", "role", "outcome_key"):
        require(isinstance(task.get(key), str) and task[key].strip(), "task needs " + key)
    require(task["role"] in ROLES, "unrecognized employee role")
    require(task.get("family") in FAMILIES, "unrecognized task family")
    require(task.get("kind") in KINDS, "task origin must be explicit")
    return digest([normalized(task["business_case"]), task["role"], normalized(task["outcome_key"])])


def pair_fingerprint(task, units):
    """Catch relabeling the same evidence/output pair, including different roles."""
    def merged(refs):
        result = []
        for key, start, end in sorted({resolve(r, units)[2] for r in refs}):
            if result and result[-1][0] == key and start <= result[-1][2]:
                result[-1][2] = max(end, result[-1][2])
            else:
                result.append([key, start, end])
        return result
    inputs = merged(task.get("inputs", []))
    outputs = merged(task.get("outputs", []))
    return digest([inputs, outputs]) if outputs else None


def number(ref, units):
    unit, text, span = resolve(ref, units)
    if span[1:] == (0, len(unit.get("text", ""))) and isinstance(unit.get("value"), (int, float)) and not isinstance(unit.get("value"), bool):
        return Decimal(str(unit["value"]))
    clean = text.strip().replace(",", "").replace("−", "-")
    clean = re.sub(r"^(?:USD|CNY|RMB|US\$|\$|¥)\s*", "", clean, flags=re.I)
    if clean.startswith("(") and clean.endswith(")"):
        clean = "-" + clean[1:-1]
    require(bool(re.fullmatch(r"[-+]?\d+(?:\.\d+)?", clean)), "numeric check needs an exact source number")
    return Decimal(clean)


def execute_check(check, units):
    """An allowlisted declarative checker; never execute model-produced programs."""
    kind = check.get("kind")
    try:
        sources = check.get("sources", [])
        target = check.get("target")
        if kind in {"numeric_equal", "sum_equal", "difference_equal", "product_equal"}:
            require(sources and target, "numeric check needs source and target evidence")
            values = [number(r, units) for r in sources]
            if kind == "numeric_equal":
                require(len(values) == 1, "equality needs one source")
                expected = values[0]
            elif kind == "sum_equal":
                expected = sum(values, Decimal(0))
            elif kind == "difference_equal":
                require(len(values) == 2, "difference needs two sources")
                expected = values[0] - values[1]
            else:
                expected = Decimal(1)
                for value in values:
                    expected *= value
            actual = number(target, units)
            tolerance = Decimal(str(check.get("tolerance", "0")))
            require(tolerance.is_finite() and Decimal(0) <= tolerance <= Decimal("0.01"), "tolerance exceeds the numeric checker limit")
            passed = abs(actual - expected) <= tolerance
            detail = {"expected": str(expected), "actual": str(actual), "tolerance": str(tolerance)}
        elif kind == "text_equal":
            require(len(sources) == 1 and target, "text equality needs one source and target")
            expected, actual = resolve(sources[0], units)[1], resolve(target, units)[1]
            passed = normalized(expected) == normalized(actual)
            detail = {"source_text": expected, "output_text": actual}
        elif kind == "date_order":
            require(len(sources) >= 2, "date order needs at least two timestamps")
            values = [timestamp(resolve(r, units)[1].strip()) for r in sources]
            passed = values == sorted(values)
            detail = {"timestamps": [v.isoformat() for v in values]}
        elif kind == "json_schema":
            import jsonschema
            schema = check.get("schema") or json.loads(check.get("schema_json") or "null")
            require(target and isinstance(schema, dict), "schema and target required")
            # No implicit external schema retrieval, even when a source embeds a URL.
            require("$ref" not in json.dumps(schema), "schema references must be resolved locally before checking")
            jsonschema.Draft202012Validator.check_schema(schema)
            errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(json.loads(resolve(target, units)[1])), key=lambda e: str(e.path))
            passed, detail = not errors, {"errors": [e.message for e in errors]}
        else:
            raise ValueError("unsupported checker: " + str(kind))
        return {"kind": kind, "state": "met" if passed else "failed", "detail": detail,
                "check_sha256": digest(check)}
    except Exception as exc:
        return {"kind": kind, "state": "unknown", "detail": {"reason": str(exc)}, "check_sha256": digest(check)}


def graph_check(task, units):
    nodes = task.get("stages", [])
    required = set(task.get("required_stages", []))
    by_id = {n["id"]: n for n in nodes}
    require(len(by_id) == len(nodes), "duplicate workflow stage ID")
    if not required or not required <= {n.get("name") for n in nodes}:
        return {"state":"unknown", "reason":"required workflow stages are missing"}
    adjacency = {key:set() for key in by_id}; reverse = {key:set() for key in by_id}
    input_ids = {r["unit_id"] for r in task.get("inputs",[])}
    output_ids = {r["unit_id"] for r in task.get("outputs",[])}
    starts, ends = set(), set()
    covered_inputs, covered_outputs = set(), set()
    for key,node in by_id.items():
        refs=node.get("evidence",[])
        if not refs: return {"state":"unknown", "reason":"workflow stage has no evidence"}
        for ref in refs: resolve(ref,units)
        ids={r["unit_id"] for r in refs}
        if ids & input_ids: starts.add(key); covered_inputs |= ids & input_ids
        if ids & output_ids: ends.add(key); covered_outputs |= ids & output_ids
    for edge in task.get("edges",[]):
        a,b=edge.get("from"),edge.get("to")
        require(a in by_id and b in by_id and a!=b,"invalid workflow edge")
        require(edge.get("basis") and edge.get("evidence"),"workflow edge requires source evidence")
        for ref in edge["evidence"]: resolve(ref,units)
        adjacency[a].add(b); reverse[b].add(a)
    indegree={key:len(value) for key,value in reverse.items()}; todo=[k for k,v in indegree.items() if not v]; visited=[]
    while todo:
        key=todo.pop(); visited.append(key)
        for child in adjacency[key]:
            indegree[child]-=1
            if not indegree[child]: todo.append(child)
    require(len(visited)==len(nodes),"workflow graph has a cycle")
    def reachable(seeds,edges):
        seen=set(seeds); todo=list(seeds)
        while todo:
            for child in edges[todo.pop()]:
                if child not in seen: seen.add(child); todo.append(child)
        return seen
    forwards=reachable(starts,adjacency)
    terminals={k for k,v in adjacency.items() if not v}
    backwards=reachable(ends,reverse)
    connected = bool(starts and ends) and covered_inputs==input_ids and covered_outputs==output_ids and ends<=forwards
    connected = connected and all(reachable({s},adjacency)&ends for s in starts)
    connected = connected and set(by_id)<=forwards&backwards and terminals<=ends
    return {"state":"met" if connected else "failed", "reason":"supported graph connectivity" if connected else "disconnected required evidence"}


def assess(task, units, semantic=None):
    """Reexecute objective checks; a proposer-supplied passing flag is never consumed."""
    identity = task_identity(task)
    failures, unknowns = [], []
    inputs, outputs = task.get("inputs", []), task.get("outputs", [])
    require(inputs, "task needs grounded inputs")
    for ref in inputs + outputs:
        resolve(ref, units)
    for ref in inputs:
        unit, _, (_, start, end) = resolve(ref, units)
        if unit.get("image") and (start != 0 or end != len(unit.get("text", ""))):
            failures.append("IMAGE_EXPOSES_UNSELECTED_CONTENT")
    if not outputs:
        failures.append("OUTPUT_NOT_AVAILABLE")
    if outputs and references_overlap(inputs, outputs, units):
        failures.append("ANSWER_LEAKAGE")
    cutoff = task.get("temporal_cutoff")
    if task["kind"] == "historical_employee_task":
        if not cutoff:
            unknowns.append("HISTORICAL_CUTOFF_UNKNOWN")
        else:
            end = timestamp(cutoff)
            for ref in inputs:
                unit = resolve(ref, units)[0]
                available = unit.get("available_at")
                if not available:
                    unknowns.append("INPUT_AVAILABILITY_UNKNOWN")
                elif timestamp(available) > end:
                    failures.append("FUTURE_INPUT")
    for ref in inputs:
        unit = resolve(ref, units)[0]
        if unit.get("blocking"):
            failures.append("INPUT_UNUSABLE")
        if unit.get("business_defects") and task["family"] != "reconcile_and_audit" and not task.get("source_defect_dispositions"):
            unknowns.append("SOURCE_DEFECT_REQUIRES_DISPOSITION")
    results = []
    for check in task.get("checks", []) + (semantic or {}).get("objective_checks",[]):
        target=check.get("target")
        if not all(contained(r, inputs, units) for r in check.get("sources", [])) or target and not contained(target, outputs, units):
            results.append({"kind":check.get("kind"),"state":"failed","detail":{"reason":"checker references outside task inputs/outputs"}})
        else:
            results.append(execute_check(check, units))
    if any(r["state"] == "failed" for r in results):
        failures.append("OUTPUT_CHECK_FAILED")
    if any(r["state"] == "unknown" for r in results):
        unknowns.append("CHECK_NOT_EXECUTED")
    # The orchestrator, not a task JSON author, supplies the independent execution record.
    if not semantic or semantic.get("task_sha256") != digest(task) or not semantic.get("execution_id"):
        unknowns.append("INDEPENDENT_REVIEW_MISSING")
    else:
        if semantic["execution_id"] in {task.get("proposer_execution_id"), task.get("generation_execution_id")}:
            failures.append("SELF_REVIEW")
        for key in ("grounded_need", "input_completeness", "output_correctness", "answer_separation", "native_fidelity", "origin_disclosure", "supported_connections", "chronology", "task_independence"):
            state = semantic.get("checks", {}).get(key, {}).get("state", "unknown")
            if state == "failed":
                failures.append(key.upper())
            elif state != "met":
                unknowns.append(key.upper())
    if task.get("missing_dependencies"):
        unknowns.append("DEPENDENCIES_UNRESOLVED")
    graph=graph_check(task,units)
    if graph["state"]=="failed": failures.append("DISCONNECTED_WORKFLOW")
    state = "not_ready" if failures else "unknown" if unknowns else "ready"
    e2e = state == "ready" and graph["state"]=="met" and bool(semantic and semantic.get("end_to_end", {}).get("state") == "met")
    return {"schema": VERSION, "task_id": identity, "state": state, "end_to_end": e2e,
            "failures": sorted(set(failures)), "unknowns": sorted(set(unknowns)), "checks": results, "graph":graph,
            "business_completion": "not_established" if not e2e else task.get("business_completion", "not_established"),
            "validation_basis": "executed objective checks and independent source-grounded review"}


def score(tasks, search_complete=False):
    counts = {state: sum(t.get("state") == state for t in tasks) for state in STATES}
    assessed = counts["ready"] + counts["not_ready"]
    return {"readiness_score": round(100 * counts["ready"] / assessed, 2) if assessed else None,
            "capacity": counts["ready"] if tasks or search_complete else None,
            "eligible_tasks": len(tasks) if tasks or search_complete else None,
            "assessed_tasks": assessed, "unknown_tasks": counts["unknown"],
            "task_coverage": round(100 * assessed / len(tasks), 2) if tasks else None,
            "end_to_end_tasks": sum(bool(t.get("end_to_end")) for t in tasks) if tasks or search_complete else None,
            "score_scope": "full" if search_complete and not counts["unknown"] else "partial",
            "unscored_reason": None if assessed else "no_eligible_tasks" if search_complete and not tasks else "unassessed"}
