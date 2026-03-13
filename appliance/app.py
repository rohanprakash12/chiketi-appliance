"""MetricEngine thread, HTTP server, and Chromium kiosk launcher."""

from __future__ import annotations

import glob
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from appliance.collectors.base import MetricValue
from appliance.collectors.registry import create_collectors
from appliance.collectors.remote import RemoteCollector
from appliance.config import TIMING
from appliance.hosts import ApplianceConfig, load_config

logger = logging.getLogger(__name__)


class MetricEngine(threading.Thread):
    """Background thread that periodically collects metrics from remote hosts."""

    daemon = True

    def __init__(self, collectors: list[RemoteCollector]) -> None:
        super().__init__()
        self._collectors = collectors
        # Per-host latest metrics
        self._latest: dict[str, dict[str, MetricValue]] = {}
        self._lock = threading.Lock()
        self._running = True
        # Active host — defaults to first configured host
        self._active_host: str = collectors[0].name if collectors else ""

    def run(self) -> None:
        pool = ThreadPoolExecutor(max_workers=max(len(self._collectors), 1))
        try:
            self._run_loop(pool)
        finally:
            pool.shutdown(wait=False)

    def _run_loop(self, pool: ThreadPoolExecutor) -> None:
        while self._running:
            futures = {pool.submit(c.collect): c for c in self._collectors}
            for future in as_completed(futures):
                collector = futures[future]
                try:
                    result = future.result()
                    with self._lock:
                        self._latest[collector.name] = result
                except Exception as exc:
                    print(
                        f"chiketi-appliance: collector {collector.name} failed: {exc}",
                        file=sys.stderr,
                    )
            time.sleep(TIMING.collect_interval_ms / 1000)

    def stop(self) -> None:
        self._running = False

    def get_latest(self, host_name: str | None = None) -> dict[str, MetricValue]:
        """Return metrics for a specific host (or the active host)."""
        name = host_name or self._active_host
        with self._lock:
            return dict(self._latest.get(name, {}))

    def get_host_status(self) -> list[dict]:
        """Return list of {name, online, latency_ms} for every configured host."""
        result = []
        for c in self._collectors:
            result.append({
                "name": c.name,
                "online": c.online,
                "latency_ms": c.latency_ms,
            })
        return result

    def get_active_host(self) -> str:
        return self._active_host

    def set_active_host(self, name: str) -> bool:
        """Set the active host. Returns True if the host exists."""
        for c in self._collectors:
            if c.name == name:
                self._active_host = name
                return True
        return False

    def get_host_names(self) -> list[str]:
        """Return all configured host names in order."""
        return [c.name for c in self._collectors]


# ---------------------------------------------------------------------------
# Display manager helpers (copied verbatim from chiketi, import paths only)
# ---------------------------------------------------------------------------


def _find_chromium() -> str | None:
    """Find a Chromium-based browser on the system."""
    for name in ("chromium", "chromium-browser", "google-chrome"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _is_wayland() -> bool:
    """Check if the system is running a Wayland session."""
    if os.environ.get("WAYLAND_DISPLAY"):
        return True
    # Check if any user has a Wayland session via loginctl
    try:
        result = subprocess.run(
            ["loginctl", "show-session", "auto", "--property=Type"],
            capture_output=True, text=True, timeout=5,
        )
        if "wayland" in result.stdout.lower():
            return True
    except Exception:
        pass
    # Check for Wayland compositor processes
    try:
        result = subprocess.run(
            ["pgrep", "-a", "-f", "gnome-shell|kwin_wayland|sway|weston|mutter"],
            capture_output=True, text=True, timeout=5,
        )
        if result.stdout.strip():
            return True
    except Exception:
        pass
    return False


def _get_graphical_session_env() -> dict[str, str]:
    """Grab DISPLAY and WAYLAND_DISPLAY from an active graphical session.

    When running from SSH or a systemd service, these env vars aren't set.
    We find them from a running user session.
    """
    env = {}
    uid = os.getuid()

    # Try loginctl to find graphical sessions
    try:
        result = subprocess.run(
            ["loginctl", "list-sessions", "--no-legend"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            session_id = parts[0]
            try:
                props = subprocess.run(
                    ["loginctl", "show-session", session_id,
                     "--property=Type", "--property=Display",
                     "--property=User", "--property=Name"],
                    capture_output=True, text=True, timeout=5,
                )
                prop_dict = {}
                for p in props.stdout.strip().splitlines():
                    if "=" in p:
                        k, v = p.split("=", 1)
                        prop_dict[k] = v
                if prop_dict.get("Type") not in ("x11", "wayland"):
                    continue
                # Found a graphical session — get its env vars
                # Try reading from /proc of a process in that session
                sess_leader = subprocess.run(
                    ["loginctl", "show-session", session_id, "--property=Leader"],
                    capture_output=True, text=True, timeout=5,
                )
                leader_pid = sess_leader.stdout.strip().split("=")[-1]
                if leader_pid and leader_pid.isdigit():
                    candidate = _read_env_from_proc(int(leader_pid))
                    if candidate:
                        # Merge into env but keep scanning if missing XAUTHORITY
                        env.update(candidate)
            except Exception:
                continue
    except Exception:
        pass

    # Fallback: scan /proc for a process with DISPLAY or WAYLAND_DISPLAY set
    # Prefer processes that also have XAUTHORITY (needed for XWayland)
    best = {}
    try:
        for proc_dir in sorted(glob.glob("/proc/[0-9]*")):
            try:
                if os.stat(proc_dir).st_uid != uid:
                    continue
                environ_path = os.path.join(proc_dir, "environ")
                with open(environ_path, "rb") as f:
                    environ_data = f.read().decode("utf-8", errors="replace")
                proc_env = {}
                for item in environ_data.split("\0"):
                    if "=" in item:
                        k, v = item.split("=", 1)
                        if k in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XAUTHORITY"):
                            proc_env[k] = v
                if not (proc_env.get("DISPLAY") or proc_env.get("WAYLAND_DISPLAY")):
                    continue
                # If this process has XAUTHORITY, it's the best match
                if proc_env.get("XAUTHORITY"):
                    return proc_env
                # Otherwise save as fallback
                if not best:
                    best = proc_env
            except (PermissionError, FileNotFoundError, ProcessLookupError):
                continue
    except Exception:
        pass

    return best or env


def _read_env_from_proc(pid: int) -> dict[str, str]:
    """Read display-related env vars from a process."""
    env = {}
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            data = f.read().decode("utf-8", errors="replace")
        for item in data.split("\0"):
            if "=" in item:
                k, v = item.split("=", 1)
                if k in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XAUTHORITY"):
                    env[k] = v
    except Exception:
        pass
    return env


def _detect_display() -> str:
    """Auto-detect the active X display.

    Checks DISPLAY env var first, then looks for running X servers.
    """
    display = os.environ.get("DISPLAY")
    if display:
        return display

    # Try to get it from a graphical session
    session_env = _get_graphical_session_env()
    if session_env.get("DISPLAY"):
        return session_env["DISPLAY"]

    # Find running X servers by checking /tmp/.X*-lock files
    locks = sorted(glob.glob("/tmp/.X*-lock"))
    for lock in locks:
        try:
            with open(lock) as f:
                pid = int(f.read().strip())
            if os.path.isdir(f"/proc/{pid}"):
                num = lock.split(".X")[1].split("-lock")[0]
                # Skip XWayland high-numbered displays
                if int(num) < 100:
                    return f":{num}"
        except (ValueError, IndexError):
            continue

    return ":0"


class DisplayManager:
    """Manages the Chromium kiosk process — start/stop from control panel."""

    def __init__(self, display_url: str) -> None:
        self._url = display_url
        self._chromium = _find_chromium()
        self._wayland = _is_wayland()
        self._session_env = _get_graphical_session_env()
        self._display_env = self._session_env.get("DISPLAY") or _detect_display()
        self._screen_size = self._detect_screen_size()
        self._proc: subprocess.Popen | None = None
        self._adopted_pid: int | None = None
        self._lock = threading.Lock()
        self._x_vt = self._detect_x_vt() if not self._wayland else None
        self._adopt_existing()

        if self._wayland:
            print(f"chiketi-appliance: Wayland session detected")
        print(f"chiketi-appliance: using DISPLAY={self._display_env}")
        if self._screen_size:
            print(f"chiketi-appliance: screen size {self._screen_size[0]}x{self._screen_size[1]}")

    def _detect_screen_size(self) -> tuple[int, int] | None:
        """Detect the primary screen resolution via xrandr."""
        try:
            env = self._build_env()
            result = subprocess.run(
                ["xrandr", "--query"],
                capture_output=True, text=True, timeout=5, env=env,
            )
            for line in result.stdout.splitlines():
                if " connected" in line:
                    for part in line.split():
                        if "x" in part and part[0].isdigit():
                            res = part.split("+")[0]
                            w, h = res.split("x")
                            return (int(w), int(h))
        except Exception:
            pass
        return None

    def _detect_x_vt(self) -> int | None:
        """Detect which virtual terminal the X server is running on."""
        try:
            result = subprocess.run(
                ["pgrep", "-a", "-x", "Xorg"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.strip().splitlines():
                for part in line.split():
                    if part.startswith("vt"):
                        return int(part[2:])
        except Exception:
            pass
        return None

    def _adopt_existing(self) -> None:
        """Find a Chromium kiosk already showing our display URL."""
        try:
            result = subprocess.run(
                ["pgrep", "-a", "-f", "kiosk"],
                capture_output=True, text=True, timeout=5,
            )
            marker = f"--app={self._url}"
            for line in result.stdout.strip().splitlines():
                if marker in line:
                    pid = int(line.split()[0])
                    os.kill(pid, 0)
                    self._adopted_pid = pid
                    print(f"chiketi-appliance: adopted existing display (pid {pid})")
                    return
        except Exception:
            pass

    def _build_env(self) -> dict[str, str]:
        """Build the environment for launching Chromium."""
        env = {**os.environ}
        env["DISPLAY"] = self._display_env
        # Pass through session env vars (Wayland, X auth, runtime dir)
        for key in ("WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XAUTHORITY"):
            val = self._session_env.get(key) or os.environ.get(key)
            if val:
                env[key] = val
        return env

    @property
    def is_on(self) -> bool:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return True
            if self._adopted_pid is not None:
                try:
                    os.kill(self._adopted_pid, 0)
                    return True
                except OSError:
                    self._adopted_pid = None
            return False

    def _switch_vt(self, vt: int) -> None:
        """Switch to a virtual terminal (requires sudo/root)."""
        try:
            subprocess.run(
                ["sudo", "-n", "chvt", str(vt)],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass

    def turn_on(self) -> bool:
        """Launch Chromium kiosk. Returns True if started."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return True
            if self._adopted_pid is not None:
                try:
                    os.kill(self._adopted_pid, 0)
                    return True
                except OSError:
                    self._adopted_pid = None
            if not self._chromium:
                return False
            # Switch to X virtual terminal (X11 only)
            if self._x_vt and not self._wayland:
                self._switch_vt(self._x_vt)
            env = self._build_env()
            chrome_args = [
                self._chromium,
                "--kiosk",
                f"--app={self._url}",
                "--no-first-run",
                "--disable-translate",
                "--disable-infobars",
                "--disable-session-crashed-bubble",
                "--disable-features=TranslateUI",
                "--noerrdialogs",
                "--start-fullscreen",
            ]
            if self._screen_size:
                w, h = self._screen_size
                chrome_args.append(f"--window-size={w},{h}")
            if self._wayland:
                chrome_args.append("--ozone-platform=wayland")
            try:
                self._proc = subprocess.Popen(
                    chrome_args, env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                print(f"chiketi-appliance: display ON (pid {self._proc.pid})")
                return True
            except Exception as exc:
                print(f"chiketi-appliance: failed to start display: {exc}", file=sys.stderr)
                return False

    def turn_off(self) -> bool:
        """Stop Chromium kiosk. Returns True if stopped."""
        with self._lock:
            if self._adopted_pid is not None:
                try:
                    os.kill(self._adopted_pid, signal.SIGTERM)
                    print(f"chiketi-appliance: display OFF (adopted pid {self._adopted_pid})")
                except OSError:
                    pass
                self._adopted_pid = None
            if self._proc is not None and self._proc.poll() is None:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait()
                print("chiketi-appliance: display OFF")
            self._proc = None
            # Switch to console (X11 only — on Wayland, closing Chromium
            # just returns to the desktop)
            if self._x_vt and not self._wayland:
                self._switch_vt(1)
            return True


# Module-level display manager — set during run()
_display_mgr: DisplayManager | None = None

# ── Setup wizard mode ──
_setup_mode = False
_setup_complete = threading.Event()
_setup_config: ApplianceConfig | None = None


def get_display_manager() -> DisplayManager | None:
    return _display_mgr


def run_setup_mode(port: int = 7777) -> int:
    """Start in setup wizard mode -- no config, no collectors, just the HTTP server."""
    global _setup_mode, _display_mgr
    _setup_mode = True

    from appliance.server import start_server, set_setup_mode

    set_setup_mode(True)

    try:
        start_server(port=port)
    except OSError as exc:
        print(f"chiketi-appliance: failed to start: {exc}", file=sys.stderr)
        return 1

    # Print instructions with IP addresses
    import socket

    hostname = socket.gethostname()
    try:
        ip = socket.gethostbyname(hostname)
    except Exception:
        ip = "localhost"

    print(f"chiketi-appliance: Setup wizard running!")
    print(f"  Open http://{ip}:{port}/ in your browser to configure.")
    print(f"  (or http://localhost:{port}/ from this machine)")

    # Wait for setup to complete
    _setup_complete.wait()

    # Transition to monitoring mode
    set_setup_mode(False)
    _setup_mode = False

    if _setup_config is not None:
        return run(config=_setup_config)
    return 0


def complete_setup(config: ApplianceConfig) -> None:
    """Called by server.py when setup wizard finishes."""
    global _setup_config
    _setup_config = config
    _setup_complete.set()


def run(config_path: str | None = None, *, config: ApplianceConfig | None = None) -> int:
    """Start metric engine, HTTP server, and Chromium kiosk. Returns exit code.

    Either *config_path* (path to a YAML file) or *config* (an already-built
    ApplianceConfig) must be provided.  If both are given, *config* wins.
    """
    global _display_mgr

    # Load configuration
    if config is None:
        if config_path is None:
            print("chiketi-appliance: no config path or config object provided", file=sys.stderr)
            return 1
        try:
            config = load_config(config_path)
        except (FileNotFoundError, ValueError) as exc:
            print(f"chiketi-appliance: config error: {exc}", file=sys.stderr)
            return 1

    print(f"chiketi-appliance: loaded config with {len(config.hosts)} host(s)")

    # Create collectors
    collectors = create_collectors(config)

    # Connect all SSH connections in parallel
    print("chiketi-appliance: connecting to remote hosts...")
    with ThreadPoolExecutor(max_workers=len(collectors)) as pool:
        futures = {pool.submit(c.connect): c for c in collectors}
        for future in as_completed(futures):
            collector = futures[future]
            try:
                ok = future.result()
                if ok:
                    print(f"  {collector.name}: connected")
                else:
                    print(f"  {collector.name}: FAILED (will retry in background)")
            except Exception as exc:
                print(f"  {collector.name}: ERROR: {exc}")

    # Start metric collection thread
    engine = MetricEngine(collectors)
    engine.start()

    # Start control panel HTTP server (with metrics access)
    from appliance.server import start_server, set_metrics_source, set_host_source, CONTROL_PORT

    set_metrics_source(engine.get_latest)
    set_host_source(
        engine.get_host_status,
        engine.get_active_host,
        engine.set_active_host,
        engine.get_host_names,
    )

    # Apply display config to server module
    display_cfg = config.display
    if display_cfg.get("host_rotate") and display_cfg.get("host_rotate_interval"):
        import appliance.server as _srv
        _srv._host_rotate_interval = int(display_cfg["host_rotate_interval"])

    server_port = config.server.get("port", CONTROL_PORT)
    server_bind = config.server.get("bind", "0.0.0.0")

    try:
        start_server(port=server_port, bind=server_bind)
    except OSError as exc:
        print(f"chiketi-appliance: control server failed to bind: {exc}", file=sys.stderr)
        _shutdown(engine, collectors)
        return 1

    display_url = f"http://localhost:{server_port}/display"
    print(f"chiketi-appliance: server running on http://{server_bind}:{server_port}/")
    print(f"chiketi-appliance: display at {display_url}")

    # Create display manager and auto-start
    _display_mgr = DisplayManager(display_url)
    if _display_mgr._chromium:
        _display_mgr.turn_on()
    else:
        print(
            "chiketi-appliance: no Chromium browser found, running headless (server only)",
            file=sys.stderr,
        )

    # Keep running until interrupted
    _shutting_down = False

    def _handle_term(signum, frame):
        nonlocal _shutting_down
        if _shutting_down:
            return
        _shutting_down = True
        if _display_mgr:
            _display_mgr.turn_off()
        _shutdown(engine, collectors)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _handle_term)

    try:
        signal.pause()
    except KeyboardInterrupt:
        pass
    except SystemExit:
        return 0

    if not _shutting_down:
        if _display_mgr:
            _display_mgr.turn_off()
        _shutdown(engine, collectors)
    return 0


def _shutdown(engine: MetricEngine, collectors: list[RemoteCollector]) -> None:
    """Clean shutdown — stop engine and close all SSH connections."""
    engine.stop()
    for c in collectors:
        try:
            c.disconnect()
        except Exception:
            pass
    print("chiketi-appliance: shutdown complete")
