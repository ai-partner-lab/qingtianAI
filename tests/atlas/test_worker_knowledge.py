import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

from qingtian_engine.knowledge import KnowledgeProviderError
from qingtian_engine.worker_entry import knowledge_prompt


class WorkerKnowledgeTest(TestCase):
    def test_current_task_query_and_safe_event_only(self):
        db, context = Mock(), Mock()
        context.to_prompt.return_value = "untrusted reference data"
        context.summary.return_value = {"authoritative_count": 1}
        with patch("qingtian_engine.worker_entry.configured_task_context", return_value=context) as retrieve:
            text = knowledge_prompt(db, {"id": "new-task", "title": "current title", "scope_summary": "current scope"}, "new-run")
        retrieve.assert_called_once_with("current title current scope")
        self.assertEqual("untrusted reference data", text)
        self.assertEqual("knowledge.retrieved", db.add_event.call_args.args[1])
        self.assertEqual({"authoritative_count": 1}, db.add_event.call_args.args[-1])

    def test_runtime_data_dir_selects_the_local_knowledge_config(self):
        db, context = Mock(), Mock()
        context.to_prompt.return_value = "reference"
        context.summary.return_value = {"authoritative_count": 1}
        with tempfile.TemporaryDirectory() as directory, patch(
            "qingtian_engine.worker_entry.configured_task_context",
            return_value=context,
        ) as retrieve:
            knowledge_prompt(
                db,
                {"id": "new-task", "title": "sample", "scope_summary": "scope"},
                "new-run",
                Path(directory),
            )
        retrieve.assert_called_once_with(
            "sample scope", data_dir=Path(directory)
        )

    def test_failure_is_visible_without_fabricated_context(self):
        db = Mock()
        with patch("qingtian_engine.worker_entry.configured_task_context", side_effect=KnowledgeProviderError("provider-timeout")):
            text = knowledge_prompt(db, {"id": "new-task", "title": "private"}, "new-run")
        self.assertIn("不可用", text)
        self.assertNotIn("private", str(db.add_event.call_args))
        self.assertEqual("knowledge.unavailable", db.add_event.call_args.args[1])

    def test_disabled_does_not_claim_retrieval(self):
        db = Mock()
        with patch("qingtian_engine.worker_entry.configured_task_context", return_value=None):
            self.assertIn("未启用", knowledge_prompt(db, {"id": "new-task"}, "new-run"))
        db.add_event.assert_not_called()
