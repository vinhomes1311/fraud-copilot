"""Batch acceptance checks plus a separate human-review worksheet.

PASS covers structural/data checks only, not semantic quality or useful advice.
"""
import argparse
import copy
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from ..contracts import ContractError
from ..providers import MockProvider, OllamaProvider
from .contracts import validate
from .knowledge import load
from .service import generate, evidence_for
from .vectors import FixtureRetriever, SemanticRetriever, OllamaEmbeddings


class NeverProvider:
    name, model = "not_called", "not_called"
    def select(self, *_):
        raise AssertionError("LLM must not be called for this case")


class Counted:
    def __init__(self, provider):
        self.provider, self.calls = provider, 0
        self.name, self.model = provider.name, provider.model
    def select(self, *args):
        self.calls += 1
        return self.provider.select(*args)


def check_output(case, result, kb):
    """Mechanical grounding checks for the current extractive implementation."""
    errors = []
    if result.get("scoring_snapshot") != case["scoring"]:
        errors.append("Scoring/routing changed")
    expected = evidence_for(case)
    content = result.get("case_brief_content", {})
    if result.get("brief_status") == "COMPLETED":
        seen = set()
        observations = content.get("observations", [])
        for obs in observations:
            identifier = obs.get("evidence_id")
            if identifier in seen or identifier not in expected:
                errors.append("Duplicate/unknown evidence")
                continue
            seen.add(identifier)
            for key, value in expected[identifier].items():
                if obs.get(key) != value:
                    errors.append(f"Evidence mismatch: {identifier}.{key}")
        if seen != set(expected):
            errors.append("Missing evidence")
        if content.get("summary") != " ".join(obs["text"] for obs in observations):
            errors.append("Summary does not match checked evidence")
        sources = {c["id"]: c for c in kb["chunks"]}
        retrieved = {c["id"] for c in result.get("trace",{}).get("retrieved_chunks",[])}
        references = result.get("playbook_references", [])
        ref_ids = [r.get("source_id") for r in references]
        selected = []
        for item in content.get("investigation_checklist", []):
            identifier = item.get("source_id")
            selected.append(identifier)
            if identifier not in sources or identifier not in retrieved:
                errors.append("Checklist cites unknown/unretrieved source")
                continue
            if item.get("action") != sources[identifier]["implementation_action"] or item.get("action_origin") != "team_proposal":
                errors.append("Checklist content/provenance mismatch")
        if not selected or len(selected) > 3 or len(set(selected)) != len(selected) or set(ref_ids) != set(selected):
            errors.append("Invalid checklist/reference set")
        for ref in references:
            source = sources.get(ref.get("source_id"))
            if source is None:
                errors.append("Unknown reference")
                continue
            for key, value in (("section_id",source["section_id"]),("rule_code",source["rule_code"]),
                               ("source_text",source["text"]),("document_id",kb["document_id"]),("version",kb["version"])):
                if ref.get(key) != value:
                    errors.append(f"Reference mismatch: {key}")
    return errors


def score_retrieval(row, result, mode):
    gold = set(row["gold_rule_ids"])
    if mode != "semantic" or not gold or result["brief_status"] != "COMPLETED":
        return None
    ranked = [c["id"] for c in result["trace"].get("retrieved_chunks", [])]
    retrieved = set(ranked)
    top3 = set(ranked[:3])
    selected = {c["source_id"] for c in result["case_brief_content"]["investigation_checklist"]}
    return {"gold": sorted(gold), "retrieved": sorted(retrieved), "ranked_candidates": ranked,
            "selected": sorted(selected), "candidate_count": len(ranked),
            "recall_at_3": len(gold & top3)/len(gold),
            "precision_at_3": len(gold & top3)/len(top3) if top3 else 0,
            "candidate_recall": len(gold & retrieved)/len(gold),
            "checklist_precision": len(gold & selected)/len(selected) if selected else 0,
            "checklist_recall": len(gold & selected)/len(gold),
            "checklist_count": len(selected)}


def evaluate_entry(row, case, kb, provider, retriever):
    original = copy.deepcopy(case)
    try:
        validate(case)
    except ContractError:
        return {"outcome":"CONTRACT_REJECTED","automatic_pass":row["expected_outcome"]=="CONTRACT_REJECTED",
                "errors":[] if row["expected_outcome"]=="CONTRACT_REJECTED" else ["Unexpected contract rejection"],
                "llm_calls":0,"retrieval_metrics":None}, None
    if row["expected_outcome"] == "CONTRACT_REJECTED":
        return {"outcome":"CONTRACT_ACCEPTED","automatic_pass":False,"errors":["Invalid input accepted"],"llm_calls":0,"retrieval_metrics":None},None
    counted = Counted(provider)
    result = generate(case,kb,counted,retriever)
    errors = check_output(case,result,kb)
    if case != original:
        errors.append("Input mutated")
    if result["brief_status"] != row["expected_outcome"]:
        errors.append("Outcome differs from expected")
    if row["expected_outcome"] == "INSUFFICIENT_INFO":
        if result["error_code"] != "UNSCORED_CASE" or counted.calls != 0:
            errors.append("Unscored fallback should not invoke LLM")
    return {"outcome":result["brief_status"],"automatic_pass":not errors,"errors":errors,
            "llm_calls":counted.calls,"error_code":result["error_code"],
            "latency_ms":result["trace"].get("latency_ms"),
            "retrieval_metrics":score_retrieval(row,result,retriever.mode)}, result


class FaultProvider:
    name, model = "injected_fault", "no_actual_model"
    def __init__(self, kind): self.kind=kind
    def select(self, *_):
        if self.kind == "timeout": raise TimeoutError("Injected test timeout")
        return {"evidence_ids":["INVENTED"],"checklist_ids":["PB-01"]}


class FaultRetriever:
    mode = "injected_fault"
    def __init__(self, kind): self.kind=kind
    def search(self, *_):
        if self.kind == "timeout": raise TimeoutError("Injected retrieval timeout")
        return []


def fault_suite(case, kb):
    scenarios = [
        ("llm_timeout",FaultProvider("timeout"),FixtureRetriever(kb),"FAILED","LLM_TIMEOUT"),
        ("invalid_llm_citation",FaultProvider("invalid"),FixtureRetriever(kb),"FAILED","INVALID_LLM_OUTPUT"),
        ("retrieval_timeout",NeverProvider(),FaultRetriever("timeout"),"FAILED","RETRIEVAL_TIMEOUT"),
        ("no_retrieval_context",NeverProvider(),FaultRetriever("empty"),"INSUFFICIENT_INFO","NO_RELEVANT_PLAYBOOK")]
    rows=[]
    for name,provider,retriever,status,code in scenarios:
        counted=Counted(provider)
        result=generate(copy.deepcopy(case),kb,counted,retriever)
        passed=result["brief_status"]==status and result["error_code"]==code and result["scoring_snapshot"]==case["scoring"]
        if name in ("retrieval_timeout","no_retrieval_context"):
            passed=passed and counted.calls==0
        rows.append({"scenario":name,"automatic_pass":passed,"outcome":result["brief_status"],"error_code":result["error_code"]})
    return rows


def main():
    p=argparse.ArgumentParser(description="Synthetic acceptance cases; semantic quality requires separate human review")
    p.add_argument("--manifest",default="evaluation/manifest.json")
    p.add_argument("--playbook",default="knowledge/common_playbook.json")
    p.add_argument("--index",default="artifacts/playbook-index.json")
    p.add_argument("--provider",choices=("mock","ollama"),required=True)
    p.add_argument("--retriever",choices=("fixture","semantic"),required=True)
    p.add_argument("--candidate-count",type=int,choices=range(1,6),default=5)
    p.add_argument("--faults",action="store_true",help="Run four injected failures without external LLM calls")
    p.add_argument("--output-dir",default="artifacts/evaluation")
    args=p.parse_args()
    source=Path(args.manifest).resolve()
    manifest=json.loads(source.read_text(encoding="utf-8"))
    kb=load(args.playbook)
    output=Path(args.output_dir)/(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"_"+uuid4().hex[:6])
    output.mkdir(parents=True)
    provider=retriever=None
    init_error=None
    rows=[]
    human=[]
    fault_case=None
    for row in manifest["cases"]:
        case_path=(source.parent/row["file"]).resolve()
        if not case_path.is_relative_to(source.parent):
            raise ValueError("Case path escapes suite directory")
        case=json.loads(case_path.read_text(encoding="utf-8"))
        complete=row["expected_outcome"]=="COMPLETED"
        if complete and fault_case is None: fault_case=copy.deepcopy(case)
        if complete and provider is None and init_error is None:
            try:
                retriever=FixtureRetriever(kb,args.candidate_count) if args.retriever=="fixture" else SemanticRetriever(kb,OllamaEmbeddings(),args.index,args.candidate_count)
                provider=MockProvider() if args.provider=="mock" else OllamaProvider(timeout=60)
            except Exception as exc:
                init_error=type(exc).__name__  # no payload or credentials
        result=None
        try:
            if complete and init_error:
                detail={"outcome":"CONFIG_ERROR","automatic_pass":False,"errors":["Provider/index initialization failed: "+init_error],"llm_calls":0,"retrieval_metrics":None}
            else:
                detail,result=evaluate_entry(row,case,kb,provider if complete else NeverProvider(),retriever if complete else FaultRetriever("empty"))
        except Exception as exc:
            detail={"outcome":"RUNNER_ERROR","automatic_pass":False,"errors":[type(exc).__name__],"llm_calls":0,"retrieval_metrics":None}
        if result is not None:
            (output/(row["id"]+".json")).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        detail.update(id=row["id"],title=row["title"],expected=row["expected_outcome"])
        rows.append(detail)
        human.append({"case_id":row["id"],"title":row["title"],"expected":row["expected_outcome"],
                      "review_checks":" | ".join(row["review_checks"]),"must_not_infer":" | ".join(row["must_not_infer"]),
                      "groundedness_pass":"","citation_support_pass":"","checklist_relevance_0_2":"","limitations_pass":"","notes":""})
        print(f"{row['id']}: {'PASS' if detail['automatic_pass'] else 'FAIL'} ({detail['outcome']})",flush=True)
    faults=fault_suite(fault_case,kb) if args.faults and fault_case is not None else []
    passed=sum(row["automatic_pass"] for row in rows)
    measured=[r["retrieval_metrics"] for r in rows if r["retrieval_metrics"] is not None]
    report={"suite_version":manifest["suite_version"],"provider":args.provider,"retriever":args.retriever,
            "candidate_count":args.candidate_count,
            "automatic_passed":passed,"case_count":len(rows),"human_quality":"NOT_EVALUATED",
            "retrieval_metric_case_count":len(measured),
            "mean_recall_at_3":sum(m["recall_at_3"] for m in measured)/len(measured) if measured else None,
            "note":"Automated PASS is not a groundedness/relevance score. Gold rules are team-proposed. Fault suite uses injected errors.",
            "cases":rows,"injected_faults":faults}
    for metric in ("precision_at_3", "candidate_recall", "checklist_precision", "checklist_recall", "checklist_count"):
        report["mean_"+metric] = sum(m[metric] for m in measured)/len(measured) if measured else None
    report["three_item_checklist_rate"] = sum(m["checklist_count"] == 3 for m in measured)/len(measured) if measured else None
    (output/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    with (output/"human_review.csv").open("w",encoding="utf-8-sig",newline="") as f:
        writer=csv.DictWriter(f,fieldnames=list(human[0]))
        writer.writeheader();writer.writerows(human)
    print(f"Automatic checks: {passed}/{len(rows)}; injected faults: {sum(r['automatic_pass'] for r in faults)}/{len(faults)}. Human review pending.")
    print(f"Reports: {output}")
    return 0 if passed==len(rows) and all(r["automatic_pass"] for r in faults) else 1


if __name__=="__main__":
    if hasattr(sys.stdout,"reconfigure"):sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
