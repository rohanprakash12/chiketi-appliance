"""Tiny HTTP control panel server."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
try:
    from http.server import ThreadingHTTPServer
except ImportError:
    import socketserver
    class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
        daemon_threads = True

from appliance.config import TIMING
from appliance.themes import (
    get_active_theme, get_active_family, set_active_theme,
    get_families, THEMES,
)
from appliance.panel_spec import web_spec

CONTROL_PORT = 7777

# Module-level metrics getter — set by app.py after engine starts
_get_metrics = None

# ── Setup wizard state ──
_setup_mode_flag = False
_staged_hosts: list[dict] = []  # hosts added during setup, not yet saved


def set_setup_mode(enabled: bool) -> None:
    global _setup_mode_flag
    _setup_mode_flag = enabled


def is_setup_mode() -> bool:
    return _setup_mode_flag

# Display configuration
_display_output: str = ""  # empty = auto/default
_display_brightness: float = 1.0
_display_width: int = 1024
_display_height: int = 600

# Per-screen rotation configuration: {screen_id: {enabled: bool, duration: int}}
# Populated with defaults on first /api/display GET
_screen_rotation: dict = {}

# Cache for UI asset files (read once, then served inline at render time)
_UI_ASSET_CACHE: dict = {}


def _ui_asset(name: str) -> str:
    """Read and cache a UI asset file (read once at module level)."""
    cached = _UI_ASSET_CACHE.get(name)
    if cached is None:
        path = os.path.join(os.path.dirname(__file__), "assets", "ui", name)
        with open(path, encoding="utf-8") as fh:
            cached = fh.read()
        _UI_ASSET_CACHE[name] = cached
    return cached


def _get_session_env() -> dict[str, str]:
    """Get display env vars, auto-detecting from graphical session if needed."""
    from appliance.app import _get_graphical_session_env
    env = {**os.environ}
    session_env = _get_graphical_session_env()
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XAUTHORITY"):
        if key not in env and key in session_env:
            env[key] = session_env[key]
    if "DISPLAY" not in env:
        from appliance.app import _detect_display
        env["DISPLAY"] = _detect_display()
    return env


def _parse_xrandr(stdout: str) -> list[dict]:
    """Parse xrandr output into a list of display dicts."""
    outputs = []
    for line in stdout.splitlines():
        if " connected" in line or " disconnected" in line:
            parts = line.split()
            name = parts[0]
            connected = parts[1] == "connected" if len(parts) > 1 else False
            resolution = ""
            if connected and len(parts) > 2:
                for p in parts[2:]:
                    if "x" in p and p[0].isdigit():
                        resolution = p.split("+")[0]
                        break
            outputs.append({
                "name": name,
                "connected": connected,
                "resolution": resolution,
            })
    return outputs


def _get_xrandr_outputs() -> list[dict]:
    """Query display outputs, supporting both X11 and Wayland."""
    import glob

    # Get full session env (DISPLAY, WAYLAND_DISPLAY, XDG_RUNTIME_DIR)
    env = _get_session_env()

    # First try xrandr with the session env (works on X11 and XWayland)
    try:
        result = subprocess.run(
            ["xrandr", "--query"],
            capture_output=True, text=True, timeout=5, env=env,
        )
        outputs = _parse_xrandr(result.stdout)
        if outputs:
            return outputs
    except Exception:
        pass

    # Try each X display from lock files
    for lock in sorted(glob.glob("/tmp/.X*-lock")):
        try:
            num = lock.split(".X")[1].split("-lock")[0]
            run_env = {**env, "DISPLAY": f":{num}"}
            result = subprocess.run(
                ["xrandr", "--query"],
                capture_output=True, text=True, timeout=5,
                env=run_env,
            )
            outputs = _parse_xrandr(result.stdout)
            if outputs:
                for o in outputs:
                    o["display"] = f":{num}"
                return outputs
        except Exception:
            continue

    return []


def _apply_display_settings(output: str, brightness: float) -> bool:
    """Apply xrandr output and brightness settings."""
    global _display_output, _display_brightness
    try:
        args = ["xrandr"]
        if output:
            args.extend(["--output", output, "--brightness", str(brightness)])
        else:
            return False
        subprocess.run(
            args, capture_output=True, timeout=5,
            env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0")},
        )
        _display_output = output
        _display_brightness = brightness
        return True
    except Exception:
        return False


def set_metrics_source(fn):
    """Register a callable that returns the latest metrics dict."""
    global _get_metrics
    _get_metrics = fn


# ── Host management infrastructure ──
_host_status_getter = None   # callable that returns host status list
_active_host_getter = None   # callable that returns active host name
_active_host_setter = None   # callable to set active host
_host_names_getter = None    # callable that returns list of host names
_host_rotate_interval = 0    # 0 = disabled; >0 = seconds between host auto-rotation
_default_screen_duration = 10  # default seconds per screen rotation; overridden by config display.rotate_interval


def set_host_source(status_fn, active_get_fn, active_set_fn, names_fn):
    """Register callables for host management (set by app.py)."""
    global _host_status_getter, _active_host_getter, _active_host_setter, _host_names_getter
    _host_status_getter = status_fn
    _active_host_getter = active_get_fn
    _active_host_setter = active_set_fn
    _host_names_getter = names_fn


def _get_or_generate_pubkey() -> str | None:
    """Return the local SSH public key, generating one if none exists."""
    import paramiko
    import logging
    logger = logging.getLogger(__name__)

    ssh_dir = os.path.expanduser("~/.ssh")
    ed25519_path = os.path.join(ssh_dir, "id_ed25519")
    rsa_path = os.path.join(ssh_dir, "id_rsa")

    # Check for existing public key files
    for key_path in (ed25519_path, rsa_path):
        pub_path = key_path + ".pub"
        if os.path.isfile(pub_path):
            try:
                with open(pub_path, "r") as f:
                    content = f.read().strip()
                if content:
                    return content
            except Exception as exc:
                logger.warning("Failed to read %s: %s", pub_path, exc)
                continue

    # If private key exists but no .pub, regenerate the .pub from it
    for key_path in (ed25519_path, rsa_path):
        if os.path.isfile(key_path):
            try:
                if "ed25519" in key_path:
                    key = paramiko.Ed25519Key.from_private_key_file(key_path)
                else:
                    key = paramiko.RSAKey.from_private_key_file(key_path)
                import socket
                hostname = socket.gethostname()
                username = os.environ.get("USER", "chiketi")
                pub_line = f"{key.get_name()} {key.get_base64()} {username}@{hostname}"
                with open(key_path + ".pub", "w") as f:
                    f.write(pub_line + "\n")
                os.chmod(key_path + ".pub", 0o644)
                return pub_line
            except Exception as exc:
                logger.warning("Failed to derive pub from %s: %s", key_path, exc)
                continue

    # Generate a new key
    os.makedirs(ssh_dir, mode=0o700, exist_ok=True)

    # Build list of generators — ed25519 only if .generate exists
    generators = []
    if hasattr(paramiko.Ed25519Key, "generate"):
        generators.append((paramiko.Ed25519Key.generate, ed25519_path, {}))
    generators.append((paramiko.RSAKey.generate, rsa_path, {"bits": 2048}))

    for gen_func, gen_path, gen_args in generators:
        try:
            key = gen_func(**gen_args)
            key.write_private_key_file(gen_path)
            os.chmod(gen_path, 0o600)

            import socket
            hostname = socket.gethostname()
            username = os.environ.get("USER", "chiketi")
            pub_line = f"{key.get_name()} {key.get_base64()} {username}@{hostname}"
            with open(gen_path + ".pub", "w") as f:
                f.write(pub_line + "\n")
            os.chmod(gen_path + ".pub", 0o644)
            logger.info("Generated SSH key at %s", gen_path)
            return pub_line
        except Exception as exc:
            logger.warning("Failed to generate key at %s: %s", gen_path, exc)
            continue

    return None


def _serialize_metrics() -> dict:
    """Convert MetricValue dict to JSON-safe dict."""
    if _get_metrics is None:
        return {}
    raw = _get_metrics()
    out = {}
    for key, mv in raw.items():
        out[key] = {
            "value": mv.value,
            "unit": mv.unit,
            "available": mv.available,
            "extra": mv.extra,
        }
    return out


class ControlHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        # ── Setup wizard routes ──
        if _setup_mode_flag and (self.path == "/" or self.path == "/index.html"):
            self.send_response(302)
            self.send_header("Location", "/setup")
            self.end_headers()
            return
        if self.path == "/setup":
            if not _setup_mode_flag:
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
                return
            html = _build_setup_html()
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/setup/status":
            if not _setup_mode_flag:
                self.send_error(404)
                return
            self._json_response({
                "setup_mode": _setup_mode_flag,
                "hosts": _staged_hosts,
            })
            return
        if self.path == "/api/setup/ssh-key":
            self._handle_ssh_key_get()
            return
        if self.path == "/api/setup/themes":
            if not _setup_mode_flag:
                self.send_error(404)
                return
            # Reuse the themes listing logic
            families = {}
            for family_name, themes in get_families().items():
                families[family_name] = {
                    t.name: {
                        "primary": t.primary,
                        "accent": t.accent,
                        "background": t.background,
                        "panel": t.panel,
                        "border": t.border,
                        "header": t.header,
                        "dim": t.dim,
                        "critical": t.critical,
                    }
                    for t in themes
                }
            self._json_response({
                "active_family": get_active_family(),
                "active_variant": get_active_theme().name,
                "families": families,
            })
            return

        # ── Normal routes ──
        if self.path == "/" or self.path == "/index.html":
            self._serve_ui()
        elif self.path == "/display":
            self._serve_display()
        elif self.path == "/api/themes":
            families = {}
            for family_name, themes in get_families().items():
                families[family_name] = {
                    t.name: {
                        "primary": t.primary,
                        "accent": t.accent,
                        "background": t.background,
                        "panel": t.panel,
                        "border": t.border,
                        "header": t.header,
                        "dim": t.dim,
                        "critical": t.critical,
                    }
                    for t in themes
                }
            self._json_response({
                "active_family": get_active_family(),
                "active_variant": get_active_theme().name,
                "families": families,
            })
        elif self.path == "/api/metrics":
            self._json_response(_serialize_metrics())
        elif self.path == "/api/hosts":
            hosts = _host_status_getter() if _host_status_getter else []
            active = _active_host_getter() if _active_host_getter else ""
            self._json_response({
                "hosts": hosts,
                "active_host": active,
                "host_rotate_interval": _host_rotate_interval,
            })
        elif self.path == "/api/health":
            self._json_response({"status": "ok"})
        elif self.path == "/api/display":
            from appliance.app import get_display_manager
            mgr = get_display_manager()
            self._json_response({
                "current_output": _display_output,
                "brightness": _display_brightness,
                "width": _display_width,
                "height": _display_height,
                "screen_rotation": _screen_rotation,
                "display_on": mgr.is_on if mgr else False,
                "outputs": _get_xrandr_outputs(),
            })
        elif self.path.startswith("/assets/fonts/"):
            self._serve_font()
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        path = self.path

        # ── Setup wizard POST routes ──
        if path == "/api/setup/copy-key":
            self._handle_copy_key()
            return
        if path == "/api/setup/test-connection":
            self._handle_test_connection()
            return
        if path == "/api/setup/add-host":
            self._handle_add_host()
            return
        if path == "/api/setup/remove-host":
            self._handle_remove_host()
            return
        if path == "/api/setup/finish":
            if not _setup_mode_flag:
                self.send_error(404)
                return
            self._handle_setup_finish()
            return

        # ── Normal POST routes ──
        if path.startswith("/api/theme/"):
            rest = path.split("/api/theme/", 1)[1]
            # Support both /api/theme/family/variant and /api/theme/variant
            if "/" in rest:
                # family/variant format
                key = rest
            else:
                # Short variant name (backward compat)
                key = rest
            if set_active_theme(key):
                self._json_response({
                    "active_family": get_active_family(),
                    "active_variant": get_active_theme().name,
                })
            else:
                self.send_error(400, f"Unknown theme: {key}")
        elif path == "/api/display":
            global _display_width, _display_height, _screen_rotation
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length else {}
                output = body.get("output", _display_output)
                brightness = float(body.get("brightness", _display_brightness))
                brightness = max(0.3, min(2.0, brightness))
                # Validate output against known xrandr outputs
                valid_outputs = {o["name"] for o in _get_xrandr_outputs()}
                if output and output not in valid_outputs:
                    self.send_error(400, f"Unknown output: {output}")
                    return
                # Display resolution
                if "width" in body and "height" in body:
                    _display_width = max(320, min(3840, int(body["width"])))
                    _display_height = max(200, min(2160, int(body["height"])))
                # Per-screen rotation settings
                if "screen_rotation" in body:
                    sr = body["screen_rotation"]
                    if isinstance(sr, dict):
                        for sid, cfg in sr.items():
                            if isinstance(cfg, dict):
                                _screen_rotation[sid] = {
                                    "enabled": bool(cfg.get("enabled", True)),
                                    "duration": max(3, min(600, int(cfg.get("duration", 10)))),
                                }
                # Display power toggle
                from appliance.app import get_display_manager
                mgr = get_display_manager()
                if "display_on" in body and mgr:
                    if body["display_on"]:
                        mgr.turn_on()
                    else:
                        mgr.turn_off()
                # Apply xrandr if output specified
                if output:
                    _apply_display_settings(output, brightness)
                self._json_response({
                    "current_output": _display_output,
                    "brightness": _display_brightness,
                    "width": _display_width,
                    "height": _display_height,
                    "screen_rotation": _screen_rotation,
                    "display_on": mgr.is_on if mgr else False,
                })
            except Exception as e:
                self.send_error(400, str(e))
        elif path.startswith("/api/host/"):
            name = path.split("/api/host/", 1)[1]
            if _active_host_setter and _host_names_getter:
                known = _host_names_getter()
                if name in known:
                    _active_host_setter(name)
                    self._json_response({"ok": True, "active_host": name})
                else:
                    self.send_error(404, f"Unknown host: {name}")
            else:
                self.send_error(503, "Host management not configured")
        else:
            self.send_error(404)

    def _json_response(self, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_font(self) -> None:
        fname = os.path.basename(self.path)
        font_dir = os.path.join(os.path.dirname(__file__), "assets", "fonts")
        fpath = os.path.join(font_dir, fname)
        if os.path.isfile(fpath):
            with open(fpath, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "font/ttf")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)


    def _serve_ui(self) -> None:
        html = _build_html()
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_display(self) -> None:
        html = _build_display_html()
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── Setup wizard handler methods ──

    def _read_json_body(self) -> dict:
        """Read and parse JSON request body."""
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw)

    def _json_error(self, status: int, message: str) -> None:
        """Send a JSON error response."""
        body = json.dumps({"success": False, "error": message}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_ssh_key_get(self) -> None:
        """GET /api/setup/ssh-key — return or generate an SSH key."""
        ssh_dir = os.path.expanduser("~/.ssh")
        ed25519_path = os.path.join(ssh_dir, "id_ed25519")
        rsa_path = os.path.join(ssh_dir, "id_rsa")

        # Check if key already existed
        already_existed = any(
            os.path.isfile(p) and os.path.isfile(p + ".pub")
            for p in (ed25519_path, rsa_path)
        )

        pub_key = _get_or_generate_pubkey()
        if pub_key:
            # Find which key path is active
            key_path = ed25519_path if os.path.isfile(ed25519_path) else rsa_path
            self._json_response({
                "public_key": pub_key,
                "key_path": key_path,
                "generated": not already_existed,
            })
        else:
            self._json_error(500, "Failed to generate SSH key")

    def _handle_copy_key(self) -> None:
        """POST /api/setup/copy-key — SSH in with password and copy the public key."""
        import paramiko

        try:
            body = self._read_json_body()
        except Exception:
            self._json_error(400, "Invalid JSON body")
            return

        host = body.get("host", "").strip()
        user = body.get("user", "").strip()
        password = body.get("password", "")
        try:
            port = int(body.get("port", 22))
        except (ValueError, TypeError):
            port = 22

        if not host or not user or not password:
            self._json_error(400, "host, user, and password are required")
            return

        # Read or generate the local public key
        pub_key = _get_or_generate_pubkey()
        if not pub_key:
            ssh_dir = os.path.expanduser("~/.ssh")
            exists = [f for f in os.listdir(ssh_dir)] if os.path.isdir(ssh_dir) else []
            self._json_error(500, f"Failed to read or generate SSH key. ~/.ssh contains: {exists}")
            return

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        try:
            client.connect(
                hostname=host, username=user, password=password,
                port=port, timeout=10, allow_agent=False, look_for_keys=False,
            )
            # Create .ssh dir and append key to authorized_keys
            cmd = (
                'mkdir -p ~/.ssh && chmod 700 ~/.ssh && '
                'touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && '
                f'grep -qF "{pub_key}" ~/.ssh/authorized_keys 2>/dev/null || '
                f'echo "{pub_key}" >> ~/.ssh/authorized_keys'
            )
            _, stdout, stderr = client.exec_command(cmd, timeout=10)
            exit_code = stdout.channel.recv_exit_status()
            if exit_code != 0:
                err = stderr.read().decode("utf-8", errors="replace").strip()
                self._json_response({
                    "success": False,
                    "error": f"Failed to copy key: {err}",
                })
            else:
                self._json_response({
                    "success": True,
                    "message": "SSH key copied successfully. You can now connect without a password.",
                })
        except Exception as exc:
            self._json_response({
                "success": False,
                "error": str(exc),
            })
        finally:
            client.close()

    def _handle_test_connection(self) -> None:
        """POST /api/setup/test-connection — test SSH connection to a host."""
        import paramiko

        try:
            body = self._read_json_body()
        except Exception:
            self._json_error(400, "Invalid JSON body")
            return

        host = body.get("host", "")
        user = body.get("user", "")
        if not isinstance(host, str) or not isinstance(user, str):
            self._json_error(400, "host and user must be strings")
            return
        host = host.strip()
        user = user.strip()
        try:
            port = int(body.get("port", 22))
        except (ValueError, TypeError):
            port = 22
        password = body.get("password")

        if not host or not user:
            self._json_error(400, "host and user are required")
            return

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        try:
            connect_kwargs: dict = {
                "hostname": host,
                "username": user,
                "port": port,
                "timeout": 10,
                "allow_agent": True,
                "look_for_keys": True,
            }
            if password:
                connect_kwargs["password"] = password
                connect_kwargs["look_for_keys"] = False
                connect_kwargs["allow_agent"] = False

            # Try with explicit key files if no password
            if not password:
                ssh_dir = os.path.expanduser("~/.ssh")
                for key_name in ("id_ed25519", "id_rsa"):
                    key_file = os.path.join(ssh_dir, key_name)
                    if os.path.isfile(key_file):
                        connect_kwargs["key_filename"] = key_file
                        break

            client.connect(**connect_kwargs)
            _, stdout, _ = client.exec_command(
                "hostname && cat /proc/uptime", timeout=10,
            )
            output = stdout.read().decode("utf-8", errors="replace").strip()
            lines = output.splitlines()
            hostname_result = lines[0] if lines else "unknown"
            uptime_str = ""
            if len(lines) > 1:
                try:
                    secs = float(lines[1].split()[0])
                    days = int(secs // 86400)
                    hours = int((secs % 86400) // 3600)
                    mins = int((secs % 3600) // 60)
                    parts = []
                    if days:
                        parts.append(f"{days}d")
                    if hours:
                        parts.append(f"{hours}h")
                    parts.append(f"{mins}m")
                    uptime_str = " ".join(parts)
                except Exception:
                    uptime_str = lines[1]

            self._json_response({
                "success": True,
                "hostname": hostname_result,
                "uptime": uptime_str,
            })
        except Exception as exc:
            self._json_response({
                "success": False,
                "error": str(exc),
            })
        finally:
            client.close()

    def _handle_add_host(self) -> None:
        """POST /api/setup/add-host — add a host to the staged list."""
        global _staged_hosts
        try:
            body = self._read_json_body()
        except Exception:
            self._json_error(400, "Invalid JSON body")
            return

        name = body.get("name", "")
        host = body.get("host", "")
        user = body.get("user", "")
        if not isinstance(name, str) or not isinstance(host, str) or not isinstance(user, str):
            self._json_error(400, "name, host, and user must be strings")
            return
        name = name.strip()
        host = host.strip()
        user = user.strip()
        try:
            port = int(body.get("port", 22))
        except (ValueError, TypeError):
            port = 22

        if not name:
            self._json_error(400, "name is required")
            return
        if not host:
            self._json_error(400, "host is required")
            return
        if not user:
            self._json_error(400, "user is required")
            return

        if _setup_mode_flag:
            # Setup mode: add to staged list
            for h in _staged_hosts:
                if h["name"] == name:
                    self._json_error(400, f"Host with name '{name}' already exists")
                    return

            _staged_hosts.append({
                "name": name,
                "host": host,
                "user": user,
                "port": port,
            })
            self._json_response({"success": True, "hosts": _staged_hosts})
        else:
            # Runtime mode: add to running engine and save config
            from appliance.app import add_host_runtime, save_current_config
            from appliance.hosts import HostConfig

            # Check for duplicate name against running hosts
            if _host_names_getter:
                existing = _host_names_getter()
                if name in existing:
                    self._json_error(400, f"Host with name '{name}' already exists")
                    return

            # Determine SSH key path
            ssh_dir = os.path.expanduser("~/.ssh")
            key_path = None
            for key_name in ("id_ed25519", "id_rsa"):
                candidate = os.path.join(ssh_dir, key_name)
                if os.path.isfile(candidate):
                    key_path = candidate
                    break

            hc = HostConfig(name=name, host=host, user=user, port=port, key_path=key_path)
            try:
                ok = add_host_runtime(hc)
                if not ok:
                    self._json_error(500, "Failed to add host: engine not available")
                    return
                save_current_config()
                self._json_response({"success": True})
            except Exception as exc:
                self._json_error(500, f"Failed to add host: {exc}")

    def _handle_remove_host(self) -> None:
        """POST /api/setup/remove-host — remove a host from the staged or running list."""
        global _staged_hosts
        try:
            body = self._read_json_body()
        except Exception:
            self._json_error(400, "Invalid JSON body")
            return

        name = body.get("name", "").strip()
        if not name:
            self._json_error(400, "name is required")
            return

        if _setup_mode_flag:
            # Setup mode: remove from staged list
            original_len = len(_staged_hosts)
            _staged_hosts = [h for h in _staged_hosts if h["name"] != name]
            if len(_staged_hosts) == original_len:
                self._json_error(404, f"Host '{name}' not found")
                return
            self._json_response({"success": True, "hosts": _staged_hosts})
        else:
            # Runtime mode: remove from running engine and save config
            from appliance.app import remove_host_runtime, save_current_config
            if remove_host_runtime(name):
                save_current_config()
                self._json_response({"success": True})
            else:
                self._json_error(404, f"Host '{name}' not found")

    def _handle_setup_finish(self) -> None:
        """POST /api/setup/finish — save config and transition to monitoring."""
        global _staged_hosts
        try:
            body = self._read_json_body()
        except Exception:
            self._json_error(400, "Invalid JSON body")
            return

        if not _staged_hosts:
            self._json_error(400, "At least one host must be added before finishing setup")
            return

        from appliance.hosts import ApplianceConfig, HostConfig, save_config

        # Determine SSH key path
        ssh_dir = os.path.expanduser("~/.ssh")
        key_path = None
        for key_name in ("id_ed25519", "id_rsa"):
            candidate = os.path.join(ssh_dir, key_name)
            if os.path.isfile(candidate):
                key_path = candidate
                break

        hosts = []
        for h in _staged_hosts:
            hosts.append(HostConfig(
                name=h["name"],
                host=h["host"],
                user=h["user"],
                port=h.get("port", 22),
                key_path=key_path,
            ))

        display: dict = {}
        theme = body.get("theme")
        if theme:
            display["theme"] = theme

        server_cfg: dict = {"port": CONTROL_PORT}

        config = ApplianceConfig(hosts=hosts, display=display, server=server_cfg)

        try:
            config_path = save_config(config)
        except Exception as exc:
            self._json_error(500, f"Failed to save config: {exc}")
            return

        self._json_response({
            "success": True,
            "config_path": config_path,
        })

        # Transition to monitoring mode in a background thread to allow
        # the HTTP response to be sent first
        from appliance.app import complete_setup

        def _finish():
            import time
            time.sleep(0.5)
            complete_setup(config)

        _staged_hosts = []
        threading.Thread(target=_finish, daemon=True).start()

    def log_message(self, format, *args) -> None:
        pass  # Silence request logging


def _build_setup_html() -> str:
    """Return the full setup wizard HTML page (self-contained with inline CSS/JS)."""
    return (
        _ui_asset("setup.html")
        .replace("__SETUP_CSS__", _ui_asset("setup.css"))
        .replace("__SETUP_APP__", _ui_asset("setup_app.js"))
    )


_server_started = False


def start_server(port: int | None = None, bind: str | None = None) -> None:
    """Start the control panel server in a daemon thread."""
    global CONTROL_PORT, _server_started
    if _server_started:
        # Server already running (e.g. setup mode → monitoring transition)
        if port is not None:
            CONTROL_PORT = port
        return
    if port is not None:
        CONTROL_PORT = port
    bind_addr = bind or "0.0.0.0"
    # Ensure a DisplayManager exists even if app.run() was not used
    # (skip in setup mode — no display needed yet)
    if not _setup_mode_flag:
        from appliance.app import get_display_manager, DisplayManager, _display_mgr
        import appliance.app as _app_mod
        if get_display_manager() is None:
            _app_mod._display_mgr = DisplayManager(
                f"http://localhost:{CONTROL_PORT}/display"
            )
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((bind_addr, CONTROL_PORT), ControlHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _server_started = True


def _build_display_html() -> str:
    """Build the fullscreen display page for Chromium kiosk mode."""
    spec = web_spec()
    pause_s = TIMING.pause_duration_s
    fonts = _ui_asset("fonts.css")
    css = _ui_asset("display.css").replace("__FONTS_CSS__", fonts)
    scripts = _ui_asset("display_app.js")
    html = (
        _ui_asset("display.html")
        .replace("__DISPLAY_CSS__", css)
        .replace("__DISPLAY_SCRIPTS__", scripts)
    )
    # Extract the screen rendering functions from _build_html to share them
    screen_fns = _screen_functions_js()
    return (
        html
        .replace("__PANEL_SPEC_JSON__", json.dumps(spec))
        .replace("__PAUSE_S__", str(pause_s))
        .replace("__DEFAULT_SCREEN_DURATION__", str(_default_screen_duration))
        .replace("__DISPLAY_W__", str(_display_width))
        .replace("__DISPLAY_H__", str(_display_height))
        .replace("__SCREEN_FUNCTIONS__", screen_fns)
    )


def _screen_functions_js() -> str:
    """Return the JS screen renderer functions shared by both pages."""
    return _ui_asset("screen_functions.js")


def _build_html() -> str:
    spec = web_spec()
    fonts = _ui_asset("fonts.css")
    css = _ui_asset("control.css").replace("__FONTS_CSS__", fonts)
    scripts = _ui_asset("control_app.js")
    html = (
        _ui_asset("control.html")
        .replace("__CONTROL_CSS__", css)
        .replace("__CONTROL_SCRIPTS__", scripts)
    )
    screen_fns = _screen_functions_js()
    return (
        html
        .replace("__PANEL_SPEC_JSON__", json.dumps(spec))
        .replace("__PANEL_GOLD__", spec["colors"]["gold"])
        .replace("__SCREEN_FUNCTIONS__", screen_fns)
    )
