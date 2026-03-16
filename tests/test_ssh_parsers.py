"""Tests for appliance.collectors.ssh_commands parsers."""

import unittest
from unittest.mock import patch

from appliance.collectors.base import MetricValue
from appliance.collectors.ssh_commands import (
    split_sections,
    parse_cpu_stat,
    parse_cpu_info,
    parse_memory,
    parse_disk,
    parse_network,
    parse_net_speed,
    parse_temps,
    parse_fans,
    parse_uptime,
    parse_gpu,
    _parse_proc_stat_lines,
    _cpu_usage_from_delta,
    _gib,
    _tib,
    _format_rate,
)


# ---------------------------------------------------------------------------
# Realistic test data
# ---------------------------------------------------------------------------

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
1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536
    inet 127.0.0.1/8 scope host lo
2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500
    inet 192.168.1.50/24 brd 192.168.1.255 scope global eth0
===NET_LINK===
1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN mode DEFAULT group default qlen 1000
    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00
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

PROC_STAT_TEXT_1 = """\
cpu  100000 500 50000 800000 2000 0 0 0 0 0
cpu0 25000 125 12500 200000 500 0 0 0 0 0
cpu1 25000 125 12500 200000 500 0 0 0 0 0
cpu2 25000 125 12500 200000 500 0 0 0 0 0
cpu3 25000 125 12500 200000 500 0 0 0 0 0
"""

PROC_STAT_TEXT_2 = """\
cpu  110000 600 55000 830000 2100 0 0 0 0 0
cpu0 27500 150 13750 207500 525 0 0 0 0 0
cpu1 27500 150 13750 207500 525 0 0 0 0 0
cpu2 27500 150 13750 207500 525 0 0 0 0 0
cpu3 27500 150 13750 207500 525 0 0 0 0 0
"""

FREE_B_OUTPUT = """\
              total        used        free      shared  buff/cache   available
Mem:    16777216000  8388608000  4294967296   134217728  4093640704  8254390272
Swap:    2147483648   536870912  1610612736
"""

DF_OUTPUT_SEPARATE = """\
Mounted on          Size         Used Use%
/            512110190592 256055095296  50%
/home       1099511627776 549755813888  50%
"""

DF_OUTPUT_SAME_PARTITION = """\
Mounted on          Size         Used Use%
/            512110190592 256055095296  50%
/            512110190592 256055095296  50%
"""

NVIDIA_SMI_CSV = "NVIDIA GeForce RTX 3080, 65, 45, 220.50, 350.00, 4096, 10240, 78, 35, 1800, 2100, 9501, 9501"


class TestSplitSections(unittest.TestCase):
    def test_all_sections_present(self):
        sections = split_sections(COMBINED_OUTPUT)
        expected_keys = [
            "CPU_STAT", "CPU_INFO", "MEMORY", "DISK", "NETWORK",
            "NET_ROUTE", "NET_ADDR", "NET_LINK", "NET_SPEED", "TEMPS",
            "FANS", "UPTIME", "GPU",
        ]
        for key in expected_keys:
            self.assertIn(key, sections, f"Missing section: {key}")
            self.assertTrue(len(sections[key]) > 0, f"Empty section: {key}")

    def test_cpu_stat_section_content(self):
        sections = split_sections(COMBINED_OUTPUT)
        self.assertIn("cpu ", sections["CPU_STAT"])

    def test_empty_input(self):
        sections = split_sections("")
        # All sections should be empty strings
        for key in sections.values():
            self.assertEqual(key, "")

    def test_partial_input(self):
        partial = "===CPU_STAT===\nsome data\n===CPU_INFO===\nmore\n===MEMORY===\n===END==="
        sections = split_sections(partial)
        self.assertEqual(sections["CPU_STAT"], "some data")
        # MEMORY has no content between its marker and END
        # but the sections between MEMORY and END that are missing get ""
        self.assertIn("CPU_STAT", sections)


class TestParseCpuStat(unittest.TestCase):
    def test_first_call_returns_zero(self):
        metrics, new_prev = parse_cpu_stat(PROC_STAT_TEXT_1, prev_stat=None)
        self.assertEqual(metrics["cpu.usage"].value, 0.0)
        self.assertEqual(metrics["cpu.per_core"].value, [])
        self.assertIsNotNone(new_prev)

    def test_delta_calculation(self):
        prev = _parse_proc_stat_lines(PROC_STAT_TEXT_1)
        metrics, new_prev = parse_cpu_stat(PROC_STAT_TEXT_2, prev_stat=prev)
        usage = metrics["cpu.usage"].value
        self.assertIsInstance(usage, float)
        self.assertGreater(usage, 0.0)
        self.assertLess(usage, 100.0)
        # per_core should have 4 entries
        per_core = metrics["cpu.per_core"].value
        self.assertEqual(len(per_core), 4)
        for core_usage in per_core:
            self.assertGreater(core_usage, 0.0)
            self.assertLess(core_usage, 100.0)

    def test_empty_input(self):
        metrics, new_prev = parse_cpu_stat("", prev_stat=None)
        self.assertFalse(metrics["cpu.usage"].available)
        self.assertFalse(metrics["cpu.per_core"].available)

    def test_malformed_input(self):
        metrics, new_prev = parse_cpu_stat("not a valid stat line\ngibberish", prev_stat=None)
        self.assertFalse(metrics["cpu.usage"].available)


class TestParseCpuInfo(unittest.TestCase):
    def test_normal_input(self):
        section = "4\n Intel(R) Core(TM) i7-10700K CPU @ 3.80GHz"
        metrics = parse_cpu_info(section)
        self.assertEqual(metrics["cpu.core_count"].value, 4)
        self.assertIn("i7-10700K", metrics["cpu.model"].value)

    def test_only_core_count(self):
        metrics = parse_cpu_info("8")
        self.assertEqual(metrics["cpu.core_count"].value, 8)
        self.assertFalse(metrics["cpu.model"].available)

    def test_empty(self):
        metrics = parse_cpu_info("")
        self.assertFalse(metrics["cpu.core_count"].available)
        self.assertFalse(metrics["cpu.model"].available)

    def test_malformed(self):
        metrics = parse_cpu_info("not_a_number\ngarbage")
        self.assertFalse(metrics["cpu.core_count"].available)


class TestParseMemory(unittest.TestCase):
    def test_normal_output(self):
        metrics = parse_memory(FREE_B_OUTPUT)
        self.assertTrue(metrics["mem.ram_used"].available)
        self.assertTrue(metrics["mem.ram_total"].available)
        self.assertTrue(metrics["mem.ram_percent"].available)
        self.assertTrue(metrics["mem.swap_used"].available)
        # Verify percent uses (total - available) / total
        total = 16777216000
        available = 8254390272
        expected_pct = round((total - available) / total * 100, 1)
        self.assertAlmostEqual(metrics["mem.ram_percent"].value, expected_pct, places=1)

    def test_gib_conversion(self):
        metrics = parse_memory(FREE_B_OUTPUT)
        # 16777216000 bytes ~ 15.6 GiB
        self.assertAlmostEqual(metrics["mem.ram_total"].value, _gib(16777216000), places=1)

    def test_empty_input(self):
        metrics = parse_memory("")
        for key in ("ram_used", "ram_total", "ram_percent", "swap_used", "swap_total", "swap_percent"):
            self.assertFalse(metrics[f"mem.{key}"].available)

    def test_malformed(self):
        metrics = parse_memory("Mem: not numbers here at all xxx")
        self.assertFalse(metrics["mem.ram_used"].available)

    def test_no_swap(self):
        no_swap = """\
              total        used        free      shared  buff/cache   available
Mem:    16777216000  8388608000  4294967296   134217728  4093640704  8254390272
"""
        metrics = parse_memory(no_swap)
        self.assertTrue(metrics["mem.ram_used"].available)
        for key in ("swap_used", "swap_total", "swap_percent"):
            self.assertFalse(metrics[f"mem.{key}"].available)


class TestParseDisk(unittest.TestCase):
    def test_separate_partitions(self):
        metrics = parse_disk(DF_OUTPUT_SEPARATE)
        self.assertTrue(metrics["disk.root_used"].available)
        self.assertTrue(metrics["disk.home_used"].available)
        self.assertEqual(metrics["disk.root_percent"].value, 50.0)
        self.assertEqual(metrics["disk.home_percent"].value, 50.0)

    def test_home_over_1tib(self):
        metrics = parse_disk(DF_OUTPUT_SEPARATE)
        # /home is 1099511627776 bytes = 1 TiB
        self.assertEqual(metrics["disk.home_used"].unit, "TiB")
        self.assertEqual(metrics["disk.root_used"].unit, "GiB")

    def test_same_partition(self):
        metrics = parse_disk(DF_OUTPUT_SAME_PARTITION)
        self.assertTrue(metrics["disk.root_used"].available)
        self.assertTrue(metrics["disk.home_used"].available)

    def test_single_row(self):
        single = """\
Mounted on          Size         Used Use%
/            512110190592 256055095296  50%
"""
        metrics = parse_disk(single)
        self.assertTrue(metrics["disk.root_used"].available)
        for key in ("home_used", "home_total", "home_percent"):
            self.assertFalse(metrics[f"disk.{key}"].available)

    def test_empty(self):
        metrics = parse_disk("")
        for key in ("root", "home"):
            for suffix in ("used", "total", "percent"):
                self.assertFalse(metrics[f"disk.{key}_{suffix}"].available)

    def test_malformed(self):
        metrics = parse_disk("garbage data here")
        for key in ("root", "home"):
            for suffix in ("used", "total", "percent"):
                self.assertFalse(metrics[f"disk.{key}_{suffix}"].available)


class TestParseNetwork(unittest.TestCase):
    NET_DEV = """\
Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 1234567890  12345    0    0    0     0          0         0 1234567890  12345    0    0    0     0       0          0
  eth0: 9876543210  98765    0    0    0     0          0       123 1234509876  54321    0    0    0     0       0          0
"""
    ROUTE = "default via 192.168.1.1 dev eth0 proto dhcp metric 100\n192.168.1.0/24 dev eth0 proto kernel scope link src 192.168.1.50"
    ADDR = """\
1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536
    inet 127.0.0.1/8 scope host lo
2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500
    inet 192.168.1.50/24 brd 192.168.1.255 scope global eth0
"""
    LINK = """\
1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN mode DEFAULT group default qlen 1000
    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00
2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc fq_codel state UP mode DEFAULT group default qlen 1000
    link/ether aa:bb:cc:dd:ee:ff brd ff:ff:ff:ff:ff:ff
"""

    def test_first_call_returns_zero(self):
        metrics, prev_net, prev_time = parse_network(
            self.NET_DEV, self.ROUTE, self.ADDR, self.LINK, None, None,
        )
        self.assertEqual(metrics["net.dl"].value, 0.0)
        self.assertEqual(metrics["net.ul"].value, 0.0)
        self.assertIsNotNone(prev_net)

    def test_ip_parsed(self):
        metrics, _, _ = parse_network(
            self.NET_DEV, self.ROUTE, self.ADDR, self.LINK, None, None,
        )
        self.assertEqual(metrics["net.ip"].value, "192.168.1.50")

    def test_mac_parsed(self):
        metrics, _, _ = parse_network(
            self.NET_DEV, self.ROUTE, self.ADDR, self.LINK, None, None,
        )
        self.assertEqual(metrics["net.mac"].value, "AA:BB:CC:DD:EE:FF")

    def test_throughput_delta(self):
        # First call
        _, prev_net, prev_time = parse_network(
            self.NET_DEV, self.ROUTE, self.ADDR, self.LINK, None, None,
        )
        # Simulate second reading with more bytes
        net_dev_2 = self.NET_DEV.replace("9876543210", "9877543210").replace("1234509876", "1235509876")
        with patch("appliance.collectors.ssh_commands.time.monotonic", return_value=prev_time + 1.5):
            metrics, _, _ = parse_network(
                net_dev_2, self.ROUTE, self.ADDR, self.LINK, prev_net, prev_time,
            )
        self.assertGreater(metrics["net.dl"].value, 0.0)
        self.assertGreater(metrics["net.ul"].value, 0.0)

    def test_empty_input(self):
        metrics, _, _ = parse_network("", "", "", "", None, None)
        self.assertFalse(metrics["net.ip"].available)
        self.assertFalse(metrics["net.mac"].available)

    def test_no_default_route(self):
        metrics, _, _ = parse_network(
            self.NET_DEV, "192.168.1.0/24 dev eth0 proto kernel", self.ADDR, self.LINK, None, None,
        )
        self.assertFalse(metrics["net.ip"].available)


class TestParseNetSpeed(unittest.TestCase):
    def test_valid_speed(self):
        result = parse_net_speed("1000")
        self.assertEqual(result["net.speed"].value, 1000)

    def test_empty(self):
        result = parse_net_speed("")
        self.assertFalse(result["net.speed"].available)

    def test_invalid(self):
        result = parse_net_speed("not_a_number")
        self.assertFalse(result["net.speed"].available)

    def test_negative_speed(self):
        result = parse_net_speed("-1")
        self.assertFalse(result["net.speed"].available)

    def test_zero_speed(self):
        result = parse_net_speed("0")
        self.assertFalse(result["net.speed"].available)


class TestParseTemps(unittest.TestCase):
    def test_millidegree_conversion(self):
        metrics = parse_temps("45000\n38000")
        self.assertEqual(metrics["cpu.temp"].value, 45)  # highest
        self.assertEqual(metrics["cpu.mb_temp"].value, 38)

    def test_single_temp(self):
        metrics = parse_temps("52000")
        self.assertEqual(metrics["cpu.temp"].value, 52)
        self.assertFalse(metrics["cpu.mb_temp"].available)

    def test_empty(self):
        metrics = parse_temps("")
        self.assertFalse(metrics["cpu.temp"].available)
        self.assertFalse(metrics["cpu.mb_temp"].available)

    def test_malformed(self):
        metrics = parse_temps("not_numbers\ngarbage")
        self.assertFalse(metrics["cpu.temp"].available)

    def test_three_zones(self):
        metrics = parse_temps("50000\n45000\n40000")
        self.assertEqual(metrics["cpu.temp"].value, 50)
        self.assertEqual(metrics["cpu.mb_temp"].value, 45)


class TestParseFans(unittest.TestCase):
    def test_normal_rpms(self):
        metrics = parse_fans("1200\n1150\n900")
        self.assertEqual(metrics["cpu.fan_count"].value, 3)
        self.assertEqual(len(metrics["cpu.fans_cpu"].value), 2)
        self.assertEqual(len(metrics["cpu.fans_case"].value), 1)
        self.assertEqual(metrics["cpu.fan"].value, 1200)

    def test_empty_fans(self):
        metrics = parse_fans("")
        self.assertFalse(metrics["cpu.fan"].available)
        self.assertEqual(metrics["cpu.fan_count"].value, 0)
        self.assertEqual(metrics["cpu.fans_cpu"].value, [])
        self.assertEqual(metrics["cpu.fans_case"].value, [])

    def test_zero_rpm_fans(self):
        metrics = parse_fans("0\n0\n0")
        self.assertEqual(metrics["cpu.fan_count"].value, 0)

    def test_mixed_active_inactive(self):
        metrics = parse_fans("1200\n0\n900")
        self.assertEqual(metrics["cpu.fan_count"].value, 2)
        # First active fan (index 0) is cpu, index 2 is case
        self.assertEqual(metrics["cpu.fans_cpu"].value, [1200.0])
        self.assertEqual(metrics["cpu.fans_case"].value, [900.0])


class TestParseUptime(unittest.TestCase):
    def test_normal(self):
        metrics = parse_uptime("myserver\n289432.67 1157730.68")
        self.assertEqual(metrics["sys.hostname"].value, "myserver")
        self.assertIn("d", metrics["sys.uptime"].value)
        self.assertIn("h", metrics["sys.uptime"].value)
        self.assertIn("m", metrics["sys.uptime"].value)
        self.assertAlmostEqual(metrics["sys.uptime"].extra["seconds"], 289432.67)

    def test_uptime_formatting(self):
        # 90061 seconds = 1d 1h 1m
        metrics = parse_uptime("host1\n90061.00 180122.00")
        self.assertEqual(metrics["sys.uptime"].value, "1d 1h 1m")

    def test_empty(self):
        metrics = parse_uptime("")
        self.assertFalse(metrics["sys.hostname"].available)
        self.assertFalse(metrics["sys.uptime"].available)

    def test_only_hostname(self):
        metrics = parse_uptime("myserver")
        self.assertEqual(metrics["sys.hostname"].value, "myserver")
        self.assertFalse(metrics["sys.uptime"].available)


class TestParseGpu(unittest.TestCase):
    def test_normal_nvidia_smi(self):
        metrics = parse_gpu(NVIDIA_SMI_CSV)
        self.assertEqual(metrics["gpu.name"].value, "NVIDIA GeForce RTX 3080")
        self.assertEqual(metrics["gpu.temp"].value, 65)
        self.assertEqual(metrics["gpu.fan"].value, 45)
        self.assertAlmostEqual(metrics["gpu.power"].value, 220)
        self.assertEqual(metrics["gpu.power"].extra["limit"], 350)
        self.assertEqual(metrics["gpu.vram_used"].value, 4096)
        self.assertEqual(metrics["gpu.vram_total"].value, 10240)
        self.assertEqual(metrics["gpu.util"].value, 78)
        self.assertEqual(metrics["gpu.mem_util"].value, 35)
        self.assertEqual(metrics["gpu.clock_gpu"].value, 1800)
        self.assertEqual(metrics["gpu.clock_gpu"].extra["max"], 2100)
        self.assertEqual(metrics["gpu.clock_mem"].value, 9501)

    def test_no_gpu(self):
        metrics = parse_gpu("")
        for key in ("name", "temp", "fan", "power", "vram_used", "vram_total",
                     "vram_percent", "util", "mem_util", "clock_gpu", "clock_mem"):
            self.assertFalse(metrics[f"gpu.{key}"].available)
        self.assertEqual(metrics["gpu.processes"].value, [])

    def test_too_few_columns(self):
        metrics = parse_gpu("NVIDIA GeForce RTX 3080, 65, 45")
        for key in ("name", "temp", "fan", "power", "vram_used", "vram_total",
                     "vram_percent", "util", "mem_util", "clock_gpu", "clock_mem"):
            self.assertFalse(metrics[f"gpu.{key}"].available)

    def test_vram_percent_calculation(self):
        metrics = parse_gpu(NVIDIA_SMI_CSV)
        expected = round(4096 / 10240 * 100, 1)
        self.assertAlmostEqual(metrics["gpu.vram_percent"].value, expected, places=1)


class TestHelpers(unittest.TestCase):
    def test_gib(self):
        self.assertAlmostEqual(_gib(1073741824), 1.0)  # 1 GiB

    def test_tib(self):
        self.assertAlmostEqual(_tib(1099511627776), 1.0)  # 1 TiB

    def test_format_rate_mb(self):
        val, unit = _format_rate(2_500_000)
        self.assertEqual(unit, "MB/s")
        self.assertAlmostEqual(val, 2.5)

    def test_format_rate_kb(self):
        val, unit = _format_rate(5_000)
        self.assertEqual(unit, "KB/s")
        self.assertAlmostEqual(val, 5.0)

    def test_format_rate_bytes(self):
        val, unit = _format_rate(500)
        self.assertEqual(unit, "B/s")

    def test_cpu_usage_from_delta(self):
        prev = [100, 0, 50, 800, 20, 0, 0, 0, 0, 0]
        curr = [110, 0, 55, 830, 21, 0, 0, 0, 0, 0]
        usage = _cpu_usage_from_delta(prev, curr)
        self.assertGreater(usage, 0.0)
        self.assertLess(usage, 100.0)

    def test_cpu_usage_identical_readings(self):
        same = [100, 0, 50, 800, 20, 0, 0, 0, 0, 0]
        usage = _cpu_usage_from_delta(same, same)
        self.assertEqual(usage, 0.0)


if __name__ == "__main__":
    unittest.main()
