from analyst_api.routes.cases import OUTCOME_TO_STATUS, OutcomeRequest, can_update_outcome
from fraud_common.schemas import CaseOutcome, CaseStatus


def test_case_outcomes_map_to_distinct_workflow_statuses():
    assert OUTCOME_TO_STATUS == {
        CaseOutcome.CONFIRMED_FRAUD: CaseStatus.CONFIRMED_FRAUD,
        CaseOutcome.DISMISSED: CaseStatus.DISMISSED,
        CaseOutcome.INSUFFICIENT_EVIDENCE: CaseStatus.RETURNED,
    }


def test_outcome_request_accepts_audit_note():
    request = OutcomeRequest.model_validate({
        "outcome": "CONFIRMED_FRAUD",
        "note": "Verified with cardholder",
    })

    assert request.outcome is CaseOutcome.CONFIRMED_FRAUD
    assert request.note == "Verified with cardholder"


def test_final_outcomes_can_be_reclassified_only_when_a_decision_is_in_history():
    assert can_update_outcome(CaseStatus.CONFIRMED_FRAUD, True)
    assert can_update_outcome(CaseStatus.DISMISSED, True)
    assert not can_update_outcome(CaseStatus.CONFIRMED_FRAUD, False)
    assert not can_update_outcome(CaseStatus.DISMISSED, False)


def test_closed_and_simulated_cases_cannot_be_reclassified():
    assert not can_update_outcome(CaseStatus.CLOSED, True)
    assert not can_update_outcome(CaseStatus.CONFIRMED_SIMULATED, True)
    assert can_update_outcome(CaseStatus.ESCALATED, False)
