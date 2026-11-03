# Artifact Health v3 contract

Schema: `artifact-health-v3`. Workflow: `workflow-readiness@2`. Native units: `native-evidence@1`.

The executable implementation is `artifact_engine/`. `provider-schemas.json` records provider interfaces; `v3-rubric.json` describes the score and gates. A protocol hashes code, shared content collectors and the dependency lock, and records installed Python/package versions. Provider receipts record CLI version, model, reasoning, actual prompt/response/image hashes, timestamps, attempts and usage.

## State and evidence

SQLite holds configuration, folders, physical and virtual attachment paths, content-addressed artifacts, native evidence units, jobs, tasks, links and verified aliases. A single writer lock prevents concurrent mutation. Source bytes are copied and hashed, with stat checks around each copy. Duplicate bytes reuse extraction. These per-file snapshots are not an atomic snapshot of a live workspace.

References identify `unit_id`, zero-based `start`, and exclusive `end`. An empty image-only unit uses 0,0. Units have canonical identity, native locator, text, optional image, and supported availability. A filename or reviewer statement is not an evidence reference. Objective checks and generated-output citations must stay inside selected input/output intervals.

Missing readers, rendering, dependencies and unreadable evidence remain unknown. Confirmed corruption and business inconsistencies retain their source and scope. Administrative indexes are checked against actual file hashes; they never substitute for reviewing underlying documents. Native iWork previews do not establish complete editable contents.

## Scoring and identity

`ready`: outputs exist, applicable checks execute successfully, no overlap/temporal violation exists, and independent review meets every required gate. `not_ready`: an established check fails, an output is absent or task inputs are unusable. `unknown`: required validation or support cannot be established. Failed checks take precedence over unknown checks.

`readiness_score = 100 * ready / (ready + not_ready)`. No assessed tasks => null. `capacity = ready` when tasks have been enumerated or search completed; otherwise null. `task_coverage = 100 * (ready + not_ready) / eligible`; no eligible tasks => null. Native-unit content coverage is separate. Unsupported files and native inspection gaps prevent full scope even if discovered tasks pass.

Identity hashes normalized business case, role and outcome key. A second fingerprint merges adjacent selected intervals and hashes canonical input/output pairs without family or role. Splitting evidence or changing labels cannot create extra credit. Distinct deliverables can share inputs. Semantic near-duplicates still require review; string normalization cannot prove two tasks mean the same thing.

Parent capacity is the union of task IDs. Standalone readiness requires original dependencies inside the folder's recursive contents; linked readiness may use other selected folders. Reconstructed outputs are disclosed instead of treated as missing original company documents. Roles and origin cohorts are separate dimensions, not point multipliers.

## Acceptance and end-to-end work

Executable checks: numeric equality/sum/difference/product, normalized text equality, timezone-aware date order, and local JSON schema. Numeric checks select exact scalar spans; maximum absolute tolerance is 0.01. Schemas cannot retrieve external references. No model-generated checker program is executed. Empty checks mean semantic validation, not a claimed mechanical pass.

Independent gates: grounded need, input completeness, output correctness, answer separation, native fidelity, supported connections, chronology, origin disclosure and task independence. Related task catalogues expose fragmented/duplicate outcomes for independent review; equivalent variants prefer a deterministic task ID. High value has a separate finding. Review execution must match the exact task and differ from proposer and generator. Objective failures cannot be overridden by semantic approval.

Every required graph stage needs evidence; edges need source basis and evidence; cycles and disconnected evidence fail. Inputs must reach outputs and all stages must lie on input-to-output paths. End-to-end also requires ready status and independent acceptance. Analytical completion does not prove payment, delivery or approval.

Generate analyses only for solvable reconstructed tasks without unresolved prerequisites. Keep explicit AI/reconstruction disclosure and generation receipts. Do not invent chronology. Solver inputs exclude withheld outputs, proposed checker answers and unselected text.

## Export and completion boundaries

Exports reexecute readiness, compare reviews to actual provider responses, hash retained source blobs/images, and reject modified sealed runs. Performer tasks contain instructions and exact input selections. `evaluator-only/` contains withheld outputs, checkers and receipts. Unselected text becomes spaces to preserve offsets; full source documents are not blindly copied.

Connected cases and shared canonical evidence form leakage groups. SHA-256 buckets select train/validation/test at 80/10/10; small datasets may have empty splits. Related tasks stay together across roles. This is reproducible grouping, not a guarantee of representative partitions.

Model findings remain judgments with saved evidence. Full content coverage means available selected units were considered within the disclosed protocol, not that every possible task was discovered. Native gaps, packet limits, blocked jobs and unknown reviews remain visible. No style detector, credential or timestamp establishes all business truth or employee hours.

Legacy v2 grades retain their original reviewer-attestation contract and provisional points. Verified v3 capacity stays null for those reports; never mix their counts/points into v3 scores.
