# Evidence and contextual review

## Scope and sampling

Freeze the source snapshot and intended use before scoring. Distinguish business
records, reference manuals, templates, generated artifacts, and unknown provenance;
neither a polished page nor unusual metadata proves origin. Measure document copies,
logical versions, and business cases separately. A PDF and its editable source are
representations of one document when supported by evidence, not necessarily duplicates
to delete. Byte-identical copies are an inventory fact, not automatically a defect.

Run mechanical checks on the complete selected inventory. For visual or semantic
sampling, record the selection seed/rule, reviewed paths and page ranges, and eligible
and reviewed units. Stratify by collection, type, purpose, language, date, and native
versus scanned text. Add suspicious cases separately from the representative sample.
A deliberately balanced sample is not a population-weighted defect estimate. Use
complete conversations for email review and treat conversations, not mailbox copies,
as independent units for uncertainty estimates.

The v1 scorer has six fixed dimensions. A dimension that cannot be assessed is null
with a reason; the scorer does not redistribute its weight. Adapt the applicable checks
within a dimension to the document type. A reference manual does not need an invoice
number, and an acknowledgment does not need an attachment.

## Document checks

| Dimension | Evidence to examine |
| --- | --- |
| Integrity | Parser results, complete pages, required embedded objects, manifest checksums; distinguish file corruption from encryption or an unavailable decoder. |
| Readability | Native text and OCR quality, table reading order, numeric and multilingual extraction; render pages to verify charts, image-only material, and illegible scans. No text on a page does not establish that it is blank. |
| Completeness | Required sections, exhibits, signatures when actually required, referenced records, and prerequisites for the intended use. A missing source outside the supplied scope is unknown, not a proven nonexistent record. |
| Consistency | Recalculate relevant amounts; check units, currencies, identifiers, quantities, rounding, and source-backed claims. Compare quotes, orders, invoices, receipts, payments, and certificates when available. Arithmetic consistency alone does not establish factual validity. |
| Provenance | Documented source, revision lineage, effective dates, superseded copies, and availability at the time of a decision. File modification time alone is not a business date. |
| Communication | Inspect rendered pages for clipping, overlapping text, broken tables and figures; check purpose, audience, terminology, and usable structure. |

Keep originals unchanged during OCR/conversion. Record conversion tools and versions,
input and output hashes, and whether rendered or extracted text was reviewed. The
bundled scanner does not perform OCR, render pages, recalculate spreadsheets, inspect
digital signatures, or validate every Office relationship. Those remain explicit review
work, not implied passes. CAD, video, and other unsupported file families are outside
the scanner's documentary scope and are counted as excluded by extension.

## Email checks

Use raw EML for headers, MIME, and payload claims. TXT exports are useful for reading
but cannot establish that an attachment or transport header was absent from the source.
Check export text against the decoded raw message contextually; matching stored
checksums alone do not prove semantic agreement.

Distinguish mailbox copies, provider message IDs, RFC Message-IDs, and conversations.
The scanner counts distinct RFC ID strings as a descriptive metric, not a definitive
canonical-message count. Same-ID variants may reflect transport differences; missing
IDs and conflicting content need review. Thread IDs can be provider/account scoped.
Preserve conflicting variants until their relationship is understood.

- **Integrity/readability:** MIME structure, decoder defects, charset, HTML/plain body
  agreement, index/file hashes, payload decoding, and CID rendering where relevant.
- **Completeness:** available parent messages, quoted/forwarded context, claimed current
  attachments, and referenced versions. Distinguish a link, future promise, quoted
  filename, and actual attachment. Embedded payload hashes are inventoried, but payload
  content still needs its own document review.
- **Consistency:** speaker and buyer/seller roles, request/answer continuity, quantities,
  product identifiers, commitments, totals, and attached records. Trace important claims
  beyond the first row of an attachment.
- **Provenance:** native dates with timezone uncertainty, reply causality, version
  availability, source/export history, and justified archive boundaries. Missing
  optional delivery/authentication headers can be normal in sent copies or exports.
- **Communication:** whether the newest authored text answers the actual thread; separate
  quotations, signatures, campaign boilerplate, and legal footers before judging repetition.

Compare newsletters with newsletters, automated notices with automated notices, and
business exchanges with matched purposes, languages, roles, and periods. For declared
synthetic mail, add realism findings for unexplained template concentration, timing,
voice, and business-state continuity. These are not probabilities of human authorship.
Do not manufacture headers, alter provenance, or inject noise to improve a grade.

## Findings and acceptance

Use `DEFECT` for demonstrated failures, `REVIEW` for clues requiring interpretation,
and `NOT_CHECKED` for missing execution or evidence. Each contextual finding needs a
code, severity, evidence path/hash/locator, observed fact, expected behavior, and next
action. Record valid controls for broad pattern findings.

Review every mechanical finding and retain it with a disposition. Any confirmed
defect blocks acceptance. Unresolved defects or review clues mean `needs_review`.
Incomplete measurement or sampled context means `partial`, even if the observed score
is high. `pass_within_scope` requires complete measurement and full reviewed coverage
of all six dimensions, no undisposed findings, and no unresolved defects. This status
does not certify authenticity, legal validity, or financial accuracy beyond the checks
actually performed.

The weights are an initial design, not empirically calibrated thresholds. Before using
them to rank suppliers or releases, review known usable and broken cases, assess reviewer
agreement, and freeze the rubric, model, selection method, and acceptance rules. Do not
tune thresholds against the same collection until it gets the desired score.

## Task mining and material points

Enumerate concrete opportunities, not generic tasks inferred from filenames. A candidate
becomes validated only after a reviewer verifies its required evidence, demonstrates a
feasible solution or output, and records an acceptance result. Use a reviewer/model
separate from the proposer when independent checking is available and authorized; the
skill does not require delegation for ordinary measurement.

Each instance has one primary family: extraction, reconciliation/audit, comparison,
timeline reconstruction, artifact creation/revision, or workflow follow-through.
Give each instance a stable `dedupe_key` covering the business case, requested outcome,
and evidence version. Same case/output with reworded instructions counts once, including
copies found in several mailboxes. Distinct cases with the same template can be separate
instances but remain one task family. The script rejects repeated keys and identical
validated instances under different keys or copied evidence paths. The reviewer must
still examine paraphrased and semantically overlapping tasks.

Use these provisional depth weights:

- **1 point:** bounded extraction or classification from a clear source.
- **2 points:** reconciliation or synthesis across independent records, with a checkable result.
- **3 points:** a multi-step decision or artifact/workflow change with dependencies and a
  substantive acceptance check. Length, page count, and file count alone do not increase depth.

`material_points` is the sum of weights for unique validated instances. Report candidate
count, validated count, family diversity, and counts by depth separately. A capped or
sampled search supplies an observed lower bound, not an exhaustive corpus total. No
reviewed candidates is a measured zero only when the planned search actually completed.

Task records include input evidence, expected output, acceptance criteria, validation
result/reviewer, and a temporal cutoff when relevant. Withhold future replies, completed
deliverables, and answer-bearing evidence from task inputs when reconstructing what
could be done at a historical point; keep such records in separate evaluator evidence.
Open-ended drafting tasks need a factual and purpose-specific rubric, not exact string
matching. Do not count unsupported or unsolvable tasks merely because they sound useful.
