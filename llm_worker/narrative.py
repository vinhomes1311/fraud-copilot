"""Validate short, evidence-linked explanations, without asking for private reasoning."""
import re

from copilot.contracts import fields
from .brief_renderer import case_facts, display_number


PLACEHOLDER = re.compile(r'\{\{(EV-[A-Za-z0-9_-]+)\}\}')
MISSING_NOTES = re.compile(
    r'time\s?zone|currency|missing|unavailable|not (?:supplied|provided)|'
    r'insufficient (?:data|history|information)|lack of|no (?:valid |historical )?baseline', re.I)
INTERNAL_NOTES = re.compile(
    r'implementation_action|provided feature|as an ai|system prompt|'
    r'\b(?:write|record|ignore|omit) (?:the |a |these |this )|'
    r'\b(?:customer was contacted|card was blocked|verified with the customer)\b', re.I)

MAX_CHECKLIST_ITEMS = 3
MAX_RATIONALE_EVIDENCE = 6


def brief_schema(evidence, chunks):
    """One output contract for local constrained decoding and hosted JSON Schema."""
    mandatory = [c['id'] for c in chunks if c.get('selection_role') == 'mandatory_gap']
    allowed = mandatory or [c['id'] for c in chunks]
    maximum = len(mandatory) if mandatory else MAX_CHECKLIST_ITEMS
    rationale = {
        'type': 'object', 'additionalProperties': False,
        'required': ['rule_code', 'evidence_ids', 'text'],
        'properties': {
            'rule_code': {'type': 'string', 'enum': allowed},
            'evidence_ids': {
                'type': 'array', 'minItems': 1, 'maxItems': MAX_RATIONALE_EVIDENCE,
                'uniqueItems': True,
                'items': {'type': 'string', 'enum': list(case_facts(evidence))}},
            'text': {'type': 'string'},
        },
    }
    return {
        'type': 'object', 'additionalProperties': False,
        'required': ['checklist_ids', 'rationales'],
        'properties': {
            'checklist_ids': {'type': 'array', 'minItems': 1, 'maxItems': maximum,
                              'uniqueItems': True, 'items': {'type': 'string', 'enum': allowed}},
            'rationales': {'type': 'array', 'minItems': 1, 'maxItems': maximum,
                           'items': rationale},
        },
    }


def prompt_payload(evidence, chunks):
    """Separate visible case facts and verified comparisons from source instructions."""
    comparisons = []
    amount = evidence['EV-AMOUNT']['value']
    for name in ('avg_amt_24h', 'avg_amt_7d', 'max_amt_7d'):
        key = 'EV-FEATURE-' + name
        if key in evidence:
            reference = evidence[key]['value']
            relation = 'greater_than' if amount > reference else 'less_than' if amount < reference else 'equal_to'
            comparisons.append({'left': 'EV-AMOUNT', 'right': key, 'relation': relation})
    return {'evidence': evidence, 'case_facts': case_facts(evidence),
            'verified_comparisons': comparisons, 'playbook': chunks}


def validate_rationales(items, rules, evidence):
    """Check source links and numeric substitutions before any prose is displayed.

    This validates structure and numeric provenance, not the semantic truth of
    arbitrary prose. Interpretations remain analyst decision support.
    """
    facts = case_facts(evidence)
    if not isinstance(items, list) or len(items) != len(rules):
        raise ValueError('Each selected rule requires one rationale')
    result = {}
    for item in items:
        fields(item, ('rule_code', 'evidence_ids', 'text'))
        rule, ids, prose = item['rule_code'], item['evidence_ids'], item['text']
        if not isinstance(rule, str) or rule not in rules or rule in result:
            raise ValueError('Unknown or duplicate rationale rule')
        if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_RATIONALE_EVIDENCE or
                any(not isinstance(key, str) for key in ids) or
                len(ids) != len(set(ids)) or not set(ids) <= facts.keys()):
            raise ValueError('Rationale must reference visible verified case facts')
        if (not isinstance(prose, str) or not 80 <= len(prose.strip()) <= 1600 or
                not 25 <= len(prose.split()) <= 180):
            raise ValueError('Rationale must contain a short substantive explanation')
        if any(c.isalpha() and not c.isascii() for c in prose):
            raise ValueError('Rationale must be in English')
        if MISSING_NOTES.search(prose) or INTERNAL_NOTES.search(prose):
            raise ValueError('Rationale contains internal or missing-data notes')
        placeholders = PLACEHOLDER.findall(prose)
        if not set(placeholders) <= set(ids):
            raise ValueError('Rationale quotes an uncited value')
        if any(key.startswith('EV-FACTOR-') for key in placeholders):
            raise ValueError('Factor values must remain in supporting facts')
        remaining = PLACEHOLDER.sub('', prose)
        remaining = re.sub(r'\b(?:1|7|24)[ -](?:hour|day)s?\b', '', remaining, flags=re.I)
        if re.search(r'\d|[{}<>`]|[\x00-\x08\x0b-\x1f]', remaining):
            raise ValueError('Rationale numbers must use verified evidence placeholders')

        def substitute(match):
            value = evidence[match[1]].get('value')
            if value is None:
                raise ValueError('Rationale placeholder has no scalar value')
            return display_number(value)

        result[rule] = {'text': ' '.join(PLACEHOLDER.sub(substitute, prose).split()),
                        'evidence_ids': list(ids)}
    if set(result) != set(rules):
        raise ValueError('Missing rationale for a selected rule')
    return result
