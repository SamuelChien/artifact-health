# Artifact Health

Evaluate document collections for useful employee tasks, provenance, native-format quality, and connected workflows. Produce a report for every selected folder and subfolder, plus datasets grouped by employee role.

The v3 evaluator keeps three measures separate:

- **Readiness /100:** ready tasks divided by ready plus not-ready tasks. Unknown tasks remain visible.
- **Verified capacity:** distinct usable input/output task sets, checked against source evidence.
- **Coverage:** reviewed native units, assessed tasks, unavailable formats, and unfinished jobs.

It inspects PDFs, spreadsheets, Word documents, presentations, images including HEIC, JSON/CSV, and email with attachments. Native inspection uses rendering, OCR, spreadsheet recalculation, metadata, and available provenance credentials. Unsupported contents remain explicitly unmeasured; iWork previews do not establish complete editable-document coverage.

Task validation combines executable numeric/text/date/schema checks with a separate Codex review. Copied artifacts, relabeled tasks, and fragmented outcomes cannot automatically earn extra capacity. Reconstructed outputs are disclosed as AI-generated analyses. Metadata and writing style do not prove human or AI authorship.

## Install

Use Python 3.10+ on a system with POSIX file locking, such as macOS or Linux.

```bash
git clone https://github.com/SamuelChien/artifact-health.git ~/dev/artifact-health
cd ~/dev/artifact-health
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
bin/artifact-health doctor
```

Full evaluation also needs Poppler (`pdftoppm`), LibreOffice (`soffice`), Tesseract, and an authenticated Codex CLI on `PATH`. Install the OCR language data needed by your collection. To use the evaluator's English and Chinese configuration, place `eng.traineddata`, `chi_sim.traineddata`, and `chi_tra.traineddata` in `.venv/share/tessdata/`.

The launcher uses the repository's `.venv` when available. `doctor` detects capabilities without starting model review.

To make the skill available to Codex, link the checkout into your skills directory:

```bash
mkdir -p ~/.codex/skills
ln -s ~/dev/artifact-health ~/.codex/skills/artifact-health
```

## Evaluate and resume

Choose the business folders and a new output directory outside the source tree. Set `--model` and `--reasoning` to values supported by your Codex account.

```bash
bin/artifact-health evaluate /path/to/company \
  --folders Finance Sales \
  --out /path/to/reports/new-run \
  --provider codex --model YOUR_MODEL --reasoning max

bin/artifact-health resume /path/to/reports/new-run
bin/artifact-health summarize /path/to/reports/new-run
bin/artifact-health verify /path/to/reports/new-run --source /path/to/company
bin/artifact-health export-dataset /path/to/reports/new-run \
  --out /path/to/datasets/new-export
```

Evaluation preserves source files and records resumable checkpoints. Full evaluation has no global sampling cap; per-file deadlines and provider failures remain visible. Use `resume --retry-blocked` when a blocked dependency or provider becomes available. Evaluator/runtime changes require a new baseline.

Model review sends prepared excerpts and images through your Codex account. Reports and datasets are written locally. Dataset inputs and evaluator-only answers are kept separate; company datasets contain source evidence and are not sanitized public exports.

Open `index.html` for recursive folder reports. JSON/CSV summaries, file-level evidence, provenance, connections, task records, and execution receipts support inspection and reuse. Scores remain partial while required evidence is unfinished.

## Skill and contracts

- [Codex skill instructions](SKILL.md)
- [v3 evidence and grading contract](references/v3-contract.md)
- [v3 rubric](references/v3-rubric.json)
- [Provider response schemas](references/provider-schemas.json)
- [Comparison with repo-health](references/repo-health-design.md)

Legacy v2 commands remain available under their original reviewer-attestation contract. Their material points and task claims are not v3 verified capacity.

## Validate

With the dependencies above installed:

```bash
.venv/bin/python -B -m unittest discover -s tests -p 'test_*.py'
```

Regression coverage includes incorrect outputs despite passing declarations, duplicate and fragmented tasks, usable audit inputs, chronology, answer leakage, native formats, source freshness, resumability, and dataset integrity.
