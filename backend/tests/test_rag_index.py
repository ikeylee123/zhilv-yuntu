import copy
import json
from pathlib import Path
import pytest
from app.rag import versions, vector_db, index_store

class Collection:
    def __init__(self,metadata):self.metadata=metadata;self.data={}
    def add(self,**kwargs):self.data=copy.deepcopy(kwargs)
    def count(self):return len(self.data.get("ids",[]))
    def get(self,**kwargs):return self.data
class Client:
    def __init__(self):self.collections={};self.creations=0
    def create_collection(self,name,metadata):
        self.creations+=1
        self.collections[name]=Collection(metadata)
        return self.collections[name]
    def get_collection(self,name):return self.collections[name]
class Embeddings:
    def embed_documents(self,documents):return [[.1,.2] for d in documents]

@pytest.fixture
def corpus(tmp_path,monkeypatch):
    data=tmp_path/"data";data.mkdir()
    (data/"beijing_guide.md").write_text("## palace\nold text",encoding="utf-8")
    (data/"dali_guide.md").write_text("## lake\nwater",encoding="utf-8")
    monkeypatch.setattr(vector_db,"DATA_DIR",data)
    return data

def test_same_manifest_same_version(corpus):
    assert versions.index_version(versions.corpus_manifest(corpus))==versions.index_version(versions.corpus_manifest(corpus))

@pytest.mark.parametrize("change",["text","deleted","embedding","chunking"])
def test_version_inputs(corpus,change):
    manifest=versions.corpus_manifest(corpus)
    before=versions.index_version(manifest)
    kwargs={}
    if change=="text":(corpus/"beijing_guide.md").write_text("changed",encoding="utf-8")
    if change=="deleted":(corpus/"dali_guide.md").unlink()
    if change=="embedding":kwargs["embedding"]={"model":"different"}
    if change=="chunking":kwargs["chunking"]={"version":"different"}
    assert before!=versions.index_version(versions.corpus_manifest(corpus),**kwargs)

def test_rebuild_exact_current_snapshot_no_stale_chunks(corpus,tmp_path):
    client=Client();directory=tmp_path/"index"
    assert index_store.rebuild_index(directory,client,Embeddings())==2
    old=json.loads((directory/"active_index.json").read_text())
    old_ids=set(client.get_collection(old["collection"]).data["ids"])
    (corpus/"dali_guide.md").unlink()
    (corpus/"beijing_guide.md").write_text("## palace\nnew text",encoding="utf-8")
    assert index_store.rebuild_index(directory,client,Embeddings())==1
    active=json.loads((directory/"active_index.json").read_text())
    new_ids=set(client.get_collection(active["collection"]).data["ids"])
    assert not old_ids & new_ids
    assert old["index_version"] != active["index_version"]
    assert client.get_collection(old["collection"]).count()==2

def test_duplicate_ids_rejected(corpus,monkeypatch,tmp_path):
    chunks=vector_db.load_guide_chunks()
    monkeypatch.setattr(vector_db,"load_guide_chunks",lambda:chunks+[chunks[0]])
    client=Client()
    with pytest.raises(ValueError,match="duplicate"):
        index_store.rebuild_index(tmp_path/"index",client,Embeddings())
    assert client.creations==0

@pytest.mark.parametrize("failure",["publish","readback","embedding","destination","blank"])
def test_build_failure_preserves_active(corpus,tmp_path,monkeypatch,failure):
    client=Client();directory=tmp_path/"index"
    index_store.rebuild_index(directory,client,Embeddings())
    previous=(directory/"active_index.json").read_bytes()
    if failure=="publish":monkeypatch.setattr(index_store.os,"replace",lambda *a: (_ for _ in ()).throw(OSError()))
    if failure=="readback":monkeypatch.setattr(Collection,"get",lambda *a,**k:{"ids":[],"documents":[],"metadatas":[]})
    if failure=="embedding":monkeypatch.setattr(Embeddings,"embed_documents",lambda *a:[[float("nan")]])
    if failure in ("destination","blank"):
        chunks=vector_db.load_guide_chunks()
        chunks[0]["destination" if failure=="destination" else "text"]="unknown" if failure=="destination" else " "
        monkeypatch.setattr(vector_db,"load_guide_chunks",lambda:chunks)
    with pytest.raises((ValueError,OSError)):
        index_store.rebuild_index(directory,client,Embeddings())
    assert (directory/"active_index.json").read_bytes()==previous

def test_read_missing_index_never_creates(tmp_path):
    client=Client()
    with pytest.raises(index_store.IndexUnavailable):index_store.open_active_index(tmp_path/"absent",client)
    assert client.creations==0
    assert not (tmp_path/"absent").exists()

def test_published_read_and_mismatch(corpus,tmp_path,monkeypatch):
    directory=tmp_path/"index";client=Client()
    index_store.rebuild_index(directory,client,Embeddings())
    version=versions.index_version(versions.corpus_manifest(corpus))
    monkeypatch.setattr(versions,"index_version",lambda *a,**k:version)
    assert index_store.open_active_index(directory,client).count()==2
    monkeypatch.setattr(versions,"index_version",lambda *a,**k:"different")
    with pytest.raises(index_store.IndexUnavailable):index_store.open_active_index(directory,client)

def test_heading_hierarchy_and_size():
    chunks=vector_db._split_markdown_into_chunks("## Parent\n### Child\n"+"x"*4000,"test.md")
    assert len(chunks)==3
    assert all(c["heading_path"]==["Parent","Child"] for c in chunks)
    assert all(len(c["text"])<=1600 for c in chunks)
