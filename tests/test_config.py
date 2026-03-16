"""Tests for appliance.hosts configuration management."""

import os
import tempfile
import unittest

import yaml

from appliance.hosts import (
    ApplianceConfig,
    HostConfig,
    default_config_path,
    load_config,
    save_config,
)


def _write_yaml(data, path):
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False)


class TestLoadConfigValid(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.tmpdir, "config.yaml")

    def test_minimal_valid(self):
        _write_yaml({
            "hosts": [
                {"name": "server1", "host": "192.168.1.50", "user": "rohan"},
            ],
        }, self.config_path)
        cfg = load_config(self.config_path)
        self.assertEqual(len(cfg.hosts), 1)
        self.assertEqual(cfg.hosts[0].name, "server1")
        self.assertEqual(cfg.hosts[0].port, 22)
        self.assertIsInstance(cfg.display, dict)
        self.assertIsInstance(cfg.server, dict)

    def test_multiple_hosts(self):
        _write_yaml({
            "hosts": [
                {"name": "a", "host": "10.0.0.1", "user": "u1"},
                {"name": "b", "host": "10.0.0.2", "user": "u2", "port": 2222},
            ],
            "display": {"theme": "Panel/Gold", "rotate_interval": 10},
            "server": {"port": 8080, "bind": "0.0.0.0"},
        }, self.config_path)
        cfg = load_config(self.config_path)
        self.assertEqual(len(cfg.hosts), 2)
        self.assertEqual(cfg.hosts[1].port, 2222)
        self.assertEqual(cfg.display["theme"], "Panel/Gold")
        self.assertEqual(cfg.server["port"], 8080)

    def test_key_path_expansion(self):
        _write_yaml({
            "hosts": [
                {"name": "s", "host": "h", "user": "u", "key": "~/.ssh/id_rsa"},
            ],
        }, self.config_path)
        cfg = load_config(self.config_path)
        self.assertNotIn("~", cfg.hosts[0].key_path)
        self.assertTrue(cfg.hosts[0].key_path.startswith("/"))

    def test_password_env(self):
        os.environ["TEST_PWD_12345"] = "secret"
        try:
            _write_yaml({
                "hosts": [
                    {"name": "s", "host": "h", "user": "u", "password_env": "TEST_PWD_12345"},
                ],
            }, self.config_path)
            cfg = load_config(self.config_path)
            self.assertEqual(cfg.hosts[0].password, "secret")
        finally:
            del os.environ["TEST_PWD_12345"]


class TestLoadConfigInvalid(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.tmpdir, "config.yaml")

    def test_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            load_config("/nonexistent/path/config.yaml")

    def test_not_a_dict(self):
        with open(self.config_path, "w") as f:
            f.write("- just a list\n")
        with self.assertRaises(ValueError):
            load_config(self.config_path)

    def test_no_hosts_key(self):
        _write_yaml({"server": {"port": 7777}}, self.config_path)
        with self.assertRaises(ValueError):
            load_config(self.config_path)

    def test_empty_hosts_list(self):
        _write_yaml({"hosts": []}, self.config_path)
        with self.assertRaises(ValueError):
            load_config(self.config_path)

    def test_missing_required_field(self):
        _write_yaml({
            "hosts": [{"name": "s", "host": "h"}],  # missing 'user'
        }, self.config_path)
        with self.assertRaises(ValueError) as ctx:
            load_config(self.config_path)
        self.assertIn("user", str(ctx.exception))

    def test_duplicate_host_names(self):
        _write_yaml({
            "hosts": [
                {"name": "dup", "host": "h1", "user": "u1"},
                {"name": "dup", "host": "h2", "user": "u2"},
            ],
        }, self.config_path)
        with self.assertRaises(ValueError) as ctx:
            load_config(self.config_path)
        self.assertIn("unique", str(ctx.exception))

    def test_invalid_port(self):
        _write_yaml({
            "hosts": [{"name": "s", "host": "h", "user": "u"}],
            "server": {"port": 99999},
        }, self.config_path)
        with self.assertRaises(ValueError):
            load_config(self.config_path)

    def test_invalid_port_string(self):
        _write_yaml({
            "hosts": [{"name": "s", "host": "h", "user": "u"}],
            "server": {"port": "abc"},
        }, self.config_path)
        with self.assertRaises(ValueError):
            load_config(self.config_path)

    def test_negative_rotate_interval(self):
        _write_yaml({
            "hosts": [{"name": "s", "host": "h", "user": "u"}],
            "display": {"rotate_interval": -5},
        }, self.config_path)
        with self.assertRaises(ValueError):
            load_config(self.config_path)

    def test_non_dict_display(self):
        _write_yaml({
            "hosts": [{"name": "s", "host": "h", "user": "u"}],
            "display": "not_a_dict",
        }, self.config_path)
        # Should not raise, just default to empty dict
        cfg = load_config(self.config_path)
        self.assertEqual(cfg.display, {})

    def test_non_dict_server(self):
        _write_yaml({
            "hosts": [{"name": "s", "host": "h", "user": "u"}],
            "server": 42,
        }, self.config_path)
        cfg = load_config(self.config_path)
        self.assertEqual(cfg.server, {})

    def test_host_entry_not_dict(self):
        _write_yaml({
            "hosts": ["just_a_string"],
        }, self.config_path)
        with self.assertRaises(ValueError):
            load_config(self.config_path)


class TestDefaultConfigPath(unittest.TestCase):
    def test_returns_expected_path(self):
        path = default_config_path()
        self.assertIn(".config/chiketi-appliance/config.yaml", path)
        self.assertTrue(path.startswith("/"))


class TestSaveConfig(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.tmpdir, "config.yaml")

    def test_roundtrip(self):
        cfg = ApplianceConfig(
            hosts=[
                HostConfig(name="srv", host="10.0.0.1", user="admin", port=22, key_path="/home/u/.ssh/id_rsa"),
                HostConfig(name="nas", host="10.0.0.2", user="root", port=2222),
            ],
            display={"theme": "Panel/Gold", "rotate_interval": 10},
            server={"port": 7777},
        )
        save_config(cfg, self.config_path)
        loaded = load_config(self.config_path)
        self.assertEqual(len(loaded.hosts), 2)
        self.assertEqual(loaded.hosts[0].name, "srv")
        self.assertEqual(loaded.hosts[1].port, 2222)
        self.assertEqual(loaded.display["theme"], "Panel/Gold")

    def test_file_permissions(self):
        cfg = ApplianceConfig(
            hosts=[HostConfig(name="s", host="h", user="u")],
        )
        save_config(cfg, self.config_path)
        mode = os.stat(self.config_path).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_creates_parent_dirs(self):
        nested_path = os.path.join(self.tmpdir, "sub", "dir", "config.yaml")
        cfg = ApplianceConfig(
            hosts=[HostConfig(name="s", host="h", user="u")],
        )
        save_config(cfg, nested_path)
        self.assertTrue(os.path.isfile(nested_path))

    def test_preserves_display_and_server(self):
        """save_config should preserve display and server settings."""
        cfg = ApplianceConfig(
            hosts=[HostConfig(name="srv", host="10.0.0.1", user="admin")],
            display={"theme": "Panel/Gold", "rotate_interval": 10, "host_rotate": True},
            server={"port": 8080, "bind": "0.0.0.0"},
        )
        save_config(cfg, self.config_path)
        loaded = load_config(self.config_path)
        self.assertEqual(loaded.display["theme"], "Panel/Gold")
        self.assertEqual(loaded.display["rotate_interval"], 10)
        self.assertTrue(loaded.display["host_rotate"])
        self.assertEqual(loaded.server["port"], 8080)
        self.assertEqual(loaded.server["bind"], "0.0.0.0")

    def test_preserves_password_env(self):
        """save_config should persist password_env (not the resolved password)."""
        cfg = ApplianceConfig(
            hosts=[
                HostConfig(name="nas", host="10.0.0.2", user="admin", password_env="NAS_PASSWORD"),
            ],
        )
        save_config(cfg, self.config_path)
        # Read raw YAML to verify the env var name is stored, not the resolved value
        with open(self.config_path) as f:
            raw = yaml.safe_load(f)
        self.assertEqual(raw["hosts"][0]["password_env"], "NAS_PASSWORD")
        self.assertNotIn("password", raw["hosts"][0])
        # Also verify roundtrip via load_config
        loaded = load_config(self.config_path)
        self.assertEqual(loaded.hosts[0].password_env, "NAS_PASSWORD")

    def test_roundtrip_full(self):
        """Full roundtrip: save then load, verify all fields match."""
        cfg = ApplianceConfig(
            hosts=[
                HostConfig(name="srv", host="10.0.0.1", user="admin", port=22,
                           key_path="/home/u/.ssh/id_rsa"),
                HostConfig(name="nas", host="10.0.0.2", user="root", port=2222,
                           password_env="NAS_PWD"),
            ],
            display={"theme": "Panel/Gold", "rotate_interval": 10},
            server={"port": 7777},
        )
        save_config(cfg, self.config_path)
        loaded = load_config(self.config_path)
        self.assertEqual(len(loaded.hosts), 2)
        self.assertEqual(loaded.hosts[0].name, "srv")
        self.assertEqual(loaded.hosts[0].key_path, "/home/u/.ssh/id_rsa")
        self.assertEqual(loaded.hosts[1].name, "nas")
        self.assertEqual(loaded.hosts[1].port, 2222)
        self.assertEqual(loaded.hosts[1].password_env, "NAS_PWD")
        self.assertEqual(loaded.display["theme"], "Panel/Gold")
        self.assertEqual(loaded.display["rotate_interval"], 10)
        self.assertEqual(loaded.server["port"], 7777)


if __name__ == "__main__":
    unittest.main()
