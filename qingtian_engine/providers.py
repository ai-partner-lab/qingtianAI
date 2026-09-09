"""Vendor-neutral contracts for optional external adapters.

The public engine does not silently connect to a hosted knowledge or workflow
service. An adopter must provide and configure a concrete implementation; the
unconfigured adapters below expose that boundary without pretending a network
call succeeded.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Any, Dict, List


class AdapterUnavailableError(RuntimeError):
    """Stable failure raised when an optional adapter is not configured."""

    code = "adapter-not-configured"


@dataclass(frozen=True)
class AdapterStatus:
    name: str
    kind: str
    availability: str = "adapter-required"
    runnable: bool = False
    boundary: str = (
        "No external service is called until an adopter supplies and enables "
        "a concrete adapter."
    )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class KnowledgeSearchAdapter(ABC):
    """Optional derived knowledge source, never the engine fact source."""

    @abstractmethod
    def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        raise NotImplementedError


class WorkflowAdapter(ABC):
    """Optional non-code workflow integration."""

    @abstractmethod
    def invoke(self, workflow: str, inputs: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError


class UnconfiguredKnowledgeAdapter(KnowledgeSearchAdapter):
    status = AdapterStatus(name="knowledge", kind="knowledge-search")

    def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        del query, limit
        raise AdapterUnavailableError(
            "knowledge adapter is not configured; no external request was made"
        )


class UnconfiguredWorkflowAdapter(WorkflowAdapter):
    status = AdapterStatus(name="workflow", kind="workflow")

    def invoke(self, workflow: str, inputs: Dict[str, Any]) -> Dict[str, Any]:
        del workflow, inputs
        raise AdapterUnavailableError(
            "workflow adapter is not configured; no external request was made"
        )
