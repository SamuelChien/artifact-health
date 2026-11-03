# Design lessons from repo-health

## Implemented v3 comparison

The downloaded `/Users/samuelchien/Downloads/repo-quality-evaluation-measure-ext` separates MEASURE from GRADE. Its material census estimates 0–6 logic depth, self-containment and category counts; its task miner enumerates ideas, capped by `--mine-n` (default 150). Neither model lane executes those proposed tasks. The default overall budget is 9,000 seconds, with nulls for runner-owned missing measurements. Its authenticity collector reports source/history clues, not calibrated document AI probabilities.

Artifact Health v3 adopts version-bound evidence, explicit nulls, execution receipts, restricted provider tools and retained failures. It changes the following mechanisms:

| Concern | Downloaded repository evaluator | Artifact Health v3 |
| --- | --- | --- |
| Points | Weighted overlapping material-category counts | Distinct usable task sets; no credit for splitting evidence or changing labels |
| Task evidence | Proposed coding task ideas | Exact input/output spans, applicable executed checks and separate source-grounded review |
| Completion | Global time budget and task cap | Full resumable queue; per-job failures visible; partial pilots labeled |
| Provenance | Git/source declarations and history clues | Asserted metadata, date checks, local PDF signatures/C2PA; unknown preserved |
| Native content | Code and executed build/tests | PDF/Office rendering, cell/formula/recalculation, image/OCR, JSON/CSV and MIME/attachments |
| Connected tasks | Code/history context | Reply/attachment links, grounded business IDs, cross-packet retrieval and reviewed graphs |
| Folder score | Repository-level measurement | Recursive direct/aggregate coverage, unioned tasks, standalone/linked readiness |
| Datasets | Task summaries | Role datasets with withheld answers and connected-case/shared-evidence split groups |

The Codex lane uses the existing account with ephemeral execution, schema-constrained output, disabled shell/web tools and saved actual receipts. Source excerpts/images reach that provider; reports stay local. Hashes detect changes, not model correctness. Rich artifact datasets contain company evidence and are not the repository package's sanitized numeric export.

The historical v2 comparison below is retained for old reports. New evaluations use [the v3 contract](v3-contract.md).

For the additional eight-area workflow score and native-format evidence collectors,
see [workflow-readiness.md](workflow-readiness.md). The downloaded reference at
`/Users/samuelchien/Downloads/repo-quality-evaluation-measure-ext` was additionally
inspected for this extension. Its `collectors/authenticity.py` collects source-code
declarations/comment patterns and history clues; it is not a calibrated document-origin
classifier. Its weighted material census is unbounded and intentionally overlaps
categories. Neither its points nor its proposed coding tasks establish artifact
quality or completed employee business workflows. That evaluator is left unchanged.

Reference inspected: the local `repo-health` wrapper, its build-evidence adapter,
tests, material census, task miner, and child-process controls in
`~/dev/repo-quality-evaluation-measure-ext` at the observed commit `7da49ef`. This work
does not edit that reference package. This document explains Artifact Health v2.

| Repository design | Artifact implementation | Limit |
| --- | --- | --- |
| Evaluate a known committed HEAD in a disposable clone | Hash temporary file copies and parse exactly those bytes; verify the delivered/current source separately | A folder has no Git commit. Individual copies do not make an atomic whole-collection snapshot. |
| Retain a completion receipt and reject interrupted or mixed runs | Sealed measurement and grade receipts bind run IDs, report hashes, and source fingerprints | Hash receipts detect alterations; they do not authenticate a signer or prove reviewer correctness. |
| Executed failures outrank missing evidence | Confirmed defects return failed even when another file times out or lacks a parser | Review clues remain incomplete, not proven defects. |
| A passing test in one project cannot hide an unmeasured project | Inventory all selected paths; require every measured PDF page, Office XML part and MIME leaf part for full coverage | Explicit coverage cannot prove that the reviewer read the evidence; XML parts are not rendered pages or individual cells. |
| Keep outcomes that the external schema omits | Retain full findings, review decisions, task validation, and local progress logs | A compact external export is deliberately insufficient to reproduce all review reasoning. |
| Counts come from enumerated material/task evidence | Count independent material units; require separate task inputs and demonstrated output; deduplicate copied content and relabeled instances | Semantic duplicates across representations and task solvability still need contextual review. |
| Distinguish completed zero from unavailable/null | Preserve nulls for absent review/mining and zero for a completed search that found no tasks | A completed bounded search is not an exhaustive census of every possible task. |
| Compare compatible evaluators and scope | Gate deltas on code/runtime/rubric digests, scope, limits, coverage, and reviewer/search protocol | Changed content mix still affects interpretation; quality weights are not calibrated. |
| Provide doctor, measure, summarize, and reusable Python functions | Add a shared CLI/API, source verification, review preparation, comparisons, and a numbers-only export | No automatic provider calls or semantic scores are invented by the scanner. |
| Restrict child environments and external emissions | Parser subprocesses receive a small environment allowlist; source files are treated as data; exports contain allowlisted numbers/enums only | Process isolation is not an OS sandbox. The installed Python/parser environment remains trusted. |

The quality percentage remains because the requested artifact scorecard includes it.
It is explicitly provisional and separate from the evidence-based health verdict and
material counts. Repo-health's absence of an overall quality percentage is not evidence
for calibrating artifact weights. Validation/model-provider automation is intentionally
not borrowed wholesale: this skill's active agent performs contextual review using
available tools and records its coverage, while the local scripts make no model calls.
