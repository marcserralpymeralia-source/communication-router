# Import plan

- Input proposals: `11_knowledge_approved.json`.
- Reviewed proposals: **12**; APPROVE=5, REVIEW=5, REJECT=2.
- Import identity: tenant-scoped `department_id` when resolvable; destination/name are only human-readable resolution aids. Content/priority changes are reported as UPDATE.
- Expected operations: CREATE, UPDATE, SKIP, CONFLICT, NEW DEPARTMENT and REVIEW. REVIEW/CONFLICT are non-importable until explicitly resolved.
- Missing departments are reported as `NEW DEPARTMENT`; they are never created implicitly by the Knowledge import.
- Deletions are not supported. The importer defaults to dry-run and the caller owns commit/rollback.
- The historical proposal JSON referenced by the brief was not present. This generated set remains a conservative, reviewable hypothesis derived from the normalized CSV and reconciled with the local tenant snapshot.

## Source validation

- Original rows: **125**; normalized rows: **125**; row differences: **11**.
- Full message bodies are used only in the evaluation JSONL; reports contain case IDs and compact semantic summaries.
- The local source snapshot was read-only. Its Knowledge table predates the pending related-department/priority migration, so those absent columns were treated as defaults for comparison; no migration was applied.

## Future real-model evaluation (not executed)

Use the existing evaluation runner only after adding a sanitized prediction file from the real routing pipeline; do not point it at operational endpoints or enable forwarding:

`PYTHONPATH=backend python3 backend/scripts/evaluate_agent.py --fixture docs/routing-learning/generated/07_evaluation_dataset.jsonl --predictions /private/tmp/kibak-luna-predictions.json --allow-mismatch`

The current runner expects JSON cases with `id`/`expected`, so a future adapter must map the JSONL records and persist only sanitized prediction metadata, latency and provider usage. No Luna call was made in this phase.
