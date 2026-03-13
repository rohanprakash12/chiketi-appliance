"""SSH-based remote metric collector using paramiko."""

from __future__ import annotations

import logging
import time
from typing import Any

import paramiko

from appliance.collectors.base import MetricCollector, MetricValue
from appliance.collectors.ssh_commands import (
    COMBINED_COMMAND,
    CpuStat,
    NetStat,
    parse_cpu_info,
    parse_cpu_stat,
    parse_disk,
    parse_fans,
    parse_gpu,
    parse_memory,
    parse_net_speed,
    parse_network,
    parse_temps,
    parse_uptime,
    split_sections,
)
from appliance.hosts import HostConfig

logger = logging.getLogger(__name__)

# How long to wait before retrying a failed SSH connection (seconds)
_RECONNECT_INTERVAL = 30.0

# SSH exec timeout (seconds)
_EXEC_TIMEOUT = 10


class RemoteCollector(MetricCollector):
    """Collects system metrics from a remote Linux host via SSH."""

    namespace = ""  # metrics already have namespace prefixes from parsers

    def __init__(self, host_config: HostConfig) -> None:
        self._config = host_config
        self._client: paramiko.SSHClient | None = None
        self._online = False
        self._last_connect_attempt: float = 0.0
        self._latency_ms: float = 0.0

        # Delta tracking state
        self._prev_cpu_stat: CpuStat | None = None
        self._prev_net: NetStat | None = None
        self._prev_net_time: float | None = None

    # -- Properties -----------------------------------------------------------

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def online(self) -> bool:
        return self._online

    @property
    def latency_ms(self) -> float:
        return self._latency_ms

    # -- Connection management ------------------------------------------------

    def connect(self) -> bool:
        """Open SSH connection. Returns True on success."""
        self._last_connect_attempt = time.monotonic()
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            connect_kwargs: dict[str, Any] = {
                "hostname": self._config.host,
                "port": self._config.port,
                "username": self._config.user,
                "timeout": _EXEC_TIMEOUT,
                "banner_timeout": _EXEC_TIMEOUT,
                "auth_timeout": _EXEC_TIMEOUT,
            }

            # Authentication: key file > password > agent
            if self._config.key_path:
                connect_kwargs["key_filename"] = self._config.key_path
            elif self._config.password:
                connect_kwargs["password"] = self._config.password
            # else: rely on ssh-agent or default keys

            client.connect(**connect_kwargs)

            # Enable keepalive to detect dead connections
            transport = client.get_transport()
            if transport:
                transport.set_keepalive(30)

            self._client = client
            self._online = True
            logger.info("Connected to %s (%s)", self._config.name, self._config.host)
            return True

        except Exception as exc:
            logger.warning(
                "Failed to connect to %s (%s): %s",
                self._config.name, self._config.host, exc,
            )
            self._client = None
            self._online = False
            return False

    def disconnect(self) -> None:
        """Close SSH connection."""
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
        self._online = False

    def is_connected(self) -> bool:
        """Check if SSH connection is alive."""
        if self._client is None:
            return False
        transport = self._client.get_transport()
        if transport is None or not transport.is_active():
            self._online = False
            return False
        return True

    def _maybe_reconnect(self) -> bool:
        """Attempt reconnect if enough time has passed. Returns True if connected."""
        if self.is_connected():
            return True
        now = time.monotonic()
        if now - self._last_connect_attempt < _RECONNECT_INTERVAL:
            return False
        return self.connect()

    # -- Collection -----------------------------------------------------------

    def collect(self) -> dict[str, MetricValue]:
        """Execute combined command via SSH and parse all sections."""
        if not self._maybe_reconnect():
            return self._all_offline()

        try:
            start = time.monotonic()
            stdin, stdout, stderr = self._client.exec_command(  # type: ignore[union-attr]
                COMBINED_COMMAND,
                timeout=_EXEC_TIMEOUT,
            )
            stdin.close()
            output = stdout.read().decode("utf-8", errors="replace")
            elapsed = time.monotonic() - start
            self._latency_ms = round(elapsed * 1000, 1)

        except Exception as exc:
            logger.warning(
                "SSH exec failed for %s: %s", self._config.name, exc,
            )
            self._online = False
            if self._client:
                try:
                    self._client.close()
                except Exception:
                    pass
            self._client = None
            return self._all_offline()

        # Parse sections
        sections = split_sections(output)
        metrics: dict[str, MetricValue] = {}

        # CPU stat (delta-based)
        cpu_metrics, self._prev_cpu_stat = parse_cpu_stat(
            sections.get("CPU_STAT", ""), self._prev_cpu_stat,
        )
        metrics.update(cpu_metrics)

        # CPU info
        metrics.update(parse_cpu_info(sections.get("CPU_INFO", "")))

        # Memory
        metrics.update(parse_memory(sections.get("MEMORY", "")))

        # Disk
        metrics.update(parse_disk(sections.get("DISK", "")))

        # Network (delta-based)
        net_metrics, self._prev_net, self._prev_net_time = parse_network(
            sections.get("NETWORK", ""),
            sections.get("NET_ROUTE", ""),
            sections.get("NET_ADDR", ""),
            sections.get("NET_LINK", ""),
            self._prev_net,
            self._prev_net_time,
        )
        metrics.update(net_metrics)

        # Net speed
        speed_metrics = parse_net_speed(sections.get("NET_SPEED", ""))
        if speed_metrics.get("net.speed", MetricValue(available=False)).available:
            metrics.update(speed_metrics)

        # Temperatures
        metrics.update(parse_temps(sections.get("TEMPS", "")))

        # Fans
        metrics.update(parse_fans(sections.get("FANS", "")))

        # Uptime / hostname
        metrics.update(parse_uptime(sections.get("UPTIME", "")))

        # GPU
        metrics.update(parse_gpu(sections.get("GPU", "")))

        self._online = True
        return metrics

    def _all_offline(self) -> dict[str, MetricValue]:
        """Return all metrics as unavailable when host is offline."""
        metrics: dict[str, MetricValue] = {}
        unavailable_keys = [
            # CPU
            "cpu.usage", "cpu.per_core", "cpu.temp", "cpu.mb_temp",
            "cpu.fan", "cpu.fan_count", "cpu.fans_cpu", "cpu.fans_case",
            "cpu.core_count", "cpu.model",
            # Memory
            "mem.ram_used", "mem.ram_total", "mem.ram_percent",
            "mem.swap_used", "mem.swap_total", "mem.swap_percent",
            # Disk
            "disk.root_used", "disk.root_total", "disk.root_percent",
            "disk.home_used", "disk.home_total", "disk.home_percent",
            # Network
            "net.ip", "net.mac", "net.speed", "net.dl", "net.ul",
            # System
            "sys.hostname", "sys.uptime",
            # GPU
            "gpu.name", "gpu.temp", "gpu.fan", "gpu.power",
            "gpu.vram_used", "gpu.vram_total", "gpu.vram_percent",
            "gpu.util", "gpu.mem_util", "gpu.clock_gpu", "gpu.clock_mem",
        ]
        for key in unavailable_keys:
            metrics[key] = MetricValue(available=False)
        metrics["gpu.processes"] = MetricValue(value=[])
        metrics["cpu.fans_cpu"] = MetricValue(value=[], extra={"count": 0})
        metrics["cpu.fans_case"] = MetricValue(value=[], extra={"count": 0})
        metrics["cpu.fan_count"] = MetricValue(value=0)
        return metrics
