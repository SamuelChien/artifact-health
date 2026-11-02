# Artifact health report contract, v2

All reports are local, UTF-8, and snapshot-bound. Evidence paths are relative to the
selected root; `source_root` records its absolute local location to prevent report
outputs inside the source. Filenames and message identifiers may contain sensitive information. These are
not external-safe exports. The scripts refuse existing output directories and output
directories inside the source collection.

## Scanner outputs

- `run.json`: unique run ID, running/completed/error state, source fingerprint, and
  SHA-256 for every completed report. An error or running receipt cannot certify
  complete-looking files. Every report is checked when reading a bundle. These hashes
  detect accidental alteration and mixed reports; they are not digital signatures.
- `measurement.log`: phase progress and bounded failed-process diagnostics.
- `inventory.jsonl`: one row per selected documentary file, including relative path,
  collection, extension, byte size, SHA-256 where read, status, and type-specific metrics.
  `inspected` means the implemented parser completed; it does not mean visually or
  semantically correct. `unavailable` means a tool/limit/encryption prevented inspection;
  `invalid` means a demonstrated parser/format failure. `changed` means the file changed
  during reading. An empty file is an integrity finding.
- `findings.jsonl`: records with ID, kind (`DEFECT`, `REVIEW`, `NOT_CHECKED`), severity
  (`critical`, `major`, `minor`, `info`), code, path, SHA-256 where available, and detail.
- `measurement.json`: scanner/schema versions, measured UTC time, scope configuration,
  source fingerprint, coverage, inventory/summary metrics, email index verification,
  limitations, per-format coverage, and per-collection summaries. `evaluator` records
  the executable code digest, Python and pypdf versions, and rubric digest. `status` describes measurement completion,
  not quality. `inventory_sha256` binds the exact inventory report to the measurement.
- `artifact_collections.csv`: file counts, inspected/invalid/unavailable counts, bytes,
  and exact duplicate copy counts per collection. JSON is authoritative; CSV strings
  beginning with spreadsheet formula characters are prefixed with an apostrophe.
- `score-summary.csv`: six null dimensions, null quality/material/task totals, and
  `unscored` status until contextual grading.
- `artifact_mining.json`: `status: not_run`, null counts/points, and an empty task list.

`source_fingerprint` hashes a sorted list of each selected path, size, and content hash
(or missing-hash status), plus the profile/exclusions. It is not proof of complete
inspection when discovery or hash coverage is incomplete. The measurement records both.
The inventory report's own hash separately protects against accidental alteration.
The parser reads a temporary copy of exactly the hashed bytes; copies are removed
after inspection. Files are copied individually, so this does not provide a single
atomic snapshot of a changing collection. `verify` checks the selected current names
and hashes, including new or removed files. Interrupted verification remains incomplete.

V1 reports lack completion receipts and are not silently upgraded to full health. Keep
them as historical evidence and run a new measurement for the v2 workflow.

The scanner recognizes PDF, DOCX, XLSX, PPTX, ODT, EML, TXT, MD, CSV, TSV, JSON, HTML,
and several legacy/unsupported documentary suffixes. It counts other extensions as
excluded. It skips symlinks and common dependency/VCS directories, recording them.
Limits leave explicit unavailable rows and make measurement partial. Missing pypdf,
encrypted PDFs, and unsupported legacy formats are not corruption verdicts.

For `index.json` objects with a `messages` list and recognizable `eml_file`/`text_file`
entries, resolve `path_base` relative to the index directory (default `.`). Each referenced
path must remain inside the selected root, must not traverse symlinks, and must exist.
Compare available `sha256`, `original_sha256`, and `text_sha256` with the corresponding
inventoried bytes. References excluded from the scan are not silently counted as checked.
Missing hashes are unknown. Other JSON files are not implicitly mailbox manifests.
The scanner does not currently implement `files.json`/`archives.json` manifest formats.

The public command uses `0 passed`, `1 failed`, `2 incomplete`, `3 error`. Failure
takes precedence over missing evidence and the `issues` array preserves both. A full
summary requires complete mechanics, a `pass_within_scope` contextual grade, and a
completed declared task search; a completed zero-task search remains zero. `--require
quality` omits the task-search requirement; `--require mechanical` checks implemented
mechanics only. The selected requirement is always recorded.

The underlying `scripts/measure.py` remains a mechanical collector: `0` means its
implemented checks completed without defects, not full health. `1` means confirmed
defects (even with incomplete coverage), `2` means incomplete evidence, and `3` means
invalid input/tooling. The standalone grader returns `0` for `pass_within_scope`, `1`
for `fail`, `2` for partial/needs-review, and `3` for invalid review/report input.
Public `grade` additionally evaluates the default full-health requirements.

## Contextual review input

Use `prepare-review` to populate the exact run/hash bindings. The template is not a
completed review: it intentionally leaves reviewer identity and grades empty. Use the
actual `source_fingerprint` and `inventory_sha256` from the measurement. The
grader accepts the following object; repeat the dimension record for all six rubric IDs.
Do not copy the example as a claim that any inspection occurred.

```json
{
  "rubric_version": "artifact-health-v1",
  "measurement_run_id": "the measured run ID",
  "measurement_sha256": "the receipt's measurement.json hash",
  "findings_sha256": "the measured findings hash",
  "source_fingerprint": "the measured fingerprint",
  "inventory_sha256": "the inventory report hash",
  "reviewer": {"kind": "agent", "name": "reviewer label", "model": "exact model/version if used"},
  "selection": "How files, pages, messages, and complete conversations were selected",
  "selection_protocol": {"version": "review-protocol-1", "method": "full declared cohort"},
  "dimensions": {
    "integrity": {
      "rating": null,
      "scope": "not_checked",
      "reviewed_paths": [],
      "evidence": [],
      "reason": "Required contextual review has not run"
    }
  },
  "dispositions": [],
  "findings": [],
  "mining": {"status": "not_run", "search_scope": "", "tasks": []}
}
```

Each dimension has `rating` null or integer 0–4, `scope` in `full`, `sample`,
`not_checked`, `reviewed_paths`, `evidence`, and a nonempty `reason`. An evidence object
is `{ "path": "relative/path", "sha256": "actual hash", "locator": "page 2, table 1" }`.
Locators can identify pages, MIME parts, message fields, or specific rows/cells.
Every reviewed path must have matching evidence. `full` requires every inventory path
and all relevant units inside those files. For PDFs with measured page counts, add
`"pages": [1, 2]` to each applicable evidence record; PPTX uses `"slides": [1, 2]`.
Numbers are one-based and must be within the measured range. The grader unions records
for each file and rejects full scope unless every measured page/slide is accounted for.
It cannot prove the review happened; other formats and MIME/section completeness still
need contextual assessment. Ratings require at least one reviewed path.
Do not use `full` for a sampled page within an otherwise listed document. A targeted
profile can be measured as its own collection rather than declaring unrelated files N/A.

`dispositions` contains `{ "finding_id": "F000001", "status": "confirmed|explained|unresolved",
"reason": "Evidence-backed disposition" }`. A scanner `DEFECT` cannot be explained away
by the grader; confirmed mechanical failures need a corrected snapshot and fresh run.
For a demonstrated scanner false positive, fix/version the check and rerun it. Missing
dispositions keep the grade in review. `findings` contains reviewer findings with `kind`,
`severity`, `code`, `detail`, `evidence`, and `status` (`confirmed`, `explained`, `unresolved`).
Reviewer defects also cannot have status `explained`; use REVIEW for a disproven clue.

Mining status is `not_run`, `partial`, or `complete`. `search_scope` states the examined
cohort, search method, and limits. Each task has:

```json
{
  "dedupe_key": "case-identifier:outcome:version",
  "family": "reconcile_and_audit",
  "depth": 2,
  "status": "candidate",
  "summary": "Reconcile a supplier invoice with its order and receiving records.",
  "evidence": [],
  "expected_output": "A reconciled table and supported exceptions",
  "acceptance_criteria": "Quantities and totals agree with cited records; each exception is supported",
  "temporal_cutoff": null,
  "validation": null
}
```

Validated tasks need nonempty matching evidence and `validation` with nonempty `reviewer`,
`result`, and `method`, plus `passed: true`. This validates the evidence contract, not
the correctness of the reviewer. Duplicate keys are rejected. Validated tasks with
identical normalized summaries/outputs, family, cutoff, and input hashes/locators are
also rejected under different keys or copied paths. Semantic duplicates still require
review. A non-null cutoff must
be an ISO date or timestamp and is assessed contextually against the evidence.

## Grade outputs and arithmetic

`grade.json`, the original `review.json`, `score-summary.csv`, and `artifact_mining.json`
go to a new directory with a completion receipt. Grades bind the measurement run ID
and its report hash, and record the grading code/runtime as `grader_evaluator`.
`observed_quality_score = sum(weight * rating / 4)` only when all six ratings exist;
otherwise it is null, while individual factor scores are preserved. Factors are scaled
to 0–100 for the CSV. `quality_score` additionally requires full coverage of every
dimension and a complete mechanical measurement. Sampled scores are labeled partial
and never treated as whole-collection grades.

`grade_status` under `artifact-health-gates@2`: any confirmed defect -> `fail`; unresolved defects or
unexplained clues -> `needs_review`; missing measurement/review -> `partial`; otherwise
`pass_within_scope`. Quality scores do not override this gate. Rubric v1 is provisional:
`score_comparable` remains false until a separately calibrated rubric is introduced.

`material_points` sums the rubric depth weights for unique validated tasks. `validated_task_count`
counts those tasks; `candidate_task_count` counts unvalidated candidates; family/depth
counts describe validated tasks. Counts are null if mining did not run. Partial mining
can report observed counts with `exhaustive: false`; complete means only that the
declared search plan completed, never that all possible tasks in the collection exist
in the report. No material score is manufactured from page count or writing style.

## Review planning, comparisons, and external summaries

`prepare-review` emits `plan.json` and an unscored `review.json`. Its reproducible
collection/extension sample uses a recorded seed and adds all flagged paths separately.
RFC Message-ID, In-Reply-To, and References components expand the selected mail context;
missing parent IDs are retained. Providers, ambiguous IDs, absent mail, and unparsed
messages still need review. Sampling does not automatically establish language, purpose,
or date coverage. Context expansion can exceed the requested per-stratum sample size.

`compare` only computes mechanical deltas when evaluator identity, scope, limits, and
measurement completeness agree. It additionally checks rubric, gates, reviewer/model,
selection protocol, grading runtime, and full coverage for quality deltas, plus declared
search scope/completion for task deltas. Null inputs stay null; incompatible deltas are
null with reasons. Matched quality deltas are descriptive: the rubric is uncalibrated.

`export` constructs a fresh fixed allowlist of status, counts, and scores. It includes no
local paths, names, source fingerprints, message IDs, explanations, or task descriptions.
The original full reports remain local and sensitive. Exporting does not transmit data.
