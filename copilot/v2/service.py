import copy
import time
import urllib.error
from datetime import datetime, timezone
from ..contracts import ContractError
from ..providers import PROMPT_VERSION
from ..service import fingerprint, validate_selection
from .contracts import validate


def evidence_for(case):
    evidence = {}

    def add(key, path, value, description):
        evidence[key] = {"path": path, "value": value, "text": description}

    tx, scoring = case["transaction"], case["scoring"]
    if tx["amount"] is not None:
        add("EV-AMOUNT", "transaction.amount", tx["amount"], f"Số tiền: {tx['amount']} {tx['currency'] or '(đơn vị chưa cung cấp)' }.")
    if tx["tx_timestamp"] is not None:
        add("EV-TIME", "transaction.tx_timestamp", tx["tx_timestamp"], f"Thời điểm giao dịch: {tx['tx_timestamp']}.")
    elif tx["relative_time_seconds"] is not None:
        add("EV-TIME", "transaction.relative_time_seconds", tx["relative_time_seconds"], f"Mốc thời gian tương đối: {tx['relative_time_seconds']} giây; không phải giờ trên đồng hồ.")
    if scoring["raw_model_score"] is not None:
        add("EV-RAW", "scoring.raw_model_score", scoring["raw_model_score"], f"Raw score: {scoring['raw_model_score']}; không mặc định là xác suất gian lận.")
    if scoring["calibrated_prob"] is not None:
        add("EV-PROB", "scoring.calibrated_prob", scoring["calibrated_prob"], f"Xác suất đã hiệu chỉnh do ML cung cấp: {scoring['calibrated_prob']:.2%}.")
    add("EV-ROUTE", "scoring.routing_decision", scoring["routing_decision"], f"Kết quả routing: {scoring['routing_decision']}.")
    for i, factor in enumerate(case["risk_factors_json"]):
        meaning = factor["business_meaning"] or "chưa có diễn giải nghiệp vụ được cung cấp"
        meaning = meaning.strip().rstrip(".").rstrip()
        add(f"EV-FACTOR-{i}", f"risk_factors_json[{i}]", factor,
            f"{factor['feature']} = {factor['value']}; đóng góp {factor['contribution']} trên thang {factor['output_scale']} theo {factor['method']}; {meaning}. Đây là giải thích dự đoán, không phải bằng chứng xác nhận gian lận.")
    history = case["history"]
    if history["available"]:
        count = history["transaction_count"]
        if count == 0:
            detail = "Chưa có dữ liệu để xác lập mức giao dịch thông thường."
        elif history["median_amount"] is None:
            detail = "Chưa cung cấp trung vị số tiền."
        else:
            detail = f"Trung vị số tiền: {history['median_amount']}."
        add("EV-HISTORY", "history", history,
            f"Lịch sử được cung cấp đến {history['window_end']}: {count} giao dịch. {detail}")
    return evidence


def retrieval_query(case):
    parts = ["Kiểm tra giao dịch bất thường; phân biệt tín hiệu nghi ngờ và bằng chứng xác nhận."]
    parts += [f["business_meaning"] for f in case["risk_factors_json"] if f["business_meaning"]]
    history = case["history"]
    if not history["available"]:
        parts.append("Thiếu lịch sử; không đủ bằng chứng để kết luận.")
    elif history["transaction_count"] == 0:
        parts.append("Kiểm tra lịch sử liên quan: truy xuất thành công nhưng không có bản ghi; không đủ dữ liệu xác lập hành vi thông thường, không đủ bằng chứng để kết luận.")
    elif history["median_amount"] is None:
        parts.append("Kiểm tra lịch sử liên quan đã có bản ghi nhưng thiếu trung vị số tiền; ghi nhận thông tin còn thiếu, chưa đủ cơ sở so sánh số tiền.")
    else:
        parts.append("Đối chiếu thông tin giao dịch với lịch sử liên quan và trung vị số tiền đã được cung cấp.")
    if not case["risk_factors_json"]:
        parts.append("Thiếu giải thích dự đoán của model; không tự suy luận nguyên nhân điểm rủi ro, nêu thông tin còn thiếu.")
    elif any(f["business_meaning"] is None for f in case["risk_factors_json"]):
        parts.append("Đặc trưng chưa có diễn giải nghiệp vụ; phân biệt đóng góp model với bằng chứng xác nhận, không suy luận ý nghĩa của feature ẩn danh.")
    if case["scoring"]["routing_decision"] == "HIGH_RISK":
        parts.append("Nguyên tắc chuyển cấp và ghi nhận quyết định.")
    return " ".join(parts)


def generate(case, kb, provider, retriever):
    started = time.monotonic()
    case = copy.deepcopy(validate(case))
    evidence = evidence_for(case)
    limitations = ["Score và giải thích model không xác nhận gian lận; quyết định thuộc chuyên viên.",
                  "Playbook quy định nguyên tắc; các bước triển khai checklist là đề xuất của nhóm."]
    if not case["history"]["available"]:
        limitations.append("Không có lịch sử theo tài khoản để đối chiếu.")
    elif case["history"]["transaction_count"] == 0:
        limitations.append("Truy xuất lịch sử thành công nhưng không có bản ghi; chưa đủ dữ liệu để xác lập mức giao dịch thông thường.")
    if not case["risk_factors_json"]:
        limitations.append("Chưa cung cấp giải thích dự đoán cho giao dịch này.")
    if any(f["business_meaning"] is None for f in case["risk_factors_json"]):
        limitations.append("Không gán ý nghĩa nghiệp vụ cho đặc trưng chưa có diễn giải, bao gồm feature ẩn danh.")
    result = {"case_id": case["case_id"], "transaction_id": case["transaction_id"],
              "brief_status": "PENDING", "error_code": None,
              "scoring_snapshot": copy.deepcopy(case["scoring"]),
              "case_brief_content": {"summary": "", "observations": [], "investigation_checklist": [], "limitations": limitations},
              "playbook_references": [],
              "trace": {"schema_version": "2.0", "input_sha256": fingerprint(case),
                        "input_data_version": case["input_data_version"], "prompt_version": PROMPT_VERSION,
                        "provider": provider.name, "provider_model": provider.model, "retrieval_mode": retriever.mode,
                        "playbook_id": kb["document_id"], "playbook_version": kb["version"],
                        "playbook_sha256": fingerprint(kb), "created_at": datetime.now(timezone.utc).isoformat()}}
    if case["scoring"]["routing_decision"] == "UNSCORED_FALLBACK":
        result.update(brief_status="INSUFFICIENT_INFO", error_code="UNSCORED_CASE")
        result["case_brief_content"]["limitations"].append("Giao dịch chưa được scoring hợp lệ; cần chuyên viên kiểm tra dữ liệu hoặc lỗi ML.")
    else:
        stage = "RETRIEVAL"
        try:
            query = retrieval_query(case)
            result["trace"]["retrieval_query"] = query
            result["trace"]["query_version"] = "case-context-v2"
            chunks = retriever.search(query)
            result["trace"]["retrieved_chunks"] = copy.deepcopy(chunks)
            result["trace"]["candidate_count"] = len(chunks)
            if not chunks:
                result.update(brief_status="INSUFFICIENT_INFO", error_code="NO_RELEVANT_PLAYBOOK")
            else:
                stage = "LLM"
                selection = provider.select(copy.deepcopy(evidence), copy.deepcopy(chunks))
                validate_selection(selection, evidence, chunks)
                lookup = {c["id"]: c for c in chunks}
                content = result["case_brief_content"]
                content["observations"] = [dict(evidence[i], evidence_id=i) for i in selection["evidence_ids"]]
                content["summary"] = " ".join(o["text"] for o in content["observations"])
                for source_id in selection["checklist_ids"]:
                    chunk = lookup[source_id]
                    content["investigation_checklist"].append({"action": chunk["implementation_action"],
                        "action_origin": chunk["action_origin"], "source_id": source_id})
                    result["playbook_references"].append({"source_id": source_id, "section_id": chunk["section_id"],
                        "rule_code": chunk["rule_code"], "source_text": chunk["text"],
                        "document_id": kb["document_id"], "version": kb["version"]})
                result["brief_status"] = "COMPLETED"
        except Exception as exc:
            timeout = isinstance(exc, TimeoutError) or (isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError))
            code = f"{stage}_TIMEOUT" if timeout else ("INVALID_LLM_OUTPUT" if stage == "LLM" and isinstance(exc, (ContractError, ValueError, KeyError, TypeError)) else f"{stage}_ERROR")
            result.update(brief_status="FAILED", error_code=code)
    result["trace"]["latency_ms"] = round((time.monotonic() - started) * 1000, 2)
    return result
