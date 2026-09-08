from __future__ import annotations

import unittest

from qingtian_core.models import ProviderResponse
from qingtian_core.providers import EchoProvider, ModelGateway


class ExplicitProvider:
    name = "explicit"

    def __init__(self, response: object) -> None:
        self.response = response

    def generate(self, **_: object) -> object:
        return self.response


class ProviderTest(unittest.TestCase):
    def test_gateway_reports_requested_and_effective_model(self) -> None:
        gateway = ModelGateway()
        gateway.register(EchoProvider())
        result = gateway.generate(
            route="test.chat",
            provider_name="offline-echo",
            model="echo-v1",
            messages=[{"role": "user", "content": "hello"}],
        )
        self.assertEqual(result.requested_model, "echo-v1")
        self.assertEqual(result.effective_model, "echo-v1")
        self.assertEqual(result.attempts[0].reported_model, "echo-v1")
        self.assertEqual(result.output["text"], "hello")
        self.assertEqual(result.attempts[0].provider, "offline-echo")

    def test_provider_report_controls_effective_model_lineage(self) -> None:
        gateway = ModelGateway()
        gateway.register(
            ExplicitProvider(
                ProviderResponse(
                    output={"text": "result"},
                    reported_model="actual-model-v2",
                )
            )
        )
        result = gateway.generate(
            route="test.chat",
            provider_name="explicit",
            model="requested-model-v1",
            messages=[{"role": "user", "content": "hello"}],
        )
        self.assertEqual(result.requested_model, "requested-model-v1")
        self.assertEqual(result.effective_model, "actual-model-v2")
        self.assertEqual(result.attempts[0].requested_model, "requested-model-v1")
        self.assertEqual(result.attempts[0].reported_model, "actual-model-v2")

    def test_unknown_effective_model_remains_none(self) -> None:
        gateway = ModelGateway()
        gateway.register(
            ExplicitProvider(
                ProviderResponse(output={"text": "result"}, reported_model=None)
            )
        )
        result = gateway.generate(
            route="test.chat",
            provider_name="explicit",
            model="requested-model-v1",
            messages=[{"role": "user", "content": "hello"}],
        )
        self.assertIsNone(result.effective_model)
        self.assertIsNone(result.attempts[0].reported_model)

    def test_legacy_or_malformed_provider_responses_fail_closed(self) -> None:
        for response, message in (
            ({"text": "legacy"}, "ProviderResponse"),
            (ProviderResponse(output={"text": "x"}, reported_model="  "), "reported_model"),
            (ProviderResponse(output={"bad": object()}, reported_model=None), "non-JSON"),
        ):
            with self.subTest(response=response):
                gateway = ModelGateway()
                gateway.register(ExplicitProvider(response))
                with self.assertRaisesRegex((TypeError, ValueError), message):
                    gateway.generate(
                        route="test.chat",
                        provider_name="explicit",
                        model="requested-model-v1",
                        messages=[{"role": "user", "content": "hello"}],
                    )

    def test_gateway_validates_request_contract(self) -> None:
        gateway = ModelGateway()
        gateway.register(EchoProvider())
        valid = {
            "route": "test.chat",
            "provider_name": "offline-echo",
            "model": "echo-v1",
            "messages": [{"role": "user", "content": "hello"}],
        }
        invalid_overrides = (
            {"route": ""},
            {"provider_name": ""},
            {"model": ""},
            {"messages": []},
            {"messages": [{"role": "user", "content": "x", "extra": True}]},
            {"messages": [{"role": "", "content": "x"}]},
            {"messages": [{"role": "user", "content": 1}]},
            {"options": []},
        )
        for override in invalid_overrides:
            with self.subTest(override=override):
                with self.assertRaises((TypeError, ValueError)):
                    gateway.generate(**(valid | override))

    def test_duplicate_provider_is_rejected(self) -> None:
        gateway = ModelGateway()
        gateway.register(EchoProvider())
        with self.assertRaises(ValueError):
            gateway.register(EchoProvider())


if __name__ == "__main__":
    unittest.main()
