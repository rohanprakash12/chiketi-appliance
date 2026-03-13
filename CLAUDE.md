# Chiketi Appliance — Build Instructions for Claude

## What This Is

**chiketi-appliance** is a standalone Raspberry Pi application that monitors one or more remote Linux servers via SSH and displays their system stats on a local dashboard. It is a companion product to [chiketi](https://github.com/rohanprakash12/chiketi) — the original runs locally on the monitored machine, while the appliance runs on a separate Pi and pulls stats from remote servers.

The appliance reuses chiketi's entire rendering stack (HTML/CSS/JS themes served via HTTP, displayed in Chromium kiosk) but replaces the local psutil-based collectors with SSH-based remote collectors.

## Architecture

```
┌──────────────────────┐         SSH          ┌───────────────────┐
│   Raspberry Pi 3+    │ ──────────────────── │  Remote Server 1  │
│                      │         SSH          │  (any Linux box)  │
│  chiketi-appliance   │ ──────────────────── ├───────────────────┤
│                      │         SSH          │  Remote Server 2  │
│  ┌────────────────┐  │ ──────────────────── ├───────────────────┤
│  │ RemoteCollector │  │                     │  Remote Server N  │
│  │ (paramiko SSH)  │  │                     └───────────────────┘
│  └───────┬────────┘  │
│          │ metrics    │
│  ┌───────▼────────┐  │
│  │ MetricEngine   │  │
│  │ (aggregates)   │  │
│  └───────┬────────┘  │
│          │ JSON       │
│  ┌───────▼────────┐  │
│  │ HTTP Server    │──┼──→ :7777/display (Chromium kiosk on Pi)
│  │ (themes + API) │──┼──→ :7777/ (control panel, phone/laptop)
│  └────────────────┘  │
└──────────────────────┘
```

## How It Differs From chiketi

| Aspect | chiketi (original) | chiketi-appliance |
|--------|-------------------|-------------------|
| Runs on | The server being monitored | A separate Pi |
| Collection | Local psutil | SSH into remote servers |
| Dependencies on remote | N/A | Just SSH access (no install needed) |
| Multi-server | No (single machine) | Yes (multiple remotes) |
| Display | Chromium on same machine | Chromium on Pi's HDMI |

## What the Remote Servers Need

**Nothing installed.** The appliance SSHes in and runs standard Linux commands:
- `/proc/stat`, `/proc/cpuinfo` — CPU usage, core count, model name
- `free -b` — RAM and swap
- `df -B1` — disk usage
- `/proc/net/dev` — network throughput (delta-based)
- `/proc/uptime` — uptime
- `hostname` — hostname
- `/sys/class/thermal/thermal_zone*/temp` — CPU temperature
- `/sys/class/hwmon/*/fan*_input` — fan speeds
- `nvidia-smi --query-gpu=...` — GPU stats (if NVIDIA GPU present)
- `sensors` — temperature and fan data (if lm-sensors installed)

All commands are read-only. The SSH user needs no special privileges (except reading /sys/class files, which are world-readable on most distros).

## Project Structure

```
chiketi-appliance/
├── CLAUDE.md                  ← you are here
├── BLUEPRINT.md               ← high-level architecture doc
├── PLAN.md                    ← step-by-step implementation plan
├── TASKS.md                   ← checklist of all tasks
├── pyproject.toml             ← package metadata + dependencies
├── setup.py                   ← fallback for older pip
├── appliance/
│   ├── __init__.py
│   ├── __main__.py            ← CLI entry point
│   ├── app.py                 ← MetricEngine + HTTP server + Chromium launcher
│   ├── config.py              ← timing, display dimensions, thresholds
│   ├── themes.py              ← COPY from chiketi/themes.py (identical)
│   ├── panel_spec.py          ← COPY from chiketi/panel_spec.py (identical)
│   ├── server.py              ← HTTP server with HTML/JS renderers (COPY from chiketi, modified)
│   ├── assets/
│   │   └── fonts/             ← COPY from chiketi/assets/fonts/ (identical)
│   ├── collectors/
│   │   ├── __init__.py
│   │   ├── base.py            ← MetricValue + MetricCollector ABC (COPY from chiketi)
│   │   ├── remote.py          ← NEW: SSH-based remote collector (replaces all local collectors)
│   │   ├── registry.py        ← NEW: returns RemoteCollector instances per configured host
│   │   └── ssh_commands.py    ← NEW: all SSH command strings and output parsers
│   └── hosts.py               ← NEW: host configuration management (YAML config)
├── config.example.yaml        ← example multi-host configuration
└── scripts/
    ├── install.sh             ← Pi installer (system deps + pipx install)
    └── setup-ssh.sh           ← helper to set up SSH keys to remote hosts
```

## Key Design Decisions

### 1. Paramiko for SSH (not subprocess ssh)
Use the `paramiko` Python library for SSH connections. Reasons:
- Connection pooling — keep persistent SSH connections open, don't reconnect every 1.5s
- Key management — supports key files, agent forwarding, password auth
- No shell escaping issues
- Works on any platform

### 2. One RemoteCollector per host
Each remote host gets its own `RemoteCollector` instance with its own SSH connection.
The collector runs all commands in a single SSH session per collection cycle.
Metrics are namespaced by host: `server1.cpu.usage`, `server1.mem.ram_percent`, etc.

### 3. MetricValue format is identical to chiketi
The `MetricValue` dataclass and metric key format are exactly the same:
```python
@dataclass
class MetricValue:
    value: Any = None
    unit: str = ""
    available: bool = True
    extra: dict[str, Any] = field(default_factory=dict)
```

Metric keys follow `namespace.metric_name` pattern:
- `cpu.usage` → float (percent)
- `cpu.per_core` → list[float]
- `cpu.temp` → int (°C)
- `cpu.mb_temp` → int (°C)
- `cpu.fans_cpu` → list[float] (RPM values)
- `cpu.fans_case` → list[float] (RPM values)
- `cpu.fan_count` → int
- `cpu.fan` → float (first fan RPM)
- `mem.ram_used` → float (GiB), extra: {total, percent}
- `mem.ram_total` → float (GiB)
- `mem.ram_percent` → float (%)
- `mem.swap_used` → float (GiB), extra: {total, percent}
- `mem.swap_total` → float (GiB)
- `mem.swap_percent` → float (%)
- `disk.root_used` → float (GiB/TiB), extra: {total, percent}
- `disk.root_total` → float (GiB/TiB)
- `disk.root_percent` → float (%)
- `disk.home_used` → float (GiB/TiB), extra: {total, percent}
- `disk.home_total` → float (GiB/TiB)
- `disk.home_percent` → float (%)
- `net.ip` → str
- `net.mac` → str
- `net.speed` → int (Mbps)
- `net.dl` → float, unit: KB/s or MB/s, extra: {raw_bytes_per_sec}
- `net.ul` → float, unit: KB/s or MB/s, extra: {raw_bytes_per_sec}
- `sys.hostname` → str
- `sys.uptime` → str ("3d 5h 22m"), extra: {seconds}
- `gpu.name` → str
- `gpu.temp` → int (°C)
- `gpu.fan` → int (%)
- `gpu.power` → int (W), extra: {limit}
- `gpu.vram_used` → int (MiB), extra: {total, percent}
- `gpu.vram_total` → int (MiB)
- `gpu.vram_percent` → float (%)
- `gpu.util` → int (%)
- `gpu.mem_util` → int (%)
- `gpu.clock_gpu` → int (MHz), extra: {max}
- `gpu.clock_mem` → int (MHz), extra: {max}
- `gpu.processes` → list[dict] with {pid, name, vram_mib}

For multi-server, the MetricEngine merges all hosts into a single dict.
When viewing a specific host, the UI filters by host prefix.
The `/api/metrics` endpoint returns the current active host's metrics (no prefix).
A new `/api/hosts` endpoint returns the list of configured hosts and their status.

### 4. Server modifications for multi-host
The server.py from chiketi needs these additions:
- `GET /api/hosts` → list of configured hosts with online/offline status
- `POST /api/host/{name}` → switch active host (which host's stats to display)
- The display page gets a host selector (small bar at top or bottom)
- Auto-rotate can cycle through hosts as well as screens

### 5. Configuration via YAML
```yaml
# config.yaml
hosts:
  - name: "gpu-server"
    host: 192.168.1.50
    user: rohan
    key: ~/.ssh/id_rsa
    # port: 22  (default)

  - name: "web-server"
    host: 192.168.1.51
    user: deploy
    key: ~/.ssh/id_rsa

  - name: "nas"
    host: 192.168.1.52
    user: admin
    password_env: NAS_PASSWORD  # read from environment variable

display:
  theme: Panel/Gold
  rotate_interval: 10
  host_rotate: true        # cycle through hosts
  host_rotate_interval: 30 # seconds per host

server:
  port: 7777
  bind: 0.0.0.0
```

### 6. SSH Command Parsing
All remote commands and their parsers must be in `ssh_commands.py`. This is the most critical file — it translates raw Linux command output into MetricValue dicts. Each parser function:
- Takes raw stdout string as input
- Returns `dict[str, MetricValue]`
- Never raises — catches all exceptions internally and returns `available=False`

## SSH Commands Reference

### CPU Usage (from /proc/stat)
```bash
cat /proc/stat | head -1 && sleep 0.1 && cat /proc/stat | head -1
```
Parse two readings of `/proc/stat` line 1 to compute CPU usage delta.
Format: `cpu user nice system idle iowait irq softirq steal guest guest_nice`
Usage = 100 * (1 - (idle_delta / total_delta))

For per-core, read all `cpu0`, `cpu1`, etc. lines.

Alternative (simpler, single command):
```bash
top -bn1 | grep "Cpu(s)" | awk '{print $2}'
```

### CPU Info
```bash
nproc && grep "model name" /proc/cpuinfo | head -1 | cut -d: -f2
```

### CPU Temperature
```bash
cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null
```
Values are in millidegrees (divide by 1000). Take the first/highest.

If `sensors` is available:
```bash
sensors -j 2>/dev/null
```
Returns JSON with all temp and fan readings.

### Memory
```bash
free -b | grep -E "^(Mem|Swap):"
```
Parse: `Mem: total used free shared buff/cache available`

### Disk
```bash
df -B1 --output=target,size,used,pcent / /home 2>/dev/null
```

### Network
```bash
cat /proc/net/dev
```
Parse the default route interface's bytes received/sent. Need two readings for delta.
```bash
ip route | grep default | awk '{print $5}'
```
Get default interface name, then read its line from /proc/net/dev.

For IP and MAC:
```bash
ip -4 addr show $(ip route | grep default | awk '{print $5}') | grep -oP '(?<=inet\s)\d+(\.\d+){3}'
ip link show $(ip route | grep default | awk '{print $5}') | grep -oP '(?<=ether\s)[a-f0-9:]+'
```

### Hostname and Uptime
```bash
hostname && cat /proc/uptime
```
Uptime first field = seconds since boot.

### Fan Speeds
```bash
cat /sys/class/hwmon/*/fan*_input 2>/dev/null
```
Each file contains RPM as integer.

### GPU (NVIDIA only)
```bash
nvidia-smi --query-gpu=name,temperature.gpu,fan.speed,power.draw,power.limit,memory.used,memory.total,utilization.gpu,utilization.memory,clocks.gr,clocks.max.gr,clocks.mem,clocks.max.mem --format=csv,noheader,nounits 2>/dev/null
```

### Combined Command (optimize for single SSH roundtrip)
Run ALL commands in a single SSH exec to minimize latency:
```bash
echo "===CPU_STAT===" && cat /proc/stat && \
echo "===CPU_INFO===" && nproc && grep "model name" /proc/cpuinfo | head -1 && \
echo "===MEMORY===" && free -b && \
echo "===DISK===" && df -B1 --output=target,size,used,pcent / /home 2>/dev/null && \
echo "===NETWORK===" && cat /proc/net/dev && \
echo "===NET_ROUTE===" && ip route 2>/dev/null && \
echo "===NET_ADDR===" && ip -4 addr show 2>/dev/null && \
echo "===NET_LINK===" && ip link show 2>/dev/null && \
echo "===TEMPS===" && cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null && \
echo "===FANS===" && cat /sys/class/hwmon/*/fan*_input 2>/dev/null && \
echo "===UPTIME===" && hostname && cat /proc/uptime && \
echo "===GPU===" && nvidia-smi --query-gpu=name,temperature.gpu,fan.speed,power.draw,power.limit,memory.used,memory.total,utilization.gpu,utilization.memory,clocks.gr,clocks.max.gr,clocks.mem,clocks.max.mem --format=csv,noheader,nounits 2>/dev/null && \
echo "===END==="
```

Split output by `===SECTION===` markers and parse each section independently.

## Files to Copy From chiketi

These files should be copied verbatim from `/home/rohan/projects/chiketi/chiketi/`:
1. `themes.py` → `appliance/themes.py`
2. `panel_spec.py` → `appliance/panel_spec.py`
3. `collectors/base.py` → `appliance/collectors/base.py`
4. `assets/fonts/*` → `appliance/assets/fonts/`
5. `config.py` → `appliance/config.py`

These files should be copied and modified:
1. `server.py` → `appliance/server.py` — add `/api/hosts`, host switching, host selector UI
2. `app.py` → `appliance/app.py` — use RemoteCollector instead of local collectors, load YAML config
3. `__main__.py` → `appliance/__main__.py` — add `--config` flag for YAML config path

## Reference: chiketi Source Location

The original chiketi codebase is at: `/home/rohan/projects/chiketi/`

Key files for reference:
- `/home/rohan/projects/chiketi/chiketi/server.py` — full HTTP server + HTML/JS renderers (~2800 lines)
- `/home/rohan/projects/chiketi/chiketi/app.py` — MetricEngine + DisplayManager + Chromium launcher
- `/home/rohan/projects/chiketi/chiketi/themes.py` — all theme definitions
- `/home/rohan/projects/chiketi/chiketi/panel_spec.py` — panel color specifications
- `/home/rohan/projects/chiketi/chiketi/config.py` — timing and display constants
- `/home/rohan/projects/chiketi/chiketi/collectors/base.py` — MetricValue dataclass
- `/home/rohan/projects/chiketi/chiketi/collectors/cpu.py` — reference for metric keys
- `/home/rohan/projects/chiketi/chiketi/collectors/memory.py` — reference for metric keys
- `/home/rohan/projects/chiketi/chiketi/collectors/disk.py` — reference for metric keys
- `/home/rohan/projects/chiketi/chiketi/collectors/network.py` — reference for metric keys
- `/home/rohan/projects/chiketi/chiketi/collectors/gpu_nvidia.py` — reference for metric keys
- `/home/rohan/projects/chiketi/chiketi/collectors/system.py` — reference for metric keys

## GitHub Repository

Create a new repo: `rohanprakash12/chiketi-appliance`
The original chiketi repo: `https://github.com/rohanprakash12/chiketi`

## Test Hosts

For development/testing, these hosts are available on the local network:
- **192.168.16.66** — Windows machine (user: rohan) — won't work for Linux stats
- **192.168.16.175** — Ubuntu 25.10 Wayland (user: wooster) — good test target

## Dependencies

```toml
[project]
name = "chiketi-appliance"
version = "0.1.0"
description = "Remote system monitoring dashboard for Raspberry Pi"
requires-python = ">=3.11"
license = {text = "MIT"}
dependencies = [
    "paramiko>=3.0",
    "pyyaml>=6.0",
]

[project.scripts]
chiketi-appliance = "appliance.__main__:main"
```

## Critical Implementation Notes

1. **SSH connection persistence**: Open paramiko SSH connections once and reuse them. Reconnect on failure with exponential backoff. Do NOT open a new SSH connection every collection cycle (1.5s) — that would be too slow and wasteful.

2. **Single combined command**: Run ALL stat commands as one big shell command per SSH exec (see combined command above). Parse the output by section markers. This gives one SSH roundtrip per host per collection cycle.

3. **Network throughput needs two readings**: For delta-based network throughput, store the previous `/proc/net/dev` reading and compute bytes/sec on the next cycle. First reading returns 0.

4. **CPU usage needs two readings**: Same as network — store previous `/proc/stat` and compute delta. First reading returns 0.

5. **Graceful degradation**: If a section (GPU, fans, temps) fails or returns empty, set those metrics to `available=False`. Never crash the whole collector because one section fails.

6. **Host offline handling**: If SSH connection fails, mark all metrics for that host as `available=False` and show "OFFLINE" in the UI. Retry connection every 30 seconds.

7. **The display page and control panel HTML/JS are in server.py**: The server.py file is ~2800 lines because it contains all HTML, CSS, and JavaScript inline. When copying it, you must keep all the screen renderer functions intact — they are what make the dashboard look good.

8. **Metric keys must match exactly**: The JS renderers in server.py access metrics by key (e.g., `m('cpu.usage')`, `m('mem.ram_percent')`). The RemoteCollector must produce the exact same keys as the original local collectors. Read the original collectors carefully.

9. **Pi 3 has 1GB RAM**: Chromium uses 200-400MB. Keep Python memory usage low. Don't buffer large amounts of SSH output.

10. **Fonts are served via HTTP**: The font files in `assets/fonts/` are served by the HTTP server at `/assets/fonts/*`. Copy them from chiketi.
