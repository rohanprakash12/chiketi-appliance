# Task Checklist

## Phase 1: Project Scaffold
- [ ] Create directory structure: `appliance/`, `appliance/collectors/`, `appliance/assets/fonts/`, `scripts/`
- [ ] Create `pyproject.toml` with paramiko + pyyaml dependencies
- [ ] Create `setup.py` fallback
- [ ] Create `appliance/__init__.py`
- [ ] Create `config.example.yaml` with example hosts config
- [ ] Copy `themes.py` from `/home/rohan/projects/chiketi/chiketi/themes.py`
- [ ] Copy `panel_spec.py` from `/home/rohan/projects/chiketi/chiketi/panel_spec.py`
- [ ] Copy `collectors/base.py` from `/home/rohan/projects/chiketi/chiketi/collectors/base.py`
- [ ] Copy `config.py` from `/home/rohan/projects/chiketi/chiketi/config.py`
- [ ] Copy all font files from `/home/rohan/projects/chiketi/chiketi/assets/fonts/`
- [ ] Create `appliance/hosts.py` with HostConfig, ApplianceConfig, load_config()

## Phase 2: SSH Remote Collection
- [ ] Create `appliance/collectors/ssh_commands.py` with COMBINED_COMMAND string
- [ ] Implement `split_sections()` to split output by markers
- [ ] Implement `parse_cpu_stat()` — CPU usage from /proc/stat (delta-based)
- [ ] Implement `parse_cpu_info()` — core count and model name
- [ ] Implement `parse_memory()` — RAM and swap from `free -b`
- [ ] Implement `parse_disk()` — disk usage from `df`
- [ ] Implement `parse_network()` — throughput from /proc/net/dev (delta-based)
- [ ] Implement `parse_net_addr()` — IP and MAC from `ip addr`/`ip link`
- [ ] Implement `parse_temps()` — temps from /sys/class/thermal
- [ ] Implement `parse_fans()` — fan RPM from /sys/class/hwmon
- [ ] Implement `parse_uptime()` — hostname + uptime from /proc/uptime
- [ ] Implement `parse_gpu()` — nvidia-smi CSV output
- [ ] Create `appliance/collectors/remote.py` with RemoteCollector class
- [ ] Implement SSH connect with paramiko (key, password, agent support)
- [ ] Implement SSH keepalive (30s interval)
- [ ] Implement reconnect on failure (30s retry interval)
- [ ] Implement `collect()` — exec combined command, parse sections, return MetricValues
- [ ] Implement delta tracking for CPU and network (store prev readings)
- [ ] Create `appliance/collectors/registry.py` with create_collectors()
- [ ] Create `appliance/collectors/__init__.py`
- [ ] Verify all metric keys match chiketi's format (compare with original collectors)

## Phase 3: MetricEngine + App
- [ ] Create `appliance/app.py` with MetricEngine class
- [ ] MetricEngine: collect from all hosts, store per-host metrics
- [ ] MetricEngine: active host selection (get/set)
- [ ] MetricEngine: host status reporting (online/offline/latency)
- [ ] Copy DisplayManager + helper functions from chiketi app.py
- [ ] Update DisplayManager imports for appliance package
- [ ] Implement `run()` function: load config, create collectors, start engine, start server, launch display
- [ ] Implement parallel SSH connection on startup (ThreadPoolExecutor)
- [ ] Implement clean shutdown (SIGTERM handler, close SSH, stop Chromium)

## Phase 4: HTTP Server
- [ ] Copy `server.py` from `/home/rohan/projects/chiketi/chiketi/server.py`
- [ ] Update all imports from `chiketi.*` to `appliance.*`
- [ ] Add module-level `_active_host` and `_host_status_getter` variables
- [ ] Add `set_host_status_source()` function (like set_metrics_source)
- [ ] Add `GET /api/hosts` endpoint — host list with online/offline/latency
- [ ] Add `POST /api/host/{name}` endpoint — switch active host
- [ ] Modify `_serialize_metrics()` to use active host's metrics
- [ ] Add host selector bar to display page HTML (bottom bar, host names)
- [ ] Add host auto-rotation JS to display page (if host_rotate enabled)
- [ ] Add keyboard shortcut H to cycle hosts on display page
- [ ] Add host name display in dashboard header area
- [ ] Add Hosts section to control panel settings tab
- [ ] Add host list with online/offline indicators in control panel
- [ ] Add click-to-switch-host in control panel
- [ ] Test control panel loads without errors

## Phase 5: CLI Entry Point
- [ ] Create `appliance/__main__.py` with argparse
- [ ] Support `--config PATH` for YAML config file
- [ ] Support `--theme NAME` override
- [ ] Support `--rotate-interval N` override
- [ ] Support `--host user@host:port` quick single-host mode
- [ ] Support `--key PATH` for SSH key with --host
- [ ] Print helpful usage if no config and no --host provided
- [ ] Wire up CLI to `app.run()`

## Phase 6: Installation + Scripts
- [ ] Create `scripts/install.sh` — system deps + pipx install
- [ ] Create `scripts/setup-ssh.sh` — SSH key setup helper
- [ ] Create default config directory logic in installer
- [ ] Test install from clean system

## Phase 7: Testing + Polish
- [ ] Test SSH collection against 192.168.16.175 (wooster@Ubuntu 25.10)
- [ ] Verify metric keys match — compare API output with chiketi's /api/metrics
- [ ] Test host offline detection and recovery
- [ ] Test multi-host switching
- [ ] Test display page renders correctly with remote metrics
- [ ] Test all theme variants work
- [ ] Test control panel host management
- [ ] Create GitHub repo and push
- [ ] Create README.md
