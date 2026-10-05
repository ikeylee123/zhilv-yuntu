"""Offline synthetic evaluation, not a production benchmark. Predictions see input only."""
from __future__ import annotations
import argparse
from collections import Counter
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.models.schemas import PlanningState
from app.services.plan_validator import validate_plan

DEFAULT_CASES = Path(__file__).resolve().parents[1] / "eval" / "planning_eval_cases.json"


def predict(inputs: dict) -> dict:
    report = validate_plan(PlanningState.model_validate(inputs))
    return {
        "status": report.status,
        "error_types": sorted({v.type for v in report.violations if v.severity == "error"}),
        "unknown_types": sorted({str(v.expected) if v.type == "unverifiable_hard_constraint" else v.type
                                 for v in report.unknowns}),
    }


def ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def evaluate(cases: list[dict]) -> dict:
    # Complete inference before touching expected labels. No case ID/category enters predict().
    predictions = [predict(case["input"]) for case in cases]
    tp = tn = fp = fn = correct = unknown_hits = unknown_total = unknown_predicted = 0
    per_type = {}
    mismatches = []
    for case, prediction in zip(cases, predictions):
        expected = case["expected"]
        correct += prediction["status"] == expected["status"]
        # Positive = cannot certify valid (hard violation OR required unknown).
        positive = expected["status"] != "valid"
        predicted_positive = prediction["status"] != "valid"
        tp += positive and predicted_positive
        tn += not positive and not predicted_positive
        fp += not positive and predicted_positive
        fn += positive and not predicted_positive
        expected_errors, actual_errors = set(expected["error_types"]), set(prediction["error_types"])
        for kind in sorted(expected_errors | actual_errors):
            counts = per_type.setdefault(kind, {"tp": 0, "fp": 0, "fn": 0})
            counts["tp"] += kind in expected_errors and kind in actual_errors
            counts["fp"] += kind not in expected_errors and kind in actual_errors
            counts["fn"] += kind in expected_errors and kind not in actual_errors
        expected_unknown, actual_unknown = set(expected["unknown_types"]), set(prediction["unknown_types"])
        unknown_hits += len(expected_unknown & actual_unknown)
        unknown_total += len(expected_unknown)
        unknown_predicted += len(actual_unknown)
        if (prediction["status"] != expected["status"] or expected_errors != actual_errors
                or expected_unknown != actual_unknown):
            mismatches.append({"id": case["id"], "expected": expected, "actual": prediction})
    for counts in per_type.values():
        counts["detection_rate"] = ratio(counts["tp"], counts["tp"] + counts["fn"])
        counts["precision"] = ratio(counts["tp"], counts["tp"] + counts["fp"])
    return {
        "cases": len(cases), "category_counts": dict(Counter(c["category"] for c in cases)),
        "overall_validation_accuracy": ratio(correct, len(cases)),
        "binary_validity_accuracy": ratio(tp + tn, len(cases)),
        "precision": ratio(tp, tp + fp), "recall": ratio(tp, tp + fn),
        "fpr": ratio(fp, fp + tn), "fnr": ratio(fn, fn + tp),
        "confusion": {"tp": tp, "tn": tn, "fp": fp, "fn": fn},
        "per_violation_type": per_type,
        "unknown_detection_rate": ratio(unknown_hits, unknown_total),
        "unknown_precision": ratio(unknown_hits, unknown_predicted),
        "mismatches": mismatches,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    args = parser.parse_args()
    data = json.loads(args.cases.read_text(encoding="utf-8"))
    results = evaluate(data["cases"])
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 1 if results["mismatches"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
