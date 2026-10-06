"""Phase 3.5.1 audit counterexamples; all providers/index clients are isolated."""
import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock
import pytest
from pydantic import ValidationError
from app.rag import versions, vector_db, index_store, knowledge_service as service, retriever, rewrite
from app.rag.snapshot import load_snapshot
from app.models.knowledge_retrieval import KnowledgeQuery, KnowledgeChunk, KnowledgeResult, RetrievalMetadata, StageEvent
from scripts import evaluate_rag_v2
from test_rag_index import Client, Embeddings

DATA = Path(__file__).parents[1]/"eval/rag_eval_cases_v2.json"

@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    for module in (service,retriever):
        monkeypatch.setattr(module,"get_cached_json",lambda k:None)
        monkeypatch.setattr(module,"set_cached_json",lambda *a,**k:False)
    monkeypatch.setattr(retriever,"LLM_API_KEY","")
    monkeypatch.setattr(vector_db,"_get_chroma_collection",lambda:(_ for _ in ()).throw(index_store.IndexUnavailable()))

@pytest.fixture
def corpus(tmp_path,monkeypatch):
    folder=tmp_path/"corpus";folder.mkdir()
    (folder/"beijing_guide.md").write_text("## Parent\n### 2.1 故宫博物院\n故宫历史\n### 2.2 颐和园\n皇家园林",encoding="utf-8")
    monkeypatch.setattr(vector_db,"DATA_DIR",folder)
    return folder

@pytest.mark.parametrize("version",["a"*64,"0"*64,"f"*64])
def test_collection_name_declared_constraint(version):
    names={index_store.collection_name(version) for _ in range(30)}
    assert len(names)==30
    assert all(len(n)<=63 and n.startswith("rag_"+version[:24]) for n in names)

@pytest.mark.parametrize("field",[None,*index_store.IDENTITY_FIELDS,"missing"])
def test_pointer_collection_binding(corpus,tmp_path,field):
    class StrictClient(Client):
        def create_collection(self,name,metadata):
            assert 3<=len(name)<=63
            return super().create_collection(name,metadata)
    client=StrictClient();directory=tmp_path/"index"
    index_store.rebuild_index(directory,client,Embeddings())
    pointer=json.loads((directory/"active_index.json").read_text())
    collection=client.get_collection(pointer["collection"])
    assert all(pointer[k]==collection.metadata[k] for k in index_store.IDENTITY_FIELDS)
    if field is None:
        assert index_store.open_active_index(directory,client) is collection
    else:
        if field=="missing":collection.metadata={}
        else:collection.metadata[field]="wrong"
        with pytest.raises(index_store.IndexUnavailable,match="MISMATCH"):
            index_store.open_active_index(directory,client)

@pytest.mark.parametrize("field",index_store.IDENTITY_FIELDS)
def test_pointer_identity_corruption(corpus,tmp_path,field):
    directory=tmp_path/"index";client=Client()
    index_store.rebuild_index(directory,client,Embeddings())
    path=directory/"active_index.json";pointer=json.loads(path.read_text());pointer[field]="wrong"
    path.write_text(json.dumps(pointer))
    with pytest.raises(index_store.IndexUnavailable):index_store.open_active_index(directory,client)

@pytest.mark.parametrize("field",["id","source_path","source_id","heading_path","destination","title","text","content_hash"])
def test_backend_provenance_rejected(field,monkeypatch):
    chunk=copy.deepcopy(next(c for c in vector_db.load_guide_chunks() if "故宫" in c["title"]))
    chunk[field]=["fabricated"] if field=="heading_path" else "fabricated"
    if field=="text":chunk["content_hash"]=hashlib.sha256(chunk["text"].encode()).hexdigest()
    monkeypatch.setattr(vector_db,"_search_guide_chunks_by_chroma",lambda *a:([chunk],None))
    result=service.retrieve_knowledge(KnowledgeQuery(query="故宫",destination="北京"),mode="vector_only",allow_fallback=False)
    assert result.outcome=="error" and result.errors==("invalid_response",)

@pytest.mark.parametrize("field",["chunk_id","source_path","source_id","heading_path","title"])
def test_cached_provenance_is_miss(field,monkeypatch):
    cache={}
    monkeypatch.setattr(service,"get_cached_json",lambda key:copy.deepcopy(cache.get(key)))
    monkeypatch.setattr(service,"set_cached_json",lambda key,value,**kw:cache.__setitem__(key,copy.deepcopy(value)))
    query=KnowledgeQuery(query="故宫",destination="北京")
    service.retrieve_knowledge(query,mode="keyword_baseline")
    key=next(iter(cache));cache[key]["chunks"][0][field]=["fake"] if field=="heading_path" else "fake"
    result=service.retrieve_knowledge(query,mode="keyword_baseline")
    assert result.outcome=="success" and not result.metadata.retrieval_cache_hit


def test_rerank_cached_candidate_identity_is_checked(monkeypatch):
    chunks=vector_db.load_guide_chunks()[:2]
    bad={"identity":[["invented","hash"]]*2,"ranking":[{"index":0},{"index":1}]}
    monkeypatch.setattr(retriever,"get_cached_json",lambda k:bad)
    output,usage,method,hit,failed=retriever.rerank_detailed("query",chunks,2)
    assert not hit and failed and method=="rule_fallback"
    assert {c["id"] for c in output}=={c["id"] for c in chunks}


def test_keyword_vector_same_snapshot(monkeypatch):
    request=KnowledgeQuery(query="故宫",destination="北京",top_k=1)
    keyword=service.retrieve_knowledge(request,mode="keyword_baseline")
    chunk=load_snapshot().members[keyword.chunks[0].chunk_id]
    monkeypatch.setattr(vector_db,"_search_guide_chunks_by_chroma",lambda *a:([chunk],None))
    vector=service.retrieve_knowledge(request,mode="vector_only")
    for key in ("chunk_id","source_id","source_path","title","heading_path","content_hash","text"):
        assert getattr(keyword.chunks[0],key)==getattr(vector.chunks[0],key)

@pytest.mark.parametrize("damage",["chunk","source","group","empty_semantic","empty_exact","manifest","duplicate","acceptable","empty_group"])
def test_label_preflight_before_any_retrieval(tmp_path,monkeypatch,damage):
    dataset=json.loads(DATA.read_text(encoding="utf-8"))
    case=dataset["cases"][0]
    if damage=="chunk":case["relevant_chunk_ids"]=["missing"]
    if damage=="source":case["relevant_source_ids"]=["missing"]
    if damage=="group":case["required_evidence_groups"]=[["missing"]]
    if damage=="empty_semantic":next(c for c in dataset["cases"] if c["category"]=="semantic_preference")["relevant_chunk_ids"]=[]
    if damage=="empty_exact":case["relevant_chunk_ids"]=[]
    if damage=="manifest":dataset["corpus_manifest_version"]="wrong"
    if damage=="duplicate":dataset["cases"][1]["id"]=case["id"]
    if damage=="acceptable":case["acceptable_chunk_sets"]=["not-a-set"]
    if damage=="empty_group":case["required_evidence_groups"]=[[]]
    path=tmp_path/"cases.json";path.write_text(json.dumps(dataset),encoding="utf-8")
    called=Mock(side_effect=AssertionError("Retrieval must not start"))
    monkeypatch.setattr(service,"retrieve_knowledge",called)
    with pytest.raises(ValueError):evaluate_rag_v2.run(path)
    called.assert_not_called()


def test_current_labels_and_no_result_preflight():
    from app.rag.evaluation import preflight_dataset
    cases=preflight_dataset(json.loads(DATA.read_text(encoding="utf-8")),load_snapshot())
    assert len(cases)==60 and sum(c["expected_outcome"]=="no_data" for c in cases)==6

@pytest.mark.parametrize("target",["manifest","version","rules","malformed_manifest","entity_map","pointer"])
def test_infrastructure_errors_typed(monkeypatch,target):
    def fail(*a,**k):raise OSError("unreadable")
    request=KnowledgeQuery(query="故宫",destination="北京")
    rewrite_result=None
    if target=="manifest":monkeypatch.setattr(versions,"corpus_manifest",fail)
    if target=="version":monkeypatch.setattr(versions,"index_version",fail)
    if target=="rules":monkeypatch.setattr(versions,"retrieval_config_version",fail)
    if target=="malformed_manifest":monkeypatch.setattr(versions,"corpus_manifest",lambda *a:[{}])
    if target=="entity_map":
        monkeypatch.setattr(rewrite,"load_guide_chunks",fail)
        rewrite_result=rewrite.rewrite_query(request.query,request.destination)
    result=service.retrieve_knowledge(request,rewrite=rewrite_result,allow_fallback=False)
    assert result.outcome=="error" and result.errors

@pytest.mark.parametrize("usage",["abc",[],{"prompt_tokens":-1},{"prompt_tokens":True},{"prompt_tokens":float('nan')},{"prompt_tokens":float('inf')}])
def test_bad_usage_does_not_fail_good_rewrite(usage):
    result=rewrite.rewrite_query("故宫","北京",provider=lambda:("北京 故宫",usage))
    assert result.status=="accepted" and result.usage is None

@pytest.mark.parametrize("usage",[None,{"prompt_tokens":0},{"prompt_tokens":10,"completion_tokens":2}])
def test_valid_usage(usage):
    result=rewrite.rewrite_query("故宫","北京",provider=lambda:("北京 故宫",usage))
    assert result.status=="accepted" and result.usage==usage

@pytest.mark.parametrize("rank",[True,1.0,"1",0])
def test_strict_rank(rank):
    c=next(c for c in vector_db.load_guide_chunks() if "故宫" in c["title"])
    with pytest.raises(ValidationError):KnowledgeChunk(chunk_id=c["id"],rank=rank,**{k:c[k] for k in ("source_id","source_path","title","heading_path","destination","text","content_hash")})

@pytest.mark.parametrize("field,value",[("top_k",True),("candidate_k",1.0),("total_latency_ms",-1),("total_latency_ms",True),("total_latency_ms",float('inf')),("rewrite_usage",{"prompt_tokens":-1}),("rewrite_usage",{"prompt_tokens":True})])
def test_numeric_metadata(field,value):
    fields=dict(schema_version="s",index_version="i",retrieval_config_version="c",original_query="q",effective_query="q",top_k=2,candidate_k=6)
    with pytest.raises(ValidationError):RetrievalMetadata(**(fields|{field:value}))


def test_success_failure_event_requires_degradation():
    result=service.retrieve_knowledge(KnowledgeQuery(query="故宫",destination="北京"),mode="keyword_baseline")
    with pytest.raises(ValidationError):KnowledgeResult(**(result.model_dump()|dict(stage_events=[dict(stage="retrieval",code="vector_store_error")],degraded=False)))

@pytest.mark.parametrize("form",["crlf","cr","spaces","reorder"])
def test_logical_version_equivalence(corpus,form):
    path=corpus/"beijing_guide.md";before=versions.corpus_manifest();text=path.read_text(encoding="utf-8")
    if form=="crlf":path.write_bytes(text.replace("\n","\r\n").encode())
    if form=="cr":path.write_bytes(text.replace("\n","\r").encode())
    if form=="spaces":path.write_bytes("\n".join(line+"  " for line in text.splitlines()).encode())
    after=versions.corpus_manifest()
    assert versions.index_version(before)==versions.index_version(list(reversed(after)))

@pytest.mark.parametrize("change",["text","delete","destination","heading","embedding","chunking"])
def test_logical_version_sensitivity(corpus,change):
    before=versions.corpus_manifest();old=versions.index_version(before);kwargs={}
    path=corpus/"beijing_guide.md"
    if change=="text":path.write_text(path.read_text(encoding="utf-8")+" changed",encoding="utf-8")
    if change=="heading":path.write_text(path.read_text(encoding="utf-8").replace("Parent","Other"),encoding="utf-8")
    if change=="delete":path.unlink()
    after=versions.corpus_manifest()
    if change=="destination":after[0]["destination"]="成都"
    if change=="embedding":kwargs["embedding"]={"model":"different"}
    if change=="chunking":kwargs["chunking"]={"version":"different"}
    assert old!=versions.index_version(after,**kwargs)

@pytest.mark.parametrize("text,paths",[("## Parent\n### A\nx\n### B\ny",[["Parent","A"],["Parent","B"]]),("### A\nx\n### B\ny",[["A"],["B"]]),("## P\n### A\nx\n## Q\n### B\ny",[["P","A"],["Q","B"]])])
def test_heading_hierarchy(text,paths):
    assert [c["heading_path"] for c in vector_db._split_markdown_into_chunks(text,"guide.md")]==paths

@pytest.mark.parametrize("original,candidate,status",[("故宫","北京 旅游","rejected"),("故宫，颐和园","北京 故宫","rejected"),("Beijing 故宫 history","北京 故宫 历史","accepted"),("经济实惠一点","北京 低预算 小吃","accepted"),("轻松 住宿 推荐","北京 休闲 酒店","accepted")])
def test_entity_matcher_boundary(original,candidate,status):
    assert rewrite.rewrite_query(original,"北京",provider=lambda:(candidate,None)).status==status


def test_wrapper_error_observable_unknown_usage(caplog,monkeypatch):
    monkeypatch.setattr(vector_db,"_search_guide_chunks_by_chroma",lambda *a:(_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(vector_db,"_search_guide_chunks_by_keywords",lambda *a,**k:[])
    texts,rerank,embedding=retriever.retrieve_travel_guide("故宫",destination="北京")
    assert not texts and rerank==embedding=={"usage_known":False}
    records=[r for r in caplog.records if r.message=="retrieval_error"]
    assert records and "vector_store_error" in records[0].retrieval_codes


def test_report_metadata_and_replay_isolation():
    report=evaluate_rag_v2.run(DATA)
    for row in report["cases"]:
        assert "stage_events" in row and "mode" in row and "metadata" in row
        assert all(k in row["metadata"] for k in ["retrieval_method","rerank_method","index_version","retrieval_config_version","retrieval_cache_hit","rerank_cache_hit","total_latency_ms","embedding_usage"])
    import inspect
    source=inspect.getsource(evaluate_rag_v2.run)
    assert "lambda payload=replay_payload" in source


@pytest.mark.parametrize("payload,expected",[({},None),({"usage":"bad"},None),({"usage":{"prompt_tokens":True}},None),({"usage":{"prompt_tokens":0}},{"prompt_tokens":0,"completion_tokens":0}),({"usage":{"prompt_tokens":3}},{"prompt_tokens":3,"completion_tokens":0})])
def test_embedding_unknown_not_zero(payload,expected):
    assert vector_db._extract_embedding_token_usage(payload)==expected


def test_manifest_order_and_file_creation_order(tmp_path,monkeypatch):
    manifests=[]
    for i in range(2):
        folder=tmp_path/str(i);folder.mkdir()
        for name in (["beijing_guide.md","dali_guide.md"] if i==0 else ["dali_guide.md","beijing_guide.md"]):
            (folder/name).write_text("## Title\nBody",encoding="utf-8")
        manifests.append(versions.corpus_manifest(folder))
    assert versions.index_version(manifests[0])==versions.index_version(list(reversed(manifests[1])))


def test_positive_category_cannot_hide_missing_labels(tmp_path,monkeypatch):
    dataset=json.loads(DATA.read_text(encoding="utf-8"))
    dataset["cases"][0].update(relevant_chunk_ids=[],expected_outcome="no_data")
    path=tmp_path/"invalid.json";path.write_text(json.dumps(dataset),encoding="utf-8")
    called=Mock();monkeypatch.setattr(service,"retrieve_knowledge",called)
    with pytest.raises(ValueError):evaluate_rag_v2.run(path)
    called.assert_not_called()
