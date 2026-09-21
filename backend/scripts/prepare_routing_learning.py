"""Generate reviewable routing-learning artifacts from historical CSV exports."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.routing.learning import (  # noqa: E402
    analyze_routing_cases,
    boundary_review,
    build_evaluation_record,
    build_department_reconciliation,
    build_knowledge_proposals,
    compare_case_sources,
    json_dump,
    load_routing_cases,
    multi_destination_analysis,
    normalize_destination,
    normalize_label,
    review_knowledge_proposals,
    validate_knowledge_proposals,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _markdown_table(rows: list[dict[str, object]], left: str, right: str) -> str:
    lines = [f"| {left} | {right} |", "|---|---:|"]
    lines.extend(f"| {row[left]} | {row[right]} |" for row in rows)
    return "\n".join(lines)


def _load_source(path: Path | None) -> dict[str, object]:
    if path is None:
        return {"departments": [], "knowledge": [], "source_status": "NOT_PROVIDED"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("departments"), list):
        raise ValueError("--source-json must contain a departments list")
    payload.setdefault("knowledge", [])
    payload["source_status"] = "LOCAL_TENANT_READ_ONLY_SNAPSHOT"
    return payload


def _no_forward_destination(departments: list[dict[str, object]]) -> str | None:
    for department in departments:
        if normalize_label(department.get("name")) == "mantenimiento":
            return normalize_destination(department.get("destination_email")) or None
    return None


def generate_artifacts(
    original_path: Path,
    normalized_path: Path,
    output_dir: Path,
    source_path: Path | None = None,
) -> dict[str, object]:
    source = _load_source(source_path)
    departments = [dict(item) for item in source.get("departments", [])]
    current_knowledge = [dict(item) for item in source.get("knowledge", [])]
    no_forward_destination = _no_forward_destination(departments)
    original = load_routing_cases(original_path, no_forward_destination=no_forward_destination)
    normalized = load_routing_cases(normalized_path, no_forward_destination=no_forward_destination)
    analysis = analyze_routing_cases(normalized)
    proposals = validate_knowledge_proposals(
        build_knowledge_proposals(normalized, departments),
        normalized,
    )
    reconciliation = build_department_reconciliation(normalized, departments)
    reviewed_proposals = review_knowledge_proposals(proposals, normalized, departments, current_knowledge)
    boundaries = boundary_review(normalized, departments, current_knowledge)
    multi_cases = multi_destination_analysis(normalized)
    source_differences = compare_case_sources(original, normalized)
    destinations = analysis["final_destination_counts"]
    confusion = analysis["confusion_matrix"]

    quality = [
        "# Dataset quality",
        "",
        f"- Rows: **{analysis['rows']}**",
        f"- Unique subject+body pairs: **{analysis['unique_subject_body']}**",
        f"- Exact legacy/human agreement: **{analysis['exact_agreement']}/{analysis['rows']}**",
        f"- Non-empty human reasons: **{analysis['non_empty_reasons']}**",
        f"- Multi-destination cases: **{len(analysis['multi_destination_cases'])}**",
        "",
        "## Interpretation",
        "",
        "`Reenviar a` is retained as the legacy proposal. `Correccion` is the human review. `NO_FORWARD` is resolved from the tenant source snapshot. Human corrections have priority; the old proposal is used to expose confusion, not as ground truth.",
        "",
        "## Final destinations",
        "",
        _markdown_table([{"destination": key, "count": value} for key, value in destinations.items()], "destination", "count"),
        "",
        "## Anomalies requiring review",
        "",
        f"- Source/normalized row differences: **{len(source_differences)}**; see `08_import_plan.md` for the import boundary.",
        f"- Unresolved correction labels: `{', '.join(analysis['unresolved_correction_labels']) or 'none'}`.",
        f"- Distinct local-part variants: `{json.dumps(analysis['destination_variants_by_local_part'], ensure_ascii=False)}`.",
        "- Domains are never merged automatically. In particular, Ingesco and Quibac maintenance mailboxes remain separate entities.",
    ]
    _write(output_dir / "01_dataset_quality.md", "\n".join(quality) + "\n")

    confusion_lines = [
        "# Confusion analysis",
        "",
        "The matrix is calculated from legacy proposal to final human destination. Multi-destination outcomes are retained as a single explicit value and are not treated as runner-up uncertainty.",
        "",
        _markdown_table(
            [{"legacy → human": f"{row['legacy_prediction']} → {row['final_human_destination']}", "count": row["count"]} for row in confusion],
            "legacy → human",
            "count",
        ),
        "",
        "## Generalizable boundaries to review",
        "",
        "- Commercial versus maintenance: a quote or commercial offer is not enough to infer operational ownership; an accepted service, order or existing contract can move the action to the operational owner.",
        "- Operations versus maintenance: scheduling, dates and visits are operational coordination; the repair or intervention itself belongs to the maintenance owner when supported by the case.",
        "- Central versus specialist teams: generic or insufficiently specified messages may start at Central, but a concrete action should take precedence.",
        "- Sender/domain is a signal, not a deterministic rule. The same sender or platform can produce different destinations depending on event and process phase.",
        "",
        "## Requested boundary review",
        "",
        "The counts below are deterministic case intersections, not a classification accuracy claim. `REVIEW` means the dataset supports a boundary hypothesis but does not justify a hard rule.",
        "",
    ]
    for boundary in boundaries:
        confusion_lines.extend([
            f"### {boundary['left']} ↔ {boundary['right']}",
            f"- Relevant cases: **{boundary['relevant_case_count']}**; directions: `{json.dumps(boundary['confusion_directions'], ensure_ascii=False)}`.",
            f"- Candidate rule: {boundary['candidate_rule']}.",
            f"- Evidence IDs: `{', '.join(boundary['supporting_case_ids']) or 'none'}`.",
            f"- Current Knowledge titles: `{', '.join(item for item in boundary['current_knowledge_titles'] if item) or 'none'}`.",
            f"- Recommendation: **{boundary['recommendation']}**.",
            "",
        ])
    confusion_lines.extend([
        "## Process phase interpretation",
        "",
        "The sample supports a reviewable sequence of guidelines: offer/price requests tend toward Comercial; acceptance/order/conformity moves ownership to the operational owner; date/visit coordination tends toward Operaciones; an incident or intervention tends toward Mantenimiento; post-service documentation remains dependent on the responsible specialty. This is not sufficient evidence for a global state machine.",
    ])
    _write(output_dir / "02_confusion_analysis.md", "\n".join(confusion_lines) + "\n")

    profiles = ["# Department profiles observed", "", "These are evidence summaries, not new departments or production rules.", ""]
    for destination, count in destinations.items():
        if "; " in destination:
            continue
        label = destination.split("@", 1)[0].replace("_", " ").title()
        ids = [case.case_id for case in normalized if destination in case.final_human_destination.split("; ")]
        profiles.extend([
            f"## {label} ({destination})",
            f"- Observed cases: **{count}**; supporting IDs: `{', '.join(ids[:20])}`.",
            "- Strong signal: the current action and process phase, with client/contract ownership as a possible disambiguator.",
            "- Weak signal: sender, subject alone, quoted history and department names mentioned in signatures.",
            "- Exception status: validate any client or contract-specific ownership before turning it into editable Knowledge.",
            "",
        ])
    _write(output_dir / "03_department_profiles.md", "\n".join(profiles))

    _write(output_dir / "04_knowledge_final.json", json_dump(proposals) + "\n")

    reconciliation_lines = [
        "# Department reconciliation",
        "",
        f"- Source: **{source.get('source_status')}**; company_id: **{source.get('company_id', 'unknown')}**.",
        f"- Current tenant departments inspected: **{len(departments)}**.",
        "- Department identity is `department_id`; destination email is an attribute used for the initial reconciliation only.",
        "- Similar names and same local-parts across different domains are not automatically equivalent.",
        "",
        "## EXISTENTES",
        "",
    ]
    existing = [item for item in reconciliation["departments"] if item["status"] == "EXISTING"]
    for item in existing:
        reconciliation_lines.append(
            f"- **{item['department_name']}** (`department_id={item['department_id']}`): current `{item['current_destination']}`; observed `{item['observed_destination']}`; active={item['current_active']}; match={item['match']}; cases={item['observed_count']} (`{', '.join(item['supporting_case_ids'])}`)."
        )
    if not existing:
        reconciliation_lines.append("- None.")
    reconciliation_lines.extend(["", "## REALMENTE NUEVOS", ""])
    new_items = [item for item in reconciliation["departments"] if item["status"] == "REALMENTE NUEVO"]
    for item in new_items:
        reconciliation_lines.append(
            f"- `{item['observed_destination']}`; cases={item['observed_count']} (`{', '.join(item['supporting_case_ids'])}`); propuesta mínima: validar nombre organizativo antes de crear."
        )
    if not new_items:
        reconciliation_lines.append("- None.")
    reconciliation_lines.extend(["", "## AMBIGUOS", ""])
    ambiguous = [item for item in reconciliation["departments"] if item["status"] == "AMBIGUOUS"]
    for item in ambiguous:
        candidates = ", ".join(
            f"{candidate['name']} ({candidate['destination_email']}, id={candidate['id']})"
            for candidate in item["local_part_candidates"]
        )
        reconciliation_lines.append(
            f"- Observado `{item['observed_destination']}`; no hay destino exacto. Candidatos por local-part: `{candidates or 'none'}`. No fusionar dominios; cases={item['observed_count']} (`{', '.join(item['supporting_case_ids'])}`)."
        )
    if not ambiguous:
        reconciliation_lines.append("- None.")
    reconciliation_lines.extend(["", "## Departamentos actuales sin destino observado", ""])
    for item in reconciliation["unused_current_departments"]:
        reconciliation_lines.append(
            f"- `{item.get('name')}` (`department_id={item.get('id')}`, `{item.get('destination_email')}`); activo={item.get('active')}."
        )
    if not reconciliation["unused_current_departments"]:
        reconciliation_lines.append("- None.")
    _write(output_dir / "09_department_reconciliation.md", "\n".join(reconciliation_lines) + "\n")

    approved_records = [item for item in reviewed_proposals if item["status"] in {"APPROVE", "REVIEW"}]
    _write(output_dir / "11_knowledge_approved.json", json_dump(approved_records) + "\n")

    signals = [
        "# Structured signals candidates",
        "",
        "These signals are deliberately not implemented as deterministic matchers.",
        "",
    ]
    for sender, destinations_for_sender in sorted(analysis["sender_with_multiple_destinations"].items()):
        signals.append(f"- **UNSAFE_HARD_RULE** — sender `{sender}` reaches multiple human destinations: `{', '.join(destinations_for_sender)}`. Sender alone is insufficient; require event/intent context.")
    signals.extend([
        "- **STRUCTURED_CANDIDATE** — sender/domain plus a concrete event type, customer/contract or process phase may add value; validate on a holdout before implementation.",
        "- **SEMANTIC** — event/intent, lifecycle phase, responsibilities, exclusions and exceptions are representable with the current Knowledge ontology and should remain the first choice.",
        "- Sender/domain alone is not a structured candidate because the same sender can reach materially different destinations.",
        "- A future RoutingMatcher is only justified after a holdout demonstrates that semantic Knowledge cannot represent a stable signal efficiently.",
    ])
    _write(output_dir / "05_structured_signals.md", "\n".join(signals) + "\n")

    new_destinations = []
    for item in new_items:
        destination = item["observed_destination"]
        new_destinations.append({
            "name": destination.split("@", 1)[0].replace("_", " ").title(),
            "destination_email": destination,
            "support_count": item["observed_count"],
            "description": "Ownership observed in reviewed historical routing cases; validate the organizational name before creation.",
            "supporting_case_ids": item["supporting_case_ids"],
        })
    _write(output_dir / "06_new_departments.md", "# New department candidates\n\n" + ("\n".join(f"- **{item['name']}** — `{item['destination_email']}`; {item['support_count']} cases; IDs `{', '.join(item['supporting_case_ids'])}`. Do not create implicitly." for item in new_destinations) or "- None after reconciliation with the real tenant inventory.") + "\n")

    evaluation_lines = [json.dumps(build_evaluation_record(case), ensure_ascii=False) for case in normalized]
    _write(output_dir / "07_evaluation_dataset.jsonl", "\n".join(evaluation_lines) + "\n")

    multi_lines = [
        "# Multi-destination cases",
        "",
        "Explicit human multi-destination corrections are analyzed individually. The second destination is not a runner-up by default.",
        "",
    ]
    for item in multi_cases:
        multi_lines.extend([
            f"## Case {item['case_id']}",
            f"- Sender: `{item['sender']}`; subject: `{item['subject'] or '(none)'}`.",
            f"- Body summary: {item['body_summary']}",
            f"- Legacy: `{item['legacy_prediction']}`; correction: `{item['human_correction']}`.",
            f"- Destinations: `{', '.join(item['destinations'])}`.",
            f"- Classification: **{item['classification']}**.",
            f"- Recommendation: {item['recommendation']}",
            "",
        ])
    _write(output_dir / "10_multi_destination_cases.md", "\n".join(multi_lines))

    reviewed_summary = Counter(item["status"] for item in reviewed_proposals)
    import_plan = [
        "# Import plan",
        "",
        "- Input proposals: `11_knowledge_approved.json`.",
        f"- Reviewed proposals: **{len(reviewed_proposals)}**; APPROVE={reviewed_summary.get('APPROVE', 0)}, REVIEW={reviewed_summary.get('REVIEW', 0)}, REJECT={reviewed_summary.get('REJECT', 0)}.",
        "- Import identity: tenant-scoped `department_id` when resolvable; destination/name are only human-readable resolution aids. Content/priority changes are reported as UPDATE.",
        "- Expected operations: CREATE, UPDATE, SKIP, CONFLICT, NEW DEPARTMENT and REVIEW. REVIEW/CONFLICT are non-importable until explicitly resolved.",
        "- Missing departments are reported as `NEW DEPARTMENT`; they are never created implicitly by the Knowledge import.",
        "- Deletions are not supported. The importer defaults to dry-run and the caller owns commit/rollback.",
        "- The historical proposal JSON referenced by the brief was not present. This generated set remains a conservative, reviewable hypothesis derived from the normalized CSV and reconciled with the local tenant snapshot.",
        "",
        "## Source validation",
        "",
        f"- Original rows: **{len(original)}**; normalized rows: **{len(normalized)}**; row differences: **{len(source_differences)}**.",
        "- Full message bodies are used only in the evaluation JSONL; reports contain case IDs and compact semantic summaries.",
        "- The local source snapshot was read-only. Its Knowledge table predates the pending related-department/priority migration, so those absent columns were treated as defaults for comparison; no migration was applied.",
        "",
        "## Future real-model evaluation (not executed)",
        "",
        "Use the existing evaluation runner only after adding a sanitized prediction file from the real routing pipeline; do not point it at operational endpoints or enable forwarding:",
        "",
        "`PYTHONPATH=backend python3 backend/scripts/evaluate_agent.py --fixture docs/routing-learning/generated/07_evaluation_dataset.jsonl --predictions /private/tmp/kibak-luna-predictions.json --allow-mismatch`",
        "",
        "The current runner expects JSON cases with `id`/`expected`, so a future adapter must map the JSONL records and persist only sanitized prediction metadata, latency and provider usage. No Luna call was made in this phase.",
    ]
    _write(output_dir / "08_import_plan.md", "\n".join(import_plan) + "\n")
    return {"rows": len(normalized), "proposals": len(proposals), "output_dir": str(output_dir)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--normalized", type=Path, required=True)
    parser.add_argument("--source-json", type=Path, help="Read-only tenant department/Knowledge snapshot used for reconciliation")
    parser.add_argument("--output-dir", type=Path, default=Path("docs/routing-learning/generated"))
    args = parser.parse_args()
    print(json.dumps(generate_artifacts(args.original, args.normalized, args.output_dir, args.source_json), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
