import json
from ..contracts import ContractError, fields, text


def load(path):
    with open(path, encoding="utf-8") as f:
        kb = json.load(f)
    fields(kb, ("document_id", "version", "source_status", "source_note", "chunks"))
    for name in ("document_id", "version", "source_status", "source_note"):
        text(kb[name], 1000)
    if not isinstance(kb["chunks"], list) or not kb["chunks"]:
        raise ContractError("Empty knowledge base")
    ids = set()
    for chunk in kb["chunks"]:
        fields(chunk, ("id", "section_id", "rule_code", "text", "implementation_action", "action_origin"))
        for value in chunk.values():
            text(value, 2000)
        if chunk["id"] in ids or chunk["action_origin"] != "team_proposal":
            raise ContractError("Invalid playbook metadata")
        ids.add(chunk["id"])
    return kb
