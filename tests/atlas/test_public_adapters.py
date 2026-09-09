from __future__ import annotations

import unittest

from qingtian_engine.providers import (
    AdapterUnavailableError,
    UnconfiguredKnowledgeAdapter,
    UnconfiguredWorkflowAdapter,
)


class PublicAdapterBoundaryTest(unittest.TestCase):
    def test_unconfigured_knowledge_adapter_is_explicit_and_offline(self) -> None:
        adapter = UnconfiguredKnowledgeAdapter()
        self.assertEqual("adapter-required", adapter.status.availability)
        self.assertFalse(adapter.status.runnable)
        self.assertIn("No external service", adapter.status.boundary)
        with self.assertRaises(AdapterUnavailableError) as raised:
            adapter.search("sample")
        self.assertEqual("adapter-not-configured", raised.exception.code)
        self.assertIn("no external request", str(raised.exception))

    def test_unconfigured_workflow_adapter_raises_stable_error(self) -> None:
        adapter = UnconfiguredWorkflowAdapter()
        with self.assertRaises(AdapterUnavailableError) as raised:
            adapter.invoke("sample", {"input": "value"})
        self.assertEqual("adapter-not-configured", raised.exception.code)
        self.assertIn("no external request", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
