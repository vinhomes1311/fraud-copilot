"""Hợp đồng dữ liệu (data contract) giữa các thành phần. Sửa ở đây = sửa hợp đồng."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ----------------------------------------------------------------- Enums
class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ScoringStatus(StrEnum):
    SCORED = "SCORED"
    MANUAL_FALLBACK_UNSCORED = "MANUAL_FALLBACK_UNSCORED"


class BriefStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    READY = "READY"
    FAILED_TIMEOUT = "FAILED_TIMEOUT"
    FAILED = "FAILED"


class CaseStatus(StrEnum):
    OPEN = "OPEN"
    IN_REVIEW = "IN_REVIEW"
    NEEDS_MORE_INFO = "NEEDS_MORE_INFO"
    CONFIRMED_SIMULATED = "CONFIRMED_SIMULATED"
    CONFIRMED_FRAUD = "CONFIRMED_FRAUD"
    DISMISSED = "DISMISSED"
    ESCALATED = "ESCALATED"
    RETURNED = "RETURNED"
    CLOSED = "CLOSED"


class CaseOutcome(StrEnum):
    CONFIRMED_FRAUD = "CONFIRMED_FRAUD"
    DISMISSED = "DISMISSED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class AnalystAction(StrEnum):
    NOTE = "NOTE"
    NEEDS_MORE_INFO = "NEEDS_MORE_INFO"
    CONFIRM_SIMULATED = "CONFIRM_SIMULATED"
    ESCALATE = "ESCALATE"


class RiskLeadDecision(StrEnum):
    CLOSE = "CLOSE"
    RETURN = "RETURN"


class Role(StrEnum):
    ANALYST = "analyst"
    RISK_LEAD = "risk_lead"
    ADMIN = "admin"


# ------------------------------------------------------- Job envelope (Redis)
JOB_SCORE_TRANSACTION = "score_transaction"
JOB_CASE_CREATED = "case_created"
JOB_GENERATE_BRIEF = "generate_brief"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobEnvelope(BaseModel):
    """Phong bì chung cho mọi message đi qua Redis."""

    job_id: str = Field(default_factory=lambda: uuid4().hex)
    job_type: str
    priority: int = 1                 # 0 = cao nhất (High), 1 = thường (Medium)
    attempts: int = 0
    trace_id: str
    created_at: datetime = Field(default_factory=utcnow)
    payload: dict[str, Any]


# ---------------------------------------------------------- Payloads
class ScoreTransactionPayload(BaseModel):
    txn_id: str
    raw: dict[str, Any]               # giao dịch thô, ĐÃ bỏ is_fraud; chưa validate


class CaseCreatedEvent(BaseModel):
    case_id: int
    txn_id: str
    risk_level: RiskLevel
    calibrated_prob: float


class GenerateBriefPayload(BaseModel):
    case_id: int
    txn_id: str
    risk_level: RiskLevel
    regenerate: bool = False
    requested_by: str = "system"


# --------------------------------------------- Giao dịch đã qua validation (FR-01)
class TransactionValidated(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    trans_num: str = Field(min_length=1)
    trans_date_trans_time: datetime
    cc_num: str = Field(min_length=8, max_length=19)
    merchant: str = Field(min_length=1)
    category: str = Field(min_length=1)
    amt: float = Field(gt=0, lt=1_000_000)
    lat: float = Field(ge=-90, le=90)
    long: float = Field(ge=-180, le=180)
    merch_lat: float = Field(ge=-90, le=90)
    merch_long: float = Field(ge=-180, le=180)
    city: str | None = None
    state: str | None = None
    city_pop: int | None = None
    dob: datetime | None = None
    unix_time: int | None = None

    @field_validator("cc_num", "trans_num", mode="before")
    @classmethod
    def _int_to_str(cls, v: Any) -> Any:
        return str(v) if isinstance(v, int) and not isinstance(v, bool) else v


# ------------------------------------------------------------ Kết quả ML
class Explanation(BaseModel):
    feature: str
    value: float
    contribution: float               # SHAP-like, dương = tăng rủi ro


class ScoringOutput(BaseModel):
    raw_model_score: float
    calibrated_prob: float
    model_version: str
    explanations: list[Explanation]


class ThresholdConfig(BaseModel):
    version: int
    low_max: float
    high_min: float


# ------------------------------------------------------------ Kết quả LLM
class Citation(BaseModel):
    section_id: str
    rule_code: str
    quote: str | None = None


class ChecklistItem(BaseModel):
    step: int
    action: str
    rule_code: str | None = None


class BriefContent(BaseModel):
    summary: str
    key_signals: list[str]
    checklist: list[ChecklistItem]
    citations: list[Citation]
    source: Literal["llm", "rule_based"] = "llm"
