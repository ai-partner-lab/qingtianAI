from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from qingtian_engine.db import Database
from qingtian_engine.intake import IntakeService
from qingtian_engine.runner import RunManager
from qingtian_engine.server import ControlPlaneHandler, LoopbackThreadingHTTPServer
from qingtian_engine.service import ControlPlane


PNG = b"\x89PNG\r\n\x1a\n" + b"http-smoke" * 4


def multipart(
    fields: dict[str, str],
    filename: str,
    mime: str,
    content: bytes,
) -> tuple[str, bytes]:
    boundary = "----atlas-local-test-boundary"
    body = bytearray()
    for name, value in fields.items():
        body.extend(("--{}\r\n".format(boundary)).encode())
        body.extend(
            (
                'Content-Disposition: form-data; name="{}"\r\n\r\n{}\r\n'.format(
                    name, value
                )
            ).encode()
        )
    body.extend(("--{}\r\n".format(boundary)).encode())
    body.extend(
        (
            'Content-Disposition: form-data; name="attachments"; filename="{}"\r\n'
            "Content-Type: {}\r\n\r\n".format(filename, mime)
        ).encode()
    )
    body.extend(content)
    body.extend(("\r\n--{}--\r\n".format(boundary)).encode())
    return boundary, bytes(body)


class IntakeHTTPTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        spawn_guard = patch("subprocess.Popen", side_effect=AssertionError("real subprocess forbidden in HTTP fixture"))
        spawn_guard.start()
        self.addCleanup(spawn_guard.stop)
        service = ControlPlane(Database(root / "control.sqlite3"))
        self.service = service
        ControlPlaneHandler.service = service
        ControlPlaneHandler.manager = RunManager(service, root)
        ControlPlaneHandler.intakes = IntakeService(service, root)
        ControlPlaneHandler.watchdog_health = {
            "last_success_at": "2026-07-27T00:00:00+00:00",
            "_last_success_monotonic": time.monotonic(),
            "consecutive_errors": 0,
            "last_error_type": "",
        }
        self.server = LoopbackThreadingHTTPServer(("127.0.0.1", 0), ControlPlaneHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method: str, path: str, body: bytes = b"", headers=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=3
        )
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        result = (response.status, dict(response.getheaders()), data)
        connection.close()
        return result

    def test_multipart_create_query_and_attachment_read(self) -> None:
        boundary, body = multipart(
            {
                "text": "修复 H5 页面交互",
                "intent": "analyze",
                "idempotency_key": "http-key",
                "advanced": "{}",
            },
            "../../screen.png",
            "image/png",
            PNG,
        )
        status, _headers, raw = self.request(
            "POST",
            "/api/intakes",
            body,
            {
                "Content-Type": "multipart/form-data; boundary={}".format(boundary),
                "Content-Length": str(len(body)),
            },
        )
        self.assertEqual(201, status)
        created = json.loads(raw)
        self.assertEqual("ROUTED", created["status"])
        self.assertEqual("screen.png", created["attachments"][0]["name"])

        status, _headers, raw = self.request("GET", "/api/intakes?limit=5")
        self.assertEqual(200, status)
        self.assertEqual(created["id"], json.loads(raw)["intakes"][0]["id"])

        attachment = created["attachments"][0]
        status, headers, raw = self.request("GET", attachment["url"])
        self.assertEqual(200, status)
        self.assertEqual(PNG, raw)
        self.assertEqual("image/png", headers["Content-Type"])
        self.assertEqual("nosniff", headers["X-Content-Type-Options"])

        status, _headers, raw = self.request(
            "POST",
            "/api/intakes",
            body,
            {
                "Content-Type": "multipart/form-data; boundary={}".format(boundary),
                "Content-Length": str(len(body)),
            },
        )
        self.assertEqual(200, status)
        self.assertTrue(json.loads(raw)["reused"])

    def test_cross_origin_mutation_is_rejected(self) -> None:
        boundary, body = multipart(
            {
                "text": "分析",
                "intent": "analyze",
                "idempotency_key": "origin",
                "advanced": "{}",
            },
            "screen.png",
            "image/png",
            PNG,
        )
        status, _headers, raw = self.request(
            "POST",
            "/api/intakes",
            body,
            {
                "Content-Type": "multipart/form-data; boundary={}".format(boundary),
                "Content-Length": str(len(body)),
                "Origin": "https://evil.example",
            },
        )
        self.assertEqual(403, status)
        self.assertIn("cross-origin", json.loads(raw)["error"])

    def test_existing_health_dashboard_report_and_task_api_stay_compatible(self) -> None:
        for path in ("/api/health", "/api/dashboard", "/api/report"):
            status, headers, raw = self.request("GET", path)
            self.assertEqual(200, status, path)
            self.assertIn("application/json", headers["Content-Type"])
            self.assertTrue(json.loads(raw))
        payload = json.dumps(
            {
                "title": "旧 API 兼容性",
                "scope_summary": "仍可从结构化调用创建",
                "priority": 2,
                "environment": "local",
                "worker_type": "auto",
            },
            ensure_ascii=False,
        ).encode()
        status, _headers, raw = self.request(
            "POST",
            "/api/tasks",
            payload,
            {
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
        )
        self.assertEqual(201, status)
        task = json.loads(raw)
        status, _headers, raw = self.request("GET", "/api/tasks/{}".format(task["id"]))
        self.assertEqual(200, status)
        self.assertEqual(task["id"], json.loads(raw)["id"])

    def test_sse_stream_starts_with_versioned_snapshot(self) -> None:
        self.service.create_task("实时状态", idempotency_key="sse")
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=3
        )
        connection.request("GET", "/api/events/stream?lastEventId=0")
        response = connection.getresponse()
        self.assertEqual(200, response.status)
        self.assertIn("text/event-stream", response.getheader("Content-Type"))
        lines = [response.readline().decode("utf-8").strip() for _ in range(6)]
        connection.close()
        self.assertEqual("retry: 3000", lines[0])
        self.assertTrue(any(line.startswith("id: ") for line in lines))
        self.assertIn("event: snapshot", lines)
        data_line = next(line for line in lines if line.startswith("data: "))
        payload = json.loads(data_line[6:])
        self.assertEqual(payload["version"], payload["dashboard"]["version"])
        self.assertGreater(payload["version"], 0)

    def test_sse_rejects_invalid_cursor(self) -> None:
        status, _headers, raw = self.request(
            "GET", "/api/events/stream?lastEventId=not-a-number"
        )
        self.assertEqual(400, status)
        self.assertIn("cursor", json.loads(raw)["error"])

    def test_local_api_registers_direct_delegated_execution(self) -> None:
        task = self.service.create_task(
            "API 直接委派", idempotency_key="api-direct-delegated"
        )
        body = json.dumps({"mode": "delegated"}).encode()
        status, _headers, raw = self.request(
            "POST",
            "/api/tasks/{}/heartbeat".format(task["id"]),
            body,
            {
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
        )
        payload = json.loads(raw)
        self.assertEqual(200, status)
        self.assertEqual("RUNNING", payload["state"])
        self.assertEqual("delegated", payload["execution_mode"])
        self.assertEqual([], payload["runs"])

    def test_health_exposes_watchdog_status(self) -> None:
        ControlPlaneHandler.watchdog_health = {
            "last_success_at": "2026-07-27T00:00:00+00:00",
            "_last_success_monotonic": time.monotonic(),
            "consecutive_errors": 0,
            "last_error_type": "",
        }
        status, _headers, raw = self.request("GET", "/api/health")
        payload = json.loads(raw)
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["watchdog"]["ok"])
        self.assertNotIn("_last_success_monotonic", payload["watchdog"])

    def test_health_fails_when_watchdog_is_stale(self) -> None:
        ControlPlaneHandler.watchdog_health = {
            "last_success_at": "2026-07-27T00:00:00+00:00",
            "_last_success_monotonic": time.monotonic() - 60,
            "consecutive_errors": 0,
            "last_error_type": "",
        }
        status, _headers, raw = self.request("GET", "/api/health")
        payload = json.loads(raw)
        self.assertEqual(503, status)
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["watchdog"]["ok"])

    def test_human_action_completion_and_external_reminder(self) -> None:
        user = self.service.create_task(
            "OAuth 人工配置", idempotency_key="http-human-user"
        )
        self.service.transition(user["id"], "WAITING", force=True)
        self.service.set_human_action(
            user["id"], "user", owner="你", text="新增 redirect URI"
        )
        body = json.dumps({"expected_action_version": self.service.get_task(user["id"])["action_version"]}).encode()
        status, _headers, raw = self.request(
            "POST",
            "/api/tasks/{}/complete-human-action".format(user["id"]),
            body,
            {
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
        )
        self.assertEqual(200, status)
        completed = json.loads(raw)
        self.assertEqual("VERIFYING", completed["state"])
        self.assertEqual("none", completed["action_owner_kind"])

        external = self.service.create_task(
            "渠道回调", idempotency_key="http-human-external"
        )
        self.service.set_human_action(
            external["id"],
            "external",
            owner="支付渠道",
            text="重发 state=2",
        )
        status, _headers, raw = self.request(
            "POST",
            "/api/tasks/{}/remind-external".format(external["id"]),
            body,
            {
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
        )
        self.assertEqual(200, status)
        self.assertEqual("external", json.loads(raw)["action_owner_kind"])
        reminder = self.service.db.one(
            """
            SELECT summary FROM events
            WHERE task_id=? AND event_type='task.external_reminder_recorded'
            """,
            (external["id"],),
        )
        self.assertIn("支付渠道", reminder["summary"])


if __name__ == "__main__":
    unittest.main()
