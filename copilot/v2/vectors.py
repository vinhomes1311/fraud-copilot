"""Persisted flat dense-vector index. Exact cosine search suits a five-rule KB."""
import json
import math
import os
from uuid import uuid4
import urllib.request
from pathlib import Path
from ..contracts import ContractError, number
from ..providers import OllamaProvider
from ..service import fingerprint


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class OllamaEmbeddings:
    def __init__(self, model=None):
        self.model = model or os.environ.get("OLLAMA_EMBED_MODEL", "embeddinggemma:300m")
        checked = OllamaProvider(model=self.model)
        if not checked.endpoint.endswith("/api/chat"):
            raise ValueError("OLLAMA_CHAT_URL must end with /api/chat")
        self.base = checked.endpoint[:-len("/api/chat")]

    def request(self, path, payload=None):
        req = urllib.request.Request(self.base + path,
              data=None if payload is None else json.dumps(payload).encode(),
              headers={"Content-Type": "application/json"})
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=60) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("Embedding response too large")
        return json.loads(raw)

    def digest(self):
        for model in self.request("/api/tags")["models"]:
            if model["name"] == self.model:
                return model["digest"]
        raise ValueError("Embedding model tag not installed; use exact tag")

    def embed(self, texts):
        result = self.request("/api/embed", {"model": self.model, "input": texts,
                    "truncate": False, "keep_alive": 0})
        vectors = result["embeddings"]
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise ValueError("Embedding count mismatch")
        return [normalize(v) for v in vectors]


def normalize(v):
    if not isinstance(v, list) or not v:
        raise ContractError("Invalid vector")
    norm = math.sqrt(sum(number(x) ** 2 for x in v))
    if not math.isfinite(norm) or norm == 0:
        raise ContractError("Invalid vector norm")
    return [x / norm for x in v]


def build(kb, embedder, path):
    digest = embedder.digest()
    vectors = embedder.embed(["title: " + c["rule_code"] + " | text: " + c["text"] for c in kb["chunks"]])
    if len(vectors) != len(kb["chunks"]) or not vectors or len({len(v) for v in vectors}) != 1:
        raise ContractError("Inconsistent embeddings")
    if embedder.digest() != digest:
        raise ContractError("Model changed while building index")
    index = {"format": "cosine-flat-v1", "kb_sha256": fingerprint(kb), "embedding_model": embedder.model,
             "model_digest": digest, "dimension": len(vectors[0]),
             "ids": [c["id"] for c in kb["chunks"]], "vectors": [normalize(v) for v in vectors]}
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + "." + uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(index, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return index


class SemanticRetriever:
    mode = "semantic"

    def __init__(self, kb, embedder, path, candidate_count=5):
        if type(candidate_count) is not int or candidate_count < 1:
            raise ValueError("candidate_count must be a positive integer")
        self.candidate_count = candidate_count
        self.kb, self.embedder = kb, embedder
        with open(path, encoding="utf-8") as f:
            self.index = json.load(f)
        index = self.index
        if index["format"] != "cosine-flat-v1" or index["kb_sha256"] != fingerprint(kb):
            raise ContractError("Stale index; rebuild after KB changes")
        if index["embedding_model"] != embedder.model or index["model_digest"] != embedder.digest():
            raise ContractError("Embedding model mismatch; rebuild index")
        if index["ids"] != [c["id"] for c in kb["chunks"]] or len(index["vectors"]) != len(index["ids"]):
            raise ContractError("Index ID mismatch")
        self.vectors = [normalize(v) for v in index["vectors"]]
        if any(len(v) != index["dimension"] for v in self.vectors):
            raise ContractError("Index dimension mismatch")

    def search(self, query, k=None):
        k = self.candidate_count if k is None else k
        q = normalize(self.embedder.embed(["task: search result | query: " + query])[0])
        if len(q) != self.index["dimension"]:
            raise ContractError("Query dimension mismatch")
        scored = [(sum(a*b for a, b in zip(q, v)), c) for v, c in zip(self.vectors, self.kb["chunks"])]
        return [dict(c, retrieval_score=round(s, 6)) for s, c in sorted(scored, key=lambda x: (-x[0], x[1]["id"]))[:k]]


class FixtureRetriever:
    """Explicit offline smoke test, NOT semantic retrieval."""
    mode = "fixture"

    def __init__(self, kb, candidate_count=5):
        if type(candidate_count) is not int or candidate_count < 1:
            raise ValueError("candidate_count must be a positive integer")
        self.candidate_count = candidate_count
        self.kb = kb

    def search(self, query, k=None):
        k = self.candidate_count if k is None else k
        return [dict(c, retrieval_score=None) for c in self.kb["chunks"][:k]]
