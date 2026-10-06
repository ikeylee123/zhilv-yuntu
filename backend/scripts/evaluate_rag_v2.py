"""Default offline keyword baseline; vector ablations require explicit --live."""
import argparse
import json
import sys
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
MODES = ("vector_only", "vector_rewrite", "vector_rerank", "vector_rewrite_rerank", "keyword_baseline")

def run(path, *, mode="keyword_baseline", live=False):
    if mode not in MODES:
        raise ValueError("Unknown ablation mode")
    if mode != "keyword_baseline" and not live:
        raise ValueError("Vector modes require explicit --live; no offline vector quality is claimed")
    from app.rag import versions, retriever, vector_db, knowledge_service
    from app.agents.tools import rag_tool
    from app.models.knowledge_retrieval import KnowledgeQuery
    from app.rag.rewrite import rewrite_query
    from app.rag.evaluation import evaluate_result, aggregate, preflight_dataset
    from app.rag.snapshot import load_snapshot
    dataset = json.loads(Path(path).read_text(encoding="utf-8"))
    snapshot = load_snapshot()
    cases = preflight_dataset(dataset, snapshot)
    rows = []
    with ExitStack() as stack:
        # Evaluations are cache-independent; the offline mode never invokes providers.
        for module in (retriever, knowledge_service):
            stack.enter_context(patch.object(module, "get_cached_json", return_value=None))
            stack.enter_context(patch.object(module, "set_cached_json", return_value=False))
        if not live or mode == "keyword_baseline":
            stack.enter_context(patch.object(vector_db, "_embed_query_with_usage", side_effect=AssertionError("offline embedding call")))
            stack.enter_context(patch.object(retriever, "_rerank_with_dashscope", side_effect=AssertionError("offline rerank call")))
            stack.enter_context(patch.object(rag_tool, "_build_chat_llm", side_effect=AssertionError("offline rewrite call")))
        for case in cases:
            request = KnowledgeQuery(query=case["query"], destination=case["destination"], top_k=6)
            rewrite = None
            if "rewrite" in mode:
                rewrite = rewrite_query(request.query, request.destination,
                    provider=lambda: rag_tool.llm_rewrite_query(request.destination, special_notes=request.query))
            elif mode == "keyword_baseline" and case["category"] == "query_rewrite":
                # Explicit deterministic replay tests rewrite policy, not LLM quality.
                replay_payload = case["rewrite_replay"]
                rewrite = rewrite_query(request.query, request.destination,
                    provider=lambda payload=replay_payload: (payload, None))
            result = knowledge_service.retrieve_knowledge(request, mode=mode, rewrite=rewrite, allow_fallback=mode == "keyword_baseline")
            row = evaluate_result(case, result)
            row.update(case_id=case["id"], mode=mode, error_code=result.errors[0] if result.errors else None,
                       stage_events=[e.model_dump() for e in result.stage_events],
                       degraded=result.degraded, metadata=result.metadata.model_dump(mode="json"))
            if live and mode != "keyword_baseline":
                from app.config import EMBEDDING_MODEL, LLM_MODEL, RERANK_MODEL, LLM_PROVIDER
                row["providers"] = {"embedding": {"provider": "openai_compatible", "model": EMBEDDING_MODEL},
                    "rewrite": {"provider": LLM_PROVIDER, "model": LLM_MODEL},
                    "rerank": {"provider": "dashscope", "model": RERANK_MODEL}}
            row["rewrite_status"] = result.metadata.rewrite_status
            row["rewrite_status_correct"] = (result.metadata.rewrite_status == case["expected_rewrite_status"]) if "expected_rewrite_status" in case and mode == "keyword_baseline" else None
            rows.append(row)
    return {"mode": mode, "live": live and mode != "keyword_baseline", "index_version": versions.index_version(),
            "retrieval_config_version": versions.retrieval_config_version(), "summary": aggregate(rows),
            "categories": {category: aggregate([r for r in rows if r["category"] == category]) for category in sorted({r["category"] for r in rows})}, "cases": rows}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=BACKEND_DIR / "eval/rag_eval_cases_v2.json")
    parser.add_argument("--mode", choices=MODES, default="keyword_baseline")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    result = run(args.cases, mode=args.mode, live=args.live)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
