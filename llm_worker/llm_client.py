"""LLM Module. >>> HÀM GIẢ <<<  Thay StubLLMClient bằng client gọi API/local LLM thật (giữ nguyên interface)."""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any, Protocol

from fraud_common.config import get_settings
from fraud_common.schemas import BriefContent, ChecklistItem, Citation

from .rag import Chunk

PROMPT_VERSION = "grounded-kaggle-sparkov-v11-explained-brief-en"


@dataclass
class LLMResult:
    brief: BriefContent
    tokens_used: int
    model_name: str


class LLMClient(Protocol):
    async def generate(self, ctx: dict[str, Any], chunks: list[Chunk]) -> LLMResult: ...


class StubLLMClient:
    async def generate(self, ctx: dict[str, Any], chunks: list[Chunk]) -> LLMResult:
        s = get_settings()
        await asyncio.sleep(s.llm_stub_latency_s)                  # giả lập độ trễ
        if random.random() < s.llm_stub_fail_rate:                  # giả lập lỗi để test breaker/fallback
            raise RuntimeError("stub LLM failure")
        tx = ctx["transaction"]
        signals = [e["feature"] for e in ctx["explanations"]]
        brief = BriefContent(
            summary=(f"Transaction {ctx['txn_id']}, amount {tx.get('amt')} at {tx.get('merchant')} "
                     f"({tx.get('category')}), risk level {ctx['risk_level']} "
                     f"(probability {ctx['calibrated_prob']:.2f})."),
            key_signals=[f"{e['feature']} contribution {e['contribution']:.2f}" for e in ctx["explanations"]],
            checklist=[ChecklistItem(step=i + 1, action=f"Review under rule {c.rule_code}: {c.text}",
                                     rule_code=c.rule_code) for i, c in enumerate(chunks)],
            citations=[Citation(section_id=c.section_id, rule_code=c.rule_code, quote=c.text[:80])
                       for c in chunks],
            source="llm",
        )
        tokens = (len(str(ctx)) + sum(len(c.text) for c in chunks)) // 4 + 300
        return LLMResult(brief=brief, tokens_used=tokens, model_name="stub-llm")


def get_llm_client() -> LLMClient:
    if get_settings().llm_provider == "stub":
        return StubLLMClient()
    if get_settings().llm_provider == "api":
        from .api_client import APIClient
        return APIClient()
    if get_settings().llm_provider == "ollama":
        from .local_integration import OllamaClient
        return OllamaClient()
    raise NotImplementedError("Chưa cấu hình LLM thật: thêm client mới vào llm_client.py")
