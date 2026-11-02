import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from measure import RUBRIC_PATH, digest_file, measure, probe
from grade import grade, load_lines


class ArtifactHealthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="artifact-health-test-")
        self.base = Path(self.temporary.name)
        self.source = self.base / "source with spaces"
        self.source.mkdir()
        self.run = self.base / "run"
        self.review_path = self.base / "review.json"

    def tearDown(self):
        self.temporary.cleanup()

    def scan(self, **kwargs):
        return measure(self.source, self.run, **kwargs)

    def clean_review(self, scope="full"):
        measurement = json.loads((self.run / "measurement.json").read_text())
        receipt = json.loads((self.run / "run.json").read_text())
        inventory = load_lines(self.run / "inventory.jsonl")
        evidence = [{"path": row["path"], "sha256": row["sha256"], "locator": "all fixture content"} for row in inventory]
        dimensions = json.loads(RUBRIC_PATH.read_text())["dimensions"]
        return {"rubric_version": "artifact-health-v1", "source_fingerprint": measurement["source_fingerprint"],
                "inventory_sha256": measurement["inventory_sha256"],
                "measurement_run_id": receipt["run_id"], "measurement_sha256": receipt["reports"]["measurement.json"],
                "findings_sha256": measurement["findings_sha256"],
                "reviewer": {"kind": "agent", "name": "test fixture", "model": "fixture-model"},
                "selection": "Synthetic fixture only; no real corpus reviewed.",
                "dimensions": {name: {"rating": 4, "scope": scope, "reviewed_paths": [row["path"] for row in inventory],
                                      "evidence": copy.deepcopy(evidence), "reason": "Fixture assertions only."} for name in dimensions},
                "dispositions": [], "findings": [], "mining": {"status": "not_run", "search_scope": "", "tasks": []}}

    def apply_review(self, review, out="grade"):
        self.review_path.write_text(json.dumps(review))
        return grade(self.run, self.review_path, self.base / out)

    def seed_clean(self):
        (self.source / "record.txt").write_text("Fixture record: quantity 2 at unit price 10, total 20.\n")
        self.scan()

    def email(self, path):
        message = EmailMessage()
        message["From"] = "sender@example.test"
        message["To"] = "recipient@example.test"
        message["Date"] = "Mon, 02 Nov 2026 12:00:00 +0000"
        message["Message-ID"] = "<fixture-1@example.test>"
        message["Subject"] = "Fixture"
        message.set_content("The fixture table is attached.")
        message.add_attachment(b"item,quantity\nfixture,2\n", maintype="text", subtype="csv", filename="fixture.csv")
        path.write_bytes(message.as_bytes())

    def test_scan_preserves_sources_and_does_not_invent_grades(self):
        self.email(self.source / "mail.eml")
        (self.source / "copy.eml").write_bytes((self.source / "mail.eml").read_bytes())
        (self.source / "record.txt").write_text("Fixture text.")
        before = {p.name: digest_file(p) for p in self.source.iterdir()}
        result, code = self.scan()
        self.assertEqual(code, 0)
        self.assertEqual(result["metrics"]["exact_duplicate_copies"], 1)
        self.assertEqual(result["metrics"]["distinct_rfc_message_ids"], 1)
        self.assertEqual(before, {p.name: digest_file(p) for p in self.source.iterdir()})
        mining = json.loads((self.run / "artifact_mining.json").read_text())
        self.assertIsNone(mining["material_points"])
        rows = load_lines(self.run / "inventory.jsonl")
        mail = next(row for row in rows if row["path"] == "mail.eml")
        self.assertEqual(mail["metrics"]["named_or_attachment_parts"][0]["bytes"], 24)

    def test_index_checks_payload_and_sidecar_hashes(self):
        box = self.source / "account"
        box.mkdir()
        self.email(box / "mail.eml")
        (box / "mail.txt").write_text("Readable fixture")
        (box / "index.json").write_text(json.dumps({"path_base": ".", "messages": [{
            "eml_file": "mail.eml", "text_file": "mail.txt", "sha256": digest_file(box / "mail.eml"), "text_sha256": "0" * 64}]}))
        result, code = self.scan()
        self.assertEqual(code, 1)
        self.assertEqual(result["email_indexes"]["hashes_matched"], 1)
        self.assertEqual(result["email_indexes"]["hash_mismatches"], 1)
        self.assertIn("INDEX_HASH_MISMATCH", {row["code"] for row in load_lines(self.run / "findings.jsonl")})

    def test_index_missing_and_escaping_paths_are_defects(self):
        (self.source / "index.json").write_text(json.dumps({"messages": [
            {"eml_file": "missing.eml"}, {"eml_file": "../outside.eml"}]}))
        _, code = self.scan()
        self.assertEqual(code, 1)
        codes = {row["code"] for row in load_lines(self.run / "findings.jsonl")}
        self.assertTrue({"INDEX_FILE_MISSING", "INDEX_PATH_OUTSIDE_SCOPE"} <= codes)

    def test_symlinked_mail_is_not_followed(self):
        external = self.base / "outside.eml"
        self.email(external)
        (self.source / "mail.eml").symlink_to(external)
        (self.source / "index.json").write_text(json.dumps({"messages": [{"eml_file": "mail.eml"}]}))
        result, code = self.scan()
        self.assertEqual(code, 1)
        self.assertEqual(result["coverage"]["excluded"]["symlink_files"], 1)
        self.assertEqual(result["coverage"]["files_discovered"], 1)

    def test_caps_and_unsupported_formats_are_not_zero_scores(self):
        (self.source / "a.txt").write_text("readable")
        (self.source / "b.pages").write_bytes(b"fixture legacy document")
        result, code = self.scan(max_files=1)
        self.assertEqual(code, 2)
        self.assertEqual(result["coverage"]["files_discovered"], 2)
        self.assertEqual(result["coverage"]["files_hashed"], 1)
        self.assertEqual(load_lines(self.run / "inventory.jsonl")[1]["status"], "unavailable")
        self.assertEqual(probe(self.source / "b.pages")["status"], "unavailable")

    def test_docx_and_broken_packages(self):
        path = self.source / "fixture.docx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
            archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Fixture</w:t></w:r></w:p></w:body></w:document>')
        self.assertEqual(probe(path)["metrics"]["native_text_characters"], 7)
        path.write_bytes(b"not a zip package")
        self.assertEqual(probe(path)["status"], "invalid")

    @unittest.skipUnless(importlib.util.find_spec("pypdf"), "pypdf is optional")
    def test_pdf_parse_and_image_only_review(self):
        from pypdf import PdfWriter
        path = self.source / "blank.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=300, height=300)
        writer.write(path)
        result = probe(path)
        self.assertEqual(result["status"], "inspected")
        self.assertEqual(result["metrics"]["pages"], 1)
        self.assertEqual(result["observations"][0]["kind"], "REVIEW")
        path.write_bytes(b"not a pdf")
        self.assertEqual(probe(path)["status"], "invalid")

    def test_refuses_output_inside_source_and_existing_output(self):
        with self.assertRaises(ValueError):
            measure(self.source, self.source / "reports")
        self.seed_clean()
        with self.assertRaises(ValueError):
            self.scan()

    def test_full_and_sampled_grades_are_distinct(self):
        self.seed_clean()
        sampled = self.apply_review(self.clean_review("sample"), "sample-grade")
        self.assertEqual(sampled["observed_quality_score"], 100)
        self.assertIsNone(sampled["quality_score"])
        self.assertEqual(sampled["grade_status"], "partial")
        full = self.apply_review(self.clean_review(), "full-grade")
        self.assertEqual(full["quality_score"], 100)
        self.assertEqual(full["grade_status"], "pass_within_scope")
        self.assertIsNone(full["validated_task_count"])
        self.assertFalse(full["score_comparable"])

    def test_missing_dimension_is_not_imputed(self):
        self.seed_clean()
        review = self.clean_review()
        review["dimensions"]["consistency"] = {"rating": None, "scope": "not_checked", "reviewed_paths": [], "evidence": [], "reason": "Not performed."}
        result = self.apply_review(review)
        self.assertIsNone(result["observed_quality_score"])
        self.assertIsNone(result["factors"]["consistency"]["score"])
        self.assertEqual(result["factors"]["integrity"]["score"], 100)

    def test_high_average_cannot_override_critical_defect(self):
        self.seed_clean()
        review = self.clean_review()
        review["findings"] = [{"kind": "DEFECT", "severity": "critical", "code": "FIXTURE_CONTRADICTION",
                               "detail": "Synthetic test contradiction.", "status": "confirmed",
                               "evidence": review["dimensions"]["consistency"]["evidence"]}]
        result = self.apply_review(review)
        self.assertEqual(result["quality_score"], 100)
        self.assertEqual(result["grade_status"], "fail")

    def test_tampered_inventory_and_wrong_evidence_are_rejected(self):
        self.seed_clean()
        review = self.clean_review()
        review["dimensions"]["integrity"]["evidence"][0]["sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            self.apply_review(review)
        review = self.clean_review()
        with (self.run / "inventory.jsonl").open("a") as stream:
            stream.write("\n")
        with self.assertRaises(ValueError):
            self.apply_review(review)

    def test_task_validation_and_duplicate_rejection(self):
        self.seed_clean()
        review = self.clean_review()
        task = {"dedupe_key": "fixture:extract:v1", "family": "extract_and_structure", "depth": 1,
                "status": "validated", "summary": "Extract the fixture quantity and price.",
                "evidence": copy.deepcopy(review["dimensions"]["integrity"]["evidence"]),
                "expected_output": "A fixture row", "acceptance_criteria": "Quantity 2, price 10, total 20.",
                "temporal_cutoff": None, "validation": {"passed": True, "reviewer": "fixture checker", "method": "fixture assertions", "result": "Fixture values agree."}}
        review["mining"] = {"status": "complete", "search_scope": "One synthetic fixture case.", "tasks": [task]}
        result = self.apply_review(review, "valid-mining")
        self.assertEqual(result["material_points"], 1)
        self.assertEqual(result["validated_task_count"], 1)
        review["mining"]["tasks"].append(copy.deepcopy(task))
        with self.assertRaises(ValueError):
            self.apply_review(review, "invalid-duplicate")
        review["mining"]["tasks"] = [copy.deepcopy(task)]
        review["mining"]["tasks"][0]["validation"] = None
        with self.assertRaises(ValueError):
            self.apply_review(review, "invalid-validation")

    def test_cli_produces_reviewable_bundle(self):
        (self.source / "record.txt").write_text("CLI fixture")
        script = Path(__file__).resolve().parents[1] / "scripts/measure.py"
        result = subprocess.run([sys.executable, "-B", str(script), str(self.source), "--out", str(self.run)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "complete")
        measurement = json.loads((self.run / "measurement.json").read_text())
        self.assertEqual(measurement["inventory_sha256"], digest_file(self.run / "inventory.jsonl"))
        self.assertEqual(measurement["coverage"]["files_hashed"], 1)

    def test_grade_output_cannot_pollute_source(self):
        self.seed_clean()
        review = self.clean_review()
        self.review_path.write_text(json.dumps(review))
        with self.assertRaises(ValueError):
            grade(self.run, self.review_path, self.source / "grade")
        self.assertFalse((self.source / "grade").exists())

    def test_full_grade_rejects_missing_file_coverage(self):
        (self.source / "a.txt").write_text("first fixture")
        (self.source / "b.txt").write_text("second fixture")
        self.scan()
        review = self.clean_review()
        dimension = review["dimensions"]["integrity"]
        dimension["reviewed_paths"] = dimension["reviewed_paths"][:1]
        dimension["evidence"] = dimension["evidence"][:1]
        with self.assertRaises(ValueError):
            self.apply_review(review)


if __name__ == "__main__":
    unittest.main()
