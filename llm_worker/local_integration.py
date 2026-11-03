"""Async adapters for the team's contracts; facts are rendered from allowlisted evidence."""
import asyncio
import json
import os
from pathlib import Path

import httpx
from copilot.providers import SYSTEM_PROMPT
from copilot.service import validate_selection
from copilot.v2.knowledge import load
from copilot.v2.vectors import normalize
from copilot.contracts import number
from fraud_common.config import get_settings
from fraud_common.schemas import BriefContent, ChecklistItem, Citation
from .llm_client import LLMResult
from .rag import Chunk

KB_PATH = Path(__file__).resolve().parents[1] / 'knowledge/common_playbook.json'


async def request(path, payload):
    base = os.getenv('OLLAMA_BASE_URL', 'http://127.0.0.1:11434').rstrip('/')
    # Deployment-owned endpoint only. No endpoint can be supplied by a job.
    try:
        async with httpx.AsyncClient(timeout=get_settings().llm_job_timeout_s,
                                     follow_redirects=False) as client:
            response = await client.post(base + path, json=payload)
            response.raise_for_status()
            return response.json()
    except httpx.TimeoutException as exc:
        raise TimeoutError('Ollama timeout') from exc


class SemanticRetriever:
    def __init__(self):
        self.kb = load(KB_PATH)

    async def retrieve(self, signals, risk_level, top_k=5):
        # Embed the tiny five-rule KB and query in one request: no stale index/model cache.
        signals = [s for s in signals if s in {'amt', 'dist_home_merchant_km', 'txn_count_24h', 'avg_amt_24h', 'amt_vs_avg_ratio', 'hour_of_day'}]
        query = 'Review transaction risk ' + str(risk_level) + '; signals: ' + ', '.join(signals)
        texts = ['title: ' + c['rule_code'] + ' | text: ' + c['text'] for c in self.kb['chunks']]
        result = await request('/api/embed', {
            'model': os.getenv('OLLAMA_EMBED_MODEL', 'embeddinggemma:300m'),
            'input': texts + ['task: search result | query: ' + query],
            'truncate': False, 'keep_alive': 0})
        vectors = [normalize(v) for v in result['embeddings']]
        if len(vectors) != len(texts) + 1 or len({len(v) for v in vectors}) != 1:
            raise ValueError('Invalid embedding dimensions/count')
        ranked = sorted(zip(vectors[:-1], self.kb['chunks']),
                        key=lambda pair: (-sum(a*b for a,b in zip(pair[0], vectors[-1])), pair[1]['id']))
        return [Chunk(section_id=c['section_id'], rule_code=c['rule_code'], text=c['text'])
                for _, c in ranked[:top_k]]


def project_evidence(ctx):
    """Exclude identifiers and arbitrary raw fields even if upstream calls them masked."""
    tx = ctx['transaction']
    evidence = {}
    def add(key, value, description):
        number(value)
        evidence[key] = {'value': value, 'text': description + ': ' + str(value)}
    add('EV-AMOUNT', tx['amt'], 'Transaction amount (currency not supplied)')
    add('EV-RAW', ctx['raw_model_score'], 'Raw model score; not necessarily a probability')
    prob = ctx['calibrated_prob']
    if prob is not None:
        number(prob)
        if not 0 <= prob <= 1:
            raise ValueError('Invalid calibrated probability')
        add('EV-PROB', prob, 'Calibrated probability')
    if ctx['risk_level'] not in ('HIGH', 'MEDIUM', 'LOW'):
        raise ValueError('Invalid risk level')
    evidence['EV-ROUTE'] = {'value': ctx['risk_level'], 'text': 'Routing: ' + ctx['risk_level']}
    allowed = {'amt', 'dist_home_merchant_km', 'txn_count_24h', 'avg_amt_24h',
               'amt_vs_avg_ratio', 'hour_of_day'}
    features = ctx.get('features') or {}
    for key in sorted(allowed & features.keys()):
        if key == 'avg_amt_24h' and not features.get('txn_count_24h', 0):
            continue  # SQL COALESCE zero is not an observed historical average.
        add('EV-FEATURE-' + key, features[key], key + ' (provided feature)')
    for i, factor in enumerate(ctx.get('explanations') or []):
        if factor['feature'] not in allowed:
            continue
        number(factor['value']); number(factor['contribution'])
        evidence['EV-FACTOR-' + str(i)] = {
            'text': f"{factor['feature']} = {factor['value']}; model contribution {factor['contribution']}; method and scale not supplied. This does not confirm fraud."}
    evidence['EV-LIMIT'] = {'text': 'Model signals do not confirm fraud. Final decision belongs to the analyst. Checklist actions are team proposals based on playbook principles.'}
    if not features.get('txn_count_24h', 0):
        evidence['EV-HISTORY-GAP'] = {'text': 'No positive historical transaction count supplied; no behavioral baseline can be established.'}
    return evidence


class OllamaClient:
    async def generate(self, ctx, chunks):
        kb = load(KB_PATH)
        lookup = {c['rule_code']: c for c in kb['chunks']}
        selected_chunks = []
        for chunk in chunks:
            c = lookup.get(chunk.rule_code)
            if c is None or c['section_id'] != chunk.section_id or c['text'] != chunk.text:
                raise ValueError('Retrieved chunk does not match approved playbook')
            selected_chunks.append(c)
        if not selected_chunks:
            raise ValueError('No playbook context')
        evidence = project_evidence(ctx)
        schema = {'type':'object', 'additionalProperties':False,
                  'required':['evidence_ids','checklist_ids'], 'properties':{
            'evidence_ids':{'type':'array','minItems':len(evidence),'maxItems':len(evidence),
                            'uniqueItems':True,'items':{'type':'string','enum':list(evidence)}},
            'checklist_ids':{'type':'array','minItems':1,'maxItems':3,'uniqueItems':True,
                             'items':{'type':'string','enum':[c['id'] for c in selected_chunks]}}}}
        model = os.environ.get('OLLAMA_MODEL')
        if not model:
            raise ValueError('OLLAMA_MODEL must name an installed model')
        result = await request('/api/chat', {'model':model,'stream':False,'format':schema,
            'options':{'temperature':0,'num_predict':512}, 'messages':[
                {'role':'system','content':SYSTEM_PROMPT},
                {'role':'user','content':json.dumps({'evidence':evidence,'playbook':selected_chunks},ensure_ascii=False)}]})
        if result.get('done') is not True or result.get('done_reason') == 'length':
            raise ValueError('Incomplete LLM response')
        counts = [result.get('prompt_eval_count'), result.get('eval_count')]
        if any(type(n) is not int or n < 0 for n in counts):
            raise ValueError('Missing/invalid token usage')
        selection = json.loads(result['message']['content'])
        validate_selection(selection, evidence, selected_chunks)
        chosen = {c['id']:c for c in selected_chunks}
        actions = [chosen[i] for i in selection['checklist_ids']]
        brief = BriefContent(
            summary=' '.join(evidence[i]['text'] for i in selection['evidence_ids']),
            key_signals=[v['text'] for k,v in evidence.items() if k.startswith(('EV-FACTOR','EV-FEATURE'))],
            checklist=[ChecklistItem(step=i+1, action=c['implementation_action'],rule_code=c['rule_code']) for i,c in enumerate(actions)],
            citations=[Citation(section_id=c['section_id'],rule_code=c['rule_code'],quote=c['text']) for c in actions],source='llm')
        return LLMResult(brief=brief,tokens_used=sum(counts),model_name=model)
