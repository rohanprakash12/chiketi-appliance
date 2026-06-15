"""Tests for appliance.collectors.remote.RemoteCollector.

All SSH is mocked at the paramiko boundary — no real network/hardware.
We patch `appliance.collectors.remote.paramiko` so that
`paramiko.SSHClient()` returns a controllable mock, and stub out
`time.monotonic` where reconnect backoff timing matters.
"""

import unittest
from unittest.mock import MagicMock, patch

from appliance.collectors.base import MetricValue
from appliance.collectors.remote import RemoteCollector, _RECONNECT_INTERVAL
from appliance.collectors.ssh_commands import COMBINED_COMMAND
from appliance.hosts import HostConfig


# A realistic combined-command output covering every section. Mirrors the
# fixture style already used in tests/test_ssh_parsers.py.
COMBINED_OUTPUT = """\
===CPU_STAT===
cpu  123456 789 101112 987654 3210 0 0 0 0 0
cpu0 30000 200 25000 250000 800 0 0 0 0 0
cpu1 31000 189 26000 245000 810 0 0 0 0 0
cpu2 31200 200 25100 246000 800 0 0 0 0 0
cpu3 31256 200 25012 246654 800 0 0 0 0 0
===CPU_INFO===
4
 Intel(R) Core(TM) i7-10700K CPU @ 3.80GHz
===MEMORY===
              total        used        free      shared  buff/cache   available
Mem:    16777216000  8388608000  4294967296   134217728  4093640704  8254390272
Swap:    2147483648   536870912  1610612736
===DISK===
Mounted on          Size         Used Use%
/            512110190592 256055095296  50%
/home        512110190592 256055095296  50%
===NETWORK===
Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 1234567890  12345    0    0    0     0          0         0 1234567890  12345    0    0    0     0       0          0
  eth0: 9876543210  98765    0    0    0     0          0       123 1234509876  54321    0    0    0     0       0          0
===NET_ROUTE===
default via 192.168.1.1 dev eth0 proto dhcp metric 100
192.168.1.0/24 dev eth0 proto kernel scope link src 192.168.1.50
===NET_ADDR===
2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500
    inet 192.168.1.50/24 brd 192.168.1.255 scope global eth0
===NET_LINK===
2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc fq_codel state UP mode DEFAULT group default qlen 1000
    link/ether aa:bb:cc:dd:ee:ff brd ff:ff:ff:ff:ff:ff
===NET_SPEED===
1000
===TEMPS===
45000
38000
===FANS===
1200
1150
900
===UPTIME===
myserver
289432.67 1157730.68
===GPU===
NVIDIA GeForce RTX 3080, 65, 45, 220.50, 350.00, 4096, 10240, 78, 35, 1800, 2100, 9501, 9501
===END===
"""


def _make_host(name="srv", host="10.0.0.1", user="u", **kw):
    return HostConfig(name=name, host=host, user=user, **kw)


def _make_client_mock(output=COMBINED_OUTPUT):
    """Build a mock paramiko.SSHClient whose exec_command returns `output`."""
    client = MagicMock()
    transport = MagicMock()
    transport.is_active.return_value = True
    client.get_transport.return_value = transport

    stdin = MagicMock()
    stdout = MagicMock()
    stdout.read.return_value = output.encode("utf-8")
    stderr = MagicMock()
    client.exec_command.return_value = (stdin, stdout, stderr)
    return client


class _PatchParamiko:
    """Context manager that patches remote.paramiko, returning the client mock.

    SSHClient() always returns the same client mock so connection reuse can
    be asserted.
    """

    def __init__(self, client=None):
        self.client = client or _make_client_mock()

    def __enter__(self):
        self._patcher = patch("appliance.collectors.remote.paramiko")
        self.paramiko = self._patcher.start()
        self.paramiko.SSHClient.return_value = self.client
        # AutoAddPolicy must be callable/usable
        self.paramiko.AutoAddPolicy.return_value = object()
        return self

    def __exit__(self, *exc):
        self._patcher.stop()
        return False


class TestConnect(unittest.TestCase):
    def test_connect_success_sets_online(self):
        with _PatchParamiko() as p:
            c = RemoteCollector(_make_host())
            self.assertTrue(c.connect())
            self.assertTrue(c.online)
            self.assertTrue(c.is_connected())
            p.client.connect.assert_called_once()

    def test_connect_uses_key_path_when_present(self):
        with _PatchParamiko() as p:
            c = RemoteCollector(_make_host(key_path="/home/u/.ssh/id_rsa"))
            c.connect()
            kwargs = p.client.connect.call_args.kwargs
            self.assertEqual(kwargs["key_filename"], "/home/u/.ssh/id_rsa")
            self.assertNotIn("password", kwargs)

    def test_connect_uses_password_env_when_no_key(self):
        import os
        os.environ["TEST_REMOTE_PW"] = "secret"
        try:
            with _PatchParamiko() as p:
                c = RemoteCollector(_make_host(password_env="TEST_REMOTE_PW"))
                c.connect()
                kwargs = p.client.connect.call_args.kwargs
                self.assertEqual(kwargs["password"], "secret")
                self.assertNotIn("key_filename", kwargs)
        finally:
            del os.environ["TEST_REMOTE_PW"]

    def test_connect_passes_host_port_user(self):
        with _PatchParamiko() as p:
            c = RemoteCollector(_make_host(host="1.2.3.4", port=2222, user="bob"))
            c.connect()
            kwargs = p.client.connect.call_args.kwargs
            self.assertEqual(kwargs["hostname"], "1.2.3.4")
            self.assertEqual(kwargs["port"], 2222)
            self.assertEqual(kwargs["username"], "bob")

    def test_connect_failure_returns_false_and_offline(self):
        client = _make_client_mock()
        client.connect.side_effect = Exception("refused")
        with _PatchParamiko(client=client):
            c = RemoteCollector(_make_host())
            self.assertFalse(c.connect())
            self.assertFalse(c.online)
            self.assertFalse(c.is_connected())

    def test_keepalive_enabled_on_transport(self):
        with _PatchParamiko() as p:
            c = RemoteCollector(_make_host())
            c.connect()
            p.client.get_transport.return_value.set_keepalive.assert_called_with(30)


class TestCollectPipeline(unittest.TestCase):
    def test_collect_runs_combined_command(self):
        with _PatchParamiko() as p:
            c = RemoteCollector(_make_host())
            c.connect()
            c.collect()
            args, kwargs = p.client.exec_command.call_args
            self.assertEqual(args[0], COMBINED_COMMAND)

    def test_collect_produces_expected_metric_keys(self):
        with _PatchParamiko():
            c = RemoteCollector(_make_host())
            c.connect()
            metrics = c.collect()
            for key in (
                "cpu.usage", "cpu.core_count", "cpu.model",
                "mem.ram_used", "mem.ram_percent",
                "disk.root_used", "disk.root_percent",
                "net.ip", "net.mac",
                "sys.hostname", "sys.uptime",
                "gpu.name", "gpu.temp",
            ):
                self.assertIn(key, metrics, f"missing {key}")

    def test_collect_parses_values(self):
        with _PatchParamiko():
            c = RemoteCollector(_make_host())
            c.connect()
            metrics = c.collect()
            self.assertEqual(metrics["cpu.core_count"].value, 4)
            self.assertEqual(metrics["sys.hostname"].value, "myserver")
            self.assertEqual(metrics["net.ip"].value, "192.168.1.50")
            # Parser uppercases the MAC address — assert actual behavior.
            self.assertEqual(metrics["net.mac"].value, "AA:BB:CC:DD:EE:FF")
            self.assertEqual(metrics["gpu.name"].value, "NVIDIA GeForce RTX 3080")

    def test_collect_sets_latency(self):
        with _PatchParamiko():
            c = RemoteCollector(_make_host())
            c.connect()
            c.collect()
            self.assertIsInstance(c.latency_ms, float)
            self.assertGreaterEqual(c.latency_ms, 0.0)

    def test_collect_online_after_success(self):
        with _PatchParamiko():
            c = RemoteCollector(_make_host())
            c.connect()
            c.collect()
            self.assertTrue(c.online)


class TestOfflineDegradation(unittest.TestCase):
    def _assert_all_unavailable(self, metrics):
        sample = [
            "cpu.usage", "mem.ram_used", "disk.root_used",
            "net.ip", "sys.hostname", "gpu.name",
        ]
        for key in sample:
            self.assertIn(key, metrics)
            self.assertFalse(metrics[key].available, f"{key} should be unavailable")

    def test_collect_when_never_connected_returns_offline(self):
        # connect fails -> reconnect blocked within backoff window? First call:
        client = _make_client_mock()
        client.connect.side_effect = Exception("no route")
        with _PatchParamiko(client=client):
            c = RemoteCollector(_make_host())
            metrics = c.collect()
            self._assert_all_unavailable(metrics)
            self.assertFalse(c.online)

    def test_exec_failure_marks_offline_and_closes(self):
        client = _make_client_mock()
        client.exec_command.side_effect = Exception("channel closed")
        with _PatchParamiko(client=client):
            c = RemoteCollector(_make_host())
            c.connect()
            metrics = c.collect()
            self._assert_all_unavailable(metrics)
            self.assertFalse(c.online)
            client.close.assert_called()

    def test_offline_includes_list_metrics(self):
        client = _make_client_mock()
        client.connect.side_effect = Exception("down")
        with _PatchParamiko(client=client):
            c = RemoteCollector(_make_host())
            metrics = c.collect()
            self.assertEqual(metrics["gpu.processes"].value, [])
            self.assertEqual(metrics["cpu.fan_count"].value, 0)


class TestReconnectBackoff(unittest.TestCase):
    def test_no_reconnect_within_backoff_window(self):
        client = _make_client_mock()
        client.connect.side_effect = Exception("fail")
        with _PatchParamiko(client=client):
            with patch("appliance.collectors.remote.time.monotonic") as mono:
                # connect attempt at t=100
                mono.return_value = 100.0
                c = RemoteCollector(_make_host())
                c.collect()  # triggers first connect (fails)
                first_calls = client.connect.call_count
                self.assertGreaterEqual(first_calls, 1)
                # t advances only a little — within backoff window
                mono.return_value = 100.0 + (_RECONNECT_INTERVAL / 2)
                c.collect()
                self.assertEqual(client.connect.call_count, first_calls,
                                 "should not reconnect within backoff window")

    def test_reconnect_after_backoff_window(self):
        client = _make_client_mock()
        client.connect.side_effect = Exception("fail")
        with _PatchParamiko(client=client):
            with patch("appliance.collectors.remote.time.monotonic") as mono:
                mono.return_value = 100.0
                c = RemoteCollector(_make_host())
                c.collect()
                first_calls = client.connect.call_count
                # advance well past the backoff window
                mono.return_value = 100.0 + _RECONNECT_INTERVAL + 1.0
                c.collect()
                self.assertGreater(client.connect.call_count, first_calls,
                                   "should attempt reconnect after backoff window")


class TestConnectionReuse(unittest.TestCase):
    def test_does_not_reconnect_every_cycle(self):
        with _PatchParamiko() as p:
            c = RemoteCollector(_make_host())
            c.connect()
            self.assertEqual(p.client.connect.call_count, 1)
            for _ in range(5):
                c.collect()
            # Still only one connect — connection reused across cycles.
            self.assertEqual(p.client.connect.call_count, 1)
            # exec_command called once per collect cycle
            self.assertEqual(p.client.exec_command.call_count, 5)

    def test_maybe_reconnect_skips_connect_when_alive(self):
        with _PatchParamiko() as p:
            c = RemoteCollector(_make_host())
            c.connect()
            # is_connected -> transport active -> no new connect
            self.assertTrue(c._maybe_reconnect())
            self.assertEqual(p.client.connect.call_count, 1)


class TestDisconnect(unittest.TestCase):
    def test_disconnect_closes_and_marks_offline(self):
        with _PatchParamiko() as p:
            c = RemoteCollector(_make_host())
            c.connect()
            c.disconnect()
            p.client.close.assert_called()
            self.assertFalse(c.online)

    def test_is_connected_false_when_transport_inactive(self):
        client = _make_client_mock()
        client.get_transport.return_value.is_active.return_value = False
        with _PatchParamiko(client=client):
            c = RemoteCollector(_make_host())
            c.connect()
            self.assertFalse(c.is_connected())
            self.assertFalse(c.online)


class TestProperties(unittest.TestCase):
    def test_name_reflects_config(self):
        c = RemoteCollector(_make_host(name="gpu-box"))
        self.assertEqual(c.name, "gpu-box")

    def test_initial_state_offline(self):
        c = RemoteCollector(_make_host())
        self.assertFalse(c.online)
        self.assertFalse(c.is_connected())


if __name__ == "__main__":
    unittest.main()
