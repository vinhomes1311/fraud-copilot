"""RAG Module. >>> HÀM GIẢ <<<  Thay bằng truy vấn Vector Index (Common Fraud Investigation Playbook)."""
from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel


class Chunk(BaseModel):
    section_id: str
    rule_code: str
    text: str


class Retriever(Protocol):
    async def retrieve(self, signals: list[str], risk_level: str, top_k: int = 3) -> list[Chunk]: ...


_FAKE_PLAYBOOK = [
    Chunk(section_id="PB-2.1", rule_code="AMT-01",
          text="Giao dịch có giá trị lớn bất thường so với lịch sử tài khoản cần xác minh nguồn gốc."),
    Chunk(section_id="PB-3.2", rule_code="GEO-02",
          text="Khoảng cách giữa nơi cư trú và điểm bán lớn: kiểm tra khả năng thẻ bị sao chép."),
    Chunk(section_id="PB-4.1", rule_code="VEL-03",
          text="Nhiều giao dịch trong thời gian ngắn là dấu hiệu velocity attack."),
    Chunk(section_id="PB-9.1", rule_code="ESC-01",
          text="Rủi ro cao hoặc không xác minh được cần chuyển cấp cho Risk Lead."),
]
_SIGNAL_TO_RULE = {"amt": "AMT-01", "dist_home_merchant_km": "GEO-02", "txn_count_24h": "VEL-03"}


class StubRetriever:
    async def retrieve(self, signals: list[str], risk_level: str, top_k: int = 3) -> list[Chunk]:
        wanted = {_SIGNAL_TO_RULE[s] for s in signals if s in _SIGNAL_TO_RULE}
        if risk_level == "HIGH":
            wanted.add("ESC-01")
        found = [c for c in _FAKE_PLAYBOOK if c.rule_code in wanted]
        return (found or _FAKE_PLAYBOOK[:1])[:top_k]


class ApprovedPlaybookRetriever:
    """Return the complete tiny approved KB; no semantic ranking or embeddings."""
    async def retrieve(self, signals, risk_level, top_k=5):
        from .local_integration import KB_PATH
        from copilot.v2.knowledge import load
        return [Chunk(section_id=c['section_id'], rule_code=c['rule_code'], text=c['text'])
                for c in load(KB_PATH)['chunks'][:top_k]]


def get_retriever() -> Retriever:
    import os
    from fraud_common.config import get_settings
    provider = get_settings().llm_provider
    if provider == 'stub':
        return StubRetriever()
    if provider not in ('api', 'ollama'):
        raise ValueError('Unknown LLM_PROVIDER')
    mode = os.getenv('LLM_RETRIEVER') or ('playbook' if provider == 'api' else 'ollama')
    if mode == 'ollama':
        from .local_integration import SemanticRetriever
        return SemanticRetriever()
    if mode == 'playbook':
        return ApprovedPlaybookRetriever()
    raise ValueError('LLM_RETRIEVER must be ollama or playbook')
