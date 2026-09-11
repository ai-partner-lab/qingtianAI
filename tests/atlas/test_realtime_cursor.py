from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from qingtian_engine.db import Database
from qingtian_engine.server import ControlPlaneHandler, LoopbackThreadingHTTPServer
from qingtian_engine.service import ControlPlane


class RealtimeCursorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qingtian-sse-")
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / "control.sqlite3")
        self.service = ControlPlane(self.db)
        self.task = self.service.create_task("Synthetic realtime cursor acceptance")

    def add_events(self, start, count):
        for index in range(start, start + count):
            self.db.add_event(self.task["id"], "cursor.test", "test", str(index), "cursor-test:" + str(index))

    def test_backlog_is_delivered_in_order_without_skipping(self):
        self.add_events(0, 307)
        expected = [row["id"] for row in self.db.all("SELECT id FROM events ORDER BY id")]
        cursor, received = 0, []
        for _ in range(10):
            payload = self.service.realtime_payload(after=cursor, limit=31)
            page = [row["id"] for row in payload["changes"]]
            received.extend(page)
            self.assertEqual(page[-1] if page else cursor, payload.get("cursor", payload["version"]))
            self.assertEqual(payload["version"], payload["dashboard"]["version"])
            cursor = payload["cursor"]
            if not payload["has_more"]:
                break
        self.assertEqual(expected, received)
        self.assertEqual(len(received), len(set(received)))

    def test_concurrent_insert_is_not_claimed_by_older_snapshot(self):
        original = self.service.dashboard_payload
        inserted = []

        def insert_before_dashboard(*args, **kwargs):
            # Separate thread / connection models a concurrent HTTP writer.
            thread = threading.Thread(target=lambda: (self.add_events(42, 1), inserted.append(True)))
            thread.start()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            return original(*args, **kwargs)

        before = self.service.event_cursor()
        with patch.object(self.service, "dashboard_payload", side_effect=insert_before_dashboard):
            payload = self.service.realtime_payload(after=before)
        self.assertTrue(inserted)
        self.assertEqual(before, payload["version"])
        self.assertEqual(before, payload["cursor"])
        next_page = self.service.realtime_payload(after=payload["cursor"])
        self.assertEqual(["42"], [row["summary"] for row in next_page["changes"]])

    def test_duplicate_events_replay_idle_and_reset(self):
        self.add_events(0, 3)
        self.add_events(0, 3)
        first = self.service.realtime_payload(after=0, limit=2)
        replay = self.service.realtime_payload(after=0, limit=2)
        self.assertEqual(first["changes"], replay["changes"])
        self.assertEqual(first["cursor"], replay["cursor"])
        tail = self.service.event_cursor()
        idle = self.service.realtime_payload(after=tail)
        self.assertEqual(tail, idle["cursor"])
        self.assertEqual([], idle["changes"])
        self.assertFalse(idle["has_more"])
        reset = self.service.realtime_payload(after=tail + 100, limit=2)
        self.assertTrue(reset["reset"])
        self.assertEqual(first["changes"], reset["changes"])
        self.assertEqual(first["cursor"], reset["cursor"])

    def test_snapshot_scope_cleans_up_after_exception(self):
        before = self.service.event_cursor()
        with self.assertRaisesRegex(RuntimeError, "synthetic"):
            with self.db.read_snapshot():
                with self.db.read_snapshot():
                    self.assertEqual(before, self.service.event_cursor())
                    raise RuntimeError("synthetic snapshot failure")
        self.add_events(71, 1)
        self.assertGreater(self.service.event_cursor(), before)

    def test_event_id_gaps_do_not_create_missing_pages(self):
        self.add_events(0, 1)
        self.add_events(0, 1)  # INSERT OR IGNORE can consume an AUTOINCREMENT ID.
        self.add_events(1, 1)
        expected = [row["id"] for row in self.db.all("SELECT id FROM events ORDER BY id")]
        received, cursor = [], 0
        while True:
            payload = self.service.realtime_payload(after=cursor, limit=1)
            received.extend(row["id"] for row in payload["changes"])
            cursor = payload["cursor"]
            if not payload["has_more"]:
                break
        self.assertEqual(expected, received)

    def start_server(self):
        handler = type("IsolatedRealtimeHandler", (ControlPlaneHandler,), {"service": self.service, "coordinator": None})
        server = LoopbackThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.addCleanup(close)
        return server.server_address[1]

    def open_stream(self, port, cursor=0, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", "/api/events/stream?lastEventId=" + str(cursor), headers=headers or {})
        response = connection.getresponse()
        self.assertEqual(200, response.status)
        self.addCleanup(connection.close)
        self.addCleanup(response.close)
        return response

    def read_frame(self, response):
        frame = {}
        while True:
            line = response.readline().decode().rstrip("\r\n")
            if not line:
                if "data" in frame:
                    frame["data"] = json.loads(frame["data"])
                    return frame
                frame = {}
                continue
            key, _, value = line.partition(":")
            frame[key] = value.strip()

    def test_real_loopback_stream_backlog_concurrent_batches_and_reconnect(self):
        self.add_events(0, 307)
        port = self.start_server()
        response = self.open_stream(port)
        received, cursors = [], []
        for index in range(20):
            frame = self.read_frame(response)
            payload = frame["data"]
            page = [event["id"] for event in payload["changes"]]
            received.extend(page)
            cursors.append(int(frame["id"]))
            self.assertEqual(page[-1], int(frame["id"]))
            if index == 0:
                self.add_events(1000, 111)
            if len(received) == 420:
                break
        expected = [row["id"] for row in self.db.all("SELECT id FROM events ORDER BY id")]
        self.assertEqual(expected, received)
        self.assertEqual(sorted(set(cursors)), cursors)
        response.close()
        self.add_events(2000, 3)
        # Native EventSource reconnect keeps its original URL. The header must win.
        reconnect = self.open_stream(port, cursor=0, headers={"Last-Event-ID": str(cursors[-1])})
        frame = self.read_frame(reconnect)
        self.assertEqual(["2000", "2001", "2002"], [row["summary"] for row in frame["data"]["changes"]])
        self.assertEqual(self.service.event_cursor(), int(frame["id"]))

    def test_real_loopback_future_cursor_resets_to_first_page(self):
        self.add_events(0, 105)
        response = self.open_stream(self.start_server(), cursor=999999)
        frame = self.read_frame(response)
        self.assertTrue(frame["data"]["reset"])
        self.assertEqual(100, len(frame["data"]["changes"]))
        self.assertEqual(frame["data"]["changes"][-1]["id"], int(frame["id"]))


if __name__ == "__main__":
    unittest.main()
