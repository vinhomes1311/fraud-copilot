"""Chat Completions adapter; queue owns retries and the overall stage deadline."""
import copy
import json
import os
from urllib.parse import urlsplit

import httpx

from copilot.providers import SYSTEM_PROMPT
from fraud_common.config import get_settings
from .local_integration import OllamaClient


class APIClient(OllamaClient):
    """Reuse the same evidence projection, selection validation and brief renderer."""

    def __init__(self):
        self.base_url = os.getenv('LLM_API_BASE_URL', '').strip().rstrip('/')
        self.model = os.getenv('LLM_API_MODEL', '').strip()
        self._key = os.getenv('LLM_API_KEY', '').strip()
        if not self.base_url or not self.model or not self._key:
            raise ValueError('Set LLM_API_BASE_URL, LLM_API_MODEL and LLM_API_KEY')
        url = urlsplit(self.base_url)
        if (url.scheme != 'https' or not url.hostname or url.username or
                url.password or url.query or url.fragment):
            raise ValueError('LLM_API_BASE_URL must be an HTTPS base URL without credentials/query/fragment')
        self.response_format = os.getenv('LLM_API_RESPONSE_FORMAT', 'json_schema')
        if self.response_format not in ('json_schema', 'json_object'):
            raise ValueError('LLM_API_RESPONSE_FORMAT must be json_schema or json_object')
        self.token_parameter = os.getenv('LLM_API_TOKEN_PARAMETER', 'max_completion_tokens')
        if self.token_parameter not in ('max_completion_tokens', 'max_tokens'):
            raise ValueError('Invalid LLM_API_TOKEN_PARAMETER')
        self.max_tokens = int(os.getenv('LLM_API_MAX_COMPLETION_TOKENS', '1024'))
        if self.max_tokens <= 0:
            raise ValueError('LLM_API_MAX_COMPLETION_TOKENS must be positive')

    def model_name(self):
        return self.model

    async def chat(self, model, schema, evidence, selected_chunks):
        # Some strict schema implementations do not support uniqueItems or array
        # length constraints. Local validation still enforces all these rules.
        api_schema = copy.deepcopy(schema)
        for prop in api_schema['properties'].values():
            for key in ('uniqueItems', 'minItems', 'maxItems'):
                prop.pop(key, None)
        fmt = {'type': self.response_format}
        if self.response_format == 'json_schema':
            fmt['json_schema'] = {'name': 'brief_selection', 'strict': True, 'schema': api_schema}
        payload = {
            'model': model, 'stream': False, 'response_format': fmt,
            self.token_parameter: self.max_tokens,
            'messages': [
                {'role': 'system', 'content': SYSTEM_PROMPT +
                 '\nReturn a JSON object containing only evidence_ids and checklist_ids. '
                 'Include every evidence ID exactly once and choose 1 to 3 unique checklist IDs from the playbook.'},
                {'role': 'user', 'content': json.dumps(
                    {'evidence': evidence, 'playbook': selected_chunks}, ensure_ascii=False)},
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=get_settings().llm_job_timeout_s,
                                         follow_redirects=False) as client:
                response = await client.post(self.base_url + '/chat/completions',
                    headers={'Authorization': 'Bearer ' + self._key}, json=payload)
        except httpx.TimeoutException:
            raise TimeoutError('LLM API timeout') from None
        except httpx.RequestError:
            raise RuntimeError('LLM API connection failed') from None
        # Do not persist provider response bodies, URLs or credentials in last_error.
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f'LLM API HTTP {response.status_code}')
        try:
            data = response.json()
            choices = data['choices']
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError()
            choice = choices[0]
            message = choice['message']
            if (choice['finish_reason'] != 'stop' or message.get('refusal') or
                    message.get('tool_calls') or message.get('function_call')):
                raise ValueError()
            content = message['content']
            if not isinstance(content, str) or not content.strip():
                raise ValueError()
            usage = data['usage']
            counts = [usage['prompt_tokens'], usage['completion_tokens'], usage['total_tokens']]
            if any(type(n) is not int or n < 0 for n in counts) or sum(counts[:2]) != counts[2]:
                raise ValueError()
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ValueError('Invalid or incomplete LLM API response') from None
        # Normalize transport fields for the shared extractive generation pipeline.
        return {'done': True, 'prompt_eval_count': counts[0], 'eval_count': counts[1],
                'message': {'content': content}}
