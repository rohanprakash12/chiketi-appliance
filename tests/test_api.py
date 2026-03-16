"""Smoke tests for server.py API helpers and runtime host management."""

import unittest
from unittest.mock import MagicMock, patch

from appliance.collectors.base import MetricValue
from appliance.hosts import HostConfig


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


class TestAddHostRuntime(unittest.TestCase):
    def test_returns_true_on_success(self):
        from appliance import app
        mock_engine = MagicMock()
        mock_engine.add_host.return_value = True
        original = app._engine
        try:
            app._engine = mock_engine
            hc = HostConfig(name="test", host="10.0.0.1", user="u")
            result = app.add_host_runtime(hc)
            self.assertTrue(result)
            mock_engine.add_host.assert_called_once_with(hc)
        finally:
            app._engine = original

    def test_returns_false_when_engine_is_none(self):
        from appliance import app
        original = app._engine
        try:
            app._engine = None
            hc = HostConfig(name="test", host="10.0.0.1", user="u")
            result = app.add_host_runtime(hc)
            self.assertFalse(result)
        finally:
            app._engine = original


class TestRemoveHostRuntime(unittest.TestCase):
    def test_returns_true_on_success(self):
        from appliance import app
        mock_engine = MagicMock()
        mock_engine.remove_host.return_value = True
        original = app._engine
        try:
            app._engine = mock_engine
            result = app.remove_host_runtime("myhost")
            self.assertTrue(result)
            mock_engine.remove_host.assert_called_once_with("myhost")
        finally:
            app._engine = original

    def test_returns_false_on_unknown_host(self):
        from appliance import app
        mock_engine = MagicMock()
        mock_engine.remove_host.return_value = False
        original = app._engine
        try:
            app._engine = mock_engine
            result = app.remove_host_runtime("nonexistent")
            self.assertFalse(result)
        finally:
            app._engine = original

    def test_returns_false_when_engine_is_none(self):
        from appliance import app
        original = app._engine
        try:
            app._engine = None
            result = app.remove_host_runtime("anyhost")
            self.assertFalse(result)
        finally:
            app._engine = original


class TestSaveCurrentConfigPreservesSettings(unittest.TestCase):
    def test_preserves_display_and_server(self):
        from appliance import app
        from appliance.hosts import ApplianceConfig
        import tempfile, os

        original_engine = app._engine
        original_config = app._original_config
        try:
            # Set up a mock engine
            mock_engine = MagicMock()
            hc = HostConfig(name="srv", host="10.0.0.1", user="u")
            mock_engine.get_host_configs.return_value = [hc]
            app._engine = mock_engine

            # Set up original config with display/server settings
            app._original_config = ApplianceConfig(
                hosts=[hc],
                display={"theme": "Panel/Gold", "rotate_interval": 15},
                server={"port": 9999, "bind": "127.0.0.1"},
            )

            tmpdir = tempfile.mkdtemp()
            config_path = os.path.join(tmpdir, "config.yaml")

            with patch("appliance.hosts.save_config") as mock_save:
                mock_save.return_value = config_path
                app.save_current_config()
                saved_ac = mock_save.call_args[0][0]
                self.assertEqual(saved_ac.display["theme"], "Panel/Gold")
                self.assertEqual(saved_ac.display["rotate_interval"], 15)
                self.assertEqual(saved_ac.server["port"], 9999)
                self.assertEqual(saved_ac.server["bind"], "127.0.0.1")
        finally:
            app._engine = original_engine
            app._original_config = original_config


if __name__ == "__main__":
    unittest.main()
