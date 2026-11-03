import asyncio
import copy
import hashlib
import json
import time
from datetime import datetime, timezone
from .contracts import ContractError, fields, validate_case
from .providers import PROMPT_VERSION
from .retrieval import retrieve


def fingerprint(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def build_evidence(case):
    tx, ml = case["transaction"], case["ml_result"]
    score_text = (f"Xác suất đã hiệu chỉnh do ML cung cấp: {ml['risk_score']:.2%}."
                  if ml["score_type"] == "calibrated_probability" else
                  f"Điểm model: {ml['risk_score']}; không mặc định là xác suất gian lận.")
    return {
        "EV-AMOUNT": {"text": f"Số tiền giao dịch: {tx['amount']} (đơn vị theo dữ liệu nguồn).", "path": "transaction.amount"},
        "EV-TIME": {"text": f"Thời gian tương đối trong dataset: {tx['relative_time_seconds']} giây; không phải giờ trên đồng hồ.", "path": "transaction.relative_time_seconds"},
        "EV-SCORE": {"text": score_text, "path": "ml_result.risk_score"},
        "EV-ROUTING": {"text": f"Mức ưu tiên do routing cung cấp: {case['routing']['priority']}.", "path": "routing.priority"}}


def validate_selection(selection, evidence, chunks):
    fields(selection, ("evidence_ids", "checklist_ids"))
    for key, allowed in (("evidence_ids", set(evidence)), ("checklist_ids", {c["id"] for c in chunks})):
        values = selection[key]
        if not isinstance(values, list) or not values or any(not isinstance(v, str) for v in values):
            raise ContractError("Invalid selection")
        if len(values) != len(set(values)) or not set(values) <= allowed:
            raise ContractError("Unknown or duplicate citation")
    if set(selection["evidence_ids"]) != set(evidence) or len(selection["checklist_ids"]) > 3:
        raise ContractError("Missing evidence or too many checklist items")


def generate_brief(case, kb, provider):
    """Call after backend persists scoring/routing; input errors raise ContractError.

    Provider errors return a failed brief with unchanged scoring/routing. This
    function never writes to a database or modifies the caller's objects.
    """
    started = time.monotonic()
    case = copy.deepcopy(validate_case(case))
    evidence = build_evidence(case)
    query = "kiểm tra giao dịch số tiền điểm rủi ro thiếu thông tin bằng chứng xác minh"
    if case["dataset"] == "ulb":
        query += " PCA ẩn danh"
    chunks = retrieve(kb, case["dataset"], query)
    result = {"case_id": case["case_id"], "status": "pending", "mode": provider.name,
              "ml_result": copy.deepcopy(case["ml_result"]), "routing": copy.deepcopy(case["routing"]),
              "observations": [], "investigation_checklist": [],
              "limitations": ["Chưa có local explanation của model trong contract v1.",
                              "Không có lịch sử theo khách hàng; không suy luận hành vi từ các feature ẩn danh.",
                              "Score không xác nhận gian lận; chuyên viên quyết định xử lý."],
              "trace": {"input_sha256": fingerprint(case), "data_version": case["data_version"],
                        "playbook_id": kb["document_id"], "playbook_version": kb["version"],
                        "playbook_sha256": fingerprint(kb), "prompt_version": PROMPT_VERSION,
                        "provider_model": provider.model, "retrieved_chunks": copy.deepcopy(chunks),
                        "created_at": datetime.now(timezone.utc).isoformat()}}
    if kb["demo"]:
        result["limitations"].append("Playbook giả lập để phát triển; chưa phải tài liệu chính thức của giảng viên.")
    if not chunks:
        result.update(status="insufficient_context", error_code="NO_RELEVANT_PLAYBOOK")
    else:
        try:
            selection = provider.select(copy.deepcopy(evidence), copy.deepcopy(chunks))
            validate_selection(selection, evidence, chunks)
            lookup = {c["id"]: c for c in chunks}
            result["observations"] = [dict(evidence[i], evidence_id=i) for i in selection["evidence_ids"]]
            result["investigation_checklist"] = [
                {"action": lookup[i]["action"], "source_id": i, "section": lookup[i]["section"],
                 "source_text": lookup[i]["text"]} for i in selection["checklist_ids"]]
            result["status"] = "ready"
        except Exception as exc:
            # Do not expose exception text: providers may include sensitive payloads.
            result.update(status="failed", error_code="INVALID_LLM_OUTPUT" if isinstance(exc, ContractError) else "PROVIDER_ERROR")
    result["trace"]["latency_ms"] = round((time.monotonic() - started) * 1000, 2)
    result["summary"] = " ".join(o["text"] for o in result["observations"])
    return result


async def generate_brief_async(case, kb, provider):
    return await asyncio.to_thread(generate_brief, case, kb, provider)
