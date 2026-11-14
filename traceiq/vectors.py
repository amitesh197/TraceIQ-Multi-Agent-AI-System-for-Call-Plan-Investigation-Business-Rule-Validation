"""Vector search with ChromaDB, running on the TraceIQ server (never on the user's computer).

Two collections:
  rules_kb      business rules per quarter and team, so the Rules agent reads only the rules that matter
  user_history  every question and answer, searchable by meaning; each user can only search their own

Where Chroma runs (set in .env):
  CHROMA_HOST unset  -> embedded in the app server, stored under TRACEIQ_STATE_DIR/chroma
  CHROMA_HOST=<host> -> a separate Chroma server (e.g. `chroma run --path /srv/chroma --port 8000`)

Embeddings (TRACEIQ_EMBEDDINGS):
  local  (default) a small open-source model run by Chroma on the server; free, no API calls
  gemini           Gemini embeddings through LiteLLM (TRACEIQ_EMBED_MODEL), uses your API quota
  hash             a simple built-in fallback that needs no download (lower quality; used if local fails)
"""
import datetime as dt
import hashlib
import json
import math
import os
import re
from functools import lru_cache

from .db import ROOT

STATE_DIR = os.environ.get("TRACEIQ_STATE_DIR", os.path.join(ROOT, "state"))
PROVIDER = os.environ.get("TRACEIQ_EMBEDDINGS", "local").strip().lower()
EMBED_MODEL = os.environ.get("TRACEIQ_EMBED_MODEL", "gemini/gemini-embedding-001")
_active = {"provider": PROVIDER, "note": ""}


# ---------------------------------------------------------------- embeddings
def _hash_embed(texts, dim=384):
    out = []
    for t in texts:
        v = [0.0] * dim
        words = re.findall(r"[a-z0-9_]+", t.lower())
        grams = words + [a + " " + b for a, b in zip(words, words[1:])]
        for g in grams:
            h = int(hashlib.md5(g.encode()).hexdigest(), 16)
            v[h % dim] += 1.0 if (h >> 8) % 2 else -1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append([x / n for x in v])
    return out


@lru_cache(maxsize=1)
def _local_model():
    from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
    ef = DefaultEmbeddingFunction()
    ef(["warm up"])  # downloads the model on first use (server side)
    return ef


def embed(texts: list[str]) -> list[list[float]]:
    p = _active["provider"]
    if p == "gemini":
        import litellm
        litellm.drop_params = True
        r = litellm.embedding(model=EMBED_MODEL, input=texts)
        return [d["embedding"] for d in r.data]
    if p == "local":
        try:
            return [list(map(float, v)) for v in _local_model()(texts)]
        except Exception as e:
            _active.update(provider="hash", note=f"local embedding model unavailable ({type(e).__name__}); using built-in hash embeddings")
    return _hash_embed(texts)


def provider_note() -> str:
    return _active["note"] or f"{_active['provider']} embeddings"


# ---------------------------------------------------------------- client and collections
@lru_cache(maxsize=1)
def client():
    import chromadb
    host = os.environ.get("CHROMA_HOST", "").strip()
    if host:
        return chromadb.HttpClient(host=host, port=int(os.environ.get("CHROMA_PORT", "8000")))
    path = os.path.join(STATE_DIR, "chroma")
    os.makedirs(path, exist_ok=True)
    return chromadb.PersistentClient(path=path)


def _settle_provider():
    """If the local model can't load (e.g. no internet on first run), switch to hash before naming collections."""
    if _active["provider"] == "local":
        try:
            _local_model()
        except Exception as e:
            _active.update(provider="hash", note=f"local embedding model unavailable ({type(e).__name__}); using built-in hash embeddings")


def _collection(base: str):
    _settle_provider()
    name = f"{base}_{_active['provider']}"   # different embedding models never share a collection
    return client().get_or_create_collection(name, embedding_function=None, metadata={"hnsw:space": "cosine"})


def available() -> bool:
    try:
        client()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- rules knowledge base
def index_rules(docs: list[dict]) -> int:
    """docs: {id, quarter, stage, rule, teams (list or ['ALL']), title, text}."""
    col = _collection("rules_kb")
    texts = [f"{d['title']}. {d['text']}" for d in docs]
    col.upsert(ids=[d["id"] for d in docs], documents=texts, embeddings=embed(texts),
               metadatas=[{"quarter": d["quarter"], "stage": d["stage"], "rule": d["rule"], "title": d["title"],
                           "teams": "," + ",".join(d["teams"]) + ","} for d in docs])
    return len(docs)


def search_rules(text: str, quarter: str | None = None, team: str | None = None, k: int = 5) -> list[dict]:
    try:
        col = _collection("rules_kb")
        if col.count() == 0:
            return []
        where = {"quarter": quarter} if quarter else None
        res = col.query(query_embeddings=embed([text]), n_results=min(max(k * 4, k), col.count()), where=where)
    except Exception:
        return []
    out = []
    for i, doc, meta, dist in zip(res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0]):
        teams = meta.get("teams", ",ALL,")
        if team and ",ALL," not in teams and f",{team}," not in teams:
            continue   # rules for other teams are never shown
        out.append({"id": i, "title": meta.get("title"), "rule": meta.get("rule"), "stage": meta.get("stage"),
                    "quarter": meta.get("quarter"), "text": doc, "score": round(1 - dist, 3)})
        if len(out) == k:
            break
    return out


def all_rules(quarter: str | None = None) -> list[dict]:
    try:
        col = _collection("rules_kb")
        got = col.get(where={"quarter": quarter} if quarter else None)
    except Exception:
        return []
    return sorted([{"id": i, "text": d, **m} for i, d, m in zip(got["ids"], got["documents"], got["metadatas"])],
                  key=lambda r: (r.get("stage", ""), r.get("rule", ""), r["id"]))


# ---------------------------------------------------------------- user history
def add_history(user_id: str, turn: dict):
    """Index one question and answer for later search by meaning. Never fails the request."""
    if turn.get("status") in ("HISTORY", "DENIED", "CLARIFY", "ERROR"):
        return   # only real answers are searchable
    try:
        col = _collection("user_history")
        text = f"Question: {turn['question']}\nAnswer: {turn.get('answer') or ''}"
        ts = dt.datetime.fromisoformat(turn["time"]).timestamp()
        col.upsert(ids=[f"{user_id}:{turn['id']}"], documents=[text], embeddings=embed([text]),
                   metadatas=[{"user_id": user_id, "turn_id": turn["id"], "time": turn["time"], "ts": ts,
                               "jnj_id": turn.get("jnj_id") or "", "territory": turn.get("territory") or "",
                               "rules": ",".join(turn.get("rules") or [])}])
    except Exception as e:
        _active["note"] = f"history indexing failed: {type(e).__name__}"


def search_history(user_id: str, text: str, k: int = 5, since_days: int | None = None) -> list[dict]:
    """Search ONLY this user's history (the filter is applied by the code, not the model)."""
    try:
        col = _collection("user_history")
        where = {"user_id": user_id}
        if since_days is not None:
            start = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            cutoff = (start - dt.timedelta(days=max(since_days - 1, 0) if since_days else 0)).timestamp()
            where = {"$and": [{"user_id": user_id}, {"ts": {"$gte": cutoff}}]}
        n = col.count()
        if n == 0:
            return []
        res = col.query(query_embeddings=embed([text]), n_results=min(k, n), where=where)
    except Exception:
        return []
    return [{"turn_id": m["turn_id"], "time": m["time"], "jnj_id": m.get("jnj_id"), "territory": m.get("territory"),
             "rules": m.get("rules"), "text": d, "score": round(1 - dist, 3)}
            for d, m, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0])]
