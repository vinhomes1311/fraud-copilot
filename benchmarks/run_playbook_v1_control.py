"""Reproduce the fixed 2026-11-05 playbook comparison; requires local Ollama for the control run."""

def main():
    import json
    from pathlib import Path
    from benchmarks import compare_llm as b

    root=Path(__file__).resolve().parents[1]
    cases=json.loads((root/'benchmarks/fixtures/playbook_v2_cases.json').read_text(encoding='utf-8'))
    for case in cases:
        case['reference_any_rules']=[]
    out=root/'benchmark_results/playbook_v1_control_20261105'
    casefile=root/'benchmark_results/playbook_v1_control_cases.json'
    casefile.write_text(json.dumps(cases,ensure_ascii=False,indent=2),encoding='utf-8')
    b.local_module.KB_PATH=root/'benchmarks/fixtures/playbooks/common_playbook.v1.json'
    raise SystemExit(b.main(['--live','--providers','ollama','--scope','rag-llm','--retriever','ollama',
        '--cases',str(casefile),'--repeats','2','--warmup','1','--completion-cap','128',
        '--max-requests','100','--ollama-model','qwen2.5:3b','--output-dir',str(out)]))

if __name__ == "__main__":
    main()
