"""Small deterministic BM25 retriever; no external database required."""
import json
import math
import re
from collections import Counter
from .contracts import ContractError, fields, text


def tokens(s):
    return re.findall(r"\w+", s.lower(), flags=re.UNICODE)


def load_playbook(path, allow_demo=False):
    with open(path, encoding="utf-8") as f:
        kb = json.load(f)
    fields(kb, ("document_id", "version", "demo", "chunks"))
    text(kb["document_id"])
    text(kb["version"])
    if type(kb["demo"]) is not bool:
        raise ContractError("Invalid demo flag")
    if kb["demo"] and not allow_demo:
        raise ContractError("Demo playbook requires explicit allow_demo=True")
    if not isinstance(kb["chunks"], list) or not kb["chunks"]:
        raise ContractError("Empty playbook")
    seen = set()
    for chunk in kb["chunks"]:
        fields(chunk, ("id", "section", "text", "action", "datasets"))
        for key in ("id", "section", "text", "action"):
            text(chunk[key], 4000)
        if chunk["id"] in seen:
            raise ContractError("Duplicate chunk ID")
        seen.add(chunk["id"])
        if not isinstance(chunk["datasets"], list) or not chunk["datasets"] or any(d not in ("ulb", "ieee_cis") for d in chunk["datasets"]):
            raise ContractError("Invalid dataset applicability")
    return kb


def retrieve(kb, dataset, query, k=3):
    if type(k) is not int or k < 1:
        raise ContractError("Invalid retrieval count")
    docs = [c for c in kb["chunks"] if dataset in c["datasets"]]
    if not docs:
        return []
    bags = [Counter(tokens(c["section"] + " " + c["text"])) for c in docs]
    lengths = [sum(b.values()) for b in bags]
    average = sum(lengths) / len(lengths) or 1
    scored = []
    for doc, bag, length in zip(docs, bags, lengths):
        score = 0.0
        for term in set(tokens(query)):
            frequency = bag[term]
            df = sum(term in other for other in bags)
            idf = math.log(1 + (len(docs) - df + 0.5) / (df + 0.5))
            score += idf * frequency * 2.5 / (frequency + 1.5 * (0.25 + 0.75 * length / average))
        if score > 0:
            scored.append((score, doc))
    return [dict(doc, retrieval_score=round(score, 6)) for score, doc in sorted(scored, key=lambda x: (-x[0], x[1]["id"]))[:k]]
