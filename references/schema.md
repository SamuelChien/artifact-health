# Artifact health report contract, v2

All reports are local, UTF-8, and snapshot-bound. Evidence paths are relative to the
selected root; `source_root` records its absolute local location to prevent report
outputs inside the source. Filenames and message identifiers may contain sensitive information. These are
not external-safe exports. The scripts refuse existing output directories and output
directories inside the source collection.

Rubric `artifact-health-v2` retains the provisional quality weights and changes the
evidence contract. Material points now come from an independent inventory, not task
counts. Remeasure and review older collections before using v2; v1 and v2 totals
are not directly comparable.

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

For `index.json` objects with a `messages` list and recognizable `eml_file`/`original_file`/`text_file`
entries, resolve `path_base` relative to the index directory (default `.`). Each referenced
path must remain inside the selected root, must not traverse symlinks, and must exist.
Compare available `sha256`, `original_sha256`, and `text_sha256` with the corresponding
inventoried bytes. References excluded from the scan are not silently counted as checked.
Missing hashes are unknown. Other JSON files are not implicitly mailbox manifests.
The scanner does not currently implement `files.json`/`archives.json` manifest formats.

Exit codes: scanner `0` = implemented checks completed without confirmed defects;
`1` = confirmed defects; `2` = incomplete measurement; `3` = invalid invocation or
tooling error. Grader `0` = pass within scope, `1` = failed grade, `2` = partial or
needs review, `3` = invalid review/report input. A failed grade can still be a valid
written report. Mechanical completion does not establish contextual quality.

## Mechanical evidence and run identity

`evidence_links.jsonl` records observed reply links and exact decoded attachment
matches to standalone files. A link identifies shared bytes or a header reference,
not business correctness. `links_sha256` binds this file. `run.json` binds the full
report set; interrupted, modified and mixed bundles cannot be used as completed
measurements. Evaluator code, Python, PDF parser and rubric identities are recorded.

`email_continuity` distinguishes inspected copies, content groups, same-ID variants
and observed reply components. Transport headers are excluded from grouping;
missing-ID messages group only by exact bytes. No observed conversation is certified
complete. Missing parents, reverse chronology and reply cycles are review clues.

Native Pages/Numbers/Keynote bundles are visible as unavailable rows. Exact file
copies reuse parser results after each copy is hashed. Office checks cover package
CRCs and internal relationship targets; external targets are counted without
fetching them. Spreadsheet cached errors and absent formula caches need review and
recalculation. They are not automatically formula-correctness verdicts.

The public command uses `0 passed`, `1 failed`, `2 incomplete`, `3 error`. Failure
takes precedence over missing evidence and the `issues` array preserves both. A full
summary requires complete mechanics, a `pass_within_scope` contextual grade, and
completed task and independent material searches; completed empty searches remain zero.
`--require quality` omits both search requirements; `--require mechanical` checks implemented
mechanics only. The selected requirement is always recorded.


## Contextual review input

Use `prepare-review` to populate the exact run/hash bindings. The template is not a
completed review: it intentionally leaves reviewer identity and grades empty. Use the
actual `source_fingerprint` and `inventory_sha256` from the measurement. The
grader accepts the following object; repeat the dimension record for all six rubric IDs.
Do not copy the example as a claim that any inspection occurred.

```json
{
  "rubric_version": "artifact-health-v2",
  "source_fingerprint": "the measured fingerprint",
  "inventory_sha256": "the inventory report hash",
  "measurement_run_id": "run_id from the measurement receipt",
  "measurement_sha256": "receipt reports.measurement.json hash",
  "findings_sha256": "the measured findings hash",
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
  "mining": {"status": "not_run", "search_scope": "", "tasks": []},
  "material": {"status": "not_run", "search_scope": "", "items": []}
}
```

Each dimension has `rating` null or integer 0–4, `scope` in `full`, `sample`,
`not_checked`, `reviewed_paths`, `evidence`, and a nonempty `reason`. An evidence object
is `{ "path": "relative/path", "sha256": "actual hash", "locator": "page 2, table 1",
"mode": "visual", "units": {"kind": "page", "ranges": [[2, 2]]} }`.
Locators can identify pages, MIME parts, message fields, or specific rows/cells.
Every reviewed path must have matching evidence. `full` requires every inventory path
and every measured unit inside those files. The script validates inclusive, one-based
unit ranges against `metrics.review_units` (PDF pages, Office XML parts, MIME leaf
parts, or a whole-file unit). Overlapping ranges are merged. `mode` is `visual`,
`text`, or `structure`; presentation ratings for PDF, Office and HTML require visual
evidence for every claimed unit. The reviewer still establishes that those units
were actually seen and correctly interpreted. Ratings need a reviewed path.
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

Validated tasks need inspected evidence without blocking defects or missing checks,
and `validation` with nonempty `reviewer`, `result`, `method`, `passed: true`, and
`output_evidence` using the same evidence object format. Inputs and demonstrated
output must occupy disjoint canonical units. Output artifacts must be part of the
measured snapshot; otherwise retain the task as a candidate until a new evidence
snapshot includes the result. This checks the contract, not reviewer correctness.
If cited MIME units contain an attachment, also cite its byte-matching, separately
inspected document with full unit coverage. MIME decoding alone does not inspect a
PDF or spreadsheet payload. Those payload bytes are counted through the document,
so the mail part cannot earn separate material points.

Duplicate keys are rejected. Same-family tasks on the same canonical evidence units
are also duplicates even if summaries, output wording, locators or keys change.
Byte copies and unambiguous email transport copies share canonical identity.
Distinct tasks in one family need distinct input units. A non-null cutoff must be a
timezone-aware ISO timestamp. Every validated input needs a native email date or
explicit `available_at` timestamp at or before that cutoff. An explicit timestamp
cannot override a later native date. Later answer evidence may be used for validation,
but never as historical inputs.

## Independent material inventory

`review.material` contains `status` (`not_run`, `partial`, `complete`), `search_scope`
and `items`. An item contains `dedupe_key`, `summary`, `reason`, `status` (`candidate`
or `validated`), depth 1–3 and `evidence`. A validated item needs inspected, usable
evidence. The `reason` records why that material supports substantive work.
Canonical evidence units cannot overlap another counted material item. Partition
large documents into disjoint units, or treat related documents as one material
item. Rewording an item or choosing another mailbox/file copy cannot add points.
The provisional `material_depth_weights` are 1, 2 and 3. Material assessment can run
without task mining, and task mining can run without material assessment.

## Grade outputs and arithmetic

`grade.json`, `score-summary.csv`, `artifact_mining.json`, `artifact_material.json`,
the input `review.json`, and a binding `run.json` go to a new directory.
Grades bind the measurement run ID and report hash, and record grading code/runtime
as `grader_evaluator`.
`observed_quality_score = sum(weight * rating / 4)` only when all six ratings exist;
otherwise it is null, while individual factor scores are preserved. Factors are scaled
to 0–100 for the CSV. `quality_score` additionally requires full coverage of every
dimension and a complete mechanical measurement. Sampled scores are labeled partial
and never treated as whole-collection grades.

`grade_status`: confirmed defect -> `fail`; unresolved defects or
unexplained clues -> `needs_review`; missing measurement/review -> `partial`; otherwise
`pass_within_scope`. Quality scores do not override this gate. Rubric v2 is provisional:
`score_comparable` remains false until a separately calibrated rubric is introduced.

`material_points` sums independent validated material depths.
`validated_material_count` counts those material items. `validated_task_count` and
family/depth counts describe separately validated tasks; candidate counts remain
separate. A task's depth does not contribute to material points. Counts are null
when a lane did not run, or an unfinished search has not validated any items.
A completed search with no qualifying items reports zero. Partial searches with
validated evidence report observed lower bounds and `exhaustive: false`; complete
means only that the declared search plan finished, not that every possible task was
enumerated. No material score is manufactured from page count or writing style.

## Review planning, comparisons, and external summaries

`prepare-review` emits `plan.json` and an unscored `review.json`. Its reproducible
collection/extension sample uses distinct canonical content groups and a recorded seed,
so extra copies cannot crowd distinct records out of the initial sample. All flagged
paths are added separately.
RFC Message-ID, In-Reply-To, and References components expand the selected mail context;
missing parent IDs are retained. Providers, ambiguous IDs, absent mail, and unparsed
messages still need review. Sampling does not automatically establish language, purpose,
or date coverage. Context expansion can exceed the requested per-stratum sample size.

`compare` only computes mechanical deltas when evaluator identity, scope, limits, and
measurement completeness agree. It additionally checks rubric, gates, reviewer/model,
selection protocol, grading runtime, and full coverage for quality deltas, plus declared
independent search scope/completion for task and material deltas. Null inputs stay null; incompatible deltas are
null with reasons. Matched quality deltas are descriptive: the rubric is uncalibrated.

`export` constructs a fresh fixed allowlist of status, counts, and scores. It includes no
local paths, names, source fingerprints, message IDs, explanations, or task descriptions.
The original full reports remain local and sensitive. Exporting does not transmit data.

`batch ROOT --folders NAME ... --out NEW_DIRECTORY` measures selected immediate
subfolders and writes a local HTML/CSV scorecard. Review and grade each measured
`NEW_DIRECTORY/NAME` separately, writing its grade to
`NEW_DIRECTORY/grades/NAME` outside the sealed measurement bundle. Then run
`batch-refresh NEW_DIRECTORY`. Batch scorecards report the quality requirement;
their status does not imply completed material or task searches. Sampled scores
remain explicitly labeled. Batch progress goes to stderr; stdout is one JSON result.

## Folder batch outputs

`batch` creates one normal measurement bundle per explicitly selected immediate child
folder. `batch.json` records selected folders, options, completion, and errors. The root
`score-summary.json`/`.csv` contains one row per folder. Each row binds measurement and
grade hashes and reports all six factors, inspected/invalid/unavailable counts, reviewed
file count, extraction coverage, duplicate copies, and task/material counts.

`artifact_health_score` is the graded `observed_quality_score`. `score_scope` is `sample`
unless the strict whole-collection `quality_score` exists. Ungraded rows remain null.
`index.html` links to each folder's `report.html` and source-bound detailed outputs.
`batch-refresh` updates these derived scorecards after actual reviews; original sealed
measurement and grade reports are not overwritten. No child folder inherits its parent's
score. A selected folder's immediate subfolder table supplies mechanical measurements.

`--workers` uses persistent parser subprocesses with bounded outstanding requests and
per-request deadlines. Workers receive hashed temporary copies and the same restricted
environment as serial inspection. Returned inventory and findings remain sorted and
deterministic. The worker count is recorded in the measurement limits.
