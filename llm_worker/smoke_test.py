"""Check API configuration, optionally make one request using synthetic evidence."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from fraud_common.config import get_settings
from .api_client import APIClient
from .grounding import check_grounding
from .rag import ApprovedPlaybookRetriever


async def check(live: bool = False) -> dict:
    settings = get_settings()
    if settings.llm_provider != 'api':
        raise ValueError('Set LLM_PROVIDER=api to check the online LLM')
    settings.validate_llm_timeouts()
    client = APIClient()
    report = {'provider': 'api', 'model': client.model_name(), 'status': 'configuration_valid'}
    if not live:
        return report
    # Never load production case data. This command does not need Redis/Postgres/Ollama.
    ctx = {
        'case_id': 1, 'txn_id': 'synthetic-smoke', 'risk_level': 'HIGH',
        'transaction': {'amt': 1500}, 'raw_model_score': 1.7, 'calibrated_prob': 0.8,
        'features': {'txn_count_24h': 0},
        'explanations': [{'feature': 'amt', 'value': 1500, 'contribution': 0.2}],
    }
    chunks = await ApprovedPlaybookRetriever().retrieve(['amt'], 'HIGH')
    started = time.perf_counter()
    result = await asyncio.wait_for(client.generate(ctx, chunks), settings.llm_job_timeout_s)
    check_grounding(result.brief, chunks)
    return dict(report, status='api_ok', tokens_used=result.tokens_used,
                latency_s=round(time.perf_counter() - started, 3),
                brief=result.brief.model_dump())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true',
                        help='Send one synthetic request to the configured API (billable).')
    args = parser.parse_args()
    try:
        report = asyncio.run(check(live=args.live))
    except Exception as exc:
        # APIClient sanitizes transport errors. Do not print tracebacks/provider bodies.
        print(f'LLM smoke test failed: {exc}', file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
