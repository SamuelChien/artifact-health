import copy
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import artifact_health as health
from scripts import measure as scanner
from scripts.evidence import load_bundle, read_object, validate_measurement


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="artifact-health-workflow-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "source"
        self.source.mkdir()
        (self.source / "record.txt").write_text("Source fixture")
        self.run = self.base / "run"

    def scan(self, **options):
        return scanner.measure(self.source, self.run, **options)

    def reviewed(self, run=None, name="grade"):
        run = run or self.run
        plan = self.base / (name + "-plan")
        health.prepare_review(run, plan, per_stratum=3)
        review = read_object(plan / "review.json")
        _, _, records, _ = validate_measurement(run)
        evidence = [{"path": r["path"], "sha256": r["sha256"], "locator": "all fixture content", "mode": "visual",
                     "units": {"kind": r["metrics"].get("review_units", {}).get("kind", "file"),
                               "ranges": [[1, r["metrics"].get("review_units", {}).get("count", 1)]]}} for r in records]
        review["reviewer"] = {"kind": "agent", "name": "fixture reviewer", "model": "fixture-model"}
        for dimension in review["dimensions"].values():
            dimension.update(rating=4, scope="full", reviewed_paths=[r["path"] for r in records], evidence=copy.deepcopy(evidence), reason="Reviewed synthetic fixture only.")
        review["mining"] = {"status": "complete", "search_scope": "All fixture files; bounded fixture-only search.", "tasks": []}
        review["material"] = {"status": "complete", "search_scope": "All fixture files; fixture-only material review.", "items": []}
        scanner.write_json(plan / "review.json", review)
        result = health.grader.grade(run, plan / "review.json", self.base / name)
        return result, plan / "review.json", self.base / name

    def test_clean_measurement_is_not_full_health_and_zero_tasks_can_be_measured(self):
        result = health.measure_collection(self.source, self.run)
        self.assertEqual(result["status"], "incomplete")
        self.assertIsNone(result["tasks"]["validated_task_count"])
        self.assertEqual(health.read_artifact_health(self.run, require="mechanical")["status"], "passed")
        _, _, grade_dir = self.reviewed()
        result = health.read_artifact_health(self.run, grade_dir)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["tasks"]["validated_task_count"], 0)

    def test_full_health_requires_independent_material_assessment(self):
        self.scan()
        _, review_path, _ = self.reviewed()
        review = read_object(review_path)
        review["material"] = {"status": "not_run", "search_scope": "", "items": []}
        scanner.write_json(review_path, review)
        destination = self.base / "without-material"
        health.grader.grade(self.run, review_path, destination)
        self.assertEqual(health.read_artifact_health(self.run, destination, require="quality")["status"], "passed")
        result = health.read_artifact_health(self.run, destination)
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("material_assessment", {issue["check"] for issue in result["issues"]})

    def test_parallel_parser_retains_deterministic_evidence_and_copy_cache(self):
        for number in range(12):
            (self.source / f"copy-{number}.txt").write_text("Shared fixture")
        (self.source / "empty.txt").write_bytes(b"")
        (self.source / "native.pages").mkdir()
        first, _ = self.scan()
        second_path = self.base / "parallel"
        second, _ = scanner.measure(self.source, second_path, workers=4)
        for name in ("inventory.jsonl", "findings.jsonl", "evidence_links.jsonl"):
            self.assertEqual((self.run / name).read_bytes(), (second_path / name).read_bytes())
        self.assertEqual(first["coverage"]["parser_cache_hits"], 11)
        self.assertEqual(second["coverage"]["parser_cache_hits"], 11)
        self.assertEqual(first["source_fingerprint"], second["source_fingerprint"])

    def test_review_queue_samples_distinct_content_before_copies(self):
        for number in range(12):
            (self.source / f"copy-{number}.txt").write_text("Shared fixture")
        (self.source / "third.txt").write_text("Third distinct fixture")
        self.scan()
        health.prepare_review(self.run, self.base / "unique-plan", per_stratum=3)
        plan = read_object(self.base / "unique-plan/plan.json")
        self.assertEqual(len(plan["queue"]), 3)
        self.assertEqual(len({row["sha256"] for row in plan["queue"]}), 3)
        self.assertEqual(plan["strata"][0]["eligible_content_groups"], 3)

    def test_material_comparison_has_its_own_search_scope(self):
        self.scan()
        _, review_path, before_grade = self.reviewed()
        review = read_object(review_path)
        review["material"]["search_scope"] = "Different material search"
        scanner.write_json(review_path, review)
        after_grade = self.base / "different-material"
        health.grader.grade(self.run, review_path, after_grade)
        result = health.compare_runs(self.run, self.run, before_grade, after_grade)
        self.assertTrue(result["tasks"]["protocol_matched"])
        self.assertFalse(result["material"]["protocol_matched"])
        self.assertIsNone(result["material"]["descriptive_deltas"]["material_points"]["delta"])

    def test_defects_take_precedence_over_incomplete_scan(self):
        (self.source / "empty.txt").write_bytes(b"")
        (self.source / "unsupported.pages").write_bytes(b"legacy fixture")
        measurement, code = self.scan()
        self.assertEqual(code, 1)
        self.assertEqual(measurement["status"], "partial")
        result = health.read_artifact_health(self.run)
        self.assertEqual(result["status"], "failed")
        self.assertEqual({issue["status"] for issue in result["issues"]}, {"failed", "incomplete"})

    def test_interruption_after_writing_reports_cannot_be_reused(self):
        original = scanner._measure
        def interrupt(*args, **kwargs):
            original(*args, **kwargs)
            raise KeyboardInterrupt("fixture interruption")
        with patch.object(scanner, "_measure", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.scan()
        self.assertTrue((self.run / "measurement.json").is_file())
        self.assertEqual(read_object(self.run / "run.json")["state"], "error")
        with self.assertRaisesRegex(ValueError, "did not complete"):
            health.read_artifact_health(self.run)

    def test_mixed_runs_are_rejected_even_for_identical_source_bytes(self):
        self.scan()
        _, _, grade_dir = self.reviewed()
        second = self.base / "second"
        scanner.measure(self.source, second)
        with self.assertRaisesRegex(ValueError, "different runs"):
            health.read_artifact_health(second, grade_dir)

    def test_any_modified_report_breaks_receipt(self):
        self.scan()
        (self.run / "score-summary.csv").write_text("replaced report")
        with self.assertRaisesRegex(ValueError, "changed"):
            health.read_artifact_health(self.run)

    def test_verified_bytes_detect_added_changed_and_removed_files(self):
        (self.source / "remove.txt").write_text("to remove")
        self.scan()
        self.assertTrue(health.verify_source(self.run)["matches"])
        (self.source / "record.txt").write_text("changed fixture")
        (self.source / "remove.txt").unlink()
        (self.source / "added.txt").write_text("new fixture")
        result = health.verify_source(self.run)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["added"], ["added.txt"])
        self.assertEqual(result["removed"], ["remove.txt"])
        self.assertEqual(result["changed"], ["record.txt"])

    def test_unhashed_files_prevent_freshness_pass(self):
        (self.source / "second.txt").write_text("second")
        self.scan(max_files=1)
        result = health.verify_source(self.run)
        self.assertEqual(result["status"], "incomplete")
        self.assertIsNone(result["matches"])

    def test_parser_reads_hashed_copy_and_does_not_inherit_credentials(self):
        run = subprocess.run
        expected = (self.source / "record.txt").read_bytes()
        calls = []
        def execute(argv, **kwargs):
            if "--probe-file" in argv:
                path = Path(argv[-1])
                calls.append(path)
                self.assertNotEqual(path, self.source / "record.txt")
                self.assertEqual(path.read_bytes(), expected)
                self.assertNotIn("AWS_SECRET_ACCESS_KEY", kwargs["env"])
            return run(argv, **kwargs)
        with patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": "fixture-only-value"}), patch.object(scanner.subprocess, "run", side_effect=execute):
            self.scan()
        self.assertTrue(calls)
        self.assertFalse(calls[0].exists())
        _, _, rows, _ = validate_measurement(self.run)
        self.assertEqual(rows[0]["sha256"], hashlib.sha256(expected).hexdigest())

    def test_review_queue_is_reproducible_and_keeps_available_thread_context(self):
        for number in range(3):
            mail = EmailMessage()
            mail["From"] = "sender@example.test"
            mail["To"] = "recipient@example.test"
            mail["Date"] = "Mon, 02 Nov 2026 12:00:00 +0000"
            mail["Message-ID"] = f"<message-{number}@example.test>"
            if number:
                mail["In-Reply-To"] = f"<message-{number - 1}@example.test>"
            mail.set_content("Fixture exchange")
            (self.source / f"mail-{number}.eml").write_bytes(mail.as_bytes())
        self.scan()
        for destination in ("plan-a", "plan-b"):
            health.prepare_review(self.run, self.base / destination, seed="fixed", per_stratum=1)
        a = read_object(self.base / "plan-a/plan.json")
        b = read_object(self.base / "plan-b/plan.json")
        self.assertEqual(a, b)
        self.assertEqual(sum(r["path"].endswith(".eml") for r in a["queue"]), 3)
        self.assertEqual(a["context_expansion_files"], 2)
        review = read_object(self.base / "plan-a/review.json")
        self.assertTrue(all(d["rating"] is None for d in review["dimensions"].values()))
        self.assertEqual(review["mining"]["status"], "not_run")

    def test_comparison_rejects_changed_scope_and_preserves_negative_deltas(self):
        (self.source / "duplicate.txt").write_text("Source fixture")
        self.scan()
        (self.source / "duplicate.txt").write_text("now distinct")
        after = self.base / "after"
        scanner.measure(self.source, after)
        result = health.compare_runs(self.run, after)
        self.assertTrue(result["mechanical"]["comparable"])
        self.assertEqual(result["mechanical"]["metrics"]["exact_duplicate_copies"]["delta"], -1)
        narrower = self.base / "narrower"
        scanner.measure(self.source, narrower, exclude=["duplicate.txt"])
        result = health.compare_runs(self.run, narrower)
        self.assertFalse(result["mechanical"]["comparable"])
        self.assertIsNone(result["mechanical"]["metrics"]["unique_file_hashes"]["delta"])

    def test_export_has_no_private_names_paths_or_prose(self):
        (self.source / "private-client-name.txt").write_text("private-client-name and sensitive fixture")
        self.scan()
        exported = health.export_summary(self.run)
        text = json.dumps(exported)
        self.assertNotIn("private-client-name", text)
        self.assertNotIn(str(self.source), text)
        self.assertNotIn("source_fingerprint", text)
        self.assertIsNone(exported["quality_score"])

    def test_cli_from_another_directory_and_json_errors(self):
        command = Path(__file__).resolve().parents[1] / "bin/artifact-health"
        result = subprocess.run([sys.executable, str(command), "doctor"], cwd=self.base, capture_output=True, text=True)
        self.assertIn(result.returncode, (0, 2))
        self.assertFalse(json.loads(result.stdout)["model_calls"])
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = health.main(["summarize", str(self.base / "missing")])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(stream.getvalue())["status"], "error")

    def test_duplicate_task_under_new_key_cannot_inflate_material(self):
        (self.source / "answer.txt").write_text("Fixture output")
        self.scan()
        _, review_path, _ = self.reviewed()
        review = read_object(review_path)
        task = {"dedupe_key": "case-one", "family": "extract_and_structure", "depth": 1,
                "status": "validated", "summary": "Extract fixture text", "expected_output": "Fixture text row",
                "acceptance_criteria": "Text agrees with the cited fixture", "temporal_cutoff": None,
                "evidence": [e for e in review["dimensions"]["integrity"]["evidence"] if e["path"] == "record.txt"],
                "validation": {"passed": True, "reviewer": "fixture", "method": "fixture assertion", "result": "Fixture matched",
                               "output_evidence": [e for e in review["dimensions"]["integrity"]["evidence"] if e["path"] == "answer.txt"]}}
        repeated = copy.deepcopy(task)
        repeated["dedupe_key"] = "different-key-same-task"
        review["mining"]["tasks"] = [task, repeated]
        scanner.write_json(review_path, review)
        with self.assertRaisesRegex(ValueError, "duplicate validated instance"):
            health.grader.grade(self.run, review_path, self.base / "duplicate-grade")

    @unittest.skipUnless(importlib.util.find_spec("pypdf"), "pypdf is optional")
    def test_one_reviewed_page_cannot_certify_a_multi_page_pdf(self):
        from pypdf import PdfWriter
        writer = PdfWriter()
        writer.add_blank_page(width=300, height=300)
        writer.add_blank_page(width=300, height=300)
        writer.write(self.source / "two-pages.pdf")
        self.scan()
        plan = self.base / "page-plan"
        health.prepare_review(self.run, plan)
        review = read_object(plan / "review.json")
        _, _, rows, _ = validate_measurement(self.run)
        evidence = [{"path": row["path"], "sha256": row["sha256"], "locator": "fixture page 1", "mode": "visual",
                     "units": {"kind": row["metrics"]["review_units"]["kind"], "ranges": [[1, 1]]}} for row in rows]
        review["reviewer"] = {"kind": "agent", "name": "fixture", "model": "fixture"}
        for dimension in review["dimensions"].values():
            dimension.update(rating=4, scope="full", reviewed_paths=[r["path"] for r in rows], evidence=copy.deepcopy(evidence))
        scanner.write_json(plan / "review.json", review)
        with self.assertRaisesRegex(ValueError, "every measured page or part"):
            health.grader.grade(self.run, plan / "review.json", self.base / "invalid-pages")
        for dimension in review["dimensions"].values():
            for item in dimension["evidence"]:
                if item["path"].endswith(".pdf"):
                    item["units"]["ranges"] = [[1, 2]]
        scanner.write_json(plan / "review.json", review)
        result = health.grader.grade(self.run, plan / "review.json", self.base / "all-pages")
        self.assertEqual(result["factors"]["integrity"]["units_reviewed"]["pages"], 2)
        self.assertEqual(result["grade_status"], "needs_review")

    def test_matched_grade_comparison_retains_quality_and_task_deltas(self):
        self.scan()
        _, _, before_grade = self.reviewed()
        second = self.base / "second"
        scanner.measure(self.source, second)
        _, _, after_grade = self.reviewed(second, "after-grade")
        result = health.compare_runs(self.run, second, before_grade, after_grade)
        self.assertTrue(result["quality"]["protocol_matched"])
        self.assertEqual(result["quality"]["descriptive_deltas"]["integrity"]["delta"], 0)
        self.assertFalse(result["quality"]["score_comparable"])
        self.assertTrue(result["tasks"]["protocol_matched"])

    def test_batch_scorecard_reads_grade_outside_measurement_bundle(self):
        from scripts.batch import measure_folders, refresh_batch
        batch = self.base / "batch"
        result = measure_folders(self.base, batch, ["source"], workers=2)
        self.assertEqual(result["status"], "incomplete")
        run = batch / "source"
        health.prepare_review(run, self.base / "batch-review")
        review_path = self.base / "batch-review/review.json"
        review = read_object(review_path)
        _, _, records, _ = validate_measurement(run)
        row = records[0]
        evidence = [{"path": row["path"], "sha256": row["sha256"], "locator": "whole fixture", "mode": "text",
                     "units": {"kind": "file", "ranges": [[1, 1]]}}]
        review["reviewer"] = {"kind": "human", "name": "fixture only"}
        for dimension in review["dimensions"].values():
            dimension.update(rating=4, scope="full", reviewed_paths=[row["path"]], evidence=evidence, reason="Fixture only")
        scanner.write_json(review_path, review)
        health.grader.grade(run, review_path, batch / "grades/source")
        self.assertEqual(refresh_batch(batch)["scored_folders"], 1)
        summary = read_object(batch / "score-summary.json")["folders"][0]
        self.assertEqual(summary["score_scope"], "full")
        self.assertEqual(summary["artifact_health_score"], 100)
        self.assertIsNone(summary["material_points"])

    def test_batch_cli_emits_one_json_result(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = health.main(["batch", str(self.base), "--out", str(self.base / "batch-json"),
                                "--folders", "source", "--workers", "2"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stream.getvalue())["folders"], 1)

    def test_grade_cli_returns_failure_for_observed_defect(self):
        self.scan()
        _, review_path, _ = self.reviewed()
        review = read_object(review_path)
        review["findings"] = [{"kind": "DEFECT", "severity": "major", "status": "confirmed",
                               "code": "FIXTURE_MISMATCH", "detail": "Fixture mismatch",
                               "evidence": review["dimensions"]["integrity"]["evidence"]}]
        scanner.write_json(review_path, review)
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = health.main(["grade", str(self.run), "--review", str(review_path), "--out", str(self.base / "failed-grade")])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stream.getvalue())["status"], "failed")


if __name__ == "__main__":
    unittest.main()
