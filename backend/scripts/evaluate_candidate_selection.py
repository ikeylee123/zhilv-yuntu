"""Separate selector evaluation. Offline replay by default; --live explicitly opts in."""
import argparse
from collections import Counter
import json
from pathlib import Path
from statistics import median
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.agents.candidate_selector import CandidateSelector, CompatibleSelectionClient
from app.models.candidate_selection import CandidateSelectionInput, CandidateSelectionOutput

DATASET = Path(__file__).resolve().parents[1] / "eval" / "candidate_selection_eval_cases.json"


class ReplayClient:
    def __init__(self, case):
        self.case, self.calls = case, 0

    def invoke(self, messages, timeout):
        self.calls += 1
        if self.case["replay_error"] == "timeout":
            raise TimeoutError()
        if self.case["replay_error"] == "provider":
            raise RuntimeError("Offline injected failure")
        return SimpleNamespace(content=self.case["replay_response"], usage_metadata=None)


def evaluate(*, live=False):
    cases = json.loads(DATASET.read_text(encoding="utf-8"))
    rows, mismatches, latencies, tokens, call_counts = [], [], [], [], []
    for case in cases:
        # Invalid-output and provider-failure rows are controlled injections, not live tasks.
        if live and case["category"] in ("invalid_output", "provider_failure"):
            continue
        data = CandidateSelectionInput.model_validate_json(json.dumps(case["input"]))
        client = None if live else ReplayClient(case)
        result = CandidateSelector(client).select(data)
        if client is not None:
            call_counts.append(client.calls)
        ids = {c.candidate_id for c in data.candidates}
        schema_valid = result.output is not None or result.error == "LLM_SELECTION_NOT_AUTHORIZED"
        authorized = result.output is not None and result.output.candidate_id in ids
        acceptable = authorized and result.output.candidate_id in case["acceptable_candidate_ids"]
        matched = (result.error == case["expected_error"] and client.calls == 1
            and (acceptable if case["expected_error"] is None else result.output is None)) if not live else None
        rows.append(dict(id=case["id"], schema_valid=schema_valid, authorized=authorized,
            acceptable=acceptable, error=result.error, contract_match=matched))
        if matched is False:
            mismatches.append(rows[-1])
        rows[-1]["expected_error"] = case["expected_error"] if not live else None
        latencies.append(result.latency_ms)
        if result.input_tokens is not None and result.output_tokens is not None:
            tokens.append(result.input_tokens + result.output_tokens)
    n = len(rows)
    def enforcement(code=None):
        applicable = [r for r in rows if r["expected_error"] is not None
            and (code is None or r["expected_error"] == code)]
        return [sum(r["error"] == r["expected_error"] for r in applicable), len(applicable)]
    report = dict(mode="live" if live else "offline_replay", cases=n,
        distribution=dict(Counter(c["category"] for c in cases if not live or c["category"] not in ("invalid_output","provider_failure"))),
        schema_compliance=[sum(r["schema_valid"] for r in rows), n],
        authorized_selection=[sum(r["authorized"] for r in rows), n],
        acceptable_choice=[sum(r["acceptable"] for r in rows), n],
        authorized_id_enforcement=None if live else enforcement("LLM_SELECTION_NOT_AUTHORIZED"),
        invalid_schema_enforcement=None if live else enforcement("LLM_SELECTION_INVALID"),
        selection_failure_mapping=None if live else enforcement(),
        contract_matches=None if live else [sum(r["contract_match"] for r in rows), n],
        one_shot_calls=None if live else [sum(c == 1 for c in call_counts), n],
        median_latency_ms=median(latencies), median_total_tokens=median(tokens) if tokens else None,
        mismatches=mismatches,
        interpretation="Live selection quality; not replanning accuracy" if live else
            "Injected replay outputs test enforcement and failure mapping, not actual model preference or injection resistance")
    return report


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live",action="store_true",help="Explicitly enable configured model network calls")
    args=parser.parse_args()
    report=evaluate(live=args.live)
    print(json.dumps(report,indent=2))
    raise SystemExit(1 if report["mismatches"] else 0)
