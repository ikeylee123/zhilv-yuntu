"""Chunk-level metrics. Irrelevant duplicates occupy rank but never earn extra credit."""
def ratio(numerator, denominator):
    return numerator / denominator if denominator else None

def evaluate_result(case, result):
    ranked = [c.chunk_id for c in result.chunks]
    relevant = set(case.get("relevant_chunk_ids", []))
    alternatives = [set(group) for group in case.get("acceptable_chunk_sets", [])]
    targets = ([relevant] if relevant else []) + alternatives
    if not targets and case.get("required_evidence_groups"):
        targets = [set().union(*map(set, case["required_evidence_groups"]))]
    relevant_union = set().union(*targets) if targets else set()
    sources = set(case.get("relevant_source_ids", []))
    if not targets and sources:
        # Source-only labels have a source-level denominator, explicitly separate.
        targets = [sources]
        ranking_for_recall = [c.source_id for c in result.chunks]
        hits = [c.source_id in sources for c in result.chunks]
    else:
        ranking_for_recall = ranked
        hits = [chunk_id in relevant_union for chunk_id in ranked]
    row = {"id": case["id"], "category": case.get("category"), "returned": len(ranked)}
    for k in (1, 3, 5, 6):
        row[f"recall@{k}"] = max((len(set(ranking_for_recall[:k]) & group)/len(group) for group in targets if group), default=None)
        row[f"hit@{k}"] = int(any(hits[:k])) if targets else None
    row["mrr"] = next((1/(i+1) for i,hit in enumerate(hits) if hit), 0.0) if targets else None
    row["contamination_count"] = sum(c.destination != case.get("canonical_destination", case["destination"]) for c in result.chunks)
    row["duplicate_count"] = len(ranked)-len(set(ranked))
    expected = case.get("expected_outcome", "success")
    code = case.get("expected_error_code")
    actual_codes = set(result.errors) | {e.code for e in result.stage_events}
    row["outcome_correct"] = int(result.outcome == expected)
    row["no_result_correct"] = int(not ranked and result.outcome == expected) if expected != "success" else None
    row["failure_class_correct"] = int(result.outcome == expected and code in actual_codes) if code else None
    groups = case.get("required_evidence_groups", [])
    found = sum(bool(set(group) & set(ranked)) for group in groups)
    row["evidence_group_coverage"] = ratio(found, len(groups))
    row["complete_evidence_coverage"] = int(found == len(groups)) if groups else None
    row["outcome"] = result.outcome
    row["errors"] = list(result.errors)
    row["ranked_chunk_ids"] = ranked
    return row

def aggregate(rows):
    names = [f"{metric}@{k}" for metric in ("recall", "hit") for k in (1,3,5,6)] + [
        "mrr", "no_result_correct", "failure_class_correct", "evidence_group_coverage", "complete_evidence_coverage", "outcome_correct"]
    summary = {"cases": len(rows)}
    for name in names:
        values = [r[name] for r in rows if r.get(name) is not None]
        summary[name] = ratio(sum(values), len(values))
        summary[name + "_denominator"] = len(values)
    count = sum(r["returned"] for r in rows)
    summary["returned_chunks"] = count
    summary["destination_contamination_rate"] = ratio(sum(r["contamination_count"] for r in rows), count)
    summary["duplicate_result_rate"] = ratio(sum(r["duplicate_count"] for r in rows), count)
    return summary


def preflight_dataset(dataset, snapshot):
    """Validate the entire label set before the first retrieval/provider call."""
    from app.rag.versions import stable_hash
    from app.models.knowledge_retrieval import KnowledgeQuery
    if not isinstance(dataset, dict) or dataset.get("corpus_manifest_version") != stable_hash(snapshot.manifest):
        raise ValueError("Dataset corpus snapshot mismatch")
    if dataset.get("expected_index_version", snapshot.index_version) != snapshot.index_version:
        raise ValueError("Dataset index snapshot mismatch")
    cases = dataset.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Dataset requires nonempty cases")
    ids, sources = set(snapshot.members), {c["source_id"] for c in snapshot.chunks}
    seen = set()
    def labels(value, allowed):
        if not isinstance(value, list) or any(not isinstance(v, str) or v not in allowed for v in value):
            raise ValueError("Invalid or missing relevance ID")
        if len(value) != len(set(value)):
            raise ValueError("Duplicate relevance ID")
        return value
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not case["id"] or case["id"] in seen:
            raise ValueError("Invalid or duplicate case ID")
        seen.add(case["id"])
        request = KnowledgeQuery(query=case.get("query"), destination=case.get("destination"), top_k=6)
        if case.get("canonical_destination", request.destination) != request.destination:
            raise ValueError("Invalid canonical destination label")
        positive = labels(case.get("relevant_chunk_ids", []), ids)
        source_positive = labels(case.get("relevant_source_ids", []), sources)
        groups = {}
        for name in ("acceptable_chunk_sets", "required_evidence_groups"):
            value = case.get(name, [])
            if not isinstance(value, list):
                raise ValueError("Malformed evidence groups")
            groups[name] = [labels(group, ids) for group in value]
            if any(not group for group in groups[name]):
                raise ValueError("Empty positive evidence group")
        has_positive = bool(positive or source_positive or any(groups.values()))
        outcome = case.get("expected_outcome", "success")
        if outcome not in ("success", "no_data", "error"):
            raise ValueError("Invalid expected outcome")
        positive_categories = {"exact_lexical", "semantic_preference", "destination_disambiguation", "multi_chunk_evidence", "query_rewrite", "adversarial_noisy"}
        if (outcome == "success" or case.get("category") in positive_categories) and not has_positive:
            raise ValueError("Positive case missing relevance labels")
        if outcome != "success" and has_positive:
            raise ValueError("Non-success case has positive labels")
        if outcome == "error" and not case.get("expected_error_code"):
            raise ValueError("Error case requires explicit error code")
        if any(not any(c["source_id"] == source and c["destination"] == request.destination for c in snapshot.chunks) for source in source_positive):
            raise ValueError("Cross-destination source label")
        for identity in positive + [i for group in groups.values() for g in group for i in g]:
            if snapshot.members[identity]["destination"] != request.destination:
                raise ValueError("Cross-destination relevance label")
    return cases
