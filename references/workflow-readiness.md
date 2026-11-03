# Workflow evidence, version 1

The six-dimensional v2 quality grade and independent material points retain their
meaning. `workflow-readiness@1` adds a separate **verified evidence** score for a
frozen cohort of business cases. It is not a human-authorship probability, a
financial audit, a population quality estimate or a replacement for contextual review.

Each of eight dimensions has two fixed checks. Weights are 10 origin, 10
authenticity, 15 task value, 15 completeness, 10 input/output, 10 connections,
20 end-to-end evidence, and 10 content-type review. Cases receive equal weight.
The score reports earned, failed and unknown points, totaling 100. Unknown points
are missing proof, not a demonstrated defect. Weights are provisional; freeze them
before the baseline and never adjust them to increase a candidate's score.

## Commands

```
artifact-health census ROOT --folders Finance Invoices Procurement email --out NEW_CENSUS
artifact-health measure FROZEN_PACKAGE --out NEW_MEASUREMENT --workers 4
artifact-health workflow-grade NEW_MEASUREMENT --protocol PROTOCOL.json --review REVIEW.json --out NEW_GRADE
artifact-health workflow-compare BEFORE_GRADE AFTER_GRADE
```

`census` inventories every selected path/size, including unsupported formats. Its
mailbox counts come from explicitly labeled **index claims**; they do not authenticate
raw emails or count tasks. The selected immediate source folders and output directory
must be disjoint. Byte/hash verification remains `measure` and `verify` work.

## Freeze the protocol

The protocol contains `schema`, `cohort_id`, selection method and a nonempty `cases`
list. Each case needs a stable `id`, `title`, `business_case`, distinct `outcome`,
`kind` (`historical_employee_task`, `reconstructed_analysis`, `simulation`), role,
business value, outcome acceptance, pinned `inputs` using v2 evidence references,
`input_document_ids` mapping every input path to its logical-document identity,
and ordered `required_stages`, normally source, analysis, output, review.
Known alternate representations must share a logical identity. This mapping needs
review; the program cannot automatically establish semantic equivalence.

The cohort is a bounded search, not a count of every task in a large archive. One
case/outcome is one task, not every possible combination of input files. Identical
logical input sets cannot multiply cases. Outputs cannot overlap inputs or another
counted case. Keep independent material inventory separate from task descriptions.

Historical tasks additionally require an aware temporal cutoff and available inputs
at or before that cutoff. Reconstructed analyses performed today do not prove an
employee performed that analysis historically. New evidence cannot retroactively
establish a payment, approval, delivery or identity.

## Independent review

A review binds `protocol_sha256`, `measurement_run_id`, and `inventory_sha256`.
`reviewer` has explicit `id`, `kind`, model/version for agents, and frozen review
`protocol`. Each case has `author_id`, all available `outputs`, every fixed rubric
check with `state` (met, failed, unknown), reason and exact evidence references.
Met/failed checks require evidence; unavailable or changed bytes cannot certify a result.

The graph has evidence-bearing nodes with `id`, `stage`, optional aware `occurred_at`,
and typed edges with source, target, concrete basis and evidence. Every required input
must reach an output. Every declared output must be represented and reachable.
Disconnected nodes cannot supply stage coverage, and a complete required-stage path
must exist. Cycles, backward stages, and known reversed times are rejected. Unknown
timestamps remain a contextual limit; graph shape is not an execution trace.

Independent acceptance requires a different, explicit reviewer and artifact author,
passed result, method, and evidence covering every declared output unit. Native checks
also cover every declared output. Presentation checks cover each applicable PDF,
Office or raster output in visual mode. References cannot prove a reviewer actually
read the content: review trust and correctness remain explicit limitations.

Broken source documents may support a repair task only with specific source-finding
dispositions labeled `retained_for_repair`. They remain broken in the original
inventory. A usable, separately inspected successor must demonstrate the repair.
The sidecar always reports real `business_completion: not_established`; a future
business-completion contract would require separate authentic transaction evidence.

## Origin and format limits

Explicit AI/synthetic declarations are collected from supported native PDF, Office,
text and email body content. They are marker hits for review; absence never means
human origin. Software such as ReportLab, Word or openpyxl does not by itself prove
AI authorship. PDF/Office properties and email From headers are **asserted metadata**,
not authenticated identity. Signature/transport authentication are not implemented.
Detector precision and recall remain null without independent labeled controls.

The scanner now records PDF creator/producer/timestamps and page geometry; Office
core properties, media counts and invalid formula/name references; DOCX tables,
drawings and supplementary text parts; raster decode/dimensions/frames and selected
EXIF; JSON structure separately from unchecked schema/source fidelity. HEIC/HEIF
remain visible as unsupported. Pixels do not establish photographic quality; PDF
text does not establish visual fidelity; cached values do not prove correct formulas;
valid JSON does not establish extraction accuracy. Native and visual review are required.

## Comparisons and review integrity

Reports seal the result, protocol, review and HTML with hashes. They bind the grader
and measured extractor/runtime, scope, limits, rubric and reviewer protocol. Changed
definitions or incomplete measurement block score deltas. Both favorable and negative
changes are retained. Source files must change for a claimed artifact gain; improved
documentation of unchanged evidence should be called a review/traceability gain.

Before claiming a whole collection passed, use the original strict v2 acceptance
workflow. A high score on these cases cannot certify hundreds of thousands of
unreviewed files, independent material points, human origin or legal authenticity.
