from __future__ import annotations

import sqlite3

import pytest

np = pytest.importorskip("numpy")

from hamchat import db_init, db_ops
from hamchat.memory_embeddings import MemoryEmbeddingService, normalize_vector


class FakeProvider:
    provider_id = "fake"; model_id = "test-v1"; format_version = 1
    def __init__(self, vectors): self.vectors = vectors
    def embed_one(self, text): return self.vectors[text]
    def embed_many(self, texts): return [self.embed_one(text) for text in texts]


def db(mode="open"):
    conn = sqlite3.connect(":memory:"); conn.execute("PRAGMA foreign_keys=ON"); db_init._create_schema(conn, mode)
    return conn


def user(conn, name, role="user"):
    return db_ops.create_user(conn, name=name, handle=name, email=None, username=name, password="p", role=role)


def test_exact_ranking_scopes_weight_threshold_and_globals():
    conn = db(); u, other, admin = user(conn,"u"), user(conn,"o"), user(conn,"a","admin")
    chat = db_ops.create_conversation(conn,u,"c"); profile = db_ops.create_ai_profile(conn,owner_user_id=u,internal_name="p",display_name="p")
    ids = {
      "user": db_ops.create_memory(conn,owner_user_id=u,content="user",scope="user",weight=.5),
      "chat": db_ops.create_memory(conn,owner_user_id=u,content="chat",scope="chat",conversation_id=chat,weight=1),
      "profile": db_ops.create_memory(conn,owner_user_id=u,content="profile",scope="profile",profile_id=profile,weight=0),
      "other": db_ops.create_memory(conn,owner_user_id=other,content="other",scope="user"),
      "global": db_ops.create_memory(conn,owner_user_id=admin,content="global",scope="global"),
    }
    provider=FakeProvider({"query":[1,0],"user":[.9,.1],"chat":[.8,.2],"profile":[.7,.3],"other":[1,0],"global":[1,0]})
    service=MemoryEmbeddingService(conn,provider)
    for key in ids: service.embed_memory(ids[key])
    inspection=service.inspect("query",user_id=u,role="user",conversation_id=chat,profile_id=profile,min_similarity=.0)
    assert inspection.global_memory_ids == [ids["global"]]
    assert {item.memory_id for item in inspection.matches} == {ids["user"],ids["chat"],ids["profile"]}
    assert ids["other"] not in {item.memory_id for item in inspection.matches}
    assert inspection.matches[0].memory_id == ids["chat"]
    assert service.inspect("query",user_id=u,role="user",min_similarity=.999).matches == []


def test_stale_invalid_corrupt_and_deleted_vectors_are_excluded():
    conn=db(); u=user(conn,"u"); mid=db_ops.create_memory(conn,owner_user_id=u,content="one",scope="user")
    provider=FakeProvider({"one":[1,0],"query":[1,0],"two":[0,1]}); service=MemoryEmbeddingService(conn,provider); service.embed_memory(mid)
    db_ops.update_memory(conn,owner_user_id=u,memory_id=mid,content="two",scope="user")
    assert service.inspect("query",user_id=u,role="user").matches == []
    service.embed_memory(mid); conn.execute("UPDATE memory_embeddings SET vector=x'00'"); conn.commit()
    assert service.inspect("query",user_id=u,role="user").matches == []
    db_ops.delete_memory(conn,owner_user_id=u,memory_id=mid)
    assert conn.execute("SELECT count(*) FROM memory_embeddings").fetchone()[0] == 0


def test_identity_mismatch_and_strict_memory_only_vectors(monkeypatch):
    conn=db(); u=user(conn,"u"); mid=db_ops.create_memory(conn,owner_user_id=u,content="one",scope="user")
    a=FakeProvider({"one":[1,0],"query":[1,0]}); service=MemoryEmbeddingService(conn,a); service.embed_memory(mid)
    b=FakeProvider({"one":[1,0],"query":[1,0]}); b.model_id="other"
    assert MemoryEmbeddingService(conn,b).inspect("query",user_id=u,role="user").matches == []
    strict=db("strict"); monkeypatch.setattr(db_ops._dbi,"_get_or_create_field_key",lambda existing_only=False:b"k"*32)
    su=user(strict,"s"); sid=db_ops.create_memory(strict,owner_user_id=su,content="one",scope="user")
    s=MemoryEmbeddingService(strict,a); assert s.embed_memory(sid)=="embedded_in_memory"
    assert strict.execute("SELECT count(*) FROM memory_embeddings").fetchone()[0] == 0
    assert s.inspect("query",user_id=su,role="user").matches
    s.clear_strict_vectors(); assert not s.inspect("query",user_id=su,role="user").matches


def test_vector_validation():
    assert np.allclose(normalize_vector([3,4]), [0.6,0.8])
    for bad in ([], [0,0], [float("nan")], [[1,2]]):
        with pytest.raises(ValueError): normalize_vector(bad)


def test_ephemeral_context_order_and_semantic_failure_keeps_globals():
    provider=FakeProvider({"query":[1,0]})
    context, status = MemoryEmbeddingService.format_context("query", provider, ([(2,"semantic",.5,np.array([1,0],dtype=np.float32))], ["global"]))
    assert context.index("administrative") < context.index("relevant memory")
    assert "1 relevant" in status
    class Down(FakeProvider):
        def embed_one(self, text): raise RuntimeError("offline")
    context, status = MemoryEmbeddingService.format_context("query", Down({}), ([], ["global"]))
    assert "global" in context and "unavailable" in status
