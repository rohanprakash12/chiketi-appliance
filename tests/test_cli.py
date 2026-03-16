"""Smoke tests for CLI entry point."""

import subprocess
import sys
import unittest

from appliance.__main__ import _parse_host_string
from appliance.hosts import HostConfig


class TestParseHostString(unittest.TestCase):
    def test_user_at_host(self):
        user, host, port = _parse_host_string("rohan@192.168.1.50")
        self.assertEqual(user, "rohan")
        self.assertEqual(host, "192.168.1.50")
        self.assertEqual(port, 22)

    def test_user_at_host_with_port(self):
        user, host, port = _parse_host_string("deploy@myserver.com:2222")
        self.assertEqual(user, "deploy")
        self.assertEqual(host, "myserver.com")
        self.assertEqual(port, 2222)

    def test_missing_at_sign(self):
        with self.assertRaises(ValueError):
            _parse_host_string("nousername")

    def test_empty_user(self):
        with self.assertRaises(ValueError):
            _parse_host_string("@hostname")

    def test_empty_host(self):
        with self.assertRaises(ValueError):
            _parse_host_string("user@")

    def test_invalid_port(self):
        with self.assertRaises(ValueError):
            _parse_host_string("user@host:notaport")

    def test_ipv4_no_port(self):
        user, host, port = _parse_host_string("admin@10.0.0.1")
        self.assertEqual(host, "10.0.0.1")
        self.assertEqual(port, 22)


class TestHelpFlag(unittest.TestCase):
    def test_help_exits_zero(self):
        result = subprocess.run(
            [sys.executable, "-m", "appliance", "--help"],
            capture_output=True, text=True,
            cwd="/home/rohan/projects/chiketi-appliance",
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("chiketi-appliance", result.stdout)


if __name__ == "__main__":
    unittest.main()
