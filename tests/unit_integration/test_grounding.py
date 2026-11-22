import pytest

from fraud_common.schemas import BriefContent, ChecklistItem, Citation
from llm_worker.grounding import GroundingError, check_grounding
from llm_worker.rag import Chunk


def brief(quote='Check history', rule='PB-01'):
    return BriefContent(summary='Source facts', key_signals=[],
                        checklist=[ChecklistItem(step=1, action='Review', rule_code=rule)],
                        citations=[Citation(section_id='s1', rule_code='PB-01', quote=quote)])


def test_full_source_and_exact_excerpt_are_valid():
    chunks = [Chunk(section_id='s1', rule_code='PB-01', text='Check history before comparison.')]
    check_grounding(brief(), chunks)
    check_grounding(brief(quote=chunks[0].text), chunks)


@pytest.mark.parametrize('quote', ['', '  ', 'Confirmed fraud', 'Check history.'])
def test_matching_source_id_does_not_allow_fabricated_or_empty_quotes(quote):
    chunks = [Chunk(section_id='s1', rule_code='PB-01', text='Check history before comparison.')]
    with pytest.raises(GroundingError, match='quote'):
        check_grounding(brief(quote=quote), chunks)


def test_quote_cannot_be_borrowed_from_another_source():
    chunks = [Chunk(section_id='s1', rule_code='PB-01', text='Check history'),
              Chunk(section_id='s2', rule_code='PB-02', text='Check amount')]
    with pytest.raises(GroundingError, match='quote'):
        check_grounding(brief(quote='Check amount'), chunks)


def test_checklist_rule_requires_its_own_citation():
    chunks = [Chunk(section_id='s1', rule_code='PB-01', text='Check history'),
              Chunk(section_id='s2', rule_code='PB-02', text='Check amount')]
    with pytest.raises(GroundingError, match='thiếu trích dẫn'):
        check_grounding(brief(rule='PB-02'), chunks)
