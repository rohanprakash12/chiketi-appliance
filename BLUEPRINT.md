# Chiketi Appliance — Blueprint

## Vision

A Raspberry Pi 3 (or later) that sits on your desk or in your server rack and displays real-time system stats from one or more remote Linux servers. No software needs to be installed on the remote servers — just SSH access.

The Pi runs a single process (`chiketi-appliance`) that:
1. SSHes into each configured remote server
2. Runs read-only Linux commands to gather CPU, memory, disk, network, GPU, temperature, and fan stats
3. Serves a web dashboard on port 7777 with the same visual themes as chiketi (Panel/Gold, Terminal/hacker, Vintage/VFD, etc.)
4. Launches Chromium in kiosk mode on the Pi's HDMI output to display the dashboard

## System Design

### Components

```
┌─────────────────────────────────────────────────────────┐
│                    chiketi-appliance                     │
│                                                         │
│  ┌─────────────┐    ┌──────────────┐    ┌───────────┐  │
│  │ HostManager │───▶│ MetricEngine │───▶│ HTTP      │  │
│  │             │    │   (thread)   │    │ Server    │  │
│  │ Per host:   │    │              │    │ :7777     │  │
│  │ ┌─────────┐ │    │ Calls each   │    │           │  │
│  │ │ Remote  │ │    │ collector    │    │ /         │──── Control panel
│  │ │Collector│ │    │ every 1.5s   │    │ /display  │──── Chromium kiosk
│  │ │ (SSH)   │ │    │              │    │ /api/*    │──── JSON API
│  │ └─────────┘ │    └──────────────┘    └───────────┘  │
│  │ ┌─────────┐ │                                       │
│  │ │ Remote  │ │    ┌──────────────┐                   │
│  │ │Collector│ │    │ Display      │                   │
│  │ │ (SSH)   │ │    │ Manager      │──── Chromium kiosk│
│  │ └─────────┘ │    │ (optional)   │                   │
│  └─────────────┘    └──────────────┘                   │
└─────────────────────────────────────────────────────────┘
         │ SSH                              │ HDMI
    ┌────▼────┐                        ┌────▼────┐
    │ Remote  │                        │ Display │
    │ Servers │                        │ (7" TFT │
    │ (Linux) │                        │  or TV) │
    └─────────┘                        └─────────┘
```

### RemoteCollector

The heart of the appliance. One instance per remote host.

**Lifecycle:**
1. On startup, open a paramiko SSH connection to the host
2. Every collection cycle (1.5s), execute a single combined shell command
3. Parse the output into MetricValue dicts using section markers
4. On SSH failure, mark host offline and retry every 30s
5. On shutdown, close all SSH connections

**Connection Management:**
- Use `paramiko.SSHClient` with `AutoAddPolicy` for host keys
- Support: key file, SSH agent, password (from env var)
- Keep connection alive with keepalive packets (every 30s)
- Connection runs in its own thread to avoid blocking other hosts

**Combined Command Strategy:**
Instead of N separate SSH commands per cycle, run ONE command that produces all stats:
```bash
echo "===SECTION===" && command1 && echo "===SECTION===" && command2 ...
```
Split output by markers, parse each section independently. If a section fails (command not found, permission denied), that section returns `available=False` metrics while others succeed.

### MetricEngine

Same as chiketi's MetricEngine but collects from multiple RemoteCollectors:
- Iterates over all collectors (one per host)
- Stores latest metrics keyed by host name
- Active host selection determines which metrics are served via API
- Thread-safe access to latest metrics dict

### HTTP Server

Copied from chiketi's server.py with these additions:

**New API endpoints:**
- `GET /api/hosts` — returns list of hosts with status:
  ```json
  {
    "hosts": [
      {"name": "gpu-server", "host": "192.168.1.50", "online": true, "latency_ms": 12},
      {"name": "web-server", "host": "192.168.1.51", "online": false, "latency_ms": null}
    ],
    "active_host": "gpu-server"
  }
  ```
- `POST /api/host/{name}` — switch which host's stats are displayed

**UI additions to display page:**
- Host selector bar (if multiple hosts configured)
- Host name shown in dashboard header
- Visual indicator for host online/offline status
- Auto-cycle through hosts if `host_rotate: true` in config

**UI additions to control panel:**
- Host list with online/offline status
- Click to switch active host
- SSH connection status per host

### DisplayManager

Copied from chiketi's app.py. Handles Chromium kiosk on the Pi's display.
Identical behavior — detect display, launch Chromium, manage process.

### Configuration

YAML file (default: `~/.config/chiketi-appliance/config.yaml`):

```yaml
hosts:
  - name: gpu-server
    host: 192.168.1.50
    user: rohan
    key: ~/.ssh/id_rsa

  - name: web-server
    host: 192.168.1.51
    user: deploy
    key: ~/.ssh/id_rsa

display:
  theme: Panel/Gold
  rotate_interval: 10
  host_rotate: true
  host_rotate_interval: 30

server:
  port: 7777
  bind: 0.0.0.0
```

CLI can override: `chiketi-appliance --config /path/to/config.yaml --theme Terminal/hacker`

## Data Flow

```
1. Startup
   └─ Load config.yaml
   └─ Create RemoteCollector per host
   └─ Open SSH connections (parallel)
   └─ Start MetricEngine thread
   └─ Start HTTP server
   └─ Launch Chromium kiosk

2. Collection Cycle (every 1.5s)
   └─ For each host (parallel):
      └─ SSH exec combined command
      └─ Parse output by section
      └─ Produce MetricValue dict
      └─ Store in MetricEngine

3. Display Cycle
   └─ Chromium polls /api/metrics every 2.5s
   └─ JS renders active host's stats
   └─ Auto-rotate screens (10s default)
   └─ Auto-rotate hosts (30s default, if enabled)

4. Control Panel
   └─ Phone/laptop at http://pi-ip:7777/
   └─ Switch themes, hosts, toggle display
```

## Error Handling

- **SSH connection refused**: Mark host offline, retry every 30s, show "OFFLINE" in UI
- **SSH auth failure**: Log error, mark host as "AUTH FAILED" in UI, don't retry (misconfigured)
- **Command timeout**: 10s timeout per SSH exec. If exceeded, use partial results from completed sections
- **Host goes down mid-session**: Paramiko raises, catch it, mark offline, schedule reconnect
- **All hosts offline**: Show "NO HOSTS ONLINE" on display with connection status per host
- **Pi loses network**: All hosts go offline, display shows status. Resume when network returns.

## Security

- SSH keys preferred over passwords
- Passwords never stored in config — use `password_env` to reference an environment variable
- Config file should be chmod 600
- Remote commands are hardcoded read-only strings — no user input in SSH commands
- Paramiko host key verification: AutoAddPolicy on first connect, then strict

## Performance Budget (Pi 3, 1GB RAM)

- Python process: ~50-80MB
- Paramiko per connection: ~5-10MB
- Chromium kiosk: 200-400MB
- Overhead: ~100MB
- **Total: ~400-600MB** — fits in 1GB with margin

- SSH roundtrip per host: ~20-50ms on LAN
- Combined command execution: ~100-200ms
- Collection cycle: 1.5s (plenty of headroom for 5-10 hosts)
