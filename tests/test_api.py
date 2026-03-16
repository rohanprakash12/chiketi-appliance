"""Smoke tests for server.py API helpers."""

import unittest

from appliance.collectors.base import MetricValue


class TestSerializeMetrics(unittest.TestCase):
    def test_basic_serialization(self):
        from appliance import server
        original_getter = server._get_metrics
        try:
            test_metrics = {
                "cpu.usage": MetricValue(value=55.3, unit="%"),
                "mem.ram_used": MetricValue(
                    value=7.8, unit="GiB",
                    extra={"total": 15.6, "percent": 50.0},
                ),
                "gpu.name": MetricValue(available=False),
            }
            server._get_metrics = lambda: test_metrics
            result = server._serialize_metrics()
            self.assertEqual(result["cpu.usage"]["value"], 55.3)
            self.assertEqual(result["cpu.usage"]["unit"], "%")
            self.assertTrue(result["cpu.usage"]["available"])
            self.assertEqual(result["mem.ram_used"]["extra"]["total"], 15.6)
            self.assertFalse(result["gpu.name"]["available"])
        finally:
            server._get_metrics = original_getter

    def test_no_metrics_source(self):
        from appliance import server
        original_getter = server._get_metrics
        try:
            server._get_metrics = None
            result = server._serialize_metrics()
            self.assertEqual(result, {})
        finally:
            server._get_metrics = original_getter


class TestSetMetricsSource(unittest.TestCase):
    def test_stores_callable(self):
        from appliance import server
        original = server._get_metrics
        try:
            fn = lambda: {"test": MetricValue(value=1)}
            server.set_metrics_source(fn)
            self.assertIs(server._get_metrics, fn)
        finally:
            server._get_metrics = original


class TestSetHostSource(unittest.TestCase):
    def test_stores_callables(self):
        from appliance import server
        originals = (
            server._host_status_getter,
            server._active_host_getter,
            server._active_host_setter,
            server._host_names_getter,
        )
        try:
            status_fn = lambda: []
            active_get = lambda: "host1"
            active_set = lambda name: None
            names_fn = lambda: ["host1"]
            server.set_host_source(status_fn, active_get, active_set, names_fn)
            self.assertIs(server._host_status_getter, status_fn)
            self.assertIs(server._active_host_getter, active_get)
            self.assertIs(server._active_host_setter, active_set)
            self.assertIs(server._host_names_getter, names_fn)
        finally:
            (
                server._host_status_getter,
                server._active_host_getter,
                server._active_host_setter,
                server._host_names_getter,
            ) = originals


class TestSetupMode(unittest.TestCase):
    def test_toggle(self):
        from appliance import server
        original = server._setup_mode_flag
        try:
            server.set_setup_mode(True)
            self.assertTrue(server.is_setup_mode())
            server.set_setup_mode(False)
            self.assertFalse(server.is_setup_mode())
        finally:
            server._setup_mode_flag = original


if __name__ == "__main__":
    unittest.main()
