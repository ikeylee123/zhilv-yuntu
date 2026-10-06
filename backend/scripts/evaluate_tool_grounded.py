"""Offline fixed-fact evaluation. Predictions never receive labels or case IDs."""
from __future__ import annotations
from collections import Counter
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.models.schemas import PlanningState
from app.services.plan_validator import validate_plan

DEFAULT_CASES = Path(__file__).resolve().parents[1] / "eval" / "tool_grounded_eval_cases.json"


def ratio(n, d):
    return n / d if d else None


def predict(inputs):
    report = validate_plan(PlanningState.model_validate(inputs))
    return {"status": report.status,
            "error_types": sorted({v.type for v in report.violations if v.severity == "error"}),
            "unknown_types": sorted({v.type for v in report.unknowns}),
            "route_unknown_reasons": sorted({v.actual["reason"] for v in report.unknowns
                                           if v.type == "route_feasibility_unverifiable"}),
            "coverage": {key: getattr(report.route_coverage, key) for key in
                         ("required_pairs", "verified_pairs", "unknown_pairs", "skipped_pairs")}}


def evaluate(cases):
    predictions = [predict(case["input"]) for case in cases]
    tp = tn = fp = fn = correct = 0
    violation_tp = violation_fp = violation_fn = 0
    unknown_hits = unknown_total = unknown_predicted = 0
    utilized_known = eligible_known = unexpected_verified = 0
    counts = {}
    coverage = Counter({key: 0 for key in ("required_pairs", "verified_pairs", "unknown_pairs", "skipped_pairs")})
    mismatches = []
    for case, actual in zip(cases, predictions):
        expected = case["expected"]
        correct += actual["status"] == expected["status"]
        positive, predicted_positive = expected["status"] != "valid", actual["status"] != "valid"
        tp += positive and predicted_positive
        tn += not positive and not predicted_positive
        fp += not positive and predicted_positive
        fn += positive and not predicted_positive
        truth = set(expected["error_types"]) | set(expected["unknown_types"])
        predicted = set(actual["error_types"]) | set(actual["unknown_types"])
        violation_tp += len(truth & predicted)
        violation_fp += len(predicted - truth)
        violation_fn += len(truth - predicted)
        for kind in truth | predicted:
            bucket = counts.setdefault(kind, {"tp": 0, "fp": 0, "fn": 0})
            bucket["tp"] += kind in truth and kind in predicted
            bucket["fp"] += kind not in truth and kind in predicted
            bucket["fn"] += kind in truth and kind not in predicted
        expected_unknown, actual_unknown = set(expected["unknown_types"]), set(actual["unknown_types"])
        unknown_hits += len(expected_unknown & actual_unknown)
        unknown_total += len(expected_unknown)
        unknown_predicted += len(actual_unknown)
        # Pair-count utilization against independently declared eligible-pair counts.
        eligible = expected["eligible_known_pairs"]
        eligible_known += eligible
        utilized_known += min(actual["coverage"]["verified_pairs"], eligible)
        unexpected_verified += max(0, actual["coverage"]["verified_pairs"] - eligible)
        coverage.update(actual["coverage"])
        comparable = {key: expected[key] for key in actual}
        if actual != comparable:
            mismatches.append({"id": case["id"], "expected": comparable, "actual": actual})
    for bucket in counts.values():
        bucket["precision"] = ratio(bucket["tp"], bucket["tp"] + bucket["fp"])
        bucket["recall"] = ratio(bucket["tp"], bucket["tp"] + bucket["fn"])
    return {"cases": len(cases), "category_counts": dict(Counter(c["category"] for c in cases)),
            "three_state_status_accuracy": ratio(correct, len(cases)),
            "binary_validity_accuracy": ratio(tp + tn, len(cases)),
            "confusion": {"tp": tp, "tn": tn, "fp": fp, "fn": fn},
            "violation_precision": ratio(violation_tp, violation_tp + violation_fp),
            "violation_recall": ratio(violation_tp, violation_tp + violation_fn),
            "per_violation_type": dict(sorted(counts.items())),
            "route_time_violation_detection_rate": counts.get("route_time_violation", {}).get("recall"),
            "route_feasibility_unverifiable_detection_rate": counts.get("route_feasibility_unverifiable", {}).get("recall"),
            "unknown_detection_rate": ratio(unknown_hits, unknown_total),
            "unknown_precision": ratio(unknown_hits, unknown_predicted),
            "known_fact_utilization_rate": ratio(utilized_known, eligible_known),
            "eligible_known_pairs": eligible_known, "unexpected_verified_pairs": unexpected_verified,
            "route_coverage": dict(coverage),
            "route_verified_fraction": ratio(coverage["verified_pairs"], coverage["required_pairs"]),
            "mismatches": mismatches}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    args = parser.parse_args()
    result = evaluate(json.loads(args.cases.read_text(encoding="utf-8"))["cases"])
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return int(bool(result["mismatches"]))


if __name__ == "__main__":
    raise SystemExit(main())
