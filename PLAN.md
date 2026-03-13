# Implementation Plan

## Phase 1: Project Scaffold + Core Infrastructure

### Step 1.1: Create project skeleton
- Create directory structure as specified in CLAUDE.md
- Create `pyproject.toml` with dependencies (paramiko, pyyaml)
- Create `setup.py` fallback
- Create `appliance/__init__.py`
- Create `config.example.yaml`

### Step 1.2: Copy shared files from chiketi
Copy these files verbatim from `/home/rohan/projects/chiketi/chiketi/`:
- `themes.py` → `appliance/themes.py`
- `panel_spec.py` → `appliance/panel_spec.py`
- `collectors/base.py` → `appliance/collectors/base.py`
- `config.py` → `appliance/config.py`
- `assets/fonts/*` → `appliance/assets/fonts/` (all .ttf and .txt files)

### Step 1.3: Create host configuration module
Create `appliance/hosts.py`:
- `HostConfig` dataclass: name, host, port (default 22), user, key path, password_env
- `ApplianceConfig` dataclass: list of HostConfig, display settings, server settings
- `load_config(path: str) -> ApplianceConfig` — parse YAML config file
- `default_config_path()` — returns `~/.config/chiketi-appliance/config.yaml`
- Validate config on load (required fields, file existence for keys)

---

## Phase 2: SSH Remote Collection

### Step 2.1: Create SSH command definitions
Create `appliance/collectors/ssh_commands.py`:
- `COMBINED_COMMAND` — the single shell command string with `===SECTION===` markers
- `split_sections(output: str) -> dict[str, str]` — split raw output by markers
- Parser functions for each section, each returning `dict[str, MetricValue]`:
  - `parse_cpu_stat(section: str, prev_stat: dict | None) -> tuple[dict, dict]` — returns (metrics, new_prev_stat)
  - `parse_cpu_info(section: str) -> dict`
  - `parse_memory(section: str) -> dict`
  - `parse_disk(section: str) -> dict`
  - `parse_network(section: str, prev_net: dict | None) -> tuple[dict, dict]` — returns (metrics, new_prev_net)
  - `parse_net_addr(section: str) -> dict` — IP and MAC
  - `parse_temps(section: str) -> dict`
  - `parse_fans(section: str) -> dict`
  - `parse_uptime(section: str) -> dict` — hostname + uptime
  - `parse_gpu(section: str) -> dict` — nvidia-smi CSV output

### Step 2.2: Create RemoteCollector
Create `appliance/collectors/remote.py`:
- `RemoteCollector(MetricCollector)`:
  - `__init__(self, host_config: HostConfig)` — store config, init paramiko client
  - `connect(self)` — open SSH connection with key/password/agent
  - `disconnect(self)` — close SSH connection
  - `is_connected(self) -> bool`
  - `collect(self) -> dict[str, MetricValue]` — execute combined command, parse all sections
  - Internal state: `_prev_cpu_stat`, `_prev_net_bytes`, `_prev_time` for delta calculations
  - Reconnect logic: if SSH fails, mark offline, try reconnect after 30s
  - Keepalive: set `Transport.set_keepalive(30)`

### Step 2.3: Create collector registry
Create `appliance/collectors/registry.py`:
- `create_collectors(config: ApplianceConfig) -> list[RemoteCollector]`
- One RemoteCollector per host in config
- Also create `appliance/collectors/__init__.py`

### Step 2.4: Test SSH collection standalone
Create a temporary test script that:
- Loads config from config.example.yaml
- Creates a RemoteCollector for one host
- Connects and collects metrics
- Prints all metrics in a readable format
- Verify metric keys match chiketi's format exactly

---

## Phase 3: MetricEngine + App Core

### Step 3.1: Create MetricEngine
Create `appliance/app.py`:
- `MetricEngine(threading.Thread)`:
  - Takes list of RemoteCollectors
  - Runs collection cycle every 1.5s
  - Stores latest metrics per host: `dict[str, dict[str, MetricValue]]`
  - `get_latest(host_name: str) -> dict[str, MetricValue]`
  - `get_host_status() -> list[dict]` — name, online, latency_ms per host
  - `get_active_host() -> str`
  - `set_active_host(name: str)`
- `run()` function:
  - Load config
  - Create collectors and connect SSH (parallel with ThreadPoolExecutor)
  - Start MetricEngine
  - Start HTTP server
  - Launch Chromium (DisplayManager from chiketi, copied)
  - Handle SIGTERM for clean shutdown

### Step 3.2: Copy and adapt DisplayManager
Copy the DisplayManager class and helper functions from chiketi's app.py:
- `_find_chromium()`, `_is_wayland()`, `_get_graphical_session_env()`, `_detect_display()`, `_read_env_from_proc()`
- `DisplayManager` class (unchanged)
- These go in `appliance/app.py`

---

## Phase 4: HTTP Server

### Step 4.1: Copy server.py from chiketi
Copy `/home/rohan/projects/chiketi/chiketi/server.py` → `appliance/server.py`

Modifications needed:
1. Change imports from `chiketi.*` to `appliance.*`
2. Change `from chiketi.app import get_display_manager` → `from appliance.app import get_display_manager`
3. Add `_active_host: str = ""` module-level variable
4. Add `_host_status_getter = None` — function to get host status list

### Step 4.2: Add host API endpoints
In `appliance/server.py`, add to `do_GET`:
- `GET /api/hosts` → returns host list with status + active host

In `do_POST`:
- `POST /api/host/{name}` → switch active host

Modify `_serialize_metrics()` to return only the active host's metrics.

### Step 4.3: Add host selector to display page
In the display page HTML/JS (within server.py):
- If multiple hosts configured, show a small host bar at bottom of screen
- Host name labels, active host highlighted
- Auto-cycle hosts if `host_rotate` is enabled
- Keyboard shortcut: H to cycle hosts

### Step 4.4: Add host management to control panel
In the control panel HTML/JS (within server.py):
- New "Hosts" section in settings tab
- List of hosts with online/offline indicator (green/red dot)
- Click host to switch
- Show SSH connection latency

---

## Phase 5: CLI Entry Point

### Step 5.1: Create __main__.py
Create `appliance/__main__.py`:
- argparse with:
  - `--config PATH` — config YAML file (default: `~/.config/chiketi-appliance/config.yaml`)
  - `--theme NAME` — override theme from config
  - `--rotate-interval N` — override rotate interval
  - `--host HOST` — add a quick host (format: `user@host` or `user@host:port`)
  - `--key PATH` — SSH key for --host quick-add
- If no config file and no --host, print usage help
- Support quick single-host mode: `chiketi-appliance --host rohan@192.168.1.50 --key ~/.ssh/id_rsa`

---

## Phase 6: Installation + Packaging

### Step 6.1: Create install script
Create `scripts/install.sh`:
- Same structure as chiketi's installer
- Additional deps: none (paramiko installs via pip)
- Creates default config directory `~/.config/chiketi-appliance/`
- Copies config.example.yaml to default location if no config exists

### Step 6.2: Create SSH setup helper
Create `scripts/setup-ssh.sh`:
- Generates SSH key pair if none exists
- Copies public key to remote host (ssh-copy-id)
- Tests connection
- Adds host entry to config.yaml
- Usage: `./setup-ssh.sh rohan@192.168.1.50`

---

## Phase 7: Testing + Polish

### Step 7.1: Test with real hosts
- Test against 192.168.16.175 (wooster@Ubuntu 25.10)
- Verify all metrics match chiketi's local collection
- Test host offline/online transitions
- Test multi-host rotation

### Step 7.2: Pi-specific testing
- Test on actual Pi 3 hardware
- Verify RAM usage stays under 600MB
- Verify Chromium kiosk works on Pi's HDMI
- Test auto-start on boot

### Step 7.3: Create GitHub repo
- `gh repo create rohanprakash12/chiketi-appliance --public`
- Push code
- Create README.md with install instructions

---

## Implementation Order Summary

1. **Phase 1** — Scaffold + copies (30 min)
2. **Phase 2** — SSH collection, the hard part (2-3 hours)
3. **Phase 3** — MetricEngine + app (1 hour)
4. **Phase 4** — Server modifications (1-2 hours)
5. **Phase 5** — CLI (30 min)
6. **Phase 6** — Install scripts (30 min)
7. **Phase 7** — Testing (1-2 hours)

Total: ~6-8 hours of focused implementation.
