"""Synthetic capability fixture for protocol/lifecycle unit tests only.

Production host checking is exercised unmocked by test_evolution_codex_capabilities;
this fixture does not install evidence or execute a real CLI/model.
"""
from unittest.mock import patch
from qingtian_engine.config import EXECUTION_EFFORTS


def advertised_capabilities():
    return patch("qingtian_engine.execution_parameters.load_codex_capabilities", return_value={
        "adapter": "codex-cli", "cli_path": "/synthetic/codex",
        "models": {model: {"reasoning": sorted(EXECUTION_EFFORTS), "speed": ["standard", "fast"]}
                   for model in ("gpt-5.6-sol", "gpt-6-astra")}})
