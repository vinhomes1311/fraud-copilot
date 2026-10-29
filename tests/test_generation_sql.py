"""Exercise production SQL fences using SQLite; no third-party dependencies.

This checks conditional-update behavior, not PostgreSQL/Redis connectivity.
Only PostgreSQL placeholders and now() are translated for SQLite.
"""
import ast
from dataclasses import replace
from pathlib import Path
import re
import sqlite3
import unittest

from fraud_common.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def sql_from(relative_path, prefix):
    tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
    return next(node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
                and node.value.startswith(prefix))


class GenerationSQLTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE case_briefs (
            case_id INTEGER PRIMARY KEY, generation_id TEXT, status TEXT,
            attempts INTEGER DEFAULT 0, brief TEXT, citations TEXT, model_name TEXT,
            prompt_version TEXT, tokens_used INTEGER, last_error TEXT, updated_at TEXT)""")
        self.enqueue_sql = sql_from("analyst_api/events.py", "INSERT INTO case_briefs")
        self.start_sql = sql_from("llm_worker/processor.py", "UPDATE case_briefs SET status=$2, attempts")
        self.ready_sql = sql_from("llm_worker/processor.py", "UPDATE case_briefs SET status='READY'")
        self.dead_sql = sql_from("llm_worker/processor.py", "UPDATE case_briefs SET status=$2, brief")
        self.execute(self.enqueue_sql, 1, "old")

    def tearDown(self):
        self.db.close()

    def execute(self, sql, *args):
        sql = re.sub(r"\$(\d+)", r"?\1", sql).replace("now()", "CURRENT_TIMESTAMP")
        return self.db.execute(sql, args).rowcount

    def row(self):
        return dict(self.db.execute("SELECT * FROM case_briefs WHERE case_id=1").fetchone())

    def ready(self, generation="old", attempt=1):
        return self.execute(self.ready_sql, 1, '{"summary":"ok"}', '[]', attempt,
                            "model", "v1", 120, generation)

    def dead(self, generation="old", attempt=3):
        return self.execute(self.dead_sql, 1, "FAILED_TIMEOUT", '{"source":"rule_based"}',
                            '[]', attempt, "TimeoutError()", generation)

    def test_pending_running_ready_and_duplicate_delivery(self):
        self.assertEqual(self.execute(self.start_sql, 1, "RUNNING", 1, "old"), 1)
        self.assertEqual(self.ready(), 1)
        before = self.row()
        self.assertEqual(self.execute(self.start_sql, 1, "RUNNING", 1, "old"), 0)
        self.assertEqual(self.dead(), 0)
        self.assertEqual(self.ready(), 0)
        self.assertEqual(self.row(), before)

    def test_regenerate_during_generation_fences_success_and_failure(self):
        self.execute(self.start_sql, 1, "RUNNING", 1, "old")
        self.execute(self.enqueue_sql, 1, "new")
        before = self.row()
        self.assertEqual(self.ready(), 0)
        self.assertEqual(self.dead(), 0)
        self.assertEqual(self.execute(self.start_sql, 1, "RUNNING", 2, "old"), 0)
        self.assertEqual(self.row(), before)
        self.assertEqual(self.execute(self.start_sql, 1, "RUNNING", 1, "new"), 1)
        self.assertEqual(self.ready("new"), 1)

    def test_older_attempt_cannot_finish_after_newer_attempt_starts(self):
        self.execute(self.start_sql, 1, "RUNNING", 1, "old")
        self.execute(self.start_sql, 1, "RUNNING", 2, "old")
        self.assertEqual(self.ready(attempt=1), 0)
        self.assertEqual(self.dead(attempt=1), 0)
        self.assertEqual(self.ready(attempt=2), 1)

    def test_new_generation_clears_previous_content_and_usage(self):
        self.execute(self.start_sql, 1, "RUNNING", 1, "old")
        self.ready()
        self.execute(self.enqueue_sql, 1, "new")
        row = self.row()
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["status"], "PENDING")
        for key in ("brief", "citations", "model_name", "prompt_version", "tokens_used", "last_error"):
            self.assertIsNone(row[key])

    def test_dead_letter_is_terminal(self):
        self.execute(self.start_sql, 1, "RUNNING", 3, "old")
        self.assertEqual(self.dead(), 1)
        self.assertEqual(self.row()["status"], "FAILED_TIMEOUT")
        self.assertEqual(self.ready(attempt=3), 0)
        self.assertEqual(self.execute(self.start_sql, 1, "RUNNING", 4, "old"), 0)

    def test_legacy_pending_row_can_be_claimed_once(self):
        self.db.execute("UPDATE case_briefs SET generation_id=NULL")
        self.assertEqual(self.execute(self.start_sql, 1, "RUNNING", 1, "legacy"), 1)
        self.assertEqual(self.execute(self.start_sql, 1, "RUNNING", 1, "other"), 0)

    def test_unknown_case_is_not_created_by_worker(self):
        self.assertEqual(self.execute(self.start_sql, 999, "RUNNING", 1, "old"), 0)


class TimeoutConfigurationTests(unittest.TestCase):
    def test_valid_stage_deadlines(self):
        replace(Settings(), llm_job_timeout_s=30, llm_visibility_timeout_s=120).validate_llm_timeouts()

    def test_invalid_stage_deadlines(self):
        for stage, visibility in [(30, 60), (30, 70), (0, 120), (-1, 120)]:
            with self.subTest(stage=stage, visibility=visibility), self.assertRaises(ValueError):
                replace(Settings(), llm_job_timeout_s=stage,
                        llm_visibility_timeout_s=visibility).validate_llm_timeouts()


if __name__ == "__main__":
    unittest.main()
