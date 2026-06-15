"""End-to-end HTTP route tests for appliance.server.

We start a real ThreadingHTTPServer bound to an ephemeral port (port 0) in a
daemon thread, inject metric/host sources via the public registration helpers
(set_metrics_source / set_host_source), and hit the JSON endpoints with urllib.

Only the headless JSON endpoints are exercised here (/api/metrics, /api/hosts,
/api/health, POST /api/host/{name}). The HTML/display endpoints depend on
DisplayManager/Chromium and are out of scope for headless testing.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request

from appliance import server
from appliance.collectors.base import MetricValue
from appliance.server import ControlHandler, ThreadingHTTPServer


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode())


def _post(url):
    req = urllib.request.Request(url, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode())


class ServerHarness(unittest.TestCase):
    """Spins up a server on an ephemeral port and restores global state."""

    def setUp(self):
        # Snapshot mutable module-level state we touch.
        self._orig = (
            server._get_metrics,
            server._host_status_getter,
            server._active_host_getter,
            server._active_host_setter,
            server._host_names_getter,
            server._setup_mode_flag,
        )
        server.set_setup_mode(False)

        ThreadingHTTPServer.allow_reuse_address = True
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), ControlHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        (
            server._get_metrics,
            server._host_status_getter,
            server._active_host_getter,
            server._active_host_setter,
            server._host_names_getter,
            server._setup_mode_flag,
        ) = self._orig

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"


class TestHealth(ServerHarness):
    def test_health_ok(self):
        status, body = _get(self.url("/api/health"))
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})


class TestMetricsEndpoint(ServerHarness):
    def test_empty_when_no_source(self):
        server.set_metrics_source(None)
        status, body = _get(self.url("/api/metrics"))
        self.assertEqual(status, 200)
        self.assertEqual(body, {})

    def test_returns_injected_metrics_shape(self):
        server.set_metrics_source(lambda: {
            "cpu.usage": MetricValue(value=42.0, unit="%"),
            "mem.ram_used": MetricValue(
                value=7.5, unit="GiB", extra={"total": 16.0, "percent": 47.0}),
            "gpu.name": MetricValue(available=False),
        })
        status, body = _get(self.url("/api/metrics"))
        self.assertEqual(status, 200)
        self.assertEqual(body["cpu.usage"]["value"], 42.0)
        self.assertEqual(body["cpu.usage"]["unit"], "%")
        self.assertTrue(body["cpu.usage"]["available"])
        self.assertEqual(body["mem.ram_used"]["extra"]["total"], 16.0)
        self.assertFalse(body["gpu.name"]["available"])
        # Every entry must have the four-field shape.
        for v in body.values():
            self.assertEqual(set(v.keys()), {"value", "unit", "available", "extra"})


class TestHostsEndpoint(ServerHarness):
    def test_empty_when_no_source(self):
        server.set_host_source(None, None, None, None)
        status, body = _get(self.url("/api/hosts"))
        self.assertEqual(status, 200)
        self.assertEqual(body["hosts"], [])
        self.assertEqual(body["active_host"], "")
        self.assertIn("host_rotate_interval", body)

    def test_returns_host_status_and_active(self):
        hosts = [
            {"name": "a", "online": True},
            {"name": "b", "online": False},
        ]
        server.set_host_source(
            lambda: hosts,
            lambda: "a",
            lambda name: None,
            lambda: ["a", "b"],
        )
        status, body = _get(self.url("/api/hosts"))
        self.assertEqual(status, 200)
        self.assertEqual(body["hosts"], hosts)
        self.assertEqual(body["active_host"], "a")


class TestHostSwitch(ServerHarness):
    def test_switch_to_known_host(self):
        state = {"active": "a"}
        server.set_host_source(
            lambda: [],
            lambda: state["active"],
            lambda name: state.__setitem__("active", name),
            lambda: ["a", "b"],
        )
        status, body = _post(self.url("/api/host/b"))
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["active_host"], "b")
        self.assertEqual(state["active"], "b")
        # And /api/hosts now reflects the switch.
        _, hosts_body = _get(self.url("/api/hosts"))
        self.assertEqual(hosts_body["active_host"], "b")

    def test_switch_unknown_host_404(self):
        server.set_host_source(
            lambda: [],
            lambda: "a",
            lambda name: None,
            lambda: ["a", "b"],
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            _post(self.url("/api/host/zzz"))
        self.assertEqual(ctx.exception.code, 404)

    def test_switch_when_unconfigured_503(self):
        server.set_host_source(None, None, None, None)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            _post(self.url("/api/host/a"))
        self.assertEqual(ctx.exception.code, 503)


class TestUnknownRoutes(ServerHarness):
    def test_unknown_get_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            _get(self.url("/api/nope"))
        self.assertEqual(ctx.exception.code, 404)

    def test_unknown_post_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            _post(self.url("/api/also-nope"))
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
