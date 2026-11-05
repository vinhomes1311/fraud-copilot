"""Reproduce the fixed 2026-11-05 playbook comparison; requires local Ollama for the control run."""

def main():
    import json
    from pathlib import Path
    r=Path(__file__).resolve().parents[1]
    b=r/'benchmark_results'
    new=json.loads((b/'playbook_v2_20261105/results.json').read_text())
    old=json.loads((b/'playbook_v1_control_20261105/results.json').read_text())
    cases=json.loads((r/'benchmarks/fixtures/playbook_v2_cases.json').read_text(encoding='utf-8'))
    control=json.loads((b/'playbook_v1_control_cases.json').read_text(encoding='utf-8'))
    assert [(c['id'],c['context']) for c in cases]==[(c['id'],c['context']) for c in control]
    for key in ('repeats','warmup','concurrency','completion_cap','seed','scope','retriever'):
        assert old['config'][key]==new['config'][key]
    # Frozen conceptual family crosswalk; does not turn coarse matches into detailed correctness.
    families=[1,2,3,4,5,3,3,1,1,1,3,3,3,1,1,3,1,1,1,1,2,2,2,3,1,2,4,5,4]
    mapping={f'PB-{i:02d}':f'PB-{family:02d}' for i,family in enumerate(families,1)}
    lookup={c['id']:c for c in cases}
    def metric(report):
        rows=[s for s in report['samples'] if s['phase']=='measured']
        passed=0
        for row in rows:
            chosen={mapping[x] for x in row['checklist_rules']}
            groups=[{mapping[x] for x in g} for g in lookup[row['case_id']]['reference_groups']]
            passed += bool(row['ok'] and all(chosen & group for group in groups))
        p=report['providers']['ollama']
        warm=[s['latency_s'] for s in report['samples'] if s['phase']=='warmup']
        return {'valid':p['valid_briefs'],'attempted':p['attempted_measured'],
                'family_match_rate':passed/40,'p50':p['latency_success_s']['p50'],
                'p95':p['latency_success_s']['p95'],'mean':p['latency_success_s']['mean'],
                'warmup_s':warm[0] if warm else None}
    results={'old':metric(old),'new':metric(new),'family_mapping':mapping}
    (b/'playbook_comparison_20261105.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
    lines=['# So sánh playbook cũ và mới — 2026-11-05','',
           'Cùng 20 context giả, 2 lượt/case, 1 warm-up, concurrency 1, Qwen2.5:3b, embeddinggemma:300m, output cap 128 và cùng adapter đã tối ưu. Chỉ thay kho playbook 5 mục bằng 29 mục. Không thay weights hoặc phiên bản Ollama.', '',
           '| Chỉ số | Playbook cũ | Playbook mới |','|---|---:|---:|']
    for label,key in [('Brief hợp lệ','valid'),('Lượt đo','attempted'),('Khớp nhóm hành động tương đương','family_match_rate'),('p50 (giây)','p50'),('p95 (giây)','p95'),('Mean (giây)','mean'),('Warm-up (giây)','warmup_s')]:
        a,z=results['old'][key],results['new'][key]
        fmt=lambda x:f'{x:.1%}' if key=='family_match_rate' else f'{x:.2f}'
        lines.append(f'| {label} | {fmt(a)} | {fmt(z)} |')
    lines += ['', 'Nhóm hành động quy các rule chi tiết về năm nguyên tắc gốc: bất thường/đối chiếu tín hiệu, giới hạn score, lịch sử, quyết định analyst, thiếu bằng chứng. Đây là chỉ số thô để hai bộ so sánh được; không chứng minh bộ cũ đáp ứng các điều kiện chi tiết của bộ mới.',
              'Strict success được tính riêng bởi playbook_eval.py; không so với reference-any của các lần đo cũ.',
              'Hai batch chạy nối tiếp, có warm-up nhưng không ép trạng thái cold-start/GPU giống hệt. Số mẫu nhỏ và prefix cache có thể ảnh hưởng latency. Không quy toàn bộ chênh lệch cho playbook hoặc coi đây là nâng cấp weights Ollama.', '',
              'Crosswalk sử dụng trước khi chấm:', '```json', json.dumps(mapping,indent=2), '```']
    (b/'playbook_comparison_20261105.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps(results,indent=2))

if __name__ == "__main__":
    main()
