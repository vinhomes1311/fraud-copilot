"""Synthetic protocol-compliant text for offline transport tests, never live output."""


def explanatory_selection(ids):
    return {'checklist_ids': ids, 'rationales': [
        {'rule_code': rule, 'evidence_ids': ['EV-AMOUNT'], 'text': (
            'The supplied transaction amount establishes the size of the purchase being reviewed. '
            'It provides a concrete reference for interpreting the other recorded model signals, '
            'while the selected playbook rule guides how those observations are considered together.')}
        for rule in ids]}
