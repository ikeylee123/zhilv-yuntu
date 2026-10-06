from pathlib import Path
import sys
import hashlib


# 允许测试文件直接导入 backend/app 下的模块。
CURRENT_FILE = Path(__file__).resolve()
BACKEND_DIR = CURRENT_FILE.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app.rag.retriever as retriever  # noqa: E402
from app.rag import vector_db


def test_retrieve_travel_guide_formats_chunks_as_text(monkeypatch) -> None:
    """测试 retriever 会把检索结果格式化成可直接引用的文本片段。"""

    chunk = next(c for c in vector_db.load_guide_chunks() if "大理古城-南门楼" in c["title"])

    def fake_search_guide_chunks_with_usage(
        query: str, top_k: int = 3, destination=None
    ) -> tuple[list[dict[str, str]], dict[str, int]]:
        assert query == "大理 古城 美食"
        assert top_k == 6
        return [chunk], {"prompt_tokens": 0, "completion_tokens": 0}

    monkeypatch.setattr(vector_db, "_search_guide_chunks_by_chroma", fake_search_guide_chunks_with_usage)
    monkeypatch.setattr(
        retriever,
        "rerank_detailed",
        lambda query, matched_chunks, top_k, destination=None, **kwargs: (
            matched_chunks[:top_k],
            None, "rule_fallback", False, False,
        ),
    )

    results, _, _ = retriever.retrieve_travel_guide("大理 古城 美食", top_k=2)

    assert len(results) == 1
    assert results[0].startswith(f"[来源: dali_guide.md | 标题: {chunk['title']}]\n")
    assert chunk["text"] in results[0]
    assert chunk["id"] in results[0]
    assert "static_knowledge | untrusted reference" in results[0]


def test_retrieve_travel_guide_returns_empty_when_no_chunks(monkeypatch) -> None:
    """测试没有召回任何片段时，会返回空列表。"""

    def fake_search_guide_chunks_with_usage(
        query: str, top_k: int = 3, destination=None
    ) -> tuple[list[dict[str, str]], dict[str, int]]:
        assert query == "火星 沙漠 极地科考"
        assert top_k == 6
        return [], {"prompt_tokens": 0, "completion_tokens": 0}

    monkeypatch.setattr(vector_db, "_search_guide_chunks_by_chroma", fake_search_guide_chunks_with_usage)
    monkeypatch.setattr(
        retriever,
        "rerank_detailed",
        lambda query, matched_chunks, top_k, destination=None, **kwargs: (
            matched_chunks[:top_k],
            None, "rule_fallback", False, False,
        ),
    )

    results, _, _ = retriever.retrieve_travel_guide("火星 沙漠 极地科考", top_k=2, destination="北京")

    assert results == []
