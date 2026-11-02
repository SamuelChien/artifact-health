# Design lessons from repo-health

Reference inspected: the local `repo-health` wrapper, its build-evidence adapter,
tests, material census, task miner, and child-process controls in
`~/dev/repo-quality-evaluation-measure-ext` at the observed commit `7da49ef`. This work
does not edit that reference package. This document explains Artifact Health v2.

| Repository design | Artifact implementation | Limit |
| --- | --- | --- |
| Evaluate a known committed HEAD in a disposable clone | Hash temporary file copies and parse exactly those bytes; verify the delivered/current source separately | A folder has no Git commit. Individual copies do not make an atomic whole-collection snapshot. |
| Retain a completion receipt and reject interrupted or mixed runs | Sealed measurement and grade receipts bind run IDs, report hashes, and source fingerprints | Hash receipts detect alterations; they do not authenticate a signer or prove reviewer correctness. |
| Executed failures outrank missing evidence | Confirmed defects return failed even when another file times out or lacks a parser | Review clues remain incomplete, not proven defects. |
| A passing test in one project cannot hide an unmeasured project | Inventory all selected paths; report coverage by collection and format; require every path and measured PDF/PPTX page/slide for a full contextual grade | Explicit coverage cannot prove that the reviewer read the evidence; other formats still need part/section assessment. |
| Keep outcomes that the external schema omits | Retain full findings, review decisions, task validation, and local progress logs | A compact external export is deliberately insufficient to reproduce all review reasoning. |
| Counts come from enumerated material/task evidence | Require task evidence and validation; reject duplicate keys and identical instances across copied paths | Semantic duplicate detection and task solvability still need contextual review. |
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
