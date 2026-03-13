"""SSH command strings and output parsers for remote metric collection.

Every parser function catches all exceptions internally and returns
MetricValue(available=False) on failure — never raises.
"""

from __future__ import annotations

import re
import time
from typing import Any

from appliance.collectors.base import MetricValue


# ---------------------------------------------------------------------------
# Combined SSH command — single exec, split output by section markers
# ---------------------------------------------------------------------------

COMBINED_COMMAND = (
    'echo "===CPU_STAT===" && cat /proc/stat && '
    'echo "===CPU_INFO===" && nproc && grep "model name" /proc/cpuinfo | head -1 | cut -d: -f2 && '
    'echo "===MEMORY===" && free -b && '
    'echo "===DISK===" && df -B1 --output=target,size,used,pcent / /home 2>/dev/null && '
    'echo "===NETWORK===" && cat /proc/net/dev && '
    'echo "===NET_ROUTE===" && ip route 2>/dev/null && '
    'echo "===NET_ADDR===" && ip -4 addr show 2>/dev/null && '
    'echo "===NET_LINK===" && ip link show 2>/dev/null && '
    'echo "===NET_SPEED===" && cat /sys/class/net/$(ip route 2>/dev/null | grep default | head -1 | awk \'{print $5}\')/speed 2>/dev/null ; '
    'echo "===TEMPS===" && cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null ; '
    'echo "===FANS===" && cat /sys/class/hwmon/*/fan*_input 2>/dev/null ; '
    'echo "===UPTIME===" && hostname && cat /proc/uptime ; '
    'echo "===GPU===" && nvidia-smi --query-gpu=name,temperature.gpu,fan.speed,'
    'power.draw,power.limit,memory.used,memory.total,utilization.gpu,'
    'utilization.memory,clocks.gr,clocks.max.gr,clocks.mem,clocks.max.mem '
    '--format=csv,noheader,nounits 2>/dev/null ; '
    'echo "===END==="'
)


def split_sections(output: str) -> dict[str, str]:
    """Split combined command output into named sections."""
    sections: dict[str, str] = {}
    markers = [
        "CPU_STAT", "CPU_INFO", "MEMORY", "DISK", "NETWORK",
        "NET_ROUTE", "NET_ADDR", "NET_LINK", "NET_SPEED", "TEMPS", "FANS",
        "UPTIME", "GPU", "END",
    ]
    for i, marker in enumerate(markers[:-1]):
        start_tag = f"==={marker}==="
        end_tag = f"==={markers[i + 1]}==="
        start_idx = output.find(start_tag)
        end_idx = output.find(end_tag)
        if start_idx != -1 and end_idx != -1:
            content = output[start_idx + len(start_tag):end_idx].strip()
            sections[marker] = content
        else:
            sections[marker] = ""
    return sections


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gib(b: int | float) -> float:
    return round(b / (1024**3), 1)


def _tib(b: int | float) -> float:
    return round(b / (1024**4), 2)


def _format_rate(bytes_per_sec: float) -> tuple[float, str]:
    """Return (value, unit) for a byte rate."""
    if bytes_per_sec >= 1_000_000:
        return round(bytes_per_sec / 1_000_000, 1), "MB/s"
    if bytes_per_sec >= 1_000:
        return round(bytes_per_sec / 1_000, 1), "KB/s"
    return round(bytes_per_sec), "B/s"


# ---------------------------------------------------------------------------
# /proc/stat types for delta tracking
# ---------------------------------------------------------------------------

CpuStat = list[list[int]]  # list of per-line totals: [[user, nice, system, idle, ...], ...]


def _parse_proc_stat_lines(text: str) -> CpuStat:
    """Parse /proc/stat lines into lists of ints.

    Returns list where index 0 = aggregate 'cpu' line, index 1+ = per-core.
    """
    result: CpuStat = []
    for line in text.strip().splitlines():
        if line.startswith("cpu"):
            parts = line.split()
            # parts[0] is 'cpu' or 'cpu0' etc, rest are numbers
            try:
                vals = [int(x) for x in parts[1:]]
                result.append(vals)
            except (ValueError, IndexError):
                pass
    return result


def _cpu_usage_from_delta(prev: list[int], curr: list[int]) -> float:
    """Compute CPU usage % from two /proc/stat readings."""
    if len(prev) < 4 or len(curr) < 4:
        return 0.0
    prev_idle = prev[3] + (prev[4] if len(prev) > 4 else 0)  # idle + iowait
    curr_idle = curr[3] + (curr[4] if len(curr) > 4 else 0)
    prev_total = sum(prev)
    curr_total = sum(curr)
    total_d = curr_total - prev_total
    idle_d = curr_idle - prev_idle
    if total_d <= 0:
        return 0.0
    return round(100.0 * (1.0 - idle_d / total_d), 1)


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def parse_cpu_stat(
    section: str,
    prev_stat: CpuStat | None,
) -> tuple[dict[str, MetricValue], CpuStat | None]:
    """Parse /proc/stat output for CPU usage.

    Returns (metrics_dict, new_prev_stat).
    Delta-based: first call with prev_stat=None returns 0%.
    """
    metrics: dict[str, MetricValue] = {}
    try:
        curr_stat = _parse_proc_stat_lines(section)
        if not curr_stat:
            metrics["cpu.usage"] = MetricValue(available=False, unit="%")
            metrics["cpu.per_core"] = MetricValue(available=False)
            return metrics, None

        if prev_stat is not None and len(prev_stat) > 0:
            # Aggregate usage
            usage = _cpu_usage_from_delta(prev_stat[0], curr_stat[0])
            metrics["cpu.usage"] = MetricValue(value=usage, unit="%")

            # Per-core usage
            per_core: list[float] = []
            for i in range(1, min(len(prev_stat), len(curr_stat))):
                core_usage = _cpu_usage_from_delta(prev_stat[i], curr_stat[i])
                per_core.append(core_usage)
            metrics["cpu.per_core"] = MetricValue(value=per_core, unit="%")
        else:
            metrics["cpu.usage"] = MetricValue(value=0.0, unit="%")
            metrics["cpu.per_core"] = MetricValue(value=[], unit="%")

        return metrics, curr_stat
    except Exception:
        metrics["cpu.usage"] = MetricValue(available=False, unit="%")
        metrics["cpu.per_core"] = MetricValue(available=False)
        return metrics, prev_stat


def parse_cpu_info(section: str) -> dict[str, MetricValue]:
    """Parse nproc + model name output."""
    metrics: dict[str, MetricValue] = {}
    try:
        lines = section.strip().splitlines()
        if lines:
            try:
                core_count = int(lines[0].strip())
                metrics["cpu.core_count"] = MetricValue(value=core_count)
            except (ValueError, IndexError):
                metrics["cpu.core_count"] = MetricValue(available=False)

            if len(lines) > 1:
                model = lines[1].strip().lstrip(":").strip()
                metrics["cpu.model"] = MetricValue(value=model)
            else:
                metrics["cpu.model"] = MetricValue(available=False)
        else:
            metrics["cpu.core_count"] = MetricValue(available=False)
            metrics["cpu.model"] = MetricValue(available=False)
    except Exception:
        metrics["cpu.core_count"] = MetricValue(available=False)
        metrics["cpu.model"] = MetricValue(available=False)
    return metrics


def parse_memory(section: str) -> dict[str, MetricValue]:
    """Parse `free -b` output for RAM and swap."""
    metrics: dict[str, MetricValue] = {}
    try:
        mem_parsed = False
        swap_parsed = False
        for line in section.strip().splitlines():
            parts = line.split()
            if not parts:
                continue
            label = parts[0].rstrip(":")

            if label == "Mem" and len(parts) >= 7:
                total = int(parts[1])
                used = int(parts[2])
                available = int(parts[6])
                # Match psutil: percent = (total - available) / total * 100
                percent = round((total - available) / total * 100, 1) if total > 0 else 0.0
                metrics["mem.ram_used"] = MetricValue(
                    value=_gib(used), unit="GiB",
                    extra={"total": _gib(total), "percent": percent},
                )
                metrics["mem.ram_total"] = MetricValue(value=_gib(total), unit="GiB")
                metrics["mem.ram_percent"] = MetricValue(value=percent, unit="%")
                mem_parsed = True

            elif label == "Swap" and len(parts) >= 4:
                total = int(parts[1])
                used = int(parts[2])
                percent = round(used / total * 100, 1) if total > 0 else 0.0
                metrics["mem.swap_used"] = MetricValue(
                    value=_gib(used), unit="GiB",
                    extra={"total": _gib(total), "percent": percent},
                )
                metrics["mem.swap_total"] = MetricValue(value=_gib(total), unit="GiB")
                metrics["mem.swap_percent"] = MetricValue(value=percent, unit="%")
                swap_parsed = True

        if not mem_parsed:
            for k in ("ram_used", "ram_total", "ram_percent"):
                metrics[f"mem.{k}"] = MetricValue(available=False)
        if not swap_parsed:
            for k in ("swap_used", "swap_total", "swap_percent"):
                metrics[f"mem.{k}"] = MetricValue(available=False)

    except Exception:
        for k in ("ram_used", "ram_total", "ram_percent",
                   "swap_used", "swap_total", "swap_percent"):
            metrics[f"mem.{k}"] = MetricValue(available=False)
    return metrics


def parse_disk(section: str) -> dict[str, MetricValue]:
    """Parse `df -B1 --output=target,size,used,pcent / /home` output."""
    metrics: dict[str, MetricValue] = {}
    try:
        found_mounts: set[str] = set()
        # df with / and /home may produce two rows both with target "/"
        # when /home is on the same partition. Track row order to map
        # first row → root, second row → home.
        data_rows: list[list[str]] = []
        for line in section.strip().splitlines():
            # Skip header line
            if "Mounted" in line or "target" in line.lower():
                continue
            parts = line.split()
            if len(parts) < 4:
                continue
            data_rows.append(parts)

        # Map rows: if we have two rows, first is /, second is /home
        # (regardless of what mount point df reports)
        row_map: dict[str, list[str]] = {}
        if len(data_rows) >= 2:
            row_map["root"] = data_rows[0]
            row_map["home"] = data_rows[1]
        elif len(data_rows) == 1:
            row_map["root"] = data_rows[0]

        for key, parts in row_map.items():
            found_mounts.add(key)

            try:
                total = int(parts[1])
                used = int(parts[2])
                # percent from df output (strip %)
                pct_str = parts[3].rstrip("%")
                percent = float(pct_str)

                if total >= 1024**4:
                    metrics[f"disk.{key}_used"] = MetricValue(
                        value=_tib(used), unit="TiB",
                        extra={"total": _tib(total), "percent": percent},
                    )
                    metrics[f"disk.{key}_total"] = MetricValue(value=_tib(total), unit="TiB")
                else:
                    metrics[f"disk.{key}_used"] = MetricValue(
                        value=_gib(used), unit="GiB",
                        extra={"total": _gib(total), "percent": percent},
                    )
                    metrics[f"disk.{key}_total"] = MetricValue(value=_gib(total), unit="GiB")
                metrics[f"disk.{key}_percent"] = MetricValue(value=percent, unit="%")
            except (ValueError, IndexError):
                for suffix in ("used", "total", "percent"):
                    metrics[f"disk.{key}_{suffix}"] = MetricValue(available=False)

        # Mark missing mounts as unavailable
        for key in ("root", "home"):
            if key not in found_mounts:
                for suffix in ("used", "total", "percent"):
                    metrics[f"disk.{key}_{suffix}"] = MetricValue(available=False)

    except Exception:
        for key in ("root", "home"):
            for suffix in ("used", "total", "percent"):
                metrics[f"disk.{key}_{suffix}"] = MetricValue(available=False)
    return metrics


# Network stat type: {iface_name: (bytes_recv, bytes_sent)}
NetStat = dict[str, tuple[int, int]]


def _parse_proc_net_dev(text: str) -> NetStat:
    """Parse /proc/net/dev into {iface: (rx_bytes, tx_bytes)}."""
    result: NetStat = {}
    for line in text.strip().splitlines():
        if ":" not in line:
            continue
        iface, rest = line.split(":", 1)
        iface = iface.strip()
        parts = rest.split()
        if len(parts) >= 9:
            rx = int(parts[0])
            tx = int(parts[8])
            result[iface] = (rx, tx)
    return result


def parse_network(
    net_section: str,
    route_section: str,
    addr_section: str,
    link_section: str,
    prev_net: NetStat | None,
    prev_time: float | None,
) -> tuple[dict[str, MetricValue], NetStat | None, float | None]:
    """Parse network sections.

    Returns (metrics_dict, new_prev_net, new_time).
    """
    metrics: dict[str, MetricValue] = {}
    now = time.monotonic()

    # Determine default interface from ip route
    default_iface: str | None = None
    try:
        for line in route_section.strip().splitlines():
            if line.startswith("default"):
                parts = line.split()
                dev_idx = parts.index("dev") if "dev" in parts else -1
                if dev_idx >= 0 and dev_idx + 1 < len(parts):
                    default_iface = parts[dev_idx + 1]
                    break
    except Exception:
        pass

    # IP address
    try:
        ip: str | None = None
        if default_iface and addr_section:
            # Look for inet line in the block for default_iface
            in_iface_block = False
            for line in addr_section.strip().splitlines():
                if re.match(r"^\d+:", line):
                    iface_match = re.search(r"^\d+:\s+(\S+)", line)
                    in_iface_block = (
                        iface_match is not None
                        and iface_match.group(1).rstrip(":") == default_iface
                    )
                elif in_iface_block and "inet " in line:
                    m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", line)
                    if m:
                        ip = m.group(1)
                        break
        if ip:
            metrics["net.ip"] = MetricValue(value=ip)
        else:
            metrics["net.ip"] = MetricValue(available=False)
    except Exception:
        metrics["net.ip"] = MetricValue(available=False)

    # MAC address
    try:
        mac: str | None = None
        if default_iface and link_section:
            in_iface_block = False
            for line in link_section.strip().splitlines():
                if re.match(r"^\d+:", line):
                    iface_match = re.search(r"^\d+:\s+(\S+)", line)
                    in_iface_block = (
                        iface_match is not None
                        and iface_match.group(1).rstrip(":") == default_iface
                    )
                elif in_iface_block:
                    m = re.search(r"link/ether\s+([0-9a-fA-F:]+)", line)
                    if m:
                        mac = m.group(1).upper()
                        break
        if mac:
            metrics["net.mac"] = MetricValue(value=mac)
        else:
            metrics["net.mac"] = MetricValue(available=False)
    except Exception:
        metrics["net.mac"] = MetricValue(available=False)

    # Link speed — will be set by caller from NET_SPEED section
    metrics["net.speed"] = MetricValue(available=False, unit="Mbps")

    # Throughput (delta-based)
    try:
        curr_net = _parse_proc_net_dev(net_section)
        if default_iface and default_iface in curr_net:
            curr_rx, curr_tx = curr_net[default_iface]

            if (
                prev_net is not None
                and prev_time is not None
                and default_iface in prev_net
            ):
                prev_rx, prev_tx = prev_net[default_iface]
                dt = now - prev_time
                if dt > 0:
                    dl_rate = (curr_rx - prev_rx) / dt
                    ul_rate = (curr_tx - prev_tx) / dt
                    # Clamp negatives (counter reset)
                    dl_rate = max(0.0, dl_rate)
                    ul_rate = max(0.0, ul_rate)
                else:
                    dl_rate = ul_rate = 0.0

                dl_val, dl_unit = _format_rate(dl_rate)
                ul_val, ul_unit = _format_rate(ul_rate)

                metrics["net.dl"] = MetricValue(
                    value=dl_val, unit=dl_unit,
                    extra={"raw_bytes_per_sec": dl_rate},
                )
                metrics["net.ul"] = MetricValue(
                    value=ul_val, unit=ul_unit,
                    extra={"raw_bytes_per_sec": ul_rate},
                )
            else:
                metrics["net.dl"] = MetricValue(value=0.0, unit="B/s")
                metrics["net.ul"] = MetricValue(value=0.0, unit="B/s")

            return metrics, curr_net, now
        else:
            metrics["net.dl"] = MetricValue(value=0.0, unit="B/s")
            metrics["net.ul"] = MetricValue(value=0.0, unit="B/s")
            return metrics, curr_net if curr_net else None, now
    except Exception:
        metrics["net.dl"] = MetricValue(available=False)
        metrics["net.ul"] = MetricValue(available=False)
        return metrics, prev_net, prev_time


def parse_net_speed(section: str) -> dict[str, MetricValue]:
    """Parse link speed from /sys/class/net/<iface>/speed."""
    try:
        val = section.strip()
        if val:
            speed = int(val)
            if speed > 0:
                return {"net.speed": MetricValue(value=speed, unit="Mbps")}
    except (ValueError, Exception):
        pass
    return {"net.speed": MetricValue(available=False, unit="Mbps")}


def parse_temps(section: str) -> dict[str, MetricValue]:
    """Parse thermal zone temps (millidegrees)."""
    metrics: dict[str, MetricValue] = {}
    try:
        if not section.strip():
            metrics["cpu.temp"] = MetricValue(available=False, unit="°C")
            metrics["cpu.mb_temp"] = MetricValue(available=False, unit="°C")
            return metrics

        temps: list[int] = []
        for line in section.strip().splitlines():
            line = line.strip()
            if line:
                try:
                    val = int(line)
                    temps.append(val)
                except ValueError:
                    pass

        if temps:
            # Convert from millidegrees, take the highest as CPU temp
            cpu_temp = max(temps) // 1000
            metrics["cpu.temp"] = MetricValue(value=cpu_temp, unit="°C")

            # Second-highest (or same) as motherboard temp approximation
            if len(temps) > 1:
                sorted_temps = sorted(temps, reverse=True)
                mb_temp = sorted_temps[1] // 1000
                metrics["cpu.mb_temp"] = MetricValue(value=mb_temp, unit="°C")
            else:
                metrics["cpu.mb_temp"] = MetricValue(available=False, unit="°C")
        else:
            metrics["cpu.temp"] = MetricValue(available=False, unit="°C")
            metrics["cpu.mb_temp"] = MetricValue(available=False, unit="°C")
    except Exception:
        metrics["cpu.temp"] = MetricValue(available=False, unit="°C")
        metrics["cpu.mb_temp"] = MetricValue(available=False, unit="°C")
    return metrics


def parse_fans(section: str) -> dict[str, MetricValue]:
    """Parse fan RPMs from /sys/class/hwmon/*/fan*_input."""
    metrics: dict[str, MetricValue] = {}
    try:
        if not section.strip():
            metrics["cpu.fan"] = MetricValue(available=False, unit="RPM")
            metrics["cpu.fan_count"] = MetricValue(value=0)
            metrics["cpu.fans_cpu"] = MetricValue(value=[], extra={"count": 0})
            metrics["cpu.fans_case"] = MetricValue(value=[], extra={"count": 0})
            return metrics

        all_rpms: list[float] = []
        for line in section.strip().splitlines():
            line = line.strip()
            if line:
                try:
                    rpm = float(line)
                    all_rpms.append(rpm)
                except ValueError:
                    pass

        # Filter to only fans that are spinning (RPM > 0)
        active_rpms = [r for r in all_rpms if r > 0]

        # Convention from chiketi: first 2 are CPU, rest are case
        cpu_fans = [r for i, r in enumerate(all_rpms) if r > 0 and i < 2]
        case_fans = [r for i, r in enumerate(all_rpms) if r > 0 and i >= 2]

        metrics["cpu.fans_cpu"] = MetricValue(
            value=cpu_fans, extra={"count": len(cpu_fans)},
        )
        metrics["cpu.fans_case"] = MetricValue(
            value=case_fans, extra={"count": len(case_fans)},
        )
        metrics["cpu.fan_count"] = MetricValue(value=len(cpu_fans) + len(case_fans))

        first_rpm = (cpu_fans + case_fans + [0.0])[0]
        metrics["cpu.fan"] = MetricValue(
            value=first_rpm, unit="RPM",
            available=bool(cpu_fans or case_fans),
        )
    except Exception:
        metrics["cpu.fan"] = MetricValue(available=False, unit="RPM")
        metrics["cpu.fan_count"] = MetricValue(value=0)
        metrics["cpu.fans_cpu"] = MetricValue(value=[], extra={"count": 0})
        metrics["cpu.fans_case"] = MetricValue(value=[], extra={"count": 0})
    return metrics


def parse_uptime(section: str) -> dict[str, MetricValue]:
    """Parse hostname + /proc/uptime output."""
    metrics: dict[str, MetricValue] = {}
    try:
        lines = section.strip().splitlines()
        if not lines:
            metrics["sys.hostname"] = MetricValue(available=False)
            metrics["sys.uptime"] = MetricValue(available=False)
            return metrics

        # First line is hostname
        hostname = lines[0].strip()
        metrics["sys.hostname"] = MetricValue(value=hostname)

        if len(lines) > 1:
            # Second line is /proc/uptime: "12345.67 98765.43"
            uptime_str = lines[1].strip().split()[0]
            uptime_s = float(uptime_str)
            days = int(uptime_s // 86400)
            hours = int((uptime_s % 86400) // 3600)
            mins = int((uptime_s % 3600) // 60)
            metrics["sys.uptime"] = MetricValue(
                value=f"{days}d {hours}h {mins}m",
                extra={"seconds": uptime_s},
            )
        else:
            metrics["sys.uptime"] = MetricValue(available=False)
    except Exception:
        metrics["sys.hostname"] = MetricValue(available=False)
        metrics["sys.uptime"] = MetricValue(available=False)
    return metrics


def parse_gpu(section: str) -> dict[str, MetricValue]:
    """Parse nvidia-smi CSV output.

    Expected CSV columns (noheader, nounits):
    name, temperature.gpu, fan.speed, power.draw, power.limit,
    memory.used, memory.total, utilization.gpu, utilization.memory,
    clocks.gr, clocks.max.gr, clocks.mem, clocks.max.mem
    """
    metrics: dict[str, MetricValue] = {}

    ALL_KEYS = (
        "name", "temp", "fan", "power", "vram_used", "vram_total",
        "vram_percent", "util", "mem_util", "clock_gpu", "clock_mem",
    )

    try:
        if not section.strip():
            for k in ALL_KEYS:
                metrics[f"gpu.{k}"] = MetricValue(available=False)
            metrics["gpu.processes"] = MetricValue(value=[])
            return metrics

        # Take the first line (first GPU)
        line = section.strip().splitlines()[0]
        parts = [p.strip() for p in line.split(",")]

        if len(parts) < 13:
            for k in ALL_KEYS:
                metrics[f"gpu.{k}"] = MetricValue(available=False)
            metrics["gpu.processes"] = MetricValue(value=[])
            return metrics

        # name
        metrics["gpu.name"] = MetricValue(value=parts[0])

        # temperature
        try:
            metrics["gpu.temp"] = MetricValue(value=int(float(parts[1])), unit="°C")
        except (ValueError, IndexError):
            metrics["gpu.temp"] = MetricValue(available=False, unit="°C")

        # fan speed
        try:
            metrics["gpu.fan"] = MetricValue(value=int(float(parts[2])), unit="%")
        except (ValueError, IndexError):
            metrics["gpu.fan"] = MetricValue(available=False, unit="%")

        # power
        try:
            power = round(float(parts[3]))
            limit = round(float(parts[4]))
            metrics["gpu.power"] = MetricValue(
                value=power, unit="W", extra={"limit": limit},
            )
        except (ValueError, IndexError):
            metrics["gpu.power"] = MetricValue(available=False, unit="W")

        # vram
        try:
            used = int(float(parts[5]))
            total = int(float(parts[6]))
            percent = round(used / total * 100, 1) if total > 0 else 0.0
            metrics["gpu.vram_used"] = MetricValue(
                value=used, unit="MiB",
                extra={"total": total, "percent": percent},
            )
            metrics["gpu.vram_total"] = MetricValue(value=total, unit="MiB")
            metrics["gpu.vram_percent"] = MetricValue(value=percent, unit="%")
        except (ValueError, IndexError):
            for k in ("vram_used", "vram_total", "vram_percent"):
                metrics[f"gpu.{k}"] = MetricValue(available=False)

        # utilization
        try:
            metrics["gpu.util"] = MetricValue(value=int(float(parts[7])), unit="%")
        except (ValueError, IndexError):
            metrics["gpu.util"] = MetricValue(available=False, unit="%")

        try:
            metrics["gpu.mem_util"] = MetricValue(value=int(float(parts[8])), unit="%")
        except (ValueError, IndexError):
            metrics["gpu.mem_util"] = MetricValue(available=False, unit="%")

        # clocks
        try:
            gpu_clk = int(float(parts[9]))
            gpu_max = int(float(parts[10]))
            metrics["gpu.clock_gpu"] = MetricValue(
                value=gpu_clk, unit="MHz", extra={"max": gpu_max},
            )
        except (ValueError, IndexError):
            metrics["gpu.clock_gpu"] = MetricValue(available=False, unit="MHz")

        try:
            mem_clk = int(float(parts[11]))
            mem_max = int(float(parts[12]))
            metrics["gpu.clock_mem"] = MetricValue(
                value=mem_clk, unit="MHz", extra={"max": mem_max},
            )
        except (ValueError, IndexError):
            metrics["gpu.clock_mem"] = MetricValue(available=False, unit="MHz")

        # processes — not available via nvidia-smi CSV query
        metrics["gpu.processes"] = MetricValue(value=[])

    except Exception:
        for k in ALL_KEYS:
            metrics[f"gpu.{k}"] = MetricValue(available=False)
        metrics["gpu.processes"] = MetricValue(value=[])

    return metrics
