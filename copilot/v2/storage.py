"""Provisional repository in isolated copilot_dev schema; parameterized SQL.

Configure DATABASE_URL to PgBouncer 2 / primary. No replica reads for new tasks.
No open DB transaction is held while the LLM runs.
"""
import json
import os
from uuid import uuid4
from .contracts import validate

MAX_ATTEMPTS = 3
LEASE_SECONDS = 300


def connect():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row,
                           connect_timeout=5, prepare_threshold=None)


def register(conn, case, regenerate=False):
    """Must run in same transaction as case creation; caller commits.

    Development import helper, not a scoring endpoint. Normal replay is idempotent.
    Regenerate intentionally creates a new generation and immutable snapshot.
    """
    validate(case)
    if case["scoring"]["routing_decision"] == "LOW_RISK":
        raise ValueError("Low risk does not create normal review case")
    cid, txid = case["case_id"], case["transaction_id"]
    priority = "high" if case["scoring"]["routing_decision"] in ("HIGH_RISK", "UNSCORED_FALLBACK") else "medium"
    conn.execute("INSERT INTO copilot_dev.transactions_scoring VALUES (%s,%s::jsonb) ON CONFLICT DO NOTHING", (txid, json.dumps(case)))
    conn.execute("INSERT INTO copilot_dev.investigation_cases (case_id,transaction_id,priority_rank) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", (cid, txid, priority))
    row = conn.execute("SELECT * FROM copilot_dev.investigation_cases WHERE case_id=%s FOR UPDATE", (cid,)).fetchone()
    if row["transaction_id"] != txid:
        raise ValueError("Case transaction mismatch")
    generation = row["brief_generation"]
    old = conn.execute("SELECT snapshot FROM copilot_dev.brief_tasks WHERE case_id=%s AND generation=%s", (cid, generation)).fetchone()
    if old and not regenerate:
        if old["snapshot"] != case:
            raise ValueError("Existing case changed; use explicit regenerate")
        return generation
    if regenerate:
        generation += 1
        conn.execute("UPDATE copilot_dev.investigation_cases SET brief_generation=%s,brief_status='PENDING',brief_error_code=NULL,case_brief_content=NULL,playbook_references=NULL,brief_trace=NULL,priority_rank=%s WHERE case_id=%s", (generation, priority, cid))
    conn.execute("INSERT INTO copilot_dev.brief_tasks (case_id,generation,transaction_id,priority,snapshot) VALUES (%s,%s,%s,%s,%s::jsonb)", (cid, generation, txid, priority, json.dumps(case)))
    return generation


class Repository:
    def claim(self, job):
        with connect() as conn:
            # Serialize claim/regeneration/completion in the same lock order.
            row = conn.execute("SELECT brief_generation FROM copilot_dev.investigation_cases WHERE case_id=%s FOR UPDATE", (job["case_id"],)).fetchone()
            if not row or row["brief_generation"] != job["generation"]:
                return None
            token = str(uuid4())
            task = conn.execute("""UPDATE copilot_dev.brief_tasks SET state='RUNNING',attempts=attempts+1,
                 owner_token=%s,lease_until=now()+(%s * interval '1 second')
                 WHERE case_id=%s AND generation=%s AND transaction_id=%s
                   AND attempts < %s AND ((state='QUEUED' AND not_before <= now()) OR (state='RUNNING' AND lease_until < now()))
                 RETURNING snapshot,attempts""", (token, LEASE_SECONDS, job["case_id"], job["generation"], job["transaction_id"], MAX_ATTEMPTS)).fetchone()
            if not task:
                return None
            conn.execute("UPDATE copilot_dev.investigation_cases SET brief_status='PENDING',brief_error_code=NULL WHERE case_id=%s", (job["case_id"],))
            return {**task, "token": token}

    def finish(self, job, claim, result):
        with connect() as conn:
            row = conn.execute("SELECT brief_generation FROM copilot_dev.investigation_cases WHERE case_id=%s FOR UPDATE", (job["case_id"],)).fetchone()
            if not row or row["brief_generation"] != job["generation"]:
                return False
            task = conn.execute("SELECT * FROM copilot_dev.brief_tasks WHERE case_id=%s AND generation=%s FOR UPDATE", (job["case_id"],job["generation"])).fetchone()
            if not task or str(task["owner_token"]) != claim["token"] or task["state"] != "RUNNING":
                return False
            retryable = result["error_code"] in ("LLM_TIMEOUT","LLM_ERROR","RETRIEVAL_TIMEOUT","RETRIEVAL_ERROR","WORKER_ERROR")
            retry = result["brief_status"] == "FAILED" and retryable and task["attempts"] < MAX_ATTEMPTS
            status = "PENDING" if retry else result["brief_status"]
            conn.execute("""UPDATE copilot_dev.investigation_cases SET brief_status=%s,brief_error_code=%s,
                case_brief_content=%s::jsonb,playbook_references=%s::jsonb,brief_trace=%s::jsonb WHERE case_id=%s""",
                (status,result["error_code"],json.dumps(result["case_brief_content"]),json.dumps(result["playbook_references"]),json.dumps(result["trace"]),job["case_id"]))
            conn.execute("""UPDATE copilot_dev.brief_tasks SET state=%s,owner_token=NULL,lease_until=NULL,
                next_dispatch=now()+(%s * interval '1 second'),not_before=now()+(%s * interval '1 second') WHERE case_id=%s AND generation=%s""",
                ("QUEUED" if retry else "DONE", 5 * 2 ** task["attempts"],5 * 2 ** task["attempts"],job["case_id"],job["generation"]))
            if not retry:
                conn.execute("""INSERT INTO copilot_dev.case_audit_logs (case_id,generation,decision_payload)
                    VALUES (%s,%s,%s::jsonb) ON CONFLICT DO NOTHING""",
                    (job["case_id"],job["generation"],json.dumps({"brief_status":status,"error_code":result["error_code"]})))
            return True


def dispatch_once(redis):
    """Periodic re-publication tolerates Redis loss, duplicate messages and worker death."""
    with connect() as conn:
        # Reap killed workers after their final permitted attempt.
        exhausted = conn.execute("""SELECT t.case_id,t.generation,t.owner_token FROM copilot_dev.brief_tasks t
            JOIN copilot_dev.investigation_cases c USING(case_id)
            WHERE t.generation=c.brief_generation AND t.state='RUNNING' AND t.lease_until < now() AND t.attempts >= %s""", (MAX_ATTEMPTS,)).fetchall()
    for task in exhausted:
        Repository().finish({"case_id":str(task["case_id"]),"generation":task["generation"]},
            {"token":str(task["owner_token"])}, failed_result("WORKER_LEASE_EXPIRED"))
    with connect() as conn:
        rows = conn.execute("""SELECT t.case_id,t.generation,t.transaction_id,t.priority
            FROM copilot_dev.brief_tasks t JOIN copilot_dev.investigation_cases c USING(case_id)
            WHERE t.generation=c.brief_generation AND t.attempts < %s
              AND t.next_dispatch <= now()
              AND (t.state='QUEUED' OR (t.state='RUNNING' AND t.lease_until < now()))
            ORDER BY t.priority,t.next_dispatch LIMIT 50 FOR UPDATE OF t SKIP LOCKED""", (MAX_ATTEMPTS,)).fetchall()
        for row in rows:
            job = dict(row, case_id=str(row["case_id"]))
            redis.rpush("copilot:" + row["priority"], json.dumps(job))
            conn.execute("UPDATE copilot_dev.brief_tasks SET next_dispatch=now()+interval '60 seconds' WHERE case_id=%s AND generation=%s", (row["case_id"],row["generation"]))
    return len(rows)


def failed_result(code):
    return {"brief_status":"FAILED","error_code":code,"case_brief_content":{},"playbook_references":[],"trace":{}}
