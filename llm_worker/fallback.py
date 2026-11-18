"""Brief rule-based khi LLM lỗi/timeout: chuyên viên vẫn có tóm tắt tối thiểu để làm việc (FR-07)."""
from __future__ import annotations

from typing import Any

from fraud_common.schemas import BriefContent, ChecklistItem


def rule_based_brief(ctx: dict[str, Any]) -> BriefContent:
    tx = ctx.get("transaction", {})
    return BriefContent(
        summary=(f"Transaction {ctx.get('txn_id')}, amount {tx.get('amt')}, "
                 f"risk level {ctx.get('risk_level')}."),
        key_signals=[f"{e['feature']} contribution {e['contribution']:.2f}" for e in (ctx.get("explanations") or [])],
        checklist=[
            ChecklistItem(step=1, action="Verify the transaction with the cardholder through official channels."),
            ChecklistItem(step=2, action="Review the account's recent transaction history."),
            ChecklistItem(step=3, action="Escalate to the Risk Lead if verification is unsuccessful."),
        ],
        citations=[],
        source="rule_based",
    )
