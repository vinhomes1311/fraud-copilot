"""Brief rule-based khi LLM lỗi/timeout: chuyên viên vẫn có tóm tắt tối thiểu để làm việc (FR-07)."""
from __future__ import annotations

from typing import Any

from fraud_common.schemas import BriefContent, ChecklistItem


def rule_based_brief(ctx: dict[str, Any]) -> BriefContent:
    tx = ctx.get("transaction", {})
    return BriefContent(
        summary=(f"[Tóm tắt tự động theo luật] Giao dịch {ctx.get('txn_id')} số tiền {tx.get('amt')}, "
                 f"mức rủi ro {ctx.get('risk_level')}. LLM không khả dụng."),
        key_signals=[f"{e['feature']} đóng góp {e['contribution']:.2f}" for e in (ctx.get("explanations") or [])],
        checklist=[
            ChecklistItem(step=1, action="Xác minh giao dịch với chủ thẻ qua kênh chính thức."),
            ChecklistItem(step=2, action="Đối chiếu lịch sử giao dịch gần đây của tài khoản."),
            ChecklistItem(step=3, action="Chuyển cấp cho Risk Lead nếu không xác minh được."),
        ],
        citations=[],
        source="rule_based",
    )
