# Resume claims and evidence

These bullets describe implementation, not ownership of every historical repository feature. Use only work you can personally explain and substantiate. Numbers below are fixed offline regression sizes, not production accuracy.

## English bullets

1. Built a constraint-aware travel planning backend that separates LLM generation from deterministic validation and server-grounded POI/route evidence, explicitly classifying plans as valid, invalid or unverifiable.
2. Implemented single-day partial replanning with stable activity IDs, optimistic concurrency and independently checked preservation of unaffected content; only valid replans update authoritative state.
3. Bounded LLM-assisted edits to one-shot selection from server-authorized candidates, rejecting invented IDs and stale versions before they can bypass deterministic patching and validation.
4. Hardened destination retrieval with typed results, chunk provenance and versioned indexes/caches; added reproducible offline evaluation and an optional typed Route/POI MCP gateway.

## 中文简历 bullets

1. 构建约束感知旅行规划后端，将 LLM 生成与确定性校验、服务端 POI/路线证据采集解耦，显式区分 valid、invalid 与 unverifiable，避免把缺失证据视为满足约束。
2. 实现单日局部重规划，通过稳定活动 ID、版本 CAS 和内容保留检查控制修改范围，仅允许校验通过的变更写入权威状态。
3. 将 LLM 辅助编辑限制为一次性选择服务端授权候选，对虚构 ID、过期版本和异常输出执行拒绝或不确定性处理，保留确定性合并与校验边界。
4. 完成目的地 RAG 来源与版本加固，建立 typed result、chunk 来源校验及索引/缓存版本绑定，配套可复现离线评估和可选 Route/POI MCP 网关。

## Evidence map

| Bullet | Source files | Tests | Evaluator | Qualification |
|---|---|---|---|---|
| 1 — reliability | [validator](../backend/app/services/plan_validator.py), [fact collector](../backend/app/services/planning_fact_service.py), [route rules](../backend/app/services/route_validation.py) | [validator tests](../backend/tests/test_plan_validator.py), [route facts](../backend/tests/test_route_facts.py), [route workflow](../backend/tests/test_route_workflow.py) | [67-case validator](../backend/scripts/evaluate_planning.py), [48-case route](../backend/scripts/evaluate_tool_grounded.py) | Driving-only, estimated costs; initial drafts are not valid-only. |
| 2 — state | [authoritative state](../backend/app/services/authoritative_state.py), [replanning](../backend/app/services/replanning_service.py), [patch/preservation](../backend/app/services/plan_patch_service.py) | [state tests](../backend/tests/test_authoritative_state.py), [preservation](../backend/tests/test_replanning_preservation.py), [commit tests](../backend/tests/test_replanning_commit.py) | [50-case replanning](../backend/scripts/evaluate_replanning.py), [E2E](../backend/scripts/evaluate_end_to_end.py) | Single-day/single-operation attraction edits; SQLite CAS; no distributed guarantee. |
| 3 — model boundary | [selector](../backend/app/agents/candidate_selector.py), [selection orchestration](../backend/app/services/replanning_selection.py) | [boundary hardening](../backend/tests/test_candidate_selector_hardening.py), [real replan path](../backend/tests/test_replanning_llm_selection.py) | [36-case selector](../backend/scripts/evaluate_candidate_selection.py), E2E selector scenarios | Replay outputs test authorization/failure handling, not live preference quality or model injection resistance. |
| 4 — retrieval/tools | [knowledge service](../backend/app/rag/knowledge_service.py), [snapshot](../backend/app/rag/snapshot.py), [index](../backend/app/rag/index_store.py), [MCP server](../backend/app/mcp/server.py) | [integrity hardening](../backend/tests/test_rag_integrity_hardening.py), [MCP tests](../backend/tests/test_mcp_gateway.py) | [60-case RAG](../backend/scripts/evaluate_rag_v2.py), [28-case MCP](../backend/scripts/evaluate_mcp_contract.py) | Six-city static corpus; keyword semantic weakness retained; optional protocol, direct production tools. |

If space permits one measured result: “Verified failure-state preservation in 6/6 failed replanning scenarios within a 24-scenario offline cross-layer benchmark.” This is more precise than claiming an AI accuracy improvement. The larger 11/11 measure also includes read-only route and knowledge failures.

## Role positioning

| Role | Lead with | Useful evidence to discuss |
|---|---|---|
| AI Agent Engineer | Tool authority, state ownership and bounded model actions | Typed tools/MCP equivalence, version races, grant recheck, preservation after selection failure. |
| LLM Application Engineer | Structured generation, explicit uncertainty and retrieval | Draft parsing, deterministic validator, provenance/cache failures, controlled fallback and measured semantic weakness. |
| Applied AI Engineer | System reliability and evaluation design | Independent invariants, fixed labels and denominators, negative controls, model/system boundaries and honest deployment limits. |

Do not list unimplemented technologies. Do not turn offline tool-call counts into production cost savings. The complete [claim-safety matrix](claim-safety.md) takes precedence over abbreviated resume wording.
