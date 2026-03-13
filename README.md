# Chiketi Appliance

A Raspberry Pi that monitors your servers. SSH in, pull stats, display them on a beautiful dashboard — no software needed on the servers.

Companion to [chiketi](https://github.com/rohanprakash12/chiketi) (which monitors the local machine). The appliance runs on a separate Pi and monitors remote servers over SSH.

<!-- Screenshots: uncomment when added
![Setup Wizard](screenshots/setup-wizard.png)
![Dashboard](screenshots/dashboard.png)
-->

## How It Works

```
┌──────────────────────┐         SSH          ┌───────────────────┐
│   Raspberry Pi 3+    │ ──────────────────── │  Remote Server 1  │
│                      │         SSH          │  (any Linux box)  │
│  chiketi-appliance   │ ──────────────────── ├───────────────────┤
│                      │         SSH          │  Remote Server 2  │
│  ┌────────────────┐  │ ──────────────────── ├───────────────────┤
│  │ SSH Collectors  │  │                     │  Remote Server N  │
│  │ (paramiko)      │  │                     └───────────────────┘
│  └───────┬────────┘  │
│          │           │
│  ┌───────▼────────┐  │
│  │ HTTP Server    │──┼──→ :7777/display (Chromium kiosk on Pi)
│  │ (themes + API) │──┼──→ :7777/ (control panel, phone/laptop)
│  └────────────────┘  │
└──────────────────────┘
```

The appliance SSHes into each server and runs read-only Linux commands (`/proc/stat`, `free`, `df`, etc.) to gather CPU, memory, disk, network, GPU, temperature, and fan stats. **Nothing needs to be installed on the remote servers** — just SSH access.

## Install

### One-liner (Raspberry Pi / Debian / Ubuntu)

```bash
curl -fsSL https://raw.githubusercontent.com/rohanprakash12/chiketi-appliance/main/scripts/install.sh | bash
```

### Manual install

```bash
git clone https://github.com/rohanprakash12/chiketi-appliance.git
cd chiketi-appliance
pip install .
```

## Getting Started

### First run — Setup Wizard

Just run it:

```bash
chiketi-appliance
```

On first launch (no config file), a **setup wizard** starts on port 7777. Open `http://<pi-ip>:7777/` on your phone or laptop and walk through:

1. **Add a server** — enter IP, username, port
2. **SSH key** — the appliance generates a key and shows you how to add it to your server
3. **Test connection** — verifies SSH works, shows hostname and uptime
4. **Add more servers** — repeat for each server you want to monitor
5. **Pick a theme** — choose from 12 themes across 3 families
6. **Start monitoring** — saves config and launches the dashboard

No YAML editing, no terminal commands — everything through the browser.

### Quick single-host mode

Skip the wizard entirely:

```bash
chiketi-appliance --host jeeves@192.168.1.50 --key ~/.ssh/id_ed25519
```

### With a config file

```bash
chiketi-appliance --config ~/.config/chiketi-appliance/config.yaml
```

## What Gets Monitored

| Category | Metrics |
|----------|---------|
| **CPU** | Usage %, per-core usage, model name, core count |
| **Memory** | RAM used/total/%, swap used/total/% |
| **Disk** | Root and /home used/total/% |
| **Network** | Download/upload throughput, IP, MAC, link speed |
| **Temperature** | CPU temp, motherboard temp |
| **Fans** | Fan RPMs (CPU and case) |
| **GPU** | Name, temp, fan, power, VRAM, utilization, clocks (NVIDIA) |
| **System** | Hostname, uptime |

All collected via a single SSH command per host per 1.5-second cycle. Metrics that aren't available (no GPU, no fans, no temp sensors) gracefully show as unavailable.

## Multi-Host Support

Monitor multiple servers from one Pi:

- **Host switching** — click to switch between servers in the dashboard or control panel
- **Auto-rotation** — cycle through hosts automatically (configurable interval)
- **Status indicators** — see which hosts are online/offline with SSH latency
- **Keyboard shortcut** — press `H` on the display page to cycle hosts

## Themes

Same 12 themes as chiketi:

### Panel
| Theme | Style |
|-------|-------|
| Gold | Gold panels, anticlockwise donuts |
| Coral | Pill headers, clockwise donuts |
| Teal | Angular headers, butt linecap donuts |

### Terminal
Six color variants: **hacker** (green), **cyan**, **amber**, **phosphor**, **red_alert**, **blue**

### Vintage
| Theme | Style |
|-------|-------|
| Scanlines | CRT scanline overlay, retro green |
| Tubes | Warm amber nixie tube aesthetic |
| VFD | Vacuum fluorescent display glow |

## Configuration

### Config file

Default location: `~/.config/chiketi-appliance/config.yaml`

```yaml
hosts:
  - name: "gpu-server"
    host: 192.168.1.50
    user: rohan
    key: ~/.ssh/id_rsa

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
  host_rotate: true
  host_rotate_interval: 30

server:
  port: 7777
  bind: 0.0.0.0
```

### CLI flags

| Flag | Default | Description |
|------|---------|-------------|
| `--config`, `-c` | `~/.config/chiketi-appliance/config.yaml` | Path to YAML config file |
| `--host` | — | Quick mode: `user@host` or `user@host:port` |
| `--key`, `-k` | — | SSH key path (for `--host` mode) |
| `--theme`, `-t` | `Panel/Gold` | Override theme |
| `--port`, `-p` | `7777` | HTTP server port |
| `--rotate-interval` | `10` | Screen rotation interval (seconds) |

## What the Remote Servers Need

**Nothing.** Just SSH access with a user that can read:

- `/proc/stat`, `/proc/cpuinfo` — CPU
- `/proc/meminfo` via `free` — memory
- `df` — disk
- `/proc/net/dev`, `ip` — network
- `/sys/class/thermal/` — temperatures
- `/sys/class/hwmon/` — fan speeds
- `nvidia-smi` — GPU (if NVIDIA GPU present)

All commands are read-only. No root access required.

## How It Differs From Chiketi

| | chiketi | chiketi-appliance |
|--|---------|-------------------|
| **Runs on** | The server being monitored | A separate Pi |
| **Collection** | Local (psutil) | Remote (SSH) |
| **Install on server** | Required | Not required |
| **Multi-server** | No | Yes |
| **Display** | Same machine's HDMI | Pi's HDMI |

## Requirements

- **Python** 3.11+
- **paramiko** — SSH connections
- **PyYAML** — configuration
- **Chromium** — dashboard display (auto-detected)
- **SSH access** to remote servers (key-based recommended)

## SSH Setup Helper

```bash
./scripts/setup-ssh.sh jeeves@192.168.1.50
```

Generates an SSH key (if needed), copies it to the server, and tests the connection.

## License

MIT
