import copy
import json
import sys
import unittest
import zipfile
from email import policy
from email.parser import BytesParser
from pathlib import Path

import test_artifact_health as fixtures

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from grade import load_lines
from measure import probe


class QualityEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ArtifactHealthTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.source, self.run = self.fixture.source, self.fixture.run

    def evidence(self, review, name):
        return [copy.deepcopy(item) for item in review["dimensions"]["integrity"]["evidence"] if item["path"] == name]

    def task(self, review, name="record.txt", output="answer.txt"):
        evidence = self.evidence(review, name)
        if name.endswith(".eml"):
            evidence[0]["units"]["ranges"] = [[1, 1]]
        return {"dedupe_key": "case:extract", "family": "extract_and_structure", "depth": 1,
                "status": "validated", "summary": "Extract fixture values", "evidence": evidence,
                "expected_output": "Normalized fixture row", "acceptance_criteria": "Fixture values agree",
                "temporal_cutoff": None, "validation": {"passed": True, "reviewer": "fixture checker",
                    "result": "Output matches the fixture", "method": "Compare the fixture values",
                    "output_evidence": self.evidence(review, output)}}

    def setup_task(self, name="record.txt", data=b"quantity: 2"):
        (self.source / name).write_bytes(data)
        (self.source / "answer.txt").write_text("Normalized output: 2")
        self.fixture.scan()
        review = self.fixture.clean_review()
        review["mining"] = {"status": "complete", "search_scope": "Fixture only", "tasks": [self.task(review, name)]}
        return review

    def material(self, review, name="record.txt"):
        return {"dedupe_key": "fixture-material", "status": "validated", "depth": 2,
                "summary": "Fixture evidence", "reason": "Fixture inspected", "evidence": self.evidence(review, name)}

    def test_task_count_does_not_manufacture_material_points(self):
        review = self.setup_task()
        result = self.fixture.apply_review(review)
        self.assertEqual(result["validated_task_count"], 1)
        self.assertIsNone(result["material_points"])

    def test_unfinished_empty_search_does_not_report_measured_zero(self):
        review = self.setup_task()
        for status in ("partial", "complete"):
            review["mining"] = {"status": status, "search_scope": "Fixture search", "tasks": []}
            review["material"] = {"status": status, "search_scope": "Fixture search", "items": []}
            result = self.fixture.apply_review(review, status)
            self.assertEqual(result["validated_task_count"], 0 if status == "complete" else None)
            self.assertEqual(result["material_points"], 0 if status == "complete" else None)

    def test_more_task_families_do_not_increase_material_points(self):
        review = self.setup_task()
        review["material"] = {"status": "complete", "search_scope": "Fixture", "items": [self.material(review)]}
        second = copy.deepcopy(review["mining"]["tasks"][0])
        second.update(dedupe_key="case:audit", family="reconcile_and_audit", depth=3)
        review["mining"]["tasks"].append(second)
        result = self.fixture.apply_review(review)
        self.assertEqual(result["validated_task_count"], 2)
        self.assertEqual(result["material_points"], 2)
        self.assertEqual(result["validated_material_count"], 1)

    def test_reworded_task_with_new_key_is_still_duplicate(self):
        review = self.setup_task()
        second = copy.deepcopy(review["mining"]["tasks"][0])
        second.update(dedupe_key="new-label", summary="Read the quantity into a row", expected_output="Another description of the same row")
        second["evidence"][0]["locator"] = "The same content with another locator description"
        review["mining"]["tasks"].append(second)
        with self.assertRaisesRegex(ValueError, "duplicate validated instance"):
            self.fixture.apply_review(review)

    def test_copied_evidence_cannot_multiply_material(self):
        (self.source / "copy.txt").write_bytes(b"quantity: 2")
        review = self.setup_task()
        first, second = self.material(review), self.material(review, "copy.txt")
        second["dedupe_key"] = "renamed-copy"
        review["material"] = {"status": "complete", "search_scope": "Fixture copies", "items": [first, second]}
        with self.assertRaisesRegex(ValueError, "overlaps an already counted"):
            self.fixture.apply_review(review)

    def test_validated_task_requires_separate_output_evidence(self):
        review = self.setup_task()
        validation = review["mining"]["tasks"][0]["validation"]
        validation["output_evidence"] = self.evidence(review, "record.txt")
        with self.assertRaisesRegex(ValueError, "separate answer evidence"):
            self.fixture.apply_review(review)
        validation.pop("output_evidence")
        with self.assertRaisesRegex(ValueError, "evidence must be a list"):
            self.fixture.apply_review(review)

    def test_unavailable_sources_cannot_back_validated_tasks(self):
        review = self.setup_task("record.pages", b"unconverted native document")
        with self.assertRaisesRegex(ValueError, "need inspected evidence"):
            self.fixture.apply_review(review)

    def test_invalid_sources_cannot_back_validated_material(self):
        review = self.setup_task("record.docx", b"broken package")
        review["mining"] = {"status": "not_run", "tasks": []}
        review["material"] = {"status": "complete", "search_scope": "Broken fixture", "items": [self.material(review, "record.docx")]}
        with self.assertRaisesRegex(ValueError, "need inspected evidence"):
            self.fixture.apply_review(review)

    def test_future_email_cannot_enter_historical_task_inputs(self):
        self.fixture.email(self.source / "mail.eml")
        raw = (self.source / "mail.eml").read_bytes()
        review = self.setup_task("mail.eml", raw)
        task = review["mining"]["tasks"][0]
        task["temporal_cutoff"] = "2026-11-01T00:00:00Z"
        task["evidence"][0]["available_at"] = "2026-10-01T00:00:00Z"
        with self.assertRaisesRegex(ValueError, "unavailable at its temporal cutoff"):
            self.fixture.apply_review(review)

    def test_unknown_time_and_naive_cutoff_are_not_accepted(self):
        review = self.setup_task()
        task = review["mining"]["tasks"][0]
        task["temporal_cutoff"] = "2026-11-01T00:00:00Z"
        with self.assertRaisesRegex(ValueError, "known availability"):
            self.fixture.apply_review(review)
        task["temporal_cutoff"] = "2026-11-01"
        with self.assertRaisesRegex(ValueError, "require a timezone"):
            self.fixture.apply_review(review)

    def pdf_review(self):
        from pypdf import PdfWriter
        writer = PdfWriter()
        writer.add_blank_page(width=300, height=300)
        writer.add_blank_page(width=300, height=300)
        writer.write(self.source / "two-pages.pdf")
        self.fixture.scan()
        review = self.fixture.clean_review()
        review["dispositions"] = [{"finding_id": f["id"], "status": "explained", "reason": "Known blank-page fixture"}
                                  for f in load_lines(self.run / "findings.jsonl")]
        return review

    def test_one_page_cannot_pass_as_full_document_review(self):
        review = self.pdf_review()
        dimension = review["dimensions"]["integrity"]
        dimension["evidence"][0]["units"]["ranges"] = [[1, 1]]
        with self.assertRaisesRegex(ValueError, "every measured page or part"):
            self.fixture.apply_review(review)
        dimension["scope"] = "sample"
        result = self.fixture.apply_review(review)
        self.assertIsNone(result["quality_score"])
        self.assertEqual(result["factors"]["integrity"]["unit_coverage"]["two-pages.pdf"]["reviewed"], 1)

    def test_text_extraction_cannot_prove_pdf_presentation(self):
        review = self.pdf_review()
        review["dimensions"]["communication"]["evidence"][0]["mode"] = "text"
        with self.assertRaisesRegex(ValueError, "need visual evidence"):
            self.fixture.apply_review(review)

    def test_out_of_range_evidence_is_rejected(self):
        review = self.pdf_review()
        review["dimensions"]["integrity"]["evidence"][0]["units"]["ranges"] = [[1, 3]]
        with self.assertRaisesRegex(ValueError, "exceeds measured units"):
            self.fixture.apply_review(review)

    def test_mail_copies_ignore_transport_but_keep_content_variants(self):
        self.fixture.email(self.source / "mail.eml")
        raw = (self.source / "mail.eml").read_bytes()
        (self.source / "transport.eml").write_bytes(b"Received: by fixture relay\n" + raw)
        message = BytesParser(policy=policy.default).parsebytes(raw)
        message.replace_header("Subject", "Different business content")
        (self.source / "variant.eml").write_bytes(message.as_bytes())
        result, _ = self.fixture.scan()
        continuity = result["email_continuity"]
        self.assertEqual(continuity["message_content_groups"], 2)
        self.assertEqual(continuity["duplicate_message_copies"], 1)
        self.assertEqual(continuity["same_id_variant_groups"], 1)
        self.assertEqual(continuity["conversation_completeness"], "not_established")

    def test_reply_chronology_and_missing_parent_are_review_clues(self):
        self.fixture.email(self.source / "parent.eml")
        message = BytesParser(policy=policy.default).parsebytes((self.source / "parent.eml").read_bytes())
        message.replace_header("Message-ID", "<child@example.test>")
        message.replace_header("Date", "Sun, 01 Nov 2026 12:00:00 +0000")
        message["In-Reply-To"] = "<fixture-1@example.test>"
        (self.source / "child.eml").write_bytes(message.as_bytes())
        message.replace_header("Message-ID", "<orphan@example.test>")
        message.replace_header("In-Reply-To", "<outside@example.test>")
        (self.source / "orphan.eml").write_bytes(message.as_bytes())
        result, _ = self.fixture.scan()
        self.assertEqual(result["email_continuity"]["reply_chronology_flags"], 1)
        self.assertEqual(result["email_continuity"]["conversations_with_missing_parents"], 1)
        self.assertFalse(any(f["kind"] == "DEFECT" for f in load_lines(self.run / "findings.jsonl")))

    def test_reply_cycle_and_attachment_document_match(self):
        self.fixture.email(self.source / "mail.eml")
        message = BytesParser(policy=policy.default).parsebytes((self.source / "mail.eml").read_bytes())
        message["In-Reply-To"] = message["Message-ID"]
        (self.source / "mail.eml").write_bytes(message.as_bytes())
        (self.source / "fixture.csv").write_bytes(b"item,quantity\nfixture,2\n")
        result, _ = self.fixture.scan()
        self.assertTrue(result["email_continuity"]["reply_cycle_detected"])
        links = load_lines(self.run / "evidence_links.jsonl")
        link = next(link for link in links if link["kind"] == "attachment_matches_document")
        self.assertEqual(link["target"], "fixture.csv")
        self.assertEqual(link["attachment_sha256"], link["target_sha256"])

    def test_attachment_payload_requires_inspected_document_evidence(self):
        self.fixture.email(self.source / "mail.eml")
        (self.source / "answer.txt").write_text("Fixture output")
        self.fixture.scan()
        review = self.fixture.clean_review()
        task = self.task(review, "mail.eml")
        task["evidence"][0]["units"]["ranges"] = [[2, 2]]
        review["mining"] = {"status": "complete", "search_scope": "Attachment fixture", "tasks": [task]}
        with self.assertRaisesRegex(ValueError, "attachment payload needs separate inspected"):
            self.fixture.apply_review(review)

    def test_declared_charset_changes_preserve_content_variants(self):
        raw = b'From: a@example.test\nTo: b@example.test\nDate: Mon, 02 Nov 2026 12:00:00 +0000\nMessage-ID: <charset@example.test>\nContent-Type: text/plain; charset="utf-8"\n\n\xc2\xa3'
        (self.source / "utf8.eml").write_bytes(raw)
        (self.source / "latin1.eml").write_bytes(raw.replace(b'utf-8', b'iso-8859-1'))
        result, _ = self.fixture.scan()
        self.assertEqual(result["email_continuity"]["message_content_groups"], 2)
        self.assertEqual(result["email_continuity"]["same_id_variant_groups"], 1)

    def test_exact_copies_reuse_parser_results(self):
        for number in range(12):
            (self.source / f"copy-{number}.txt").write_text("identical fixture")
        result, _ = self.fixture.scan()
        self.assertEqual(result["coverage"]["parser_cache_hits"], 11)
        self.assertEqual(result["coverage"]["files_hashed"], 12)

    def test_native_document_bundle_is_visible_as_unmeasured(self):
        bundle = self.source / "proposal.pages"
        bundle.mkdir()
        (bundle / "payload.iwa").write_bytes(b"native package fixture")
        result, code = self.fixture.scan()
        self.assertEqual(code, 2)
        self.assertEqual(result["coverage"]["files_discovered"], 1)
        row = load_lines(self.run / "inventory.jsonl")[0]
        self.assertEqual(row["path"], "proposal.pages")
        self.assertEqual(row["kind"], "package_directory")

    def test_office_missing_assets_are_reported_without_fetching_external_links(self):
        path = self.source / "broken-image.docx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("word/document.xml", "<document/>")
            archive.writestr("word/_rels/document.xml.rels", '<Relationships><Relationship Target="media/missing.png"/><Relationship Target="https://example.invalid/image.png" TargetMode="External"/></Relationships>')
        result = probe(path)
        self.assertEqual(result["metrics"]["external_relationships"], 1)
        self.assertIn("OFFICE_RELATIONSHIP_MISSING", {note["code"] for note in result["observations"]})

    def test_spreadsheet_cached_errors_are_clues_not_recalculation_verdicts(self):
        path = self.source / "values.xlsx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("xl/workbook.xml", "<workbook/>")
            archive.writestr("xl/worksheets/sheet1.xml", '<worksheet><c><f>A1+1</f></c><c t="e"><v>#VALUE!</v></c></worksheet>')
            archive.writestr("xl/sharedStrings.xml", "<sst><si><t>Fixture label</t></si></sst>")
        result = probe(path)
        self.assertEqual(result["metrics"]["cached_error_cells"], 1)
        self.assertEqual(result["metrics"]["formula_cells_without_cached_values"], 1)
        self.assertEqual(result["metrics"]["native_text_characters"], len("Fixture label"))
        self.assertTrue(all(note["kind"] == "REVIEW" for note in result["observations"]))


if __name__ == "__main__":
    unittest.main()
