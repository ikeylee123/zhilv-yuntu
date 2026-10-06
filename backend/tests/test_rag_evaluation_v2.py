import json
from pathlib import Path
from types import SimpleNamespace as Obj
import pytest
from app.rag.evaluation import evaluate_result,aggregate
from scripts.evaluate_rag_v2 import run

DATA=Path(__file__).parents[1]/"eval/rag_eval_cases_v2.json"
def result(ids=(),outcome="success",errors=()):
    return Obj(chunks=[Obj(chunk_id=i,source_id="source",destination="北京") for i in ids],outcome=outcome,errors=errors,stage_events=[])
def case(**kwargs):return dict(id="test",destination="北京",relevant_chunk_ids=["a","b"],**kwargs)

def test_multiple_relevant_mrr_and_recall():
    row=evaluate_result(case(),result(["x","a","b"]))
    assert row["recall@1"]==0 and row["recall@3"]==1 and row["mrr"]==.5

def test_alternative_complete_sets():
    row=evaluate_result(case(acceptable_chunk_sets=[["c","d"]]),result(["c","d"]))
    assert row["recall@3"]==1 and row["mrr"]==1

def test_duplicate_rank_does_not_inflate_recall():
    row=evaluate_result(case(),result(["a","a","b"]))
    assert row["recall@1"]==.5 and row["recall@3"]==1 and row["duplicate_count"]==1
    row=evaluate_result(case(),result(["x","x","a"]))
    assert row["mrr"]==pytest.approx(1/3)

def test_evidence_groups():
    row=evaluate_result(case(required_evidence_groups=[["a","c"],["b"]]),result(["c"]))
    assert row["evidence_group_coverage"]==.5 and row["complete_evidence_coverage"]==0

@pytest.mark.parametrize("code",["unsupported_destination","no_matching_knowledge"])
def test_no_result_and_failure_class(code):
    item=dict(id="none",destination="北京",expected_outcome="no_data",expected_error_code=code)
    row=evaluate_result(item,result(outcome="no_data",errors=[code]))
    assert row["no_result_correct"]==row["failure_class_correct"]==1
    assert row["mrr"] is None

def test_zero_denominator():
    summary=aggregate([])
    assert summary["mrr"] is None and summary["destination_contamination_rate"] is None
    assert summary["mrr_denominator"]==0

def test_failure_mismatch():
    row=evaluate_result(dict(id="none",destination="北京",expected_outcome="no_data",expected_error_code="unsupported_destination"),result(outcome="error",errors=["vector_store_error"]))
    assert row["failure_class_correct"]==0

def test_source_labels():
    row=evaluate_result(dict(id="source",destination="北京",relevant_source_ids=["source"]),result(["a"]))
    assert row["recall@1"]==1

def test_dataset_distribution_and_static_id_labels():
    from collections import Counter
    from app.rag.vector_db import load_guide_chunks
    dataset=json.loads(DATA.read_text(encoding="utf-8"))
    cases=dataset["cases"]
    assert Counter(c["category"] for c in cases)==dict(exact_lexical=12,semantic_preference=12,destination_disambiguation=10,multi_chunk_evidence=8,query_rewrite=6,failure_unsupported=6,adversarial_noisy=6)
    ids={c["id"] for c in load_guide_chunks()}
    assert all(set(c["relevant_chunk_ids"]) <= ids for c in cases)
    assert len({c["id"] for c in cases})==60

def test_default_offline_with_credentials_does_not_network(monkeypatch):
    import socket
    monkeypatch.setenv("LLM_API_KEY","fake-but-configured")
    monkeypatch.setattr(socket.socket,"connect",lambda *a,**k: (_ for _ in ()).throw(AssertionError("network forbidden")))
    report=run(DATA)
    assert report["summary"]["cases"]==60 and not report["live"]

def test_live_ablation_requires_opt_in():
    with pytest.raises(ValueError,match="--live"):run(DATA,mode="vector_only")

def test_snapshot_mismatch_refused(tmp_path):
    data=json.loads(DATA.read_text(encoding="utf-8"));data["corpus_manifest_version"]="wrong"
    path=tmp_path/"cases.json";path.write_text(json.dumps(data),encoding="utf-8")
    with pytest.raises(ValueError,match="snapshot mismatch"):run(path)
