from __future__ import annotations

from dataclasses import asdict
from time import monotonic
from typing import Any, Protocol

from .models import ProviderAttempt, ProviderResponse, ProviderResult, require_strict_json


class ModelProvider(Protocol):
    name: str

    def generate(
        self, *, model: str, messages: list[dict[str, str]], options: dict[str, Any]
    ) -> ProviderResponse:
        ...


class EchoProvider:
    """Deterministic offline provider used only by the synthetic demo."""

    name = "offline-echo"

    def generate(
        self, *, model: str, messages: list[dict[str, str]], options: dict[str, Any]
    ) -> ProviderResponse:
        del options
        latest = next((item["content"] for item in reversed(messages) if item["role"] == "user"), "")
        return ProviderResponse(
            output={"text": latest, "provider": self.name, "model": model},
            reported_model=model,
        )


class ModelGateway:
    """Small provider boundary with explicit requested/effective model lineage.

    Real credentials, retries, pricing, content policy, and provider-specific
    idempotency belong in project adapters. The core never silently changes a
    product-approved content profile to make an integration pass.
    """

    def __init__(self) -> None:
        self._providers: dict[str, ModelProvider] = {}

    def register(self, provider: ModelProvider) -> None:
        if not isinstance(provider.name, str) or not provider.name.strip():
            raise ValueError("provider name must be a non-empty string")
        if provider.name in self._providers:
            raise ValueError(f"provider already registered: {provider.name}")
        self._providers[provider.name] = provider

    def generate(
        self,
        *,
        route: str,
        provider_name: str,
        model: str,
        messages: list[dict[str, str]],
        options: dict[str, Any] | None = None,
    ) -> ProviderResult:
        for label, value in (
            ("route", route),
            ("provider_name", provider_name),
            ("model", model),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} must be a non-empty string")
        if not isinstance(messages, list) or not messages:
            raise ValueError("messages must be a non-empty array")
        for index, message in enumerate(messages):
            if not isinstance(message, dict) or set(message) != {"role", "content"}:
                raise ValueError(
                    f"messages[{index}] must contain exactly role and content"
                )
            if not isinstance(message["role"], str) or not message["role"].strip():
                raise ValueError(f"messages[{index}].role must be a non-empty string")
            if not isinstance(message["content"], str):
                raise ValueError(f"messages[{index}].content must be a string")
        if options is not None and not isinstance(options, dict):
            raise ValueError("options must be an object when provided")
        require_strict_json(messages, path="$.messages")
        require_strict_json(options or {}, path="$.options")
        if provider_name not in self._providers:
            raise KeyError(f"provider is not registered: {provider_name}")
        provider = self._providers[provider_name]
        started = monotonic()
        response = provider.generate(model=model, messages=messages, options=options or {})
        latency_ms = max(0, round((monotonic() - started) * 1000))
        if not isinstance(response, ProviderResponse):
            raise ValueError("provider must return a ProviderResponse")
        if response.reported_model is not None and (
            not isinstance(response.reported_model, str)
            or not response.reported_model.strip()
        ):
            raise ValueError("provider reported_model must be a non-empty string or None")
        require_strict_json(response.output, path="$.provider_response.output")
        attempt = ProviderAttempt(
            provider=provider.name,
            requested_model=model,
            reported_model=response.reported_model,
            status="succeeded",
            latency_ms=latency_ms,
        )
        return ProviderResult(
            requested_model=model,
            resolved_route=route,
            effective_model=response.reported_model,
            output=response.output,
            attempts=(attempt,),
        )

    @staticmethod
    def as_dict(result: ProviderResult) -> dict[str, Any]:
        return asdict(result)
