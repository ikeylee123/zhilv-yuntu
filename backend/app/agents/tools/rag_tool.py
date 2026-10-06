import json
import logging
from functools import lru_cache

from app.config import (
    BACKEND_DIR,
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MAX_RETRIES,
    LLM_MODEL,
    LLM_TIMEOUT_SECONDS,
)
from app.rag.retriever import retrieve_travel_guide
from app.rag.telemetry import legacy_usage, observe_result, parse_usage


logger = logging.getLogger(__name__)
RETRIEVAL_RULES_PATH = BACKEND_DIR / "data" / "retrieval_rules.json"


# rag_tool.py 自己不直接检索，
# 它只负责把"旅行规划语义"转成"检索查询"。
def _append_unique(parts: list[str], value: str) -> None:
    normalized = value.strip()
    if not normalized:
        return
    if normalized not in parts:
        parts.append(normalized)


@lru_cache(maxsize=1)
def _parse_retrieval_rules(raw_text: str) -> dict:
    try:
        value = json.loads(raw_text)
    except json.JSONDecodeError:
        logger.warning("Invalid retrieval rules JSON")
        return {"global_rules": [], "destinations": {}}
    return value if isinstance(value, dict) else {"global_rules": [], "destinations": {}}


def _load_retrieval_rules() -> dict:
    """Cache by file content, so changed rules and their version stay aligned."""
    try:
        return _parse_retrieval_rules(RETRIEVAL_RULES_PATH.read_text(encoding="utf-8"))
    except OSError:
        logger.warning("Retrieval rules unavailable")
        return {"global_rules": [], "destinations": {}}


# Preserve the existing cache-reset seam for legacy callers/tests.
_load_retrieval_rules.cache_clear = _parse_retrieval_rules.cache_clear


def _valid_rules(value: object) -> list[dict[str, list[str]]]:
    """过滤格式错误的单条规则，保证规则配置不会破坏 query fallback。"""
    if not isinstance(value, list):
        return []

    valid_rules: list[dict[str, list[str]]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        triggers = item.get("triggers")
        keywords = item.get("keywords")
        if not isinstance(triggers, list) or not isinstance(keywords, list):
            continue
        if not all(isinstance(term, str) for term in [*triggers, *keywords]):
            continue
        if triggers and keywords:
            valid_rules.append({"triggers": triggers, "keywords": keywords})
    return valid_rules


def _destination_rules(destination: str | None) -> list[dict[str, list[str]]]:
    """返回通用规则及当前目的地规则，兼容“大理市”等输入。"""
    config = _load_retrieval_rules()
    rules = _valid_rules(config.get("global_rules"))
    if not destination:
        return rules

    destinations = config.get("destinations")
    if not isinstance(destinations, dict):
        return rules

    for configured_destination, destination_config in destinations.items():
        if not isinstance(configured_destination, str) or configured_destination not in destination:
            continue
        if not isinstance(destination_config, dict):
            continue
        rules.extend(_valid_rules(destination_config.get("rules")))
    return rules


def _extract_note_keywords(special_notes: str | None, destination: str | None = None) -> list[str]:
    """根据版本化规则，从用户备注中提炼适合检索的关键词。"""
    if not special_notes:
        return []

    keywords: list[str] = []
    note = special_notes.strip()
    for rule in _destination_rules(destination):
        if any(trigger in note for trigger in rule["triggers"]):
            for value in rule["keywords"]:
                _append_unique(keywords, value)

    return keywords


def _build_chat_llm():
    """创建 ChatOpenAI 实例，用于 Query Rewrite。"""
    if not LLM_API_KEY:
        return None
    try:
        from langchain_openai import ChatOpenAI
    except ImportError:
        return None
    return ChatOpenAI(
        model=LLM_MODEL,
        temperature=0.2,
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL or None,
        timeout=LLM_TIMEOUT_SECONDS,
        max_retries=LLM_MAX_RETRIES,
    )


def _extract_token_usage(response) -> dict[str, int]:
    """从 LangChain AIMessage 中提取 token 使用量。"""
    metadata = getattr(response, "response_metadata", None)
    return parse_usage(metadata.get("token_usage") if isinstance(metadata, dict) else None)


def llm_rewrite_query(
    destination: str,
    preferences: list[str] | None = None,
    pace: str | None = None,
    special_notes: str | None = None,
) -> tuple[str | None, dict[str, int]]:
    """用 LLM 把用户旅行需求改写成适合向量检索的 query。返回 (query, token_usage)。"""
    empty_usage = {"prompt_tokens": 0, "completion_tokens": 0}
    try:
        llm = _build_chat_llm()
    except Exception:
        return None, empty_usage
    if llm is None:
        return None, empty_usage

    system_prompt = (
        "你是一个 RAG 检索 query 改写专家。"
        "你的任务是把用户的旅行需求改写成适合向量检索的关键词组合。"
        "输出要求："
        "1. 只输出检索关键词，用空格分隔"
        "2. 不要输出解释、标点或任何多余文字"
        "3. 关键词要具体，优先包含景点名称、活动类型、场景特征"
        "4. 包含目的地城市名"
    )

    parts = [f"目的地：{destination}"]
    if preferences:
        parts.append(f"偏好：{'、'.join(preferences)}")
    if pace:
        parts.append(f"节奏：{pace}")
    if special_notes:
        parts.append(f"备注：{special_notes}")
    human_prompt = "\n".join(parts)

    try:
        response = llm.invoke([
            ("system", system_prompt),
            ("human", human_prompt),
        ])
        token_usage = _extract_token_usage(response)
        raw_text = getattr(response, "content", "")
        if isinstance(raw_text, list):
            raw_text = "".join(
                item.get("text", "") if isinstance(item, dict) else str(item)
                for item in raw_text
            )
        query = raw_text.strip()
        if query:
            logger.info("llm_rewrite_query completed")
            return query, token_usage
    except Exception:
        logger.warning("llm_rewrite_query failed, falling back to rule-based")

    return None, empty_usage


def _rule_based_query(
    destination: str,
    preferences: list[str] | None = None,
    pace: str | None = None,
    special_notes: str | None = None,
) -> str:
    """规则级 Query Rewrite，作为 LLM Rewrite 的 fallback。"""
    from app.rag.guide_catalog import canonical_destination
    destination = canonical_destination(destination)
    parts: list[str] = [destination]
    if special_notes:
        _append_unique(parts, special_notes[:1024])

    if preferences:
        for preference in preferences:
            _append_unique(parts, preference)

    if pace:
        _append_unique(parts, pace)

    for keyword in _extract_note_keywords(special_notes, destination=destination):
        _append_unique(parts, keyword)

    for stable_term in ["景点", "行程", "攻略", "推荐", "餐饮", "住宿"]:
        _append_unique(parts, stable_term)

    return " ".join(part for part in parts if part).strip()[:2048]


def build_destination_query(
    destination: str,
    preferences: list[str] | None = None,
    pace: str | None = None,
    special_notes: str | None = None,
) -> tuple[str, dict[str, int]]:
    """把目的地、偏好、节奏和备注改写成更贴近检索场景的 query。返回 (query, token_usage)。"""
    result = build_destination_rewrite(destination, preferences, pace, special_notes)
    return result.effective_query, legacy_usage(result.usage)


def build_destination_rewrite(destination, preferences=None, pace=None, special_notes=None, *, offline=False):
    from app.rag.rewrite import rewrite_query
    from app.rag.guide_catalog import canonical_destination
    destination = canonical_destination(destination)
    original = " ".join([destination, *(preferences or []), pace or "", special_notes or ""]).strip()
    if len(original) > 2048:
        raise ValueError("Query exceeds 2048 characters")
    def provider():
        if offline:
            return _rule_based_query(destination, preferences, pace, special_notes), None
        candidate, usage = llm_rewrite_query(destination, preferences, pace, special_notes)
        return candidate, usage
    return rewrite_query(original, destination,
        preferences=[*_extract_note_keywords(special_notes, destination), "景点", "行程", "攻略", "推荐", "餐饮", "住宿"],
        provider=provider)


def _build_destination_query(
    destination: str,
    preferences: list[str] | None = None,
    pace: str | None = None,
    special_notes: str | None = None,
) -> tuple[str, dict[str, int]]:
    """兼容旧调用，内部转到公开的 query 构造函数。"""
    return build_destination_query(
        destination=destination,
        preferences=preferences,
        pace=pace,
        special_notes=special_notes,
    )


def get_destination_guide_context(
    destination: str,
    preferences: list[str] | None = None,
    pace: str | None = None,
    special_notes: str | None = None,
    top_k: int = 5,
) -> tuple[list[str], dict[str, int], dict[str, int], dict[str, int]]:
    """根据目的地和偏好返回本地攻略片段。返回 (contexts, rewrite_usage, rerank_usage, embedding_usage)。"""
    from app.models.knowledge_retrieval import KnowledgeQuery
    from app.rag.knowledge_service import retrieve_knowledge, format_context, MAX_TOTAL_CONTEXT
    rewrite = build_destination_rewrite(destination, preferences, pace, special_notes)
    result = retrieve_knowledge(KnowledgeQuery(query=rewrite.original_query, destination=destination, top_k=top_k), rewrite=rewrite)
    contexts = format_context(result)
    observe_result(result)
    rerank_usage = legacy_usage(result.metadata.rerank_usage)
    embedding_usage = legacy_usage(result.metadata.embedding_usage)
    seen = {c.chunk_id for c in result.chunks}
    for query in [f"{destination} 住宿 酒店 民宿", f"{destination} 餐饮 美食 餐厅"]:
        extra = retrieve_knowledge(KnowledgeQuery(query=query, destination=destination, top_k=2))
        observe_result(extra)
        fresh = tuple(c for c in extra.chunks if c.chunk_id not in seen)
        seen.update(c.chunk_id for c in fresh)
        contexts.extend(format_context(extra.model_copy(update={"chunks": fresh})))
        for target, usage in [(rerank_usage, extra.metadata.rerank_usage), (embedding_usage, extra.metadata.embedding_usage)]:
            if usage is None or not target.get("usage_known"):
                target.clear()
                target["usage_known"] = False
            else:
                for key, value in usage.items():
                    target[key] = target.get(key, 0) + value
    bounded, remaining = [], MAX_TOTAL_CONTEXT
    for context in contexts:
        if remaining <= 0:
            break
        bounded.append(context[:remaining])
        remaining -= len(bounded[-1])
    return bounded, legacy_usage(rewrite.usage), rerank_usage, embedding_usage
