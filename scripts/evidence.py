"""Local run receipts and evidence integrity; no model or network operations."""
import hashlib
import json
import os
import platform
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
REPORTS = {
    "measure": {"measurement.json", "inventory.jsonl", "findings.jsonl", "artifact_mining.json",
                "artifact_collections.csv", "score-summary.csv", "evidence_links.jsonl", "measurement.log"},
    "grade": {"grade.json", "review.json", "artifact_mining.json", "artifact_material.json", "score-summary.csv"},
}


def digest_file(path, deadline=float("inf")):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("run budget exhausted during hashing")
            chunk = stream.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name("." + path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def evaluator_identity():
    sources = [*sorted((ROOT / "scripts").glob("*.py")), ROOT / "artifact_health.py"]
    code = {str(path.relative_to(ROOT)): digest_file(path) for path in sources if path.is_file()}
    try:
        pdf_version = metadata.version("pypdf")
    except metadata.PackageNotFoundError:
        pdf_version = None
    try:
        pillow_version = metadata.version("Pillow")
    except metadata.PackageNotFoundError:
        pillow_version = None
    return {"code_sha256": hashlib.sha256(json.dumps(code, sort_keys=True).encode()).hexdigest(),
            "python": platform.python_version(), "pypdf": pdf_version,
            "pillow": pillow_version,
            "rubric_sha256": digest_file(ROOT / "references/rubric.json")}


@contextmanager
def run_receipt(out, kind, **context):
    out = Path(out)
    receipt = {"schema": "artifact-health-run@2", "run_id": uuid4().hex, "kind": kind,
               "state": "running", "started_at": datetime.now(timezone.utc).isoformat(), **context}
    write_json(out / "run.json", receipt)
    try:
        yield receipt
        receipt["reports"] = {name: digest_file(out / name) for name in sorted(REPORTS[kind])}
        receipt["state"] = "completed"
    except BaseException as exc:
        receipt.update(state="error", error_type=type(exc).__name__)
        raise
    finally:
        receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_json(out / "run.json", receipt)


def read_object(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(Path(path).name + " must contain a JSON object")
    return value


def load_bundle(directory, kind):
    """Reject unsealed, interrupted, modified, and mixed-run report bundles."""
    directory = Path(directory).expanduser().resolve()
    if not (directory / "run.json").is_file():
        raise ValueError("run receipt is missing; legacy reports need a fresh measurement")
    receipt = read_object(directory / "run.json")
    if receipt.get("schema") != "artifact-health-run@2" or receipt.get("kind") != kind:
        raise ValueError("unsupported run receipt or wrong report kind")
    if receipt.get("state") != "completed":
        raise ValueError("run did not complete; inspect run.json before using its evidence")
    reports = receipt.get("reports")
    if not isinstance(reports, dict) or set(reports) != REPORTS[kind]:
        raise ValueError("run receipt does not bind the complete report set")
    for name, expected in reports.items():
        if not isinstance(expected, str) or digest_file(directory / name) != expected:
            raise ValueError(name + " was changed or belongs to a different run")
    principal = read_object(directory / ("measurement.json" if kind == "measure" else "grade.json"))
    if principal.get("run_id") != receipt.get("run_id") or principal.get("schema_version") != 2:
        raise ValueError("report does not match its completed run receipt")
    if principal.get("source_fingerprint") != receipt.get("source_fingerprint"):
        raise ValueError("source fingerprint does not match the run receipt")
    return principal, receipt


def load_lines(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def source_fingerprint(scope, records):
    rows = [[r["path"], r["bytes"], r["sha256"], r["status"] if not r["sha256"] else "hashed"]
            for r in sorted(records, key=lambda row: row["path"])]
    return hashlib.sha256(json.dumps([scope, rows], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def validate_measurement(directory):
    measurement, receipt = load_bundle(directory, "measure")
    records = load_lines(Path(directory) / "inventory.jsonl")
    findings = load_lines(Path(directory) / "findings.jsonl")
    if source_fingerprint(measurement["scope"], records) != measurement["source_fingerprint"]:
        raise ValueError("source fingerprint does not match inventory bytes")
    if len({row["path"] for row in records}) != len(records):
        raise ValueError("duplicate inventory paths")
    if measurement["coverage"]["files_discovered"] != len(records):
        raise ValueError("inventory count does not match measured coverage")
    for name, field in (("inventory.jsonl", "inventory_sha256"), ("findings.jsonl", "findings_sha256"),
                        ("evidence_links.jsonl", "links_sha256")):
        if receipt["reports"][name] != measurement.get(field):
            raise ValueError("measurement does not bind " + name)
    return measurement, receipt, records, findings
