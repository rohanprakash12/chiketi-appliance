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
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chiketi Appliance — Setup</title>
<style>
*,*::before,*::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg: #0a0a0a;
  --surface: #111111;
  --surface2: #1a1a1a;
  --border: #333;
  --green: #00ff41;
  --green-dim: #00cc33;
  --green-glow: rgba(0,255,65,0.15);
  --green-soft: rgba(0,255,65,0.08);
  --red: #ff4444;
  --yellow: #ffcc00;
  --text: #e0e0e0;
  --text-dim: #888;
  --text-muted: #555;
  --radius: 8px;
}

body {
  font-family: -apple-system, 'Segoe UI', system-ui, Roboto, sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  display: flex;
  justify-content: center;
  padding: 1.25rem 1rem;
  -webkit-font-smoothing: antialiased;
}

.wizard {
  width: 100%;
  max-width: 520px;
  display: flex;
  flex-direction: column;
  gap: 1.25rem;
}

/* ── Progress indicator ── */
.progress {
  display: flex;
  justify-content: center;
  align-items: center;
  gap: 0.4rem;
  padding: 1rem 0 0.25rem;
}
.dot {
  width: 10px; height: 10px;
  border-radius: 50%;
  background: var(--border);
  transition: all 0.35s ease;
  flex-shrink: 0;
}
.dot-line {
  width: 18px; height: 2px;
  background: var(--border);
  border-radius: 1px;
  transition: background 0.35s ease;
  flex-shrink: 0;
}
.dot.active {
  background: var(--green);
  box-shadow: 0 0 10px var(--green-glow), 0 0 4px var(--green-glow);
}
.dot.done { background: var(--green-dim); }
.dot-line.done { background: var(--green-dim); }

/* ── Step container ── */
.step { display: none; animation: fadeIn 0.35s ease; }
.step.active { display: block; }
@keyframes fadeIn {
  from { opacity: 0; transform: translateY(10px); }
  to { opacity: 1; transform: none; }
}

/* ── Cards ── */
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 1.25rem;
  margin-bottom: 0.75rem;
}

/* ── Typography ── */
h1 {
  font-size: 1.8rem;
  font-weight: 700;
  color: var(--green);
  text-align: center;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  text-shadow: 0 0 30px var(--green-glow);
}
h2 {
  font-size: 1.05rem;
  font-weight: 700;
  color: var(--text);
  margin-bottom: 0.75rem;
}
.subtitle {
  text-align: center;
  color: var(--text-dim);
  font-size: 0.92rem;
  line-height: 1.6;
  margin-top: 0.5rem;
}
.desc {
  text-align: center;
  color: var(--text-muted);
  font-size: 0.82rem;
  line-height: 1.5;
  margin-top: 0.75rem;
}

/* ── Form elements ── */
label {
  display: block;
  font-size: 0.75rem;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.1em;
  margin-bottom: 0.35rem;
  margin-top: 0.85rem;
}
label:first-child { margin-top: 0; }

input[type="text"],
input[type="number"],
input[type="password"],
textarea {
  width: 100%;
  padding: 0.65rem 0.8rem;
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 5px;
  color: var(--text);
  font-family: inherit;
  font-size: 0.95rem;
  outline: none;
  transition: border-color 0.2s, box-shadow 0.2s;
}
input:focus, textarea:focus {
  border-color: var(--green);
  box-shadow: 0 0 0 2px var(--green-soft);
}
input::placeholder, textarea::placeholder { color: var(--text-muted); }
textarea {
  resize: none;
  font-family: 'SFMono-Regular', 'Consolas', 'Courier New', monospace;
  font-size: 0.78rem;
  line-height: 1.5;
}

.field-row {
  display: flex;
  gap: 0.75rem;
}
.field-row .field-col { flex: 1; }
.field-row .field-col-sm { width: 90px; flex-shrink: 0; }

/* ── Buttons ── */
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 0.5rem;
  padding: 0.75rem 1.5rem;
  border: 1px solid var(--green);
  border-radius: 5px;
  background: transparent;
  color: var(--green);
  font-family: inherit;
  font-size: 0.9rem;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  cursor: pointer;
  transition: background 0.2s, color 0.2s, box-shadow 0.2s, transform 0.1s;
  width: 100%;
  margin-top: 0.75rem;
}
.btn:hover {
  background: var(--green);
  color: var(--bg);
  box-shadow: 0 0 20px var(--green-glow);
}
.btn:active { transform: scale(0.98); }
.btn:disabled {
  opacity: 0.3;
  cursor: not-allowed;
  pointer-events: none;
}
.btn-big {
  font-size: 1rem;
  padding: 0.9rem 2rem;
  margin-top: 1.5rem;
  letter-spacing: 0.1em;
}
.btn-sm {
  padding: 0.4rem 0.85rem;
  font-size: 0.75rem;
  width: auto;
  margin-top: 0;
}
.btn-danger {
  border-color: var(--red);
  color: var(--red);
}
.btn-danger:hover {
  background: var(--red);
  color: #fff;
  box-shadow: 0 0 16px rgba(255,68,68,0.2);
}
.btn-secondary {
  border-color: var(--border);
  color: var(--text-dim);
}
.btn-secondary:hover {
  background: var(--surface2);
  color: var(--text);
  box-shadow: none;
}
.btn-row {
  display: flex;
  gap: 0.75rem;
  margin-top: 0.75rem;
}
.btn-row .btn { flex: 1; }

/* ── Status indicators ── */
.spinner {
  display: inline-block;
  width: 22px; height: 22px;
  border: 2px solid var(--border);
  border-top-color: var(--green);
  border-radius: 50%;
  animation: spin 0.7s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

.status-msg {
  text-align: center;
  padding: 1rem;
  font-size: 0.95rem;
  line-height: 1.5;
}
.status-msg.success { color: var(--green); }
.status-msg.error { color: var(--red); }

/* success checkmark animation */
@keyframes checkPop {
  0% { transform: scale(0); opacity: 0; }
  50% { transform: scale(1.2); }
  100% { transform: scale(1); opacity: 1; }
}
.check-anim {
  display: inline-block;
  font-size: 2.5rem;
  animation: checkPop 0.4s ease forwards;
}
@keyframes xPop {
  0% { transform: scale(0) rotate(-15deg); opacity: 0; }
  100% { transform: scale(1) rotate(0); opacity: 1; }
}
.x-anim {
  display: inline-block;
  font-size: 2.5rem;
  animation: xPop 0.3s ease forwards;
}

/* ── Host list ── */
.host-list { list-style: none; }
.host-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  padding: 0.75rem 0.85rem;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 5px;
  margin-bottom: 0.5rem;
}
.host-item:last-child { margin-bottom: 0; }
.host-dot {
  width: 8px; height: 8px;
  border-radius: 50%;
  background: var(--green);
  box-shadow: 0 0 6px var(--green-glow);
  flex-shrink: 0;
}
.host-info { flex: 1; min-width: 0; }
.host-name { font-weight: 700; color: var(--green); font-size: 0.95rem; }
.host-addr { font-size: 0.78rem; color: var(--text-dim); margin-top: 0.1rem; }

/* ── Copyable area ── */
.copy-wrap { position: relative; }
.copy-btn {
  position: absolute;
  top: 6px; right: 6px;
  padding: 0.25rem 0.6rem;
  font-size: 0.7rem;
  font-weight: 600;
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 3px;
  color: var(--text-dim);
  cursor: pointer;
  font-family: inherit;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  transition: all 0.15s;
}
.copy-btn:hover { color: var(--green); border-color: var(--green); }
.copy-btn.copied { color: var(--green); border-color: var(--green); }

/* ── Expandable section ── */
.expand-toggle {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  background: none;
  border: none;
  color: var(--text-dim);
  font-family: inherit;
  font-size: 0.82rem;
  cursor: pointer;
  padding: 0.5rem 0 0.25rem;
  transition: color 0.2s;
}
.expand-toggle:hover { color: var(--text); }
.expand-toggle .arrow {
  display: inline-block;
  transition: transform 0.2s;
  font-size: 0.7rem;
}
.expand-toggle.open .arrow { transform: rotate(90deg); }
.expand-content {
  display: none;
  padding-top: 0.5rem;
}
.expand-content.open { display: block; animation: fadeIn 0.2s ease; }

/* ── Theme picker ── */
.theme-families { display: flex; flex-direction: column; gap: 1.25rem; }
.theme-family-name {
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.1em;
  margin-bottom: 0.5rem;
}
.theme-swatches {
  display: flex;
  flex-wrap: wrap;
  gap: 0.6rem;
}
.swatch {
  width: 56px; height: 40px;
  border-radius: 5px;
  border: 2px solid transparent;
  cursor: pointer;
  transition: border-color 0.2s, transform 0.15s, box-shadow 0.2s;
  position: relative;
  overflow: hidden;
}
.swatch:hover { transform: scale(1.08); }
.swatch.selected {
  border-color: var(--green);
  box-shadow: 0 0 12px var(--green-glow);
}
.swatch-inner {
  position: absolute;
  inset: 0;
  display: flex;
}
.swatch-bg {
  flex: 1;
}
.swatch-accent {
  width: 8px;
}
.swatch-label {
  position: absolute;
  bottom: -18px;
  left: 50%;
  transform: translateX(-50%);
  font-size: 0.6rem;
  color: var(--text-muted);
  white-space: nowrap;
  transition: color 0.2s;
}
.swatch.selected .swatch-label { color: var(--green); }
.swatch-wrap {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding-bottom: 18px;
}
.swatch-tooltip {
  display: none;
  position: absolute;
  bottom: calc(100% + 6px);
  left: 50%;
  transform: translateX(-50%);
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 3px;
  padding: 0.2rem 0.5rem;
  font-size: 0.7rem;
  color: var(--text);
  white-space: nowrap;
  z-index: 10;
  pointer-events: none;
}
.swatch:hover .swatch-tooltip { display: block; }

/* ── Finish summary ── */
.summary-text {
  text-align: center;
  color: var(--text-dim);
  font-size: 0.9rem;
  margin-bottom: 1rem;
  line-height: 1.5;
}
.summary-row {
  display: flex;
  justify-content: space-between;
  padding: 0.6rem 0;
  border-bottom: 1px solid rgba(255,255,255,0.06);
  font-size: 0.92rem;
}
.summary-row:last-child { border-bottom: none; }
.summary-label { color: var(--text-dim); }
.summary-value { color: var(--green); font-weight: 600; }

/* ── Connecting overlay ── */
.overlay {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.9);
  z-index: 100;
  justify-content: center;
  align-items: center;
  flex-direction: column;
  gap: 1.25rem;
}
.overlay.show { display: flex; }
.overlay .spinner { width: 44px; height: 44px; border-width: 3px; }
.overlay p { color: var(--green); font-size: 1.1rem; letter-spacing: 0.06em; }

/* ── Misc ── */
code {
  font-family: 'SFMono-Regular', 'Consolas', 'Courier New', monospace;
  color: var(--green);
  font-size: 0.88em;
}
.note {
  font-size: 0.8rem;
  color: var(--yellow);
  margin-top: 0.5rem;
}
.hint {
  font-size: 0.78rem;
  color: var(--text-muted);
  margin-top: 0.5rem;
  line-height: 1.4;
}
</style>
</head>
<body>
<div class="wizard">
  <div class="progress" id="progress"></div>
  <div id="steps"></div>
</div>
<div class="overlay" id="overlay">
  <div class="spinner"></div>
  <p id="overlay-msg">Saving configuration...</p>
</div>

<script>
(function(){
"use strict";

var STEPS = ['welcome','add-server','test','servers','finish'];
var state = {
  step: 0,
  hosts: [],
  currentHost: { name:'', host:'', user:'', port:22 },
  sshKey: null,
  theme: 'Panel/Gold',
  themes: null,
  testResult: null,
  testPassword: '',
};

function $(sel, ctx) { return (ctx||document).querySelector(sel); }
function $$(sel, ctx) { return Array.from((ctx||document).querySelectorAll(sel)); }

/* ── Progress dots with connecting lines ── */
function renderProgress() {
  var el = document.getElementById('progress');
  var html = '';
  for (var i = 0; i < STEPS.length; i++) {
    var cls = 'dot';
    if (i < state.step) cls += ' done';
    if (i === state.step) cls += ' active';
    html += '<div class="' + cls + '"></div>';
    if (i < STEPS.length - 1) {
      html += '<div class="dot-line' + (i < state.step ? ' done' : '') + '"></div>';
    }
  }
  el.innerHTML = html;
}

/* ── Step rendering ── */
function goTo(n) {
  state.step = Math.max(0, Math.min(STEPS.length-1, n));
  renderProgress();
  renderStep();
}

function renderStep() {
  var container = document.getElementById('steps');
  var stepName = STEPS[state.step];
  var renderers = {
    'welcome': renderWelcome,
    'add-server': renderAddServer,
    'test': renderTest,
    'servers': renderServers,
    'finish': renderFinish,
  };
  container.innerHTML = '<div class="step active">' + renderers[stepName]() + '</div>';
  bindStep(stepName);
}

/* ── Step 0: Welcome ── */
function renderWelcome() {
  return '' +
    '<div style="padding:2.5rem 0;text-align:center;">' +
      '<h1>CHIKETI<br>APPLIANCE</h1>' +
      '<p class="subtitle" style="margin-top:1rem;font-size:0.95rem;color:var(--text-dim);">Remote System Monitor</p>' +
      '<p class="subtitle" style="margin-top:1.5rem;">' +
        'Monitor your Linux servers from a single dashboard.<br>' +
        'No software needed on your servers &mdash; just SSH access.' +
      '</p>' +
      '<p class="desc">' +
        'This wizard will connect to your servers, set up SSH keys,<br>' +
        'and configure the dashboard theme.' +
      '</p>' +
      '<button class="btn btn-big" id="btn-start">Get Started</button>' +
    '</div>';
}

/* ── Step 1: Add Server ── */
function renderAddServer() {
  var h = state.currentHost;
  var filled = h.name && h.host && h.user;
  return '' +
    '<h2>Add Server</h2>' +
    '<div class="card">' +
      '<label for="srv-name">Friendly Name</label>' +
      '<input type="text" id="srv-name" placeholder="my-server" value="' + esc(h.name) + '">' +
      '<label for="srv-host">Host / IP Address</label>' +
      '<input type="text" id="srv-host" placeholder="192.168.1.50" value="' + esc(h.host) + '">' +
      '<div class="field-row" style="margin-top:0.85rem;">' +
        '<div class="field-col">' +
          '<label for="srv-user" style="margin-top:0;">Username</label>' +
          '<input type="text" id="srv-user" placeholder="rohan" value="' + esc(h.user) + '">' +
        '</div>' +
        '<div class="field-col-sm">' +
          '<label for="srv-port" style="margin-top:0;">Port</label>' +
          '<input type="number" id="srv-port" value="' + h.port + '" min="1" max="65535">' +
        '</div>' +
      '</div>' +
      '<label for="srv-password" style="margin-top:0.85rem;">SSH Password</label>' +
      '<input type="password" id="srv-password" placeholder="Server password (used once to set up key)" value="' + esc(state.testPassword) + '">' +
      '<p class="hint" style="margin-top:0.25rem;">The password is used once to copy the SSH key. It is never stored.</p>' +
    '</div>' +
    '<button class="btn" id="btn-to-test" ' + (filled ? '' : 'disabled') + '>Connect</button>' +
    (state.hosts.length > 0 ? '<button class="btn btn-secondary" id="btn-add-cancel" style="margin-top:0.5rem;">Cancel</button>' : '');
}

/* ── Step 2: SSH Key ── */
function renderSSHKey() {
  if (!state.sshKey) {
    return '' +
      '<h2>SSH Key</h2>' +
      '<div class="card">' +
        '<div class="status-msg"><div class="spinner"></div><br>Loading SSH key...</div>' +
      '</div>';
  }
  var k = state.sshKey;
  var genNote = k.generated
    ? '<p class="note">A new SSH key was generated for this appliance at <code>' + esc(k.key_path) + '</code></p>'
    : '';
  var cmdText = "echo '" + k.public_key + "' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys";
  return '' +
    '<h2>SSH Key</h2>' +
    '<div class="card">' +
      '<p style="font-size:0.85rem;color:var(--text-dim);margin-bottom:0.75rem;">' +
        'Add this key to your server\'s <code>~/.ssh/authorized_keys</code>:' +
      '</p>' +
      '<div class="copy-wrap">' +
        '<textarea id="pubkey-area" rows="3" readonly>' + esc(k.public_key) + '</textarea>' +
        '<button class="copy-btn" id="btn-copy-key">COPY</button>' +
      '</div>' +
      genNote +
      '<p style="font-size:0.82rem;color:var(--text-dim);margin-top:1rem;">Run this on your server:</p>' +
      '<div class="copy-wrap" style="margin-top:0.3rem;">' +
        '<textarea id="cmd-area" rows="2" readonly>' + esc(cmdText) + '</textarea>' +
        '<button class="copy-btn" id="btn-copy-cmd">COPY</button>' +
      '</div>' +
    '</div>' +
    '<button class="btn" id="btn-key-done">I\'ve Added the Key</button>' +
    '<div class="card" style="margin-top:0.75rem;">' +
      '<button class="expand-toggle" id="btn-expand-pw">' +
        '<span class="arrow">&#9654;</span>' +
        'Or enter password for one-time key setup' +
      '</button>' +
      '<div class="expand-content" id="pw-section">' +
        '<p class="hint" style="margin-top:0.25rem;margin-bottom:0.5rem;">' +
          'Enter the server password to automatically copy the key via SSH. The password is not stored.' +
        '</p>' +
        '<label for="ssh-password" style="margin-top:0;">Server Password</label>' +
        '<input type="password" id="ssh-password" placeholder="Enter server password" value="' + esc(state.testPassword) + '">' +
        '<button class="btn btn-sm" id="btn-auto-copy" style="margin-top:0.6rem;width:100%;">Copy Key Automatically</button>' +
      '</div>' +
    '</div>';
}

/* ── Step 2: Test Connection ── */
function renderTest() {
  var statusHTML = '';
  var phase = state.testPhase || '';
  if (state.testResult === null) {
    statusHTML = '<div class="status-msg"><div class="spinner"></div><br>' + (phase || 'Setting up connection...') + '</div>';
  } else if (state.testResult === 'loading') {
    statusHTML = '<div class="status-msg"><div class="spinner"></div><br>' + (phase || 'Connecting...') + '</div>';
  } else if (state.testResult.success) {
    statusHTML = '' +
      '<div class="status-msg success">' +
        '<span class="check-anim">&#10003;</span><br>' +
        'Connected successfully!<br>' +
        '<span style="font-size:0.85rem;color:var(--text-dim);display:inline-block;margin-top:0.5rem;">' +
          'Hostname: <strong style="color:var(--green);">' + esc(state.testResult.hostname) + '</strong><br>' +
          'Uptime: ' + esc(state.testResult.uptime || 'N/A') +
        '</span>' +
      '</div>';
  } else {
    statusHTML = '' +
      '<div class="status-msg error">' +
        '<span class="x-anim">&#10007;</span><br>' +
        'Connection failed<br>' +
        '<span style="font-size:0.82rem;display:inline-block;margin-top:0.35rem;">' + esc(state.testResult.error) + '</span>' +
      '</div>';
  }
  var isLoading = state.testResult === null || state.testResult === 'loading';
  var canProceed = state.testResult && state.testResult !== 'loading' && state.testResult.success;
  return '' +
    '<h2>Connecting</h2>' +
    '<div class="card">' +
      '<p style="font-size:0.85rem;color:var(--text-dim);margin-bottom:0.75rem;">' +
        'Server: <strong style="color:var(--text);">' + esc(state.currentHost.name) + '</strong> &mdash; ' +
        '<span style="color:var(--text-muted);">' + esc(state.currentHost.user) + '@' + esc(state.currentHost.host) + ':' + state.currentHost.port + '</span>' +
      '</p>' +
      statusHTML +
      ((!isLoading && !canProceed) ? '<button class="btn" id="btn-test">Retry</button>' : '') +
    '</div>' +
    '<div class="btn-row">' +
      '<button class="btn btn-secondary" id="btn-test-back">Back</button>' +
      '<button class="btn" id="btn-test-next" ' + (canProceed ? '' : 'disabled') + '>Add Server</button>' +
    '</div>';
}

/* ── Step 4: Server List ── */
function renderServers() {
  var listHTML = '';
  if (state.hosts.length === 0) {
    listHTML = '<p class="status-msg" style="color:var(--text-dim);">No servers added yet.</p>';
  } else {
    var items = '';
    for (var i = 0; i < state.hosts.length; i++) {
      var h = state.hosts[i];
      items += '' +
        '<li class="host-item">' +
          '<div class="host-dot"></div>' +
          '<div class="host-info">' +
            '<div class="host-name">' + esc(h.name) + '</div>' +
            '<div class="host-addr">' + esc(h.user) + '@' + esc(h.host) + ':' + h.port + '</div>' +
          '</div>' +
          '<button class="btn btn-sm btn-danger" data-remove="' + esc(h.name) + '">&times;</button>' +
        '</li>';
    }
    listHTML = '<ul class="host-list">' + items + '</ul>';
  }
  return '' +
    '<h2>Servers (' + state.hosts.length + ')</h2>' +
    '<div class="card">' + listHTML + '</div>' +
    '<button class="btn btn-secondary" id="btn-add-another" style="margin-top:0;">+ Add Another Server</button>' +
    '<button class="btn" id="btn-to-finish" ' + (state.hosts.length === 0 ? 'disabled' : '') + '>Finish Setup</button>';
}

/* ── Step 5: Theme Picker ── */
function renderTheme() {
  if (!state.themes) {
    return '' +
      '<h2>Choose Theme</h2>' +
      '<div class="card">' +
        '<div class="status-msg"><div class="spinner"></div><br>Loading themes...</div>' +
      '</div>';
  }
  var familiesHTML = '';
  var families = state.themes.families;
  var famKeys = Object.keys(families);
  for (var f = 0; f < famKeys.length; f++) {
    var fam = famKeys[f];
    var variants = families[fam];
    var swatchesHTML = '';
    var vKeys = Object.keys(variants);
    for (var v = 0; v < vKeys.length; v++) {
      var vname = vKeys[v];
      var vdata = variants[vname];
      var fullName = fam + '/' + vname;
      var sel = state.theme === fullName ? ' selected' : '';
      swatchesHTML += '' +
        '<div class="swatch-wrap">' +
          '<div class="swatch' + sel + '" data-theme="' + esc(fullName) + '">' +
            '<div class="swatch-inner">' +
              '<div class="swatch-bg" style="background:' + vdata.background + ';"></div>' +
              '<div class="swatch-bg" style="background:' + vdata.panel + ';"></div>' +
              '<div class="swatch-accent" style="background:' + vdata.primary + ';"></div>' +
            '</div>' +
            '<div class="swatch-tooltip">' + esc(fam + '/' + vname) + '</div>' +
          '</div>' +
          '<span class="swatch-label">' + esc(vname) + '</span>' +
        '</div>';
    }
    familiesHTML += '' +
      '<div>' +
        '<div class="theme-family-name">' + esc(fam) + '</div>' +
        '<div class="theme-swatches">' + swatchesHTML + '</div>' +
      '</div>';
  }
  return '' +
    '<h2>Choose Theme</h2>' +
    '<div class="card">' +
      '<div class="theme-families">' + familiesHTML + '</div>' +
    '</div>' +
    '<p style="text-align:center;font-size:0.82rem;color:var(--text-dim);margin-bottom:0.25rem;">' +
      'Selected: <strong style="color:var(--green);">' + esc(state.theme) + '</strong>' +
    '</p>' +
    '<div class="btn-row">' +
      '<button class="btn btn-secondary" id="btn-theme-back">Back</button>' +
      '<button class="btn" id="btn-to-finish">Finish Setup</button>' +
    '</div>';
}

/* ── Step 6: Finish ── */
function renderFinish() {
  var hostNames = '';
  for (var i = 0; i < state.hosts.length; i++) {
    if (i > 0) hostNames += ', ';
    hostNames += state.hosts[i].name;
  }
  var serverWord = state.hosts.length === 1 ? 'server' : 'servers';
  return '' +
    '<h2>Ready to Go</h2>' +
    '<p class="summary-text">' +
      'Setting up <strong style="color:var(--green);">' + state.hosts.length + ' ' + serverWord + '</strong> ' +
      'with theme <strong style="color:var(--green);">' + esc(state.theme) + '</strong>' +
    '</p>' +
    '<div class="card">' +
      '<div class="summary-row">' +
        '<span class="summary-label">Servers</span>' +
        '<span class="summary-value">' + state.hosts.length + '</span>' +
      '</div>' +
      '<div class="summary-row">' +
        '<span class="summary-label">Hosts</span>' +
        '<span class="summary-value">' + esc(hostNames) + '</span>' +
      '</div>' +
      '<div class="summary-row">' +
        '<span class="summary-label">Theme</span>' +
        '<span class="summary-value">' + esc(state.theme) + '</span>' +
      '</div>' +
    '</div>' +
    '<button class="btn btn-big" id="btn-finish">Start Monitoring</button>' +
    '<button class="btn btn-secondary" id="btn-finish-back" style="margin-top:0.5rem;">Back</button>';
}

/* ── Event binding ── */
function bindStep(stepName) {
  switch(stepName) {
    case 'welcome':
      on('btn-start', function() { goTo(1); });
      break;

    case 'add-server':
      var nameEl = document.getElementById('srv-name');
      var hostEl = document.getElementById('srv-host');
      var userEl = document.getElementById('srv-user');
      var portEl = document.getElementById('srv-port');
      var nextBtn = document.getElementById('btn-to-test');

      function checkFields() {
        var n = nameEl ? nameEl.value.trim() : '';
        var h = hostEl ? hostEl.value.trim() : '';
        var u = userEl ? userEl.value.trim() : '';
        if (nextBtn) nextBtn.disabled = !(n && h && u);
        /* Keep state in sync as user types */
        state.currentHost.name = n;
        state.currentHost.host = h;
        state.currentHost.user = u;
        state.currentHost.port = parseInt((portEl ? portEl.value : '22'), 10) || 22;
      }

      if (nameEl) nameEl.addEventListener('input', checkFields);
      if (hostEl) hostEl.addEventListener('input', checkFields);
      if (userEl) userEl.addEventListener('input', checkFields);
      if (portEl) portEl.addEventListener('input', checkFields);

      on('btn-to-test', function() {
        var name = val('srv-name'), host = val('srv-host'), user = val('srv-user');
        var port = parseInt($('#srv-port').value, 10) || 22;
        var pw = val('srv-password');
        if (!name || !host || !user) return;
        state.currentHost = { name:name, host:host, user:user, port:port };
        state.testPassword = pw;
        state.testResult = null;
        state.testPhase = '';
        goTo(2);
        setTimeout(function() { doFullConnect(); }, 100);
      });
      on('btn-add-cancel', function() { goTo(3); });
      break;

    case 'test':
      on('btn-test', function() {
        state.testResult = null;
        state.testPhase = '';
        renderStep();
        setTimeout(function() { doFullConnect(); }, 100);
      });
      on('btn-test-back', function() { goTo(1); });
      on('btn-test-next', function() { addHost(); });
      break;

    case 'servers':
      on('btn-add-another', function() {
        state.currentHost = { name:'', host:'', user:'', port:22 };
        state.testResult = null;
        state.testPassword = '';
        goTo(1);
      });
      on('btn-to-finish', function() {
        goTo(4);
      });
      $$('[data-remove]').forEach(function(btn) {
        btn.addEventListener('click', function() { removeHost(btn.dataset.remove); });
      });
      break;

    case 'theme':
      $$('.swatch').forEach(function(s) {
        s.addEventListener('click', function() {
          state.theme = s.dataset.theme;
          renderStep();
        });
      });
      on('btn-theme-back', function() { goTo(3); });
      on('btn-to-finish', function() { goTo(5); });
      break;

    case 'finish':
      on('btn-finish', doFinish);
      on('btn-finish-back', function() { goTo(3); });
      break;
  }
}

/* ── Helpers ── */
function on(id, fn) {
  var el = document.getElementById(id);
  if (el) el.addEventListener('click', fn);
}
function val(id) { var el = document.getElementById(id); return el ? el.value.trim() : ''; }
function esc(s) {
  if (s == null) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function copyText(id, btnId) {
  var el = document.getElementById(id);
  if (!el) return;
  el.select();
  navigator.clipboard.writeText(el.value).catch(function() { document.execCommand('copy'); });
  var btn = btnId ? document.getElementById(btnId) : el.parentElement.querySelector('.copy-btn');
  if (btn) {
    btn.textContent = 'COPIED';
    btn.classList.add('copied');
    setTimeout(function() { btn.textContent = 'COPY'; btn.classList.remove('copied'); }, 2000);
  }
}

function api(method, path, body) {
  var opts = { method: method, headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  return fetch(path, opts).then(function(r) {
    if (!r.ok) {
      return r.text().then(function(text) {
        try { return JSON.parse(text); } catch(e) {}
        return { success: false, error: 'Server error: ' + r.status };
      });
    }
    return r.json();
  });
}

/* ── API calls ── */
function fetchSSHKey() {
  if (state.sshKey) return;
  api('GET', '/api/setup/ssh-key').then(function(data) {
    state.sshKey = data;
  }).catch(function(e) {
    state.sshKey = { public_key: 'Error loading key: ' + e.message, key_path: '', generated: false };
  }).then(function() {
    if (STEPS[state.step] === 'ssh-key') renderStep();
  });
}

function fetchThemes() {
  if (state.themes) { console.log('[setup] themes already cached'); renderStep(); return; }
  console.log('[setup] fetching themes...');
  var xhr = new XMLHttpRequest();
  xhr.open('GET', '/api/setup/themes', true);
  xhr.onload = function() {
    console.log('[setup] themes xhr status:', xhr.status);
    if (xhr.status === 200) {
      try {
        state.themes = JSON.parse(xhr.responseText);
        console.log('[setup] themes parsed OK, families:', Object.keys(state.themes.families || {}).length);
      } catch(e) {
        console.error('[setup] themes parse error:', e);
        state.themes = { families: {} };
      }
    } else {
      console.error('[setup] themes bad status:', xhr.status);
      state.themes = { families: {} };
    }
    console.log('[setup] calling renderStep after themes load');
    renderStep();
  };
  xhr.onerror = function() {
    console.error('[setup] themes xhr error');
    state.themes = { families: {} };
    renderStep();
  };
  xhr.send();
}

function doFullConnect() {
  var h = state.currentHost;
  var pw = state.testPassword;

  /* Step 1: If password provided, copy SSH key first */
  if (pw) {
    state.testPhase = 'Copying SSH key to ' + h.host + '...';
    state.testResult = 'loading';
    renderStep();

    api('POST', '/api/setup/copy-key', {
      host: h.host, user: h.user, port: h.port, password: pw
    }).then(function(data) {
      if (!data.success) {
        state.testResult = { success: false, error: 'Key copy failed: ' + (data.error || 'Unknown error') };
        renderStep();
        return;
      }
      state.testPassword = '';
      /* Step 2: Test with key (no password) */
      doTestConnection();
    }).catch(function(e) {
      state.testResult = { success: false, error: 'Key copy error: ' + e.message };
      renderStep();
    });
  } else {
    /* No password — try key-based auth directly */
    doTestConnection();
  }
}

function doTestConnection() {
  var h = state.currentHost;
  state.testPhase = 'Testing connection to ' + h.host + '...';
  state.testResult = 'loading';
  renderStep();

  api('POST', '/api/setup/test-connection', {
    host: h.host, user: h.user, port: h.port
  }).then(function(data) {
    state.testResult = data;
  }).catch(function(e) {
    state.testResult = { success: false, error: e.message };
  }).then(function() {
    state.testPhase = '';
    renderStep();
  });
}

function addHost() {
  api('POST', '/api/setup/add-host', state.currentHost).then(function(data) {
    if (data.success) {
      state.hosts = data.hosts;
      goTo(4);
    } else {
      alert(data.error || 'Failed to add host');
    }
  }).catch(function(e) {
    alert('Error: ' + e.message);
  });
}

function removeHost(name) {
  api('POST', '/api/setup/remove-host', { name: name }).then(function(data) {
    if (data.success) {
      state.hosts = data.hosts;
      renderStep();
    }
  }).catch(function(e) {
    alert('Error: ' + e.message);
  });
}

function doFinish() {
  var btn = document.getElementById('btn-finish');
  if (btn) btn.disabled = true;
  var overlay = document.getElementById('overlay');
  var msg = document.getElementById('overlay-msg');
  overlay.classList.add('show');
  msg.textContent = 'Saving configuration...';
  api('POST', '/api/setup/finish', { theme: state.theme }).then(function(data) {
    if (data.success) {
      msg.textContent = 'Setup complete! Loading dashboard...';
      setTimeout(function() { window.location.href = '/'; }, 2000);
    } else {
      overlay.classList.remove('show');
      alert(data.error || 'Setup failed');
      if (btn) btn.disabled = false;
    }
  }).catch(function(e) {
    overlay.classList.remove('show');
    alert('Error: ' + e.message);
    if (btn) btn.disabled = false;
  });
}

/* ── Init ── */
goTo(0);

})();
</script>
</body>
</html>"""


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
    # The display page reuses all existing JS screen renderers from the control panel.
    # It runs fullscreen at 1024x600 with no chrome, auto-rotates, polls metrics.
    html = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chiketi Display</title>
<style>
  @font-face {
    font-family: 'Chakra Petch';
    src: url('/assets/fonts/ChakraPetch-Bold.ttf') format('truetype');
    font-weight: bold; font-style: normal;
  }
  @font-face {
    font-family: 'Chakra Petch';
    src: url('/assets/fonts/ChakraPetch-Regular.ttf') format('truetype');
    font-weight: normal; font-style: normal;
  }
  @font-face {
    font-family: 'Antonio';
    src: url('/assets/fonts/Antonio-VariableFont.ttf') format('truetype');
    font-weight: 100 700; font-style: normal;
  }
  @font-face {
    font-family: 'Rajdhani';
    src: url('/assets/fonts/Rajdhani-SemiBold.ttf') format('truetype');
    font-weight: 600; font-style: normal;
  }
  @font-face {
    font-family: 'Rajdhani';
    src: url('/assets/fonts/Rajdhani-Regular.ttf') format('truetype');
    font-weight: normal; font-style: normal;
  }
  @font-face {
    font-family: 'Share Tech Mono';
    src: url('/assets/fonts/ShareTechMono-Regular.ttf') format('truetype');
    font-weight: normal; font-style: normal;
  }
  @font-face {
    font-family: 'Nixie One';
    src: url('/assets/fonts/NixieOne-Regular.ttf') format('truetype');
    font-weight: normal; font-style: normal;
  }
  @font-face {
    font-family: 'IBM Plex Mono';
    src: url('/assets/fonts/IBMPlexMono-Regular.ttf') format('truetype');
    font-weight: normal; font-style: normal;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body { width: 100vw; height: 100vh; overflow: hidden; background: #000; }
  body { cursor: none; }

  /* ── Terminal panels ── */
  .t-screen { display: grid; gap: 6px; padding: 6px; width: 100%; height: 100%; }
  .t-2col-3row {
    grid-template-columns: 1fr 1fr;
    grid-template-rows: 4fr 3fr 3fr;
  }
  .t-1col-3row {
    grid-template-columns: 1fr;
    grid-template-rows: 5fr 3fr 2fr;
  }
  .t-panel { padding: 8px 10px; overflow: hidden; }
  .t-title {
    font-size: 18px; margin-bottom: 6px; white-space: nowrap;
    font-family: 'Consolas', 'DejaVu Sans Mono', monospace;
  }
  .t-row {
    display: flex; align-items: center; gap: 6px;
    font-size: 16px; margin-bottom: 3px; white-space: nowrap;
    font-family: 'Consolas', 'DejaVu Sans Mono', monospace;
  }
  .t-label { flex-shrink: 0; }
  .t-bar {
    flex: 1; height: 16px; display: flex; border-radius: 1px; overflow: hidden;
    font-size: 14px; font-family: 'Consolas', monospace; line-height: 16px;
  }
  .t-val { flex-shrink: 0; }

  /* ── Panel layout (sizes in cqw = % of 1024px container width) ── */
  .screen-frame {
    width: 1024px; height: 600px; position: relative; container-type: inline-size;
  }
  .l-screen {
    display: grid; gap: 0.78cqw; padding: 0.78cqw; width: 100%; height: 100%;
    font-family: 'Chakra Petch', sans-serif;
  }
  .l-2x2 { grid-template-columns: 1fr 1fr; grid-template-rows: 1fr 1fr; }
  .l-top-2bot {
    grid-template-columns: 1fr 1fr;
    grid-template-rows: 55fr 45fr;
  }
  .l-top-2bot .l-panel:first-child { grid-column: 1 / -1; }
  .l-clock-layout {
    grid-template-columns: 1fr;
    grid-template-rows: 1fr;
  }
  .l-panel { display: flex; flex-direction: column; overflow: hidden; border-radius: 2px; }
  .l-titlebar {
    font-size: 3.32cqw; font-weight: bold; color: #000; padding: 0.29cqw 0.78cqw;
    text-transform: uppercase; letter-spacing: 0.05cqw; white-space: nowrap;
    flex-shrink: 0; font-family: 'Chakra Petch', sans-serif;
  }
  .l-body {
    flex: 1; background: #0a0a0a; padding: 0.39cqw 0.78cqw;
    display: flex; flex-direction: column; justify-content: flex-start; gap: 0.39cqw;
  }
  .l-stat {
    display: flex; justify-content: space-between; align-items: baseline;
    font-size: 2.73cqw; white-space: nowrap;
  }
  .l-stat-label {
    color: #ccc; text-transform: uppercase; font-size: 2.73cqw;
    font-weight: bold; font-family: 'Chakra Petch', sans-serif;
  }
  .l-stat-val {
    font-weight: bold; font-size: 3.13cqw;
    font-family: 'Chakra Petch', sans-serif;
  }
  .l-bar {
    height: 1.17cqw; background: #282828; border-radius: 2px; overflow: hidden;
  }
  .l-bar-fill {
    height: 100%; border-radius: 2px; position: relative;
  }
  .l-bar-fill::after {
    content: ''; position: absolute; top: 0; left: 0; right: 0; height: 15%;
    background: rgba(255,255,255,0.3); border-radius: 2px 2px 0 0;
  }
  .l-kv {
    display: flex; gap: 1.17cqw; font-size: 3.13cqw; align-items: baseline;
  }
  .l-kv-label {
    color: #ccc; text-transform: uppercase; font-size: 2.73cqw;
    font-weight: bold; font-family: 'Chakra Petch', sans-serif;
  }
  .l-kv-val {
    font-weight: bold; font-size: 3.13cqw;
    font-family: 'Chakra Petch', sans-serif;
  }

  /* ── Clock ── */
  .l-clock-body {
    flex: 1; background: #0a0a0a; display: flex; flex-direction: column;
    align-items: center; justify-content: center; gap: 1.56cqw;
  }
  .l-clock-time {
    font-size: 11.72cqw; font-weight: bold; letter-spacing: 0.39cqw;
    font-family: 'Chakra Petch', sans-serif;
  }
  .l-clock-sec {
    font-size: 4.69cqw; font-weight: bold;
    font-family: 'Chakra Petch', sans-serif; margin-left: 0.78cqw;
  }
  .l-clock-day {
    font-size: 2.73cqw; text-transform: uppercase; letter-spacing: 0.2cqw;
    font-weight: normal; font-family: 'Chakra Petch', sans-serif;
  }
  .l-clock-date {
    font-size: 2.73cqw; text-transform: uppercase; letter-spacing: 0.1cqw;
    font-weight: normal; font-family: 'Chakra Petch', sans-serif;
  }

  /* ── 4-col grid inside NPU panel ── */
  .l-4col { display: grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap: 0.78cqw; }

  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.12} }
  @keyframes spin { from{transform:rotate(0deg)} to{transform:rotate(360deg)} }
  @keyframes nixieFlicker { 0%{opacity:1} 5%{opacity:0.98} 10%{opacity:1} 15%{opacity:0.96} 17%{opacity:1} 50%{opacity:1} 52%{opacity:0.97} 54%{opacity:1} 80%{opacity:1} 82%{opacity:0.95} 83%{opacity:1} 90%{opacity:0.98} 100%{opacity:1} }
  @keyframes nixieMicroFlicker { 0%,100%{filter:brightness(1)} 25%{filter:brightness(0.97)} 50%{filter:brightness(1.02)} 75%{filter:brightness(0.98)} }
  @keyframes spinFan { from{transform:rotate(0deg)} to{transform:rotate(360deg)} }

  /* ── Host bar (bottom of display) ── */
  #host-bar {
    position: fixed; bottom: 0; left: 0; right: 0;
    height: 28px; background: rgba(0,0,0,0.85);
    display: flex; align-items: center; justify-content: center; gap: 12px;
    font-family: 'Chakra Petch', monospace; font-size: 12px;
    z-index: 9999; border-top: 1px solid #333;
    opacity: 0.7; transition: opacity 0.3s;
  }
  #host-bar:hover { opacity: 1; }
  #host-bar .host-btn {
    background: none; border: 1px solid #444; color: #888;
    padding: 2px 10px; border-radius: 3px; cursor: pointer;
    font-family: inherit; font-size: 11px; transition: all 0.2s;
  }
  #host-bar .host-btn.active { color: #0f0; border-color: #0f0; }
  #host-bar .host-btn.offline { color: #f44; border-color: #f44; opacity: 0.6; }
  #host-bar .host-btn.active.offline { color: #f80; border-color: #f80; }
</style>
</head>
<body>
<div id="display"></div>
<div id="host-bar"></div>

<script>
/* Scale the 1024x600 screen-frame to fill the viewport */
function scaleDisplay() {
  const frame = document.querySelector('.screen-frame');
  if (!frame) return;
  const sx = window.innerWidth / 1024;
  const sy = window.innerHeight / 600;
  const s = Math.max(sx, sy);
  frame.style.transform = 'scale(' + s + ')';
  frame.style.transformOrigin = 'top left';
}
new MutationObserver(scaleDisplay).observe(document.getElementById('display'), {childList: true});
window.addEventListener('resize', scaleDisplay);
</script>

<script>
const API = window.location.origin;
const PANEL_SPEC = __PANEL_SPEC_JSON__;
let metrics = null;
let activeFamily = null, activeVariant = null;
let themeColors = null;
let currentScreenIdx = 0;
let enabledScreens = []; // [{id, name, html, duration}]
let pauseUntil = 0;
let lastRotate = Date.now();
const PAUSE_MS = __PAUSE_S__ * 1000;
const DEFAULT_SCREEN_DURATION = __DEFAULT_SCREEN_DURATION__;
let screenRotation = {}; // {id: {enabled, duration}}
let hostData = null; // {hosts: [...], active_host: "..."}
let lastHostRotate = Date.now();

/* ── Data helpers ── */
function m(key) {
  if (!metrics || !metrics[key]) return { value: null, available: false, unit: '', extra: {} };
  return metrics[key];
}
function mv(key, suffix) {
  const d = m(key);
  if (!d.available) return 'N/A';
  return suffix ? d.value + suffix : String(d.value);
}

function cleanModel() {
  const d = m('llama.model');
  if (!d.available) return '--';
  return String(d.value).replace(/\.gguf$/i, '').replace(/[-_]Q\d[A-Z0-9_]*$/i, '').replace(/_/g, ' ').replace(/-$/, '');
}

/* ── Shared rendering helpers ── */
function tBar(c, pct) {
  if (pct == null) return '';
  pct = Math.max(0, Math.min(100, pct));
  const filled = Math.round(pct / 5), empty = 20 - filled;
  return `<span class="t-bar"><span style="color:${c.primary}">${'\u2588'.repeat(filled)}</span><span style="color:${c.primary};opacity:0.2">${'\u2591'.repeat(empty)}</span></span>`;
}
function tPanel(c, title, rows) {
  return `<div class="t-panel" style="background:${c.panel};border:1px solid ${c.border}">` +
    `<div class="t-title" style="color:${c.header}">\u2500\u2500[ ${title} ]</div>${rows}</div>`;
}
function tRow(c, label, bar, val, color) {
  color = color || c.primary;
  return `<div class="t-row"><span class="t-label" style="color:${c.primary}">${label}</span>` +
    (bar || '') + `<span class="t-val" style="color:${color}">${val}</span></div>`;
}

const GOLD = PANEL_SPEC.colors.gold;
const AMBER = PANEL_SPEC.colors.amber;
const GREEN = PANEL_SPEC.colors.green;
const TEAL = PANEL_SPEC.colors.teal;
function _thermColor(t) {
  if (t >= 90) return PANEL_SPEC.colors.thermOrange || '#FF7700';
  if (t >= 70) return PANEL_SPEC.colors.thermYellow || '#DDCC00';
  if (t >= 50) return PANEL_SPEC.colors.thermGreen || '#22BB44';
  return PANEL_SPEC.colors.thermBlue || '#2288DD';
}
function lPanel(titleLeft, color, body, titleRight) {
  const right = titleRight ? `<span>${titleRight}</span>` : '';
  return `<div class="l-panel" style="border:2px solid ${color}">` +
    `<div class="l-titlebar" style="background:${color};display:flex;justify-content:space-between;align-items:center">`+
    `<span>${titleLeft}</span>${right}</div>` +
    `<div class="l-body">${body}</div></div>`;
}
function lStat(label, val, color) {
  return `<div class="l-stat"><span class="l-stat-label">${label}</span>` +
    `<span class="l-stat-val" style="color:${color}">${val}</span></div>`;
}
function lBar(color, pct) {
  if (pct == null) return '';
  return `<div class="l-bar"><div class="l-bar-fill" style="width:${Math.max(0,Math.min(100,pct))}%;background:${color}"></div></div>`;
}

/* ═══ Screen renderers (identical to control panel) ═══ */

__SCREEN_FUNCTIONS__

/* ── Screen registry for current theme ── */
function getScreenRegistry(c) {
  const isPanel = activeFamily === 'Panel';
  const isVintage = activeFamily === 'Vintage';
  const isCoral = isPanel && activeVariant === 'Coral';
  const isTeal = isPanel && activeVariant === 'Teal';
  let screens;
  if (isTeal) screens = [{id:'screen1',name:'System Stats',fn:panelTealScreen1},{id:'screen2',name:'Clock',fn:panelTealScreen2}];
  else if (isCoral) screens = [{id:'screen1',name:'System Stats',fn:panelCoralScreen1},{id:'screen2',name:'Clock',fn:panelCoralScreen2}];
  else if (isPanel) screens = [{id:'screen1',name:'System Stats',fn:panelGoldScreen1},{id:'screen2',name:'Clock',fn:panelGoldScreen2}];
  else if (isVintage && activeVariant === 'Tubes') screens = [{id:'screen1',name:'System Stats',fn:tubeScreen1},{id:'screen2',name:'Clock',fn:tubeScreen2}];
  else if (isVintage && activeVariant === 'VFD') screens = [{id:'screen1',name:'System Stats',fn:vfdScreen1},{id:'screen2',name:'Clock',fn:vfdScreen2}];
  else if (isVintage) screens = [{id:'screen1',name:'System Stats',fn:scanScreen1},{id:'screen2',name:'Clock',fn:scanScreen2}];
  else screens = [{id:'screen1',name:'System Stats',fn:terminalScreen1},{id:'screen2',name:'AI Monitor',fn:terminalScreen2}];
  screens.push({id:'screen3',name:'Claude Usage',fn:claudeScreen3});
  return screens;
}

function renderDisplay() {
  if (!themeColors || !activeFamily) return;
  const c = themeColors;
  const allScreens = getScreenRegistry(c);
  // Filter to enabled screens
  enabledScreens = allScreens.filter(s => {
    const cfg = screenRotation[s.id];
    return !cfg || cfg.enabled !== false;
  }).map(s => {
    const cfg = screenRotation[s.id];
    return { id: s.id, name: s.name, html: s.fn(c), duration: (cfg && cfg.duration) || DEFAULT_SCREEN_DURATION };
  });
  if (enabledScreens.length === 0) {
    // Fallback: show first screen if all disabled
    enabledScreens = [{ id: allScreens[0].id, name: allScreens[0].name, html: allScreens[0].fn(c), duration: DEFAULT_SCREEN_DURATION }];
  }
  if (currentScreenIdx >= enabledScreens.length) currentScreenIdx = 0;
  document.getElementById('display').innerHTML = enabledScreens[currentScreenIdx].html;
}

/* ── Host bar rendering ── */
function renderHostBar() {
  const bar = document.getElementById('host-bar');
  if (!hostData || !hostData.hosts || hostData.hosts.length <= 1) {
    bar.style.display = 'none';
    return;
  }
  bar.style.display = 'flex';
  bar.innerHTML = hostData.hosts.map(h => {
    const isActive = h.name === hostData.active_host;
    const cls = 'host-btn' + (isActive ? ' active' : '') + (!h.online ? ' offline' : '');
    return `<button class="${cls}" onclick="switchHost('${h.name}')">${h.name}${!h.online ? ' \u2718' : ''}</button>`;
  }).join('');
}

async function switchHost(name) {
  try {
    const res = await fetch(API + '/api/host/' + encodeURIComponent(name), { method: 'POST' });
    if (res.ok) { poll(); }
  } catch(e) {}
}

function cycleHost() {
  if (!hostData || !hostData.hosts || hostData.hosts.length <= 1) return;
  const names = hostData.hosts.map(h => h.name);
  const idx = names.indexOf(hostData.active_host);
  const next = names[(idx + 1) % names.length];
  switchHost(next);
}

/* ── Polling ── */
async function poll() {
  try {
    const [tr, mr, dr, hr] = await Promise.all([
      fetch(API + '/api/themes'),
      fetch(API + '/api/metrics'),
      fetch(API + '/api/display'),
      fetch(API + '/api/hosts'),
    ]);
    const themeData = await tr.json();
    metrics = await mr.json();
    const displayData = await dr.json();
    hostData = await hr.json();

    // Apply per-screen rotation config
    if (displayData.screen_rotation) screenRotation = displayData.screen_rotation;

    const newFamily = themeData.active_family;
    const newVariant = themeData.active_variant;
    if (newFamily !== activeFamily || newVariant !== activeVariant) {
      activeFamily = newFamily;
      activeVariant = newVariant;
      currentScreenIdx = 0;
      lastRotate = Date.now();
    }
    themeColors = (themeData.families[activeFamily] || {})[activeVariant];

    renderDisplay();
    renderHostBar();
  } catch(e) { /* retry next poll */ }
}

/* ── Auto-rotate (per-screen durations + host rotation) ── */
function tick() {
  const now = Date.now();
  if (enabledScreens.length > 1 && now > pauseUntil) {
    const currentDuration = (enabledScreens[currentScreenIdx] || {}).duration || DEFAULT_SCREEN_DURATION;
    if (now - lastRotate >= currentDuration * 1000) {
      currentScreenIdx = (currentScreenIdx + 1) % enabledScreens.length;
      lastRotate = now;
      renderDisplay();
    }
  }
  /* Host auto-rotate: cycle hosts every HOST_ROTATE_S seconds (if enabled via config) */
  if (hostData && hostData.hosts && hostData.hosts.length > 1) {
    const hostRotateS = hostData.host_rotate_interval || 0;
    if (hostRotateS > 0 && now - lastHostRotate >= hostRotateS * 1000) {
      lastHostRotate = now;
      cycleHost();
    }
  }
  requestAnimationFrame(tick);
}

/* ── Keyboard shortcuts ── */
document.addEventListener('keydown', (e) => {
  const n = enabledScreens.length || 1;
  if (e.key >= '1' && e.key <= '9') { currentScreenIdx = Math.min(parseInt(e.key) - 1, n - 1); pauseUntil = Date.now() + PAUSE_MS; lastRotate = Date.now(); renderDisplay(); }
  else if (e.key === ' ') { e.preventDefault(); currentScreenIdx = (currentScreenIdx + 1) % n; pauseUntil = Date.now() + PAUSE_MS; lastRotate = Date.now(); renderDisplay(); }
  else if (e.key === 'h' || e.key === 'H') { cycleHost(); }
  else if (e.key === 'Escape') { window.close(); }
});

/* ── Start ── */
poll();
setInterval(poll, 2500);
requestAnimationFrame(tick);
</script>
</body>
</html>"""
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
    return r"""
/* ── SVG Donut gauge ── */
function donut(pct, label, ringColor, size, sw, font, opts) {
  opts = opts || {};
  const bgRing = opts.bgRing || '#1a1a1a';
  const labelColor = opts.labelColor || '#aaa';
  const valColor = (pct > 80 ? (opts.critColor || '#BF0F0F') : (opts.valColor || '#fff'));
  const linecap = opts.linecap || 'round';
  const fontWeight = opts.fontWeight || '700';
  const labelFW = opts.labelFW || '600';
  const r = (size - sw - 4) / 2;
  const circ = 2 * Math.PI * r;
  const offset = circ - (Math.min(100, Math.max(0, pct)) / 100) * circ;
  const cx = size / 2, cy = size / 2;
  // Gold uses anticlockwise (scale(-1,1)), Coral/Teal use clockwise (rotate(-90))
  const xform = opts.anticlockwise
    ? `translate(${size}, 0) scale(-1, 1) rotate(-90 ${cx} ${cy})`
    : `rotate(-90 ${cx} ${cy})`;
  return `<div style="text-align:center">` +
    `<div style="color:${labelColor};font-size:${opts.labelSize||'2.34cqw'};font-family:${font};font-weight:${labelFW};letter-spacing:1px;margin-bottom:2px;text-transform:uppercase">${label}</div>` +
    `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">` +
      `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${bgRing}" stroke-width="${sw}"/>` +
      `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${ringColor}" stroke-width="${sw}" ` +
        `stroke-dasharray="${circ}" stroke-dashoffset="${offset}" stroke-linecap="${linecap}" ` +
        `transform="${xform}" style="transition:stroke-dashoffset 0.8s ease"/>` +
      `<text x="${cx}" y="${cy+2}" text-anchor="middle" dominant-baseline="central" ` +
        `fill="${valColor}" font-size="${opts.valSize||'3.22cqw'}" font-family="${font}" font-weight="${fontWeight}">${Math.round(pct)}%</text>` +
    `</svg></div>`;
}

/* ── Thermal scale with tick marks ── */
function thermalScale(tickColor, font, marginLeft) {
  const marks = [20, 50, 70, 90, 110, 120];
  let html = `<div style="margin-left:${marginLeft||'4.69cqw'};position:relative;height:2.34cqw">` +
    `<div style="position:absolute;top:0;left:0;right:0;height:1px;background:#333"></div>`;
  for (const t of marks) {
    const pct = ((t - 20) / 100) * 100;
    html += `<div style="position:absolute;left:${pct}%;top:0;transform:translateX(-50%)">` +
      `<div style="width:1px;height:0.78cqw;background:${tickColor}"></div>` +
      `<div style="color:${tickColor};font-size:1.37cqw;font-family:${font};text-align:center;margin-top:1px">${t}</div></div>`;
  }
  return html + `</div>`;
}

/* ── Spinning fan icon — speed scales with actual RPM ── */
function fanIcon(color, size, rpm) {
  if (!rpm || rpm <= 0) {
    // Stopped fan — static, dimmed
    return `<svg width="${size}" height="${size}" viewBox="0 0 40 40" style="opacity:0.3">` +
      [0,90,180,270].map(a => `<path d="M20 20 C18 10,12 4,20 2 C28 4,22 10,20 20Z" fill="${color}" transform="rotate(${a} 20 20)"/>`).join('') +
      `<circle cx="20" cy="20" r="4" fill="#111" stroke="${color}" stroke-width="1"/></svg>`;
  }
  // Animation speed: ~10% of real RPM feel
  // 1000 RPM → ~1.7 rps → 0.6s per revolution
  // 500 RPM → ~0.83 rps → 1.2s per revolution
  const rps = (rpm / 60) * 0.1;
  const speed = Math.max(0.3, 1 / Math.max(0.1, rps));
  return `<svg width="${size}" height="${size}" viewBox="0 0 40 40" style="animation:spin ${speed.toFixed(2)}s linear infinite">` +
    [0,90,180,270].map(a => `<path d="M20 20 C18 10,12 4,20 2 C28 4,22 10,20 20Z" fill="${color}" opacity="0.9" transform="rotate(${a} 20 20)"/>`).join('') +
    `<circle cx="20" cy="20" r="4" fill="#111" stroke="${color}" stroke-width="1"/></svg>`;
}

/* ── Fan strip (dynamic — grouped by CPU / CASE / GPU) ── */
function fanStrip(fanColor, font, bgColor) {
  const cpuFans = m('cpu.fans_cpu');
  const caseFans = m('cpu.fans_case');
  const gpuFan = m('gpu.fan');
  const cpuList = cpuFans.available ? cpuFans.value : [];
  const caseList = caseFans.available ? caseFans.value : [];
  let html = `<div style="display:flex;align-items:center;gap:0.88cqw;${bgColor ? 'background:'+bgColor+';border-radius:3px;padding:0.44cqw 1.17cqw;' : ''}">`;
  let shown = 0;
  if (cpuList.length) {
    html += `<span style="color:${fanColor};font-size:1.76cqw;font-family:${font};font-weight:700;opacity:0.7">CPU</span>`;
    for (const rpm of cpuList) { html += fanIcon(fanColor, '2.93cqw', rpm); shown++; }
  }
  if (caseList.length) {
    if (shown) html += `<div style="width:0.59cqw"></div>`;
    html += `<span style="color:${fanColor};font-size:1.76cqw;font-family:${font};font-weight:700;opacity:0.7">CASE</span>`;
    for (const rpm of caseList) { html += fanIcon(fanColor, '2.93cqw', rpm); shown++; }
  }
  if (gpuFan.available) {
    if (shown) html += `<div style="width:0.59cqw"></div>`;
    html += `<span style="color:${fanColor};font-size:1.76cqw;font-family:${font};font-weight:700;opacity:0.7">GPU</span>`;
    html += fanIcon(fanColor, '2.93cqw', gpuFan.value * 10);
    shown++;
  }
  if (!shown) html += `<span style="color:${fanColor};font-size:1.76cqw;font-family:${font};opacity:0.4">NO FANS DETECTED</span>`;
  html += `</div>`;
  return html;
}

function terminalScreen1(c) {
  const cpuTemp = m('cpu.temp'), cpuUsage = m('cpu.usage');
  const cores = m('cpu.per_core');
  const gpuTemp = m('gpu.temp'), gpuFan = m('gpu.fan'), gpuPower = m('gpu.power');
  const gpuVram = m('gpu.vram_used'), gpuVramPct = m('gpu.vram_percent'), gpuUtil = m('gpu.util');
  const ramUsed = m('mem.ram_used'), ramPct = m('mem.ram_percent');
  const swapUsed = m('mem.swap_used'), swapPct = m('mem.swap_percent');
  const diskRoot = m('disk.root_used'), diskRootPct = m('disk.root_percent');
  const diskHome = m('disk.home_used'), diskHomePct = m('disk.home_percent');
  const dl = m('net.dl'), ul = m('net.ul');
  const hostname = m('sys.hostname'), uptime = m('sys.uptime');

  const coresStr = cores.available && Array.isArray(cores.value)
    ? cores.value.slice(0, 8).map(v => Math.round(v)).join(' ') + (cores.value.length > 8 ? ' ...' : '')
    : 'N/A';
  const fanStr = gpuFan.available ? `  Fan: ${gpuFan.value}%` : '';
  const powerStr = gpuPower.available ? `${gpuPower.value}W / ${gpuPower.extra.limit || '?'}W` : 'N/A';
  const vramG = gpuVram.available ? `${(gpuVram.value/1024).toFixed(1)}/${((gpuVram.extra.total||0)/1024).toFixed(1)}G` : 'N/A';
  const ramStr = ramUsed.available ? `${ramUsed.value}/${ramUsed.extra.total || '?'}G` : 'N/A';
  const swapStr = swapUsed.available ? `${swapUsed.value}/${swapUsed.extra.total || '?'}G` : 'N/A';
  const rootStr = diskRoot.available ? `${diskRoot.value}/${diskRoot.extra.total || '?'}${(diskRoot.unit||'')[0]||'G'}` : 'N/A';
  const homeStr = diskHome.available ? `${diskHome.value}/${diskHome.extra.total || '?'}${(diskHome.unit||'')[0]||'G'}` : 'N/A';

  return `<div class="screen-frame"><div class="t-screen t-2col-3row" style="background:${c.background}">` +
    tPanel(c, 'CPU',
      tRow(c, 'Temp:', '', cpuTemp.available ? cpuTemp.value + '\u00b0C' : 'N/A') +
      tRow(c, 'Load:', tBar(c, cpuUsage.available ? cpuUsage.value : null), cpuUsage.available ? Math.round(cpuUsage.value) + '%' : 'N/A') +
      tRow(c, 'Fans:', '', (function(){ var cpuF=m('cpu.fans_cpu'),caseF=m('cpu.fans_case'),parts=[]; var cl=cpuF.available?cpuF.value:[],ca=caseF.available?caseF.value:[]; if(cl.length)parts.push('CPU:'+cl.filter(function(r){return r>0}).length); if(ca.length)parts.push('Case:'+ca.filter(function(r){return r>0}).length); var gpuF=m('gpu.fan'); if(gpuF.available)parts.push('GPU:'+(gpuF.value>0?'On':'Off')); return parts.length?parts.join(' | '):'N/A'; })(), c.dim) +
      tRow(c, 'Cores:', '', coresStr, c.dim)
    ) +
    tPanel(c, 'GPU',
      tRow(c, 'Temp:', '', gpuTemp.available ? gpuTemp.value + '\u00b0C' + fanStr : 'N/A') +
      tRow(c, 'Power:', '', powerStr) +
      tRow(c, 'VRAM:', tBar(c, gpuVramPct.available ? gpuVramPct.value : null), vramG) +
      tRow(c, 'Util:', tBar(c, gpuUtil.available ? gpuUtil.value : null), gpuUtil.available ? gpuUtil.value + '%' : 'N/A')
    ) +
    tPanel(c, 'MEMORY',
      tRow(c, 'RAM:', tBar(c, ramPct.available ? ramPct.value : null), ramStr) +
      tRow(c, 'Swap:', tBar(c, swapPct.available ? swapPct.value : null), swapStr)
    ) +
    tPanel(c, 'DISK',
      tRow(c, '/', tBar(c, diskRootPct.available ? diskRootPct.value : null), rootStr) +
      tRow(c, '/home', tBar(c, diskHomePct.available ? diskHomePct.value : null), homeStr)
    ) +
    tPanel(c, 'NETWORK',
      tRow(c, '\u2193', '', dl.available ? dl.value + ' ' + dl.unit : 'N/A') +
      tRow(c, '\u2191', '', ul.available ? ul.value + ' ' + ul.unit : 'N/A')
    ) +
    tPanel(c, 'SYSTEM',
      tRow(c, 'Host:', '', mv('sys.hostname')) +
      tRow(c, 'Up:', '', mv('sys.uptime'), c.dim)
    ) +
    `</div></div>`;
}

function terminalScreen2(c) {
  const gpuName = m('gpu.name'), gpuUtil = m('gpu.util');
  const vram = m('gpu.vram_used'), vramPct = m('gpu.vram_percent');
  const gpuTemp = m('gpu.temp'), gpuPower = m('gpu.power');
  const gpuClk = m('gpu.clock_gpu'), memClk = m('gpu.clock_mem'), memUtil = m('gpu.mem_util');
  const procs = m('gpu.processes');
  const llamaStatus = m('llama.status'), llamaHealth = m('llama.health'), llamaModel = m('llama.model');

  const nameStr = gpuName.available ? String(gpuName.value) : 'GPU Not Detected';
  const vramStr = vram.available ? `${vram.value}/${vram.extra.total || '?'} MiB` : 'N/A';
  let tempPowerStr = 'N/A';
  if (gpuTemp.available) {
    tempPowerStr = gpuTemp.value + '\u00b0C';
    if (gpuPower.available) tempPowerStr += `      Power: ${gpuPower.value}W / ${gpuPower.extra.limit||'?'}W`;
  }
  let clockStr = 'N/A';
  if (gpuClk.available) {
    clockStr = `${gpuClk.value}/${gpuClk.extra.max||'?'} MHz`;
    if (memClk.available) clockStr += `  Mem: ${memClk.value}/${memClk.extra.max||'?'} MHz`;
  }

  let procRows = `<div class="t-row" style="color:${c.dim};font-size:12px">PID       Name              VRAM</div>`;
  if (procs.available && Array.isArray(procs.value)) {
    for (const p of procs.value.slice(0, 5)) {
      const pid = String(p.pid || '').padEnd(10);
      const name = String(p.name || '').padEnd(18);
      const mem = (p.vram || p.used_memory || '?') + ' MiB';
      procRows += `<div class="t-row" style="color:${c.primary};font-size:12px">${pid}${name}${mem}</div>`;
    }
  } else {
    procRows += `<div class="t-row" style="color:${c.dim};font-size:12px">No processes</div>`;
  }

  let statusText = 'Unknown';
  if (llamaStatus.available) {
    if (llamaStatus.value === 'Running') {
      statusText = llamaHealth.available ? `Running (${llamaHealth.value})` : 'Running';
    } else statusText = 'Stopped';
  }

  return `<div class="screen-frame"><div class="t-screen t-1col-3row" style="background:${c.background}">` +
    tPanel(c, 'GPU PERFORMANCE',
      `<div class="t-row" style="color:${c.accent};font-size:13px">${nameStr}</div>` +
      tRow(c, 'Utilization:', tBar(c, gpuUtil.available ? gpuUtil.value : null), gpuUtil.available ? gpuUtil.value + '%' : 'N/A') +
      tRow(c, 'VRAM:', tBar(c, vramPct.available ? vramPct.value : null), vramStr) +
      tRow(c, 'Temperature:', '', tempPowerStr) +
      tRow(c, 'GPU Clock:', '', clockStr) +
      tRow(c, 'Mem BW Util:', tBar(c, memUtil.available ? memUtil.value : null), memUtil.available ? memUtil.value + '%' : 'N/A')
    ) +
    tPanel(c, 'CUDA PROCESSES', procRows) +
    tPanel(c, 'LLAMA.CPP',
      tRow(c, 'Status:', '', statusText) +
      tRow(c, 'Model:', '', cleanModel()) +
      tRow(c, 'Quant:', '', mv('llama.quant')) +
      tRow(c, 'Context:', '', mv('llama.context'))
    ) +
    `</div></div>`;
}

function panelGoldScreen1(c) {
  const F = "'Chakra Petch', sans-serif";
  const RED = PANEL_SPEC.colors.red;
  const BLUE = PANEL_SPEC.colors.blue;
  const MAROON = PANEL_SPEC.colors.maroon || '#8B0000';
  const cpuUsage = m('cpu.usage'), ramPct = m('mem.ram_percent');
  const diskRootPct = m('disk.root_percent');
  const diskHome = m('disk.home_used'), diskHomePct = m('disk.home_percent');
  const cpuTemp = m('cpu.temp'), mbTemp = m('cpu.mb_temp'), gpuTemp = m('gpu.temp');
  const ip = m('net.ip'), mac = m('net.mac'), netSpeed = m('net.speed');
  const dl = m('net.dl'), ul = m('net.ul');
  const llamaModel = m('llama.model'), tokSec = m('llama.tok_per_sec');
  const vramUsed = m('gpu.vram_used'), vramTotal = m('gpu.vram_total');
  const temps = [cpuTemp.available?cpuTemp.value:0, mbTemp.available?mbTemp.value:0, gpuTemp.available?gpuTemp.value:0];
  const anyDanger = temps.some(t => t >= 110);
  const anyOrange = temps.some(t => t >= 90);
  const thermalStatus = anyDanger ? 'CRITICAL' : anyOrange ? 'WARNING' : 'NOMINAL';
  const thermalStatusColor = anyDanger ? PANEL_SPEC.colors.thermDarkRed : anyOrange ? PANEL_SPEC.colors.thermOrange : GREEN;

  const speedStr = netSpeed.available ? (netSpeed.value >= 1000 ? Math.floor(netSpeed.value/1000) + ' GBPS' : netSpeed.value + ' MBPS') : '--';
  const vramStr = vramUsed.available && vramTotal.available ? `${vramUsed.value > 100 ? (vramUsed.value/1024).toFixed(1) : vramUsed.value}/${vramTotal.value > 100 ? Math.round(vramTotal.value/1024) : vramTotal.value}` : '--';
  const donutOpts = {anticlockwise: true, critColor: RED, valColor: '#fff', bgRing: '#1a1a1a', valSize: '4.2cqw', labelSize: '2.8cqw'};

  function thermBar(label, temp) {
    const pct = Math.max(0, Math.min(100, ((temp-20)/100)*100));
    const flash = temp >= 100 ? ';animation:blink 0.5s infinite' : '';
    return `<div style="display:flex;align-items:center;gap:0.88cqw"><span style="color:${GOLD};font-size:2.34cqw;font-family:${F};font-weight:700;width:4.69cqw;text-align:right;flex-shrink:0">${label}</span><div style="flex:1;height:2.05cqw;background:#1a1a1a;border-radius:2px;overflow:hidden"><div style="height:100%;width:${pct}%;background:${_thermColor(temp)};border-radius:2px;transition:width 0.8s ease${flash}"></div></div></div>`;
  }

  const hostname = m('sys.hostname');
  const hostStr = hostname.available ? String(hostname.value).toUpperCase() : '--';
  return `<div class="screen-frame"><div class="l-screen l-2x2" style="background:#000">` +
    lPanel('CORE', GOLD,
      `<div style="display:flex;justify-content:space-around;align-items:center;flex:1">` +
        donut(cpuUsage.available?cpuUsage.value:0, 'CPU', GOLD, 160, 11, F, donutOpts) +
        donut(ramPct.available?ramPct.value:0, 'RAM', BLUE, 160, 11, F, donutOpts) +
        donut(diskRootPct.available?diskRootPct.value:0, 'SSD', MAROON, 160, 11, F, donutOpts) +
      `</div>` +
      (diskHome.available ?
        `<div style="position:relative;height:2.93cqw;background:${GREEN};border-radius:3px;overflow:hidden">` +
          `<div style="position:absolute;top:0;left:0;height:100%;width:${diskHomePct.available?diskHomePct.value:0}%;background:${GOLD};border-radius:3px;transition:width 0.8s ease"></div>` +
          `<div style="position:absolute;top:0;left:0;right:0;height:100%;display:flex;justify-content:space-between;align-items:center;padding:0 1.17cqw;font-family:${F};font-size:1.95cqw;font-weight:700;color:#000"><span>SECONDARY</span><span>${diskHome.value?(diskHome.value/1000).toFixed(1):'?'}T / ${diskHome.extra.total?(diskHome.extra.total/1000).toFixed(1):'?'}T</span></div>` +
        `</div>` :
        `<div style="height:2.93cqw;background:#111;border-radius:3px;display:flex;align-items:center;justify-content:center;font-family:${F};font-size:1.95cqw;color:#444">SECONDARY \u2014 NONE</div>`)
    , hostStr) +
    lPanel('THERMALS', AMBER,
      `<div style="display:flex;flex-direction:column;gap:0.59cqw;flex:1;justify-content:center">` +
        thermBar('CPU', cpuTemp.available?cpuTemp.value:20) +
        thermBar('MB', mbTemp.available?mbTemp.value:20) +
        thermBar('GPU', gpuTemp.available?gpuTemp.value:20) +
        thermalScale('#555', F) +
      `</div>` +
      fanStrip(RED, F, RED+'22')
    , `<span style="color:${thermalStatusColor}">${thermalStatus}</span>`) +
    lPanel('COMMS', TEAL,
      `<div style="font-family:${F}">` +
        `<div style="display:flex;justify-content:space-between"><span style="color:#666;font-size:2.05cqw">IP</span><span style="color:#ddd;font-size:4.10cqw;font-weight:700">${ip.available?ip.value:'N/A'}</span></div>` +
        `<div style="display:flex;justify-content:space-between"><span style="color:#666;font-size:2.05cqw">MAC</span><span style="color:#ddd;font-size:4.10cqw;font-weight:700">${mac.available?mac.value:'N/A'}</span></div>` +
      `</div>` +
      `<div style="display:flex;justify-content:space-around;align-items:center;font-family:${F}">` +
        `<div style="display:flex;align-items:center;gap:0.88cqw"><svg width="2.64cqw" height="2.64cqw" viewBox="0 0 24 24"><polygon points="12,2 4,14 20,14" fill="${GREEN}"/></svg><span style="color:${GREEN};font-size:4.69cqw;font-weight:700">${ul.available?ul.value:'0'} <span style="font-size:2.34cqw">${ul.available?ul.unit:'B/s'}</span></span></div>` +
        `<div style="display:flex;align-items:center;gap:0.88cqw"><svg width="2.64cqw" height="2.64cqw" viewBox="0 0 24 24"><polygon points="12,22 4,10 20,10" fill="${TEAL}"/></svg><span style="color:${TEAL};font-size:4.69cqw;font-weight:700">${dl.available?dl.value:'0'} <span style="font-size:2.34cqw">${dl.available?dl.unit:'B/s'}</span></span></div>` +
      `</div>`
    , speedStr) +
    lPanel('NPU', GOLD,
      `<div style="font-family:${F}">` +
        `<div style="display:flex;justify-content:space-between;align-items:baseline"><span style="color:#666;font-size:2.05cqw;flex-shrink:0">MODEL</span><span style="color:#ddd;font-size:3.22cqw;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:right">${cleanModel()}</span></div>` +
        `<div style="display:flex;justify-content:space-between;align-items:baseline"><span style="color:#666;font-size:2.05cqw">QUANT</span><span style="color:#ddd;font-size:3.22cqw;font-weight:700">${mv('llama.quant')}</span></div>` +
        `<div style="display:flex;justify-content:space-between;align-items:baseline"><span style="color:#666;font-size:2.05cqw">CTX</span><span style="color:#ddd;font-size:3.22cqw;font-weight:700">${mv('llama.context')}</span></div>` +
      `</div>` +
      `<div style="display:flex;justify-content:space-between;align-items:baseline;font-family:${F}">` +
        `<div style="display:flex;align-items:baseline;gap:0.59cqw"><span style="color:#666;font-size:2.05cqw">T/S</span><span style="color:#ddd;font-size:4.69cqw;font-weight:700">${tokSec.available?Math.round(tokSec.value):'--'}</span></div>` +
        `<div style="display:flex;align-items:baseline;gap:0.59cqw"><span style="color:#666;font-size:2.05cqw">VRAM</span><span style="color:#ddd;font-size:4.69cqw;font-weight:700">${vramStr}</span></div>` +
      `</div>`
    , 'LLAMA.CPP') +
    `</div></div>`;
}

function panelGoldScreen2(c) {
  const now = new Date();
  const hh = String(now.getHours()).padStart(2, '0');
  const mm = String(now.getMinutes()).padStart(2, '0');
  const ss = String(now.getSeconds()).padStart(2, '0');
  const days = ['SUNDAY','MONDAY','TUESDAY','WEDNESDAY','THURSDAY','FRIDAY','SATURDAY'];
  const months = ['JANUARY','FEBRUARY','MARCH','APRIL','MAY','JUNE','JULY','AUGUST','SEPTEMBER','OCTOBER','NOVEMBER','DECEMBER'];
  const dayName = days[now.getDay()];
  const dateStr = `${months[now.getMonth()]} ${now.getDate()}, ${now.getFullYear()}`;

  return `<div class="screen-frame"><div style="background:#000;width:100%;height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;font-family:'Chakra Petch',sans-serif">` +
    `<div style="color:${GOLD};font-size:2.05cqw;text-transform:uppercase;letter-spacing:1.17cqw;margin-bottom:0.88cqw">United Federation of Planets</div>` +
    `<div style="border:2px solid ${GOLD};padding:2.05cqw 7.32cqw;position:relative">` +
      `<div style="position:absolute;top:-1.32cqw;left:3.52cqw;background:#000;padding:0 1.46cqw;color:${AMBER};font-size:1.76cqw;letter-spacing:0.29cqw">SHIP CHRONOMETER</div>` +
      `<div style="display:flex;align-items:baseline">` +
        `<span style="color:${GOLD};font-size:19.53cqw;font-weight:700">${hh}</span>` +
        `<span style="color:${GOLD};font-size:13.67cqw;animation:blink 1s infinite;margin:0 0.29cqw">:</span>` +
        `<span style="color:${GOLD};font-size:19.53cqw;font-weight:700">${mm}</span>` +
        `<span style="color:${AMBER};font-size:8.79cqw;margin-left:1.46cqw">${ss}</span>` +
      `</div>` +
    `</div>` +
    `<div style="color:${GREEN};font-size:4.69cqw;letter-spacing:0.88cqw;margin-top:2.64cqw;text-transform:uppercase">${dayName}</div>` +
    `<div style="color:#ddd;font-size:5.57cqw;letter-spacing:0.44cqw;margin-top:0.88cqw">${dateStr}</div>` +
  `</div></div>`;
}

function panelCoralScreen1(c) {
  const T = PANEL_SPEC.coral || {};
  const F = "'Antonio', sans-serif";
  const cpuUsage = m('cpu.usage'), ramPct = m('mem.ram_percent');
  const diskRootPct = m('disk.root_percent');
  const diskHome = m('disk.home_used'), diskHomePct = m('disk.home_percent');
  const cpuTemp = m('cpu.temp'), mbTemp = m('cpu.mb_temp'), gpuTemp = m('gpu.temp');
  const ip = m('net.ip'), mac = m('net.mac'), netSpeed = m('net.speed');
  const dl = m('net.dl'), ul = m('net.ul');
  const llamaModel = m('llama.model'), tokSec = m('llama.tok_per_sec');
  const vramUsed = m('gpu.vram_used'), vramTotal = m('gpu.vram_total');
  const vramStr = vramUsed.available && vramTotal.available ? `${vramUsed.value > 100 ? (vramUsed.value/1024).toFixed(1) : vramUsed.value}/${vramTotal.value > 100 ? Math.round(vramTotal.value/1024) : vramTotal.value}` : '--';
  const speedStr = netSpeed.available ? (netSpeed.value >= 1000 ? Math.floor(netSpeed.value/1000) + ' GBPS' : netSpeed.value + ' MBPS') : '--';
  const hostname = m('sys.hostname');
  const hostStr = hostname.available ? String(hostname.value).toUpperCase() : '--';
  const temps = [cpuTemp.available?cpuTemp.value:0, mbTemp.available?mbTemp.value:0, gpuTemp.available?gpuTemp.value:0];
  const anyDanger = temps.some(t => t >= 110);
  const anyOrange = temps.some(t => t >= 90);
  const thermalStatus = anyDanger ? 'CRITICAL' : anyOrange ? 'WARNING' : 'NOMINAL';
  const secPct = diskHome.available && diskHomePct.available ? diskHomePct.value : 0;

  function coralThermColor(t) {
    if (t >= 90) return T.thermOrange || '#FF9933';
    if (t >= 70) return T.thermYellow || '#FFCC66';
    if (t >= 50) return T.thermGreen || '#99CC66';
    return T.thermBlue || '#99CCFF';
  }
  function coralBar(label, temp) {
    const pct = Math.max(0, Math.min(100, ((temp-20)/100)*100));
    const flash = temp >= 100 ? ';animation:blink 0.5s infinite' : '';
    return `<div style="display:flex;align-items:center;gap:0.88cqw"><span style="color:${T.tanoi||'#FFCC99'};font-size:2.64cqw;font-family:${F};width:4.69cqw;text-align:right;flex-shrink:0;text-transform:uppercase">${label}</span><div style="flex:1;height:2.93cqw;background:#1a1a2a;border-radius:999px;overflow:hidden"><div style="height:100%;width:${pct}%;background:${coralThermColor(temp)};border-radius:999px;transition:width 0.8s ease${flash}"></div></div></div>`;
  }
  function coralHdr(title, color, rightText) {
    return `<div style="display:flex;align-items:center;gap:0.59cqw;margin-bottom:0.59cqw"><div style="background:${color};border-radius:999px;height:2.34cqw;padding:0 1.46cqw;display:flex;align-items:center"><span style="color:#000;font-size:1.76cqw;font-family:${F};text-transform:uppercase;letter-spacing:0.15cqw">${title}</span></div><div style="flex:1;height:0.44cqw;background:${color};border-radius:2px"></div>${rightText?`<span style="color:${color};font-size:2.05cqw;font-family:${F};text-transform:uppercase;letter-spacing:0.15cqw">${rightText}</span>`:''}</div>`;
  }
  const donutOpts = {critColor: T.mars||'#FF2200', valColor: T.paleCanary||'#FFFF99', bgRing: '#1a1a2a', labelColor: T.tanoi||'#FFCC99', fontWeight: '400', valSize: '4.10cqw', labelSize: '2.64cqw'};

  return `<div class="screen-frame"><div style="background:#000;width:100%;height:100%;display:flex;flex-direction:column;padding:0.88cqw;gap:0.59cqw;font-family:${F}">` +
    `<div style="display:flex;gap:0.88cqw">` +
      `<div style="flex:1">${coralHdr('Core Systems', T.goldenTanoi||'#FFCC66', hostStr)}</div>` +
      `<div style="flex:1">${coralHdr('Thermals', T.neonCarrot||'#FF9933', thermalStatus)}</div>` +
    `</div>` +
    `<div style="display:flex;gap:0.88cqw;flex:1">` +
      `<div style="flex:1;display:flex;justify-content:space-around;align-items:center">` +
        donut(cpuUsage.available?cpuUsage.value:0, 'CPU', T.neonCarrot||'#FF9933', 170, 14, F, donutOpts) +
        donut(ramPct.available?ramPct.value:0, 'RAM', T.anakiwa||'#99CCFF', 170, 14, F, donutOpts) +
        donut(diskRootPct.available?diskRootPct.value:0, 'SSD', T.lilac||'#CC99CC', 170, 14, F, donutOpts) +
      `</div>` +
      `<div style="flex:1;display:flex;flex-direction:column;justify-content:center;gap:0.88cqw">` +
        coralBar('CPU', cpuTemp.available?cpuTemp.value:20) +
        coralBar('MB', mbTemp.available?mbTemp.value:20) +
        coralBar('GPU', gpuTemp.available?gpuTemp.value:20) +
        thermalScale(T.tanoi||'#FFCC99', F, '4.69cqw') +
      `</div>` +
    `</div>` +
    `<div style="display:flex;gap:0.88cqw;align-items:center">` +
      `<div style="flex:1">` +
        (diskHome.available ?
          `<div style="position:relative;height:2.93cqw;background:${T.eggplant||'#664466'};border-radius:999px;overflow:hidden">` +
            `<div style="position:absolute;top:0;left:0;height:100%;width:${secPct}%;background:${T.goldenTanoi||'#FFCC66'};border-radius:999px;transition:width 0.8s ease"></div>` +
            `<div style="position:absolute;top:0;left:0;right:0;height:100%;display:flex;justify-content:space-between;align-items:center;padding:0 1.46cqw;font-family:${F};font-size:1.76cqw;color:#000"><span>SECONDARY</span><span>${diskHome.value?(diskHome.value/1000).toFixed(1):'?'}T / ${diskHome.extra.total?(diskHome.extra.total/1000).toFixed(1):'?'}T</span></div>` +
          `</div>` :
          `<div style="height:2.93cqw;background:#111;border-radius:999px;display:flex;align-items:center;justify-content:center;font-family:${F};font-size:1.76cqw;color:${T.eggplant||'#664466'}">SECONDARY \u2014 NONE</div>`) +
      `</div>` +
      `<div style="flex:1;display:flex;justify-content:center;align-items:center;gap:1.17cqw">` +
        fanStrip('#AAAAAA', F, '') +
      `</div>` +
    `</div>` +
    `<div style="display:flex;gap:0.88cqw">` +
      `<div style="flex:1">${coralHdr('Comms', T.anakiwa||'#99CCFF', speedStr)}</div>` +
      `<div style="flex:1">${coralHdr('NPU', T.lilac||'#CC99CC', 'llama.cpp')}</div>` +
    `</div>` +
    `<div style="display:flex;gap:0.88cqw;flex:1">` +
      `<div style="flex:1;display:flex;flex-direction:column;justify-content:space-between">` +
        `<div style="display:flex;flex-direction:column;gap:0.29cqw"><div style="display:flex;justify-content:space-between"><span style="color:${T.tanoi||'#FFCC99'};font-size:2.64cqw;letter-spacing:0.15cqw">IP</span><span style="color:${T.paleCanary||'#FFFF99'};font-size:4.69cqw">${ip.available?ip.value:'N/A'}</span></div>` +
        `<div style="display:flex;justify-content:space-between"><span style="color:${T.tanoi||'#FFCC99'};font-size:2.64cqw;letter-spacing:0.15cqw">MAC</span><span style="color:${T.paleCanary||'#FFFF99'};font-size:4.69cqw">${mac.available?mac.value:'N/A'}</span></div></div>` +
        `<div style="display:flex;justify-content:space-between;align-items:center"><div style="display:flex;align-items:center;gap:0.59cqw"><svg width="2.64cqw" height="2.64cqw" viewBox="0 0 24 24"><polygon points="12,2 4,14 20,14" fill="${T.anakiwa||'#99CCFF'}"/></svg><span style="color:${T.anakiwa||'#99CCFF'};font-size:4.98cqw">${ul.available?ul.value+' '+ul.unit:'0'}</span></div><div style="display:flex;align-items:center;gap:0.59cqw"><svg width="2.64cqw" height="2.64cqw" viewBox="0 0 24 24"><polygon points="12,22 4,10 20,10" fill="${T.mariner||'#3366CC'}"/></svg><span style="color:${T.mariner||'#3366CC'};font-size:4.98cqw">${dl.available?dl.value+' '+dl.unit:'0'}</span></div></div>` +
      `</div>` +
      `<div style="flex:1;display:flex;flex-direction:column;justify-content:space-between">` +
        `<div style="display:flex;flex-direction:column;gap:0.29cqw"><div style="display:flex;justify-content:space-between"><span style="color:${T.tanoi||'#FFCC99'};font-size:2.64cqw;letter-spacing:0.15cqw">MODEL</span><span style="color:${T.paleCanary||'#FFFF99'};font-size:3.81cqw">${cleanModel()}</span></div>` +
        `<div style="display:flex;justify-content:space-between"><span style="color:${T.tanoi||'#FFCC99'};font-size:2.64cqw;letter-spacing:0.15cqw">QUANT</span><span style="color:${T.paleCanary||'#FFFF99'};font-size:3.81cqw">${mv('llama.quant')}</span></div>` +
        `<div style="display:flex;justify-content:space-between"><span style="color:${T.tanoi||'#FFCC99'};font-size:2.64cqw;letter-spacing:0.15cqw">CTX</span><span style="color:${T.paleCanary||'#FFFF99'};font-size:3.81cqw">${mv('llama.context')}</span></div></div>` +
        `<div style="display:flex;justify-content:space-between"><div><span style="color:${T.tanoi||'#FFCC99'};font-size:2.64cqw">T/S </span><span style="color:${T.paleCanary||'#FFFF99'};font-size:4.98cqw">${tokSec.available?Math.round(tokSec.value):'--'}</span></div><div><span style="color:${T.tanoi||'#FFCC99'};font-size:2.64cqw">VRAM </span><span style="color:${T.paleCanary||'#FFFF99'};font-size:4.98cqw">${vramStr}</span></div></div>` +
      `</div>` +
    `</div>` +
  `</div></div>`;
}

function panelCoralScreen2(c) {
  const T = PANEL_SPEC.coral || {};
  const FONT = "'Antonio', sans-serif";
  const now = new Date();
  const hh = String(now.getHours()).padStart(2, '0');
  const mm = String(now.getMinutes()).padStart(2, '0');
  const ss = String(now.getSeconds()).padStart(2, '0');
  const days = ['SUNDAY','MONDAY','TUESDAY','WEDNESDAY','THURSDAY','FRIDAY','SATURDAY'];
  const months = ['JANUARY','FEBRUARY','MARCH','APRIL','MAY','JUNE','JULY','AUGUST','SEPTEMBER','OCTOBER','NOVEMBER','DECEMBER'];
  const dayName = days[now.getDay()];
  const dateStr = `${months[now.getMonth()]} ${now.getDate()}, ${now.getFullYear()}`;
  const ank = T.anakiwa || '#99CCFF';
  const gt = T.goldenTanoi || '#FFCC66';
  const nc = T.neonCarrot || '#FF9933';
  const tn = T.tanoi || '#FFCC99';
  const li = T.lilac || '#CC99CC';

  return `<div class="screen-frame"><div style="background:#000;width:100%;height:100%;display:flex;font-family:${FONT}">` +
    `<div style="width:7.32cqw;flex-shrink:0;display:flex;flex-direction:column">` +
      `<div style="height:4.10cqw;background:${ank};border-bottom-left-radius:2.93cqw"></div>` +
      `<div style="flex:1;display:flex;flex-direction:column;gap:0.44cqw;padding-top:0.88cqw">` +
        `<div style="background:${gt};height:3.52cqw;border-top-right-radius:999px;border-bottom-right-radius:999px;width:6.74cqw"></div>` +
        `<div style="background:${nc};height:3.52cqw;border-top-right-radius:999px;border-bottom-right-radius:999px;width:6.74cqw"></div>` +
        `<div style="flex:1"></div>` +
        `<div style="background:${li};height:3.52cqw;border-top-right-radius:999px;border-bottom-right-radius:999px;width:6.74cqw"></div>` +
      `</div>` +
      `<div style="height:2.93cqw;background:${ank};border-top-left-radius:2.34cqw"></div>` +
    `</div>` +
    `<div style="flex:1;display:flex;flex-direction:column">` +
      `<div style="height:4.10cqw;background:${ank};display:flex;align-items:center;justify-content:flex-end;padding-right:1.76cqw"><span style="color:#000;font-size:2.64cqw;text-transform:uppercase;letter-spacing:0.44cqw">UNITED FEDERATION OF PLANETS</span></div>` +
      `<div style="flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center">` +
        `<div style="display:flex;align-items:baseline"><span style="color:${gt};font-size:19.53cqw;letter-spacing:0.59cqw">${hh}</span><span style="color:${nc};font-size:13.67cqw;animation:blink 1s infinite;margin:0 0.59cqw">:</span><span style="color:${gt};font-size:19.53cqw;letter-spacing:0.59cqw">${mm}</span><span style="color:${tn};font-size:8.79cqw;margin-left:1.76cqw">${ss}</span></div>` +
        `<div style="color:${ank};font-size:4.69cqw;letter-spacing:1.17cqw;margin-top:1.17cqw;text-transform:uppercase">${dayName}</div>` +
        `<div style="color:${li};font-size:5.57cqw;letter-spacing:0.59cqw;margin-top:0.59cqw">${dateStr}</div>` +
      `</div>` +
      `<div style="height:2.93cqw;background:${ank};display:flex;align-items:center;justify-content:flex-end;padding-right:1.76cqw"><span style="color:#000;font-size:1.90cqw;letter-spacing:0.29cqw">SHIP CHRONOMETER</span></div>` +
    `</div>` +
  `</div></div>`;
}

function panelTealScreen1(c) {
  const D = PANEL_SPEC.teal || {};
  const F = "'Rajdhani', sans-serif";
  const cpuUsage = m('cpu.usage'), ramPct = m('mem.ram_percent');
  const diskRootPct = m('disk.root_percent');
  const diskHome = m('disk.home_used'), diskHomePct = m('disk.home_percent');
  const cpuTemp = m('cpu.temp'), mbTemp = m('cpu.mb_temp'), gpuTemp = m('gpu.temp');
  const ip = m('net.ip'), mac = m('net.mac');
  const dl = m('net.dl'), ul = m('net.ul');
  const llamaModel = m('llama.model'), tokSec = m('llama.tok_per_sec');
  const vramUsed = m('gpu.vram_used'), vramTotal = m('gpu.vram_total');
  const vramStr = vramUsed.available && vramTotal.available ? `${vramUsed.value > 100 ? (vramUsed.value/1024).toFixed(1) : vramUsed.value}/${vramTotal.value > 100 ? Math.round(vramTotal.value/1024) : vramTotal.value}` : '--';
  const hostname = m('sys.hostname');
  const hostStr = hostname.available ? String(hostname.value).toUpperCase() : '--';
  const temps = [cpuTemp.available?cpuTemp.value:0, mbTemp.available?mbTemp.value:0, gpuTemp.available?gpuTemp.value:0];
  const anyDanger = temps.some(t => t >= 110);
  const anyOrange = temps.some(t => t >= 90);
  const thermalStatus = anyDanger ? 'CRITICAL' : anyOrange ? 'WARNING' : 'NOMINAL';
  const secPct = diskHome.available && diskHomePct.available ? diskHomePct.value : 0;

  function tealThermColor(t) {
    if (t >= 90) return D.thermOrange || '#DD7733';
    if (t >= 70) return D.thermYellow || '#CCAA44';
    if (t >= 50) return D.thermGreen || '#55AA77';
    return D.thermBlue || '#4488AA';
  }
  function tealBar(label, temp) {
    const pct = Math.max(0, Math.min(100, ((temp-20)/100)*100));
    const flash = temp >= 100 ? ';animation:blink 0.5s infinite' : '';
    return `<div style="display:flex;align-items:center;gap:0.88cqw"><span style="color:${D.steel||'#9EA5BA'};font-size:2.64cqw;font-family:${F};font-weight:600;width:4.69cqw;text-align:right;flex-shrink:0;text-transform:uppercase">${label}</span><div style="flex:1;height:2.93cqw;background:${D.navy||'#2F3749'};border-radius:2px;overflow:hidden"><div style="height:100%;width:${pct}%;background:${tealThermColor(temp)};border-radius:2px;transition:width 0.8s ease${flash}"></div></div></div>`;
  }
  function tealHdr(title, color, rightText) {
    return `<div style="display:flex;align-items:center;gap:0;height:2.93cqw;margin-bottom:0.59cqw">` +
      `<svg width="2.05cqw" height="2.93cqw" viewBox="0 0 14 20" style="flex-shrink:0"><polygon points="14,0 14,20 0,20" fill="${color}"/></svg>` +
      `<div style="background:${color};height:100%;padding:0 1.46cqw;display:flex;align-items:center"><span style="color:${D.void||'#111419'};font-size:1.90cqw;font-family:${F};font-weight:600;text-transform:uppercase;letter-spacing:0.29cqw">${title}</span></div>` +
      `<div style="flex:1;height:0.29cqw;background:${color};opacity:0.4"></div>` +
      (rightText ? `<span style="color:${color};font-size:2.05cqw;font-family:${F};font-weight:600;text-transform:uppercase;margin-left:0.88cqw">${rightText}</span>` : '') +
    `</div>`;
  }
  const donutOpts = {critColor: D.alert||'#FF4444', valColor: D.pale||'#AAAACC', bgRing: D.navy||'#2F3749', labelColor: D.steel||'#9EA5BA', linecap: 'butt', fontWeight: '600', labelFW: '600', valSize: '4.10cqw', labelSize: '2.64cqw'};

  const bg = D.void || '#111419';
  return `<div class="screen-frame"><div style="background:${bg};width:100%;height:100%;display:flex;flex-direction:column;padding:0.88cqw;gap:0.59cqw;font-family:${F}">` +
    `<div style="display:flex;gap:0.88cqw">` +
      `<div style="flex:1">${tealHdr('Core Systems', D.teal||'#2A9D8F', hostStr)}</div>` +
      `<div style="flex:1">${tealHdr('Thermals', D.burnt||'#E7442A', thermalStatus)}</div>` +
    `</div>` +
    `<div style="display:flex;gap:0.88cqw;flex:1">` +
      `<div style="flex:1;display:flex;justify-content:space-around;align-items:center">` +
        donut(cpuUsage.available?cpuUsage.value:0, 'CPU', D.burnt||'#E7442A', 170, 14, F, donutOpts) +
        donut(ramPct.available?ramPct.value:0, 'RAM', D.teal||'#2A9D8F', 170, 14, F, donutOpts) +
        donut(diskRootPct.available?diskRootPct.value:0, 'SSD', D.lavender||'#8888BB', 170, 14, F, donutOpts) +
      `</div>` +
      `<div style="flex:1;display:flex;flex-direction:column;justify-content:center;gap:0.88cqw">` +
        tealBar('CPU', cpuTemp.available?cpuTemp.value:20) +
        tealBar('MB', mbTemp.available?mbTemp.value:20) +
        tealBar('GPU', gpuTemp.available?gpuTemp.value:20) +
        thermalScale(D.steel||'#9EA5BA', F) +
      `</div>` +
    `</div>` +
    `<div style="display:flex;gap:0.88cqw;align-items:center">` +
      `<div style="flex:1">` +
        (diskHome.available ?
          `<div style="position:relative;height:2.93cqw;background:${D.navy||'#2F3749'};border-radius:2px;overflow:hidden">` +
            `<div style="position:absolute;top:0;left:0;height:100%;width:${secPct}%;background:${D.teal||'#2A9D8F'};border-radius:2px;transition:width 0.8s ease"></div>` +
            `<div style="position:absolute;top:0;left:0;right:0;height:100%;display:flex;justify-content:space-between;align-items:center;padding:0 1.46cqw;font-family:${F};font-size:1.76cqw;font-weight:600;color:${D.void||'#111419'}"><span>SECONDARY</span><span>${diskHome.value?(diskHome.value/1000).toFixed(1):'?'}T / ${diskHome.extra.total?(diskHome.extra.total/1000).toFixed(1):'?'}T</span></div>` +
          `</div>` :
          `<div style="height:2.93cqw;background:${D.navy||'#2F3749'};border-radius:2px;display:flex;align-items:center;justify-content:center;font-family:${F};font-size:1.76cqw;color:${D.slate||'#6D748C'}">SECONDARY \u2014 NONE</div>`) +
      `</div>` +
      `<div style="flex:1;display:flex;justify-content:center;align-items:center;gap:1.17cqw">` +
        fanStrip('#888899', F, '') +
      `</div>` +
    `</div>` +
    `<div style="display:flex;gap:0.88cqw">` +
      `<div style="flex:1">${tealHdr('Comms', D.lavender||'#8888BB', '')}</div>` +
      `<div style="flex:1">${tealHdr('NPU', D.warm||'#CCAA77', 'LLAMA.CPP')}</div>` +
    `</div>` +
    `<div style="display:flex;gap:0.88cqw;flex:1">` +
      `<div style="flex:1;display:flex;flex-direction:column;justify-content:space-between">` +
        `<div style="display:flex;flex-direction:column;gap:0.29cqw"><div style="display:flex;justify-content:space-between"><span style="color:${D.steel||'#9EA5BA'};font-size:2.64cqw;font-weight:600;letter-spacing:0.15cqw">IP</span><span style="color:${D.pale||'#AAAACC'};font-size:4.69cqw;font-weight:600">${ip.available?ip.value:'N/A'}</span></div>` +
        `<div style="display:flex;justify-content:space-between"><span style="color:${D.steel||'#9EA5BA'};font-size:2.64cqw;font-weight:600;letter-spacing:0.15cqw">MAC</span><span style="color:${D.pale||'#AAAACC'};font-size:4.69cqw;font-weight:600">${mac.available?mac.value:'N/A'}</span></div></div>` +
        `<div style="display:flex;justify-content:space-between;align-items:center"><div style="display:flex;align-items:center;gap:0.59cqw"><svg width="2.64cqw" height="2.64cqw" viewBox="0 0 24 24"><polygon points="12,2 4,14 20,14" fill="${D.teal||'#2A9D8F'}"/></svg><span style="color:${D.teal||'#2A9D8F'};font-size:4.98cqw;font-weight:600">${ul.available?ul.value+' '+ul.unit:'0'}</span></div><div style="display:flex;align-items:center;gap:0.59cqw"><svg width="2.64cqw" height="2.64cqw" viewBox="0 0 24 24"><polygon points="12,22 4,10 20,10" fill="${D.lavender||'#8888BB'}"/></svg><span style="color:${D.lavender||'#8888BB'};font-size:4.98cqw;font-weight:600">${dl.available?dl.value+' '+dl.unit:'0'}</span></div></div>` +
      `</div>` +
      `<div style="flex:1;display:flex;flex-direction:column;justify-content:space-between">` +
        `<div style="display:flex;flex-direction:column;gap:0.29cqw"><div style="display:flex;justify-content:space-between"><span style="color:${D.steel||'#9EA5BA'};font-size:2.64cqw;font-weight:600">MODEL</span><span style="color:${D.pale||'#AAAACC'};font-size:3.81cqw;font-weight:600">${cleanModel()}</span></div>` +
        `<div style="display:flex;justify-content:space-between"><span style="color:${D.steel||'#9EA5BA'};font-size:2.64cqw;font-weight:600">QUANT</span><span style="color:${D.pale||'#AAAACC'};font-size:3.81cqw;font-weight:600">${mv('llama.quant')}</span></div>` +
        `<div style="display:flex;justify-content:space-between"><span style="color:${D.steel||'#9EA5BA'};font-size:2.64cqw;font-weight:600">CTX</span><span style="color:${D.pale||'#AAAACC'};font-size:3.81cqw;font-weight:600">${mv('llama.context')}</span></div></div>` +
        `<div style="display:flex;justify-content:space-between"><div><span style="color:${D.steel||'#9EA5BA'};font-size:2.64cqw;font-weight:600">T/S </span><span style="color:${D.cyan||'#66CCCC'};font-size:4.98cqw;font-weight:600">${tokSec.available?Math.round(tokSec.value):'--'}</span></div><div><span style="color:${D.steel||'#9EA5BA'};font-size:2.64cqw;font-weight:600">VRAM </span><span style="color:${D.cyan||'#66CCCC'};font-size:4.98cqw;font-weight:600">${vramStr}</span></div></div>` +
      `</div>` +
    `</div>` +
  `</div></div>`;
}

function panelTealScreen2(c) {
  const D = PANEL_SPEC.teal || {};
  const FONT = "'Rajdhani', sans-serif";
  const now = new Date();
  const hh = String(now.getHours()).padStart(2, '0');
  const mm = String(now.getMinutes()).padStart(2, '0');
  const ss = String(now.getSeconds()).padStart(2, '0');
  const days = ['SUNDAY','MONDAY','TUESDAY','WEDNESDAY','THURSDAY','FRIDAY','SATURDAY'];
  const months = ['JANUARY','FEBRUARY','MARCH','APRIL','MAY','JUNE','JULY','AUGUST','SEPTEMBER','OCTOBER','NOVEMBER','DECEMBER'];
  const dayName = days[now.getDay()];
  const dateStr = `${months[now.getMonth()]} ${now.getDate()}, ${now.getFullYear()}`;
  const bg = D.void || '#111419';
  const tl = D.teal || '#2A9D8F';
  const bn = D.burnt || '#E7442A';
  const st = D.steel || '#9EA5BA';
  const pl = D.pale || '#AAAACC';
  const lv = D.lavender || '#8888BB';

  return `<div class="screen-frame"><div style="background:${bg};width:100%;height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;font-family:${FONT}">` +
    `<div style="display:flex;width:80%;align-items:center;gap:0;margin-bottom:1.17cqw">` +
      `<div style="flex:1;height:0.29cqw;background:${tl};opacity:0.4"></div>` +
      `<svg width="2.05cqw" height="2.05cqw" viewBox="0 0 14 14"><polygon points="0,14 14,0 14,14" fill="${tl}" opacity="0.6"/></svg>` +
      `<div style="padding:0 1.76cqw"><span style="color:${tl};font-size:2.05cqw;font-weight:600;text-transform:uppercase;letter-spacing:0.88cqw">BAJORAN SECTOR</span></div>` +
      `<svg width="2.05cqw" height="2.05cqw" viewBox="0 0 14 14"><polygon points="0,0 14,14 0,14" fill="${tl}" opacity="0.6"/></svg>` +
      `<div style="flex:1;height:0.29cqw;background:${tl};opacity:0.4"></div>` +
    `</div>` +
    `<div style="display:flex;align-items:baseline"><span style="color:${pl};font-size:19.53cqw;font-weight:600;letter-spacing:0.59cqw">${hh}</span><span style="color:${bn};font-size:13.67cqw;animation:blink 1s infinite;margin:0 0.59cqw">:</span><span style="color:${pl};font-size:19.53cqw;font-weight:600;letter-spacing:0.59cqw">${mm}</span><span style="color:${st};font-size:8.79cqw;margin-left:1.76cqw">${ss}</span></div>` +
    `<div style="color:${tl};font-size:4.69cqw;letter-spacing:1.17cqw;margin-top:1.46cqw;text-transform:uppercase;font-weight:600">${dayName}</div>` +
    `<div style="color:${lv};font-size:5.57cqw;letter-spacing:0.59cqw;margin-top:0.59cqw;font-weight:500">${dateStr}</div>` +
    `<div style="display:flex;width:80%;align-items:center;gap:0;margin-top:1.76cqw">` +
      `<div style="flex:1;height:0.29cqw;background:${bn};opacity:0.3"></div>` +
      `<svg width="1.46cqw" height="1.46cqw" viewBox="0 0 10 10"><polygon points="0,10 10,0 10,10" fill="${bn}" opacity="0.5"/></svg>` +
      `<div style="padding:0 1.46cqw"><span style="color:${st};font-size:1.76cqw;font-weight:600;letter-spacing:0.44cqw">STATION CHRONOMETER</span></div>` +
      `<svg width="1.46cqw" height="1.46cqw" viewBox="0 0 10 10"><polygon points="0,0 10,10 0,10" fill="${bn}" opacity="0.5"/></svg>` +
      `<div style="flex:1;height:0.29cqw;background:${bn};opacity:0.3"></div>` +
    `</div>` +
  `</div></div>`;
}

/* ═══════════════════════════════════════
   VINTAGE / SCANLINES
   ═══════════════════════════════════════ */

function scanGlow(color, spread) {
  spread = spread || 4;
  return 'color:' + color + ';text-shadow:0 0 ' + spread + 'px ' + color + ', 0 0 ' + (spread*2) + 'px ' + color + '66';
}

function scanSectionLabel(text, color, rightText, rightColor) {
  return '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.59cqw">' +
    '<span style="' + scanGlow(color, 4) + ';font-size:1.95cqw;font-family:\'Share Tech Mono\',monospace;letter-spacing:3px">' + text + '</span>' +
    '<div style="flex:1;height:1px;background:' + color + '33;margin:0 1.17cqw"></div>' +
    (rightText ? '<span style="' + scanGlow(rightColor||color, 3) + ';font-size:1.95cqw;font-family:\'Share Tech Mono\',monospace;letter-spacing:2px">' + rightText + '</span>' : '') +
  '</div>';
}

function scanDonut(pct, label, color, size) {
  const S = PANEL_SPEC.scanlines || {};
  const F = "'Share Tech Mono', monospace";
  const dim = S.dim || '#334455';
  const red = S.red || '#FF3344';
  const sw = size * 0.07;
  const r = (size - sw - 6) / 2;
  const circ = 2 * Math.PI * r;
  const offset = circ - (Math.min(100, Math.max(0, pct)) / 100) * circ;
  const valColor = pct > 80 ? red : color;
  const cx = size / 2, cy = size / 2;
  return '<div style="text-align:center">' +
    '<div style="' + scanGlow(color, 3) + ';font-size:2.34cqw;font-family:' + F + ';letter-spacing:2px;margin-bottom:2px">' + label + '</div>' +
    '<svg width="' + size + '" height="' + size + '" viewBox="0 0 ' + size + ' ' + size + '" style="filter:drop-shadow(0 0 4px ' + color + '66)">' +
      '<circle cx="' + cx + '" cy="' + cy + '" r="' + r + '" fill="none" stroke="' + dim + '" stroke-width="' + sw + '"/>' +
      '<circle cx="' + cx + '" cy="' + cy + '" r="' + r + '" fill="none" stroke="' + color + '" stroke-width="' + sw + '" ' +
        'stroke-dasharray="' + circ + '" stroke-dashoffset="' + offset + '" stroke-linecap="butt" ' +
        'transform="rotate(-90 ' + cx + ' ' + cy + ')" style="transition:stroke-dashoffset 0.8s ease;filter:drop-shadow(0 0 3px ' + color + ')"/>' +
      '<text x="' + cx + '" y="' + (cy+2) + '" text-anchor="middle" dominant-baseline="central" ' +
        'fill="' + valColor + '" font-size="3.81cqw" font-family="' + F + '" style="filter:drop-shadow(0 0 4px ' + valColor + ')">' + Math.round(pct) + '%</text>' +
    '</svg></div>';
}

function scanThermBar(label, temp) {
  const S = PANEL_SPEC.scanlines || {};
  const F = "'Share Tech Mono', monospace";
  const cyan = S.cyan || '#00FFCC';
  const dim = S.dim || '#334455';
  const pct = Math.max(0, Math.min(100, ((temp-20)/100)*100));
  let fillColor = S.blue || '#4488FF';
  if (temp >= 90) fillColor = S.red || '#FF3344';
  else if (temp >= 70) fillColor = S.amber || '#FFAA00';
  else if (temp >= 50) fillColor = S.green || '#00FF88';
  const thermFlash = temp >= 100 ? ';animation:blink 0.5s infinite' : '';
  return '<div style="display:flex;align-items:center;gap:0.88cqw">' +
    '<span style="' + scanGlow(cyan, 3) + ';font-size:2.34cqw;font-family:' + F + ';width:4.69cqw;text-align:right;flex-shrink:0">' + label + '</span>' +
    '<div style="flex:1;height:2.64cqw;background:' + dim + ';border-radius:1px;overflow:hidden">' +
      '<div style="height:100%;width:' + pct + '%;background:' + fillColor + ';border-radius:1px;transition:width 0.8s ease,background 0.5s ease;box-shadow:0 0 4px ' + fillColor + ',0 0 8px ' + fillColor + '44' + thermFlash + '"></div>' +
    '</div></div>';
}

function scanScreen1(c) {
  const S = PANEL_SPEC.scanlines || {};
  const F = "'Share Tech Mono', monospace";
  const cyan = S.cyan || '#00FFCC';
  const amber = S.amber || '#FFAA00';
  const green = S.green || '#00FF88';
  const blue = S.blue || '#4488FF';
  const red = S.red || '#FF3344';
  const cyanDim = S.cyanDim || '#009977';
  const dim = S.dim || '#334455';
  const bg = S.bg || '#060810';
  const cpuUsage = m('cpu.usage'), ramPct = m('mem.ram_percent');
  const diskRootPct = m('disk.root_percent');
  const diskHome = m('disk.home_used'), diskHomePct = m('disk.home_percent');
  const cpuTemp = m('cpu.temp'), mbTemp = m('cpu.mb_temp'), gpuTemp = m('gpu.temp');
  const ip = m('net.ip'), mac = m('net.mac'), netSpeed = m('net.speed');
  const dl = m('net.dl'), ul = m('net.ul');
  const llamaModel = m('llama.model'), tokSec = m('llama.tok_per_sec');
  const vramUsed = m('gpu.vram_used'), vramTotal = m('gpu.vram_total');
  const vramStr = vramUsed.available && vramTotal.available ? (vramUsed.value > 100 ? (vramUsed.value/1024).toFixed(1) : vramUsed.value) + '/' + (vramTotal.value > 100 ? Math.round(vramTotal.value/1024) : vramTotal.value) : '--';
  const speedStr = netSpeed.available ? (netSpeed.value >= 1000 ? Math.floor(netSpeed.value/1000) + ' GBPS' : netSpeed.value + ' MBPS') : '--';
  const hostname = m('sys.hostname');
  const hostStr = hostname.available ? String(hostname.value).toUpperCase() : '--';
  const temps = [cpuTemp.available?cpuTemp.value:0, mbTemp.available?mbTemp.value:0, gpuTemp.available?gpuTemp.value:0];
  const anyDanger = temps.some(t => t >= 110);
  const anyOrange = temps.some(t => t >= 90);
  const thermalStatus = anyDanger ? 'CRITICAL' : anyOrange ? 'WARNING' : 'NOMINAL';
  const thermalStatusColor = anyDanger ? red : anyOrange ? amber : green;
  const secPct = diskHome.available && diskHomePct.available ? diskHomePct.value : 0;

  return '<div class="screen-frame"><div style="background:' + bg + ';width:100%;height:100%;position:relative;padding:0.88cqw;display:flex;flex-direction:column;gap:0.59cqw">' +
    /* Scanline overlay */
    '<div style="position:absolute;top:0;left:0;right:0;bottom:0;pointer-events:none;background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,0.08) 2px,rgba(0,0,0,0.08) 4px);z-index:10"></div>' +
    /* Row 1: Headers */
    '<div style="display:flex;gap:1.17cqw">' +
      '<div style="flex:1">' + scanSectionLabel('CORE SYSTEMS', cyan, hostStr) + '</div>' +
      '<div style="flex:1">' + scanSectionLabel('THERMALS', amber, thermalStatus, thermalStatusColor) + '</div>' +
    '</div>' +
    /* Row 2: Donuts | Bars */
    '<div style="display:flex;gap:1.17cqw;flex:1">' +
      '<div style="flex:1;display:flex;justify-content:space-around;align-items:center">' +
        scanDonut(cpuUsage.available?cpuUsage.value:0, 'CPU', amber, 170) +
        scanDonut(ramPct.available?ramPct.value:0, 'RAM', cyan, 170) +
        scanDonut(diskRootPct.available?diskRootPct.value:0, 'SSD', green, 170) +
      '</div>' +
      '<div style="flex:1;display:flex;flex-direction:column;justify-content:center;gap:0.88cqw">' +
        scanThermBar('CPU', cpuTemp.available?cpuTemp.value:20) +
        scanThermBar('MB', mbTemp.available?mbTemp.value:20) +
        scanThermBar('GPU', gpuTemp.available?gpuTemp.value:20) +
        thermalScale(cyanDim, F) +
      '</div>' +
    '</div>' +
    /* Row 3: Secondary | Fans */
    '<div style="display:flex;gap:1.17cqw;align-items:center">' +
      '<div style="flex:1">' +
        (diskHome.available ?
          '<div style="position:relative;height:2.64cqw;background:' + dim + ';border-radius:1px;overflow:hidden">' +
            '<div style="position:absolute;top:0;left:0;height:100%;width:' + secPct + '%;background:' + green + ';border-radius:1px;transition:width 0.8s ease;box-shadow:0 0 3px ' + green + ',0 0 6px ' + green + '44"></div>' +
            '<div style="position:absolute;top:0;left:0;right:0;height:100%;display:flex;justify-content:space-between;align-items:center;padding:0 1.17cqw;font-family:' + F + ';font-size:1.46cqw;color:' + bg + '">' +
              '<span>SECONDARY</span><span>' + (diskHome.value?(diskHome.value/1000).toFixed(1):'?') + 'T / ' + (diskHome.extra.total?(diskHome.extra.total/1000).toFixed(1):'?') + 'T</span></div>' +
          '</div>' :
          '<div style="height:2.64cqw;background:' + dim + ';border-radius:1px;display:flex;align-items:center;justify-content:center;font-family:' + F + ';font-size:1.46cqw;color:' + dim + '">SECONDARY \u2014 NONE</div>') +
      '</div>' +
      '<div style="flex:1;display:flex;justify-content:center;align-items:center;gap:0.88cqw">' +
        fanStrip(cyan, F, '') +
      '</div>' +
    '</div>' +
    /* Row 4: Headers */
    '<div style="display:flex;gap:1.17cqw">' +
      '<div style="flex:1">' + scanSectionLabel('COMMS', blue, speedStr) + '</div>' +
      '<div style="flex:1">' + scanSectionLabel('NPU', amber, 'LLAMA.CPP') + '</div>' +
    '</div>' +
    /* Row 5+6: Data */
    '<div style="display:flex;gap:1.17cqw;flex:1">' +
      '<div style="flex:1;display:flex;flex-direction:column;justify-content:space-between">' +
        '<div style="display:flex;flex-direction:column;gap:0.29cqw">' +
          '<div style="display:flex;justify-content:space-between"><span style="' + scanGlow(cyanDim, 2) + ';font-size:2.64cqw;font-family:' + F + '">IP</span><span style="' + scanGlow(cyan, 4) + ';font-size:4.69cqw;font-family:' + F + '">' + (ip.available?ip.value:'N/A') + '</span></div>' +
          '<div style="display:flex;justify-content:space-between"><span style="' + scanGlow(cyanDim, 2) + ';font-size:2.64cqw;font-family:' + F + '">MAC</span><span style="' + scanGlow(cyan, 4) + ';font-size:4.69cqw;font-family:' + F + '">' + (mac.available?mac.value:'N/A') + '</span></div>' +
        '</div>' +
        '<div style="display:flex;justify-content:space-between;align-items:center">' +
          '<div style="display:flex;align-items:center;gap:0.59cqw"><span style="' + scanGlow(green, 4) + ';font-size:1.76cqw;font-family:' + F + '">\u25B2</span><span style="' + scanGlow(green, 4) + ';font-size:4.98cqw;font-family:' + F + '">' + (ul.available?ul.value+' '+ul.unit:'0') + '</span></div>' +
          '<div style="display:flex;align-items:center;gap:0.59cqw"><span style="' + scanGlow(blue, 4) + ';font-size:1.76cqw;font-family:' + F + '">\u25BC</span><span style="' + scanGlow(blue, 4) + ';font-size:4.98cqw;font-family:' + F + '">' + (dl.available?dl.value+' '+dl.unit:'0') + '</span></div>' +
        '</div>' +
      '</div>' +
      '<div style="flex:1;display:flex;flex-direction:column;justify-content:space-between">' +
        '<div style="display:flex;flex-direction:column;gap:0.29cqw">' +
          '<div style="display:flex;justify-content:space-between"><span style="' + scanGlow(cyanDim, 2) + ';font-size:2.64cqw;font-family:' + F + '">MODEL</span><span style="' + scanGlow(amber, 4) + ';font-size:3.81cqw;font-family:' + F + ';overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:right">' + cleanModel() + '</span></div>' +
          '<div style="display:flex;justify-content:space-between"><span style="' + scanGlow(cyanDim, 2) + ';font-size:2.64cqw;font-family:' + F + '">QUANT</span><span style="' + scanGlow(amber, 4) + ';font-size:3.81cqw;font-family:' + F + '">' + mv('llama.quant') + '</span></div>' +
          '<div style="display:flex;justify-content:space-between"><span style="' + scanGlow(cyanDim, 2) + ';font-size:2.64cqw;font-family:' + F + '">CTX</span><span style="' + scanGlow(amber, 4) + ';font-size:3.81cqw;font-family:' + F + '">' + mv('llama.context') + '</span></div>' +
        '</div>' +
        '<div style="display:flex;justify-content:space-between;align-items:baseline">' +
          '<div><span style="' + scanGlow(cyanDim, 2) + ';font-size:2.64cqw;font-family:' + F + '">T/S </span><span style="' + scanGlow(cyan, 5) + ';font-size:4.98cqw;font-family:' + F + '">' + (tokSec.available?Math.round(tokSec.value):'--') + '</span></div>' +
          '<div><span style="' + scanGlow(cyanDim, 2) + ';font-size:2.64cqw;font-family:' + F + '">VRAM </span><span style="' + scanGlow(cyan, 5) + ';font-size:4.98cqw;font-family:' + F + '">' + vramStr + '</span></div>' +
        '</div>' +
      '</div>' +
    '</div>' +
  '</div></div>';
}

function scanScreen2(c) {
  const S = PANEL_SPEC.scanlines || {};
  const F = "'Share Tech Mono', monospace";
  const cyan = S.cyan || '#00FFCC';
  const cyanDim = S.cyanDim || '#009977';
  const amber = S.amber || '#FFAA00';
  const green = S.green || '#00FF88';
  const bg = S.bg || '#060810';
  const now = new Date();
  const hh = String(now.getHours()).padStart(2, '0');
  const mm = String(now.getMinutes()).padStart(2, '0');
  const ss = String(now.getSeconds()).padStart(2, '0');
  const days = ['SUNDAY','MONDAY','TUESDAY','WEDNESDAY','THURSDAY','FRIDAY','SATURDAY'];
  const months = ['JANUARY','FEBRUARY','MARCH','APRIL','MAY','JUNE','JULY','AUGUST','SEPTEMBER','OCTOBER','NOVEMBER','DECEMBER'];
  const dayName = days[now.getDay()];
  const dateStr = months[now.getMonth()] + ' ' + now.getDate() + ', ' + now.getFullYear();

  return '<div class="screen-frame"><div style="background:' + bg + ';width:100%;height:100%;position:relative;display:flex;flex-direction:column;align-items:center;justify-content:center;font-family:' + F + '">' +
    '<div style="position:absolute;top:0;left:0;right:0;bottom:0;pointer-events:none;background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,0.08) 2px,rgba(0,0,0,0.08) 4px);z-index:10"></div>' +
    '<div style="' + scanGlow(cyanDim, 3) + ';font-size:1.76cqw;letter-spacing:8px;margin-bottom:1.76cqw">VACUUM FLUORESCENT CHRONOMETER</div>' +
    '<div style="display:flex;align-items:baseline">' +
      '<span style="' + scanGlow(cyan, 10) + ';font-size:21.48cqw;letter-spacing:6px">' + hh + '</span>' +
      '<span style="' + scanGlow(amber, 8) + ';font-size:14.65cqw;animation:blink 1s infinite;margin:0 0.29cqw">:</span>' +
      '<span style="' + scanGlow(cyan, 10) + ';font-size:21.48cqw;letter-spacing:6px">' + mm + '</span>' +
      '<span style="' + scanGlow(green, 6) + ';font-size:9.77cqw;margin-left:1.76cqw">' + ss + '</span>' +
    '</div>' +
    '<div style="' + scanGlow(amber, 5) + ';font-size:4.69cqw;letter-spacing:10px;margin-top:1.46cqw">' + dayName + '</div>' +
    '<div style="' + scanGlow(cyan, 4) + ';font-size:5.27cqw;letter-spacing:4px;margin-top:0.59cqw">' + dateStr + '</div>' +
    '<div style="display:flex;align-items:center;gap:1.17cqw;margin-top:2.34cqw;width:70%">' +
      '<div style="flex:1;height:1px;background:' + cyan + ';opacity:0.15;box-shadow:0 0 2px ' + cyan + '"></div>' +
      '<div style="width:0.88cqw;height:0.88cqw;border-radius:50%;background:' + green + ';box-shadow:0 0 4px ' + green + ',0 0 8px ' + green + '44"></div>' +
      '<div style="width:0.88cqw;height:0.88cqw;border-radius:50%;background:' + amber + ';box-shadow:0 0 4px ' + amber + ',0 0 8px ' + amber + '44"></div>' +
      '<div style="width:0.88cqw;height:0.88cqw;border-radius:50%;background:' + cyan + ';box-shadow:0 0 4px ' + cyan + ',0 0 8px ' + cyan + '44"></div>' +
      '<div style="flex:1;height:1px;background:' + cyan + ';opacity:0.15;box-shadow:0 0 2px ' + cyan + '"></div>' +
    '</div>' +
  '</div></div>';
}

/* ═══════════════════════════════════════
   VINTAGE / TUBES (Nixie + Magic Eye + IN-13 Bargraph + Dekatron)
   ═══════════════════════════════════════ */

function nixieDigit(value, size, showTube) {
  const N = PANEL_SPEC.tubes || {};
  const NIXIE = "'Nixie One', cursive";
  const bright = N.bright || '#FF6E0B';
  const bloom = N.bloom || '#FF4400';
  const glass = N.glass || '#332818';
  const cathode = N.cathode || '#1A1410';
  const bg = N.bg || '#0A0806';
  const glassTint = N.glassTint || '#181210';
  const mesh = N.mesh || '#332820';

  const chars = String(value).split('');
  let html = '<span style="display:inline-flex;gap:' + (showTube ? '3px' : '0') + '">';
  for (let ci = 0; ci < chars.length; ci++) {
    const ch = chars[ci];
    const isDigit = /\d/.test(ch);
    const tubeW = showTube ? (size * 0.7) : '';
    const tubeH = showTube ? (size * 1.3) : '';
    const tubeBg = showTube
      ? 'radial-gradient(ellipse at 40% 35%, rgba(255,68,0,0.04) 0%, ' + bg + ' 60%), linear-gradient(180deg, ' + glassTint + '88 0%, ' + bg + '22 10%, ' + bg + '11 20%, ' + bg + '11 80%, ' + bg + '22 90%, ' + glassTint + '88 100%)'
      : 'linear-gradient(180deg, ' + glassTint + '55 0%, transparent 20%, transparent 80%, ' + glassTint + '55 100%)';
    const tubeBorder = showTube ? '1px solid ' + glass + '55' : '1px solid ' + glass + '33';
    const tubeBorderBottom = showTube ? 'border-bottom:3px solid ' + glass + '88;' : '';
    const tubeRadius = showTube ? 'border-radius:' + (size*0.3) + 'px ' + (size*0.3) + 'px 4px 4px;' : 'border-radius:2px;';
    const tubeBoxShadow = showTube ? 'box-shadow:inset 0 0 ' + (size*0.4) + 'px rgba(255,68,0,0.07),inset 0 0 ' + (size*0.15) + 'px rgba(255,100,34,0.1),inset 0 ' + (size*0.15) + 'px ' + (size*0.3) + 'px rgba(0,0,0,0.4),0 0 ' + (size*0.25) + 'px rgba(255,68,0,0.1);' : '';

    html += '<span style="position:relative;display:inline-block;' +
      (showTube ? 'width:' + tubeW + 'px;height:' + tubeH + 'px;' : 'padding:0 1px;') +
      'text-align:center;background:' + tubeBg + ';border:' + tubeBorder + ';' + tubeBorderBottom + tubeRadius + tubeBoxShadow + 'overflow:hidden">';

    /* Active digit with 5-layer glow */
    const digitPos = showTube ? 'position:absolute;left:50%;top:48%;transform:translate(-50%,-50%);' : 'position:relative;';
    const textGlow = '0 0 ' + Math.max(2,size*0.03) + 'px #FFFFFF, 0 0 ' + Math.max(8,size*0.1) + 'px #FFAA55, 0 0 ' + Math.max(16,size*0.22) + 'px #FF6600, 0 0 ' + Math.max(32,size*0.45) + 'px rgba(255,68,0,0.5), 0 0 ' + Math.max(60,size*0.8) + 'px rgba(255,0,0,0.2)';
    const flickerDur = (2.5 + Math.random() * 2).toFixed(1);
    const microDur = (0.8 + Math.random() * 0.5).toFixed(1);
    html += '<span style="' + digitPos + 'font-size:' + size + 'px;font-family:' + (isDigit ? NIXIE : "'IBM Plex Mono',monospace") + ';color:#FFAA55;text-shadow:' + textGlow + ';z-index:13;animation:nixieFlicker ' + flickerDur + 's ease-in-out infinite, nixieMicroFlicker ' + microDur + 's linear infinite">' + ch + '</span>';

    /* Glass reflections for showTube */
    if (showTube) {
      html += '<div style="position:absolute;top:8%;right:10%;width:45%;height:15%;background:linear-gradient(130deg,rgba(255,255,255,0.07),rgba(255,255,255,0.02) 60%,transparent);border-radius:50%;transform:rotate(25deg);z-index:15;pointer-events:none"></div>';
    }
    html += '</span>';
  }
  html += '</span>';
  return html;
}

function bargraphBar(pct, label, color, flash) {
  const N = PANEL_SPEC.tubes || {};
  const MONO = "'IBM Plex Mono', monospace";
  const barColor = color || N.barStd || '#FF6622';
  const interior = N.interior || '#0C0A06';
  const glass = N.glass || '#332818';
  const cathode = N.cathode || '#1A1410';
  const svgW = 300;
  const h = 30;
  const barW = (Math.max(0, pct) / 100) * (svgW - 4);
  const fid = 'glow-' + label;

  const flashStyle = flash ? 'animation:blink 0.5s infinite;' : '';
  return '<div style="display:flex;align-items:center;gap:0.88cqw;' + flashStyle + '">' +
    '<span style="color:' + (N.label||'#AA8855') + ';text-shadow:0 0 2px ' + (N.label||'#AA8855') + '33;font-size:2.64cqw;font-family:' + MONO + ';width:4.10cqw;text-align:right;flex-shrink:0">' + label + '</span>' +
    '<svg width="100%" height="' + h + '" viewBox="0 0 ' + svgW + ' ' + h + '" preserveAspectRatio="none" style="overflow:visible;flex:1">' +
      '<defs>' +
        '<filter id="' + fid + '-w" x="-50%" y="-300%" width="200%" height="700%"><feGaussianBlur in="SourceGraphic" stdDeviation="8 4"/></filter>' +
        '<filter id="' + fid + '-m" x="-30%" y="-200%" width="160%" height="500%"><feGaussianBlur in="SourceGraphic" stdDeviation="4 3"/></filter>' +
        '<filter id="' + fid + '-t" x="-10%" y="-100%" width="120%" height="300%"><feGaussianBlur in="SourceGraphic" stdDeviation="2 1.5"/></filter>' +
      '</defs>' +
      '<rect x="0" y="0" width="' + svgW + '" height="' + h + '" rx="11" ry="11" fill="' + interior + '" stroke="' + glass + '44" stroke-width="1"/>' +
      '<line x1="4" y1="' + (h/2) + '" x2="' + (svgW-4) + '" y2="' + (h/2) + '" stroke="' + cathode + '" stroke-width="0.5" opacity="0.35"/>' +
      '<rect x="2" y="' + (h/2-6) + '" width="' + Math.max(0,barW) + '" height="12" rx="6" ry="6" fill="' + barColor + '" opacity="0.5" filter="url(#' + fid + '-w)" style="transition:width 0.8s ease"/>' +
      '<rect x="2" y="' + (h/2-4) + '" width="' + Math.max(0,barW) + '" height="8" rx="4" ry="4" fill="' + barColor + '" opacity="0.7" filter="url(#' + fid + '-m)" style="transition:width 0.8s ease"/>' +
      '<rect x="2" y="' + (h/2-2) + '" width="' + Math.max(0,barW) + '" height="4" rx="2" ry="2" fill="#FFCC88" opacity="0.85" filter="url(#' + fid + '-t)" style="transition:width 0.8s ease"/>' +
      '<rect x="2" y="' + (h/2-0.75) + '" width="' + Math.max(0,barW) + '" height="1.5" rx="0.75" ry="0.75" fill="#FFDDAA" style="transition:width 0.8s ease"/>' +
    '</svg></div>';
}

function magicEye(pct, label, size) {
  const cx = 210, cy = 210, rOuter = 146, rInner = 66;
  const minAngle = 4, maxAngle = 320;
  const wedgeAngle = minAngle + (pct / 100) * (maxAngle - minAngle);
  const startDeg = -wedgeAngle / 2;
  const endDeg = wedgeAngle / 2;
  function polar(r, deg) {
    const a = (deg - 90) * Math.PI / 180;
    return { x: cx + r * Math.cos(a), y: cy + r * Math.sin(a) };
  }
  function sector(rO, rI, s, e) {
    const span = ((e - s) % 360 + 360) % 360;
    const la = span > 180 ? 1 : 0;
    const p1 = polar(rO, s), p2 = polar(rO, e), p3 = polar(rI, e), p4 = polar(rI, s);
    return 'M ' + p1.x + ' ' + p1.y + ' A ' + rO + ' ' + rO + ' 0 ' + la + ' 1 ' + p2.x + ' ' + p2.y + ' L ' + p3.x + ' ' + p3.y + ' A ' + rI + ' ' + rI + ' 0 ' + la + ' 0 ' + p4.x + ' ' + p4.y + ' Z';
  }
  const shadowPath = sector(rOuter, rInner, startDeg, endDeg);
  const softPath = sector(rOuter + 4, rInner - 3, startDeg - 2.5, endDeg + 2.5);
  const s = size / 420;
  const holeSize = rInner * 2 * s;
  const MONO = "'IBM Plex Mono', monospace";
  const N = PANEL_SPEC.tubes || {};

  return '<div style="text-align:center">' +
    '<div style="color:' + (N.label||'#AA8855') + ';text-shadow:0 0 3px ' + (N.label||'#AA8855') + '44;font-size:2.34cqw;font-family:' + MONO + ';letter-spacing:3px;margin-bottom:3px">' + label + '</div>' +
    '<div style="position:relative;width:' + size + 'px;height:' + size + 'px;margin:0 auto;filter:drop-shadow(0 0 ' + (4*s) + 'px rgba(50,255,90,0.08)) drop-shadow(0 0 ' + (10*s) + 'px rgba(50,255,90,0.06))">' +
      '<svg viewBox="0 0 420 420" width="' + size + '" height="' + size + '" style="display:block">' +
        '<defs>' +
          '<filter id="og-' + label + '" x="-150%" y="-150%" width="400%" height="400%"><feGaussianBlur in="SourceGraphic" stdDeviation="14" result="g1"/><feGaussianBlur in="SourceGraphic" stdDeviation="28" result="g2"/><feMerge><feMergeNode in="g2"/><feMergeNode in="g1"/><feMergeNode in="SourceGraphic"/></feMerge></filter>' +
          '<filter id="sb-' + label + '" x="-80%" y="-80%" width="260%" height="260%"><feGaussianBlur stdDeviation="4"/></filter>' +
          '<radialGradient id="ph-' + label + '" cx="50%" cy="47%" r="50%"><stop offset="0%" stop-color="#baff9f"/><stop offset="26%" stop-color="#7fff63" stop-opacity="0.98"/><stop offset="52%" stop-color="#46ef3f" stop-opacity="0.95"/><stop offset="78%" stop-color="#1fc62c" stop-opacity="0.92"/><stop offset="100%" stop-color="#0c7e1c" stop-opacity="0.90"/></radialGradient>' +
          '<radialGradient id="rf-' + label + '" cx="50%" cy="50%" r="58%"><stop offset="62%" stop-color="rgba(0,0,0,0)"/><stop offset="100%" stop-color="rgba(0,0,0,0.40)"/></radialGradient>' +
          '<clipPath id="fc-' + label + '"><circle cx="210" cy="210" r="146"/></clipPath>' +
          '<mask id="fm-' + label + '"><rect width="420" height="420" fill="black"/><circle cx="210" cy="210" r="146" fill="white"/><circle cx="210" cy="210" r="66" fill="black"/></mask>' +
        '</defs>' +
        '<circle cx="210" cy="210" r="132" fill="#31e13a" opacity="0.12" filter="url(#og-' + label + ')"/>' +
        '<g mask="url(#fm-' + label + ')">' +
          '<circle cx="210" cy="210" r="146" fill="url(#ph-' + label + ')" filter="url(#og-' + label + ')" opacity="0.78"/>' +
          '<circle cx="210" cy="210" r="146" fill="url(#ph-' + label + ')" opacity="0.92"/>' +
          '<path d="' + softPath + '" fill="rgba(0,0,0,0.34)" filter="url(#sb-' + label + ')" style="transition:d 0.5s ease"/>' +
          '<path d="' + shadowPath + '" fill="rgba(0,0,0,0.96)" style="transition:d 0.5s ease"/>' +
          '<circle cx="210" cy="210" r="146" fill="url(#rf-' + label + ')" opacity="0.95"/>' +
        '</g>' +
      '</svg>' +
      '<div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:' + holeSize + 'px;height:' + holeSize + 'px;border-radius:50%;background:radial-gradient(circle at 38% 30%,#141714 0%,#090a09 28%,#030303 72%,#000 100%);box-shadow:inset 0 1px 1px rgba(255,255,255,0.02),inset 0 -' + (5*s) + 'px ' + (10*s) + 'px rgba(0,0,0,0.92),0 0 0 1px rgba(255,255,255,0.02);display:grid;place-items:center">' +
        '<span style="color:#eefdeb;font-size:' + Math.max(11, holeSize*0.3) + 'px;font-weight:700;font-family:\'Nixie One\',cursive;line-height:1;text-shadow:0 0 4px rgba(140,255,160,0.10),0 0 8px rgba(140,255,160,0.05)">' + Math.round(pct) + '%</span>' +
      '</div>' +
    '</div>' +
  '</div>';
}

function dekatron(rpm, size) {
  const N = PANEL_SPEC.tubes || {};
  const rps = rpm > 0 ? (rpm / 60) * 0.1 : 0;
  const speed = rps > 0 ? Math.max(0.3, 1 / rps) : 0;
  const stopped = !rpm || rpm <= 0;
  const dekOrange = N.dekOrange || '#FF6600';
  const dekGuide = N.dekGuide || '#552200';
  const dekInactive = '#181410';
  let html = '<div style="width:' + size + 'px;height:' + size + 'px;position:relative;' + (stopped ? 'opacity:0.3' : 'animation:spin ' + speed.toFixed(2) + 's linear infinite') + '">';
  for (let i = 0; i < 10; i++) {
    const angle = (i / 10) * Math.PI * 2;
    const x = size / 2 + (size / 2 - 3) * Math.cos(angle) - 1.5;
    const y = size / 2 + (size / 2 - 3) * Math.sin(angle) - 1.5;
    const isActive = i === 0;
    const isTrail = i === 9;
    const dotColor = isActive ? dekOrange : isTrail ? dekGuide : dekInactive;
    const dotOpacity = isActive ? 1 : isTrail ? 0.4 : 0.15;
    const dotGlow = isActive ? '0 0 4px ' + dekOrange + ',0 0 10px ' + dekOrange + '66' : isTrail ? '0 0 3px ' + dekGuide : 'none';
    html += '<div style="position:absolute;left:' + x + 'px;top:' + y + 'px;width:3px;height:3px;border-radius:50%;background:' + dotColor + ';box-shadow:' + dotGlow + ';opacity:' + dotOpacity + '"></div>';
  }
  html += '</div>';
  return html;
}

function tubeSectionLabel(text, color, rightText, rightColor) {
  const N = PANEL_SPEC.tubes || {};
  const MONO = "'IBM Plex Mono', monospace";
  const rc = rightColor || color;
  return '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.59cqw">' +
    '<span style="color:' + color + ';text-shadow:0 0 2px #FFFFFF66,0 0 6px ' + color + '88,0 0 14px ' + color + '44;font-size:1.95cqw;font-family:' + MONO + ';letter-spacing:3px">' + text + '</span>' +
    '<div style="flex:1;height:1px;background:' + color + '18;margin:0 1.17cqw"></div>' +
    (rightText ? '<span style="color:' + rc + ';text-shadow:0 0 2px #FFFFFF44,0 0 6px ' + rc + '66,0 0 12px ' + rc + '33;font-size:1.95cqw;font-family:' + MONO + ';letter-spacing:2px">' + rightText + '</span>' : '') +
  '</div>';
}

function tubeScreen1(c) {
  const N = PANEL_SPEC.tubes || {};
  const MONO = "'IBM Plex Mono', monospace";
  const bg = N.bg || '#0A0806';
  const core = N.core || '#FF8833';
  const warm = N.warm || '#FF9944';
  const eyeStd = N.eyeStd || '#22DD22';
  const label = N.label || '#AA8855';
  const cpuUsage = m('cpu.usage'), ramPct = m('mem.ram_percent');
  const diskRootPct = m('disk.root_percent');
  const diskHome = m('disk.home_used'), diskHomePct = m('disk.home_percent');
  const cpuTemp = m('cpu.temp'), mbTemp = m('cpu.mb_temp'), gpuTemp = m('gpu.temp');
  const ip = m('net.ip'), mac = m('net.mac'), netSpeed = m('net.speed');
  const dl = m('net.dl'), ul = m('net.ul');
  const llamaModel = m('llama.model'), tokSec = m('llama.tok_per_sec');
  const vramUsed = m('gpu.vram_used'), vramTotal = m('gpu.vram_total');
  const vramStr = vramUsed.available && vramTotal.available ? (vramUsed.value > 100 ? (vramUsed.value/1024).toFixed(1) : vramUsed.value) + '/' + (vramTotal.value > 100 ? Math.round(vramTotal.value/1024) : vramTotal.value) : '--';
  const speedStr = netSpeed.available ? (netSpeed.value >= 1000 ? Math.floor(netSpeed.value/1000) + ' GBPS' : netSpeed.value + ' MBPS') : '--';
  const hostname = m('sys.hostname');
  const hostStr = hostname.available ? String(hostname.value).toUpperCase() : '--';
  const temps = [cpuTemp.available?cpuTemp.value:0, mbTemp.available?mbTemp.value:0, gpuTemp.available?gpuTemp.value:0];
  const anyDanger = temps.some(t => t >= 110);
  const anyOrange = temps.some(t => t >= 90);
  const thermalStatus = anyDanger ? 'CRITICAL' : anyOrange ? 'WARNING' : 'NOMINAL';
  const thermalStatusColor = anyDanger ? '#FF3322' : anyOrange ? core : eyeStd;
  const secPct = diskHome.available && diskHomePct.available ? diskHomePct.value : 0;
  function tempColor(t) { if (t >= 90) return '#FF3322'; if (t >= 70) return '#DDCC00'; if (t >= 50) return eyeStd; return '#4488DD'; }
  function tempFlash(t) { return t >= 100 ? ';animation:blink 0.5s infinite' : ''; }
  function tempPct(t) { return Math.max(0, Math.min(100, ((Math.max(20, Math.min(120, t)) - 20) / 100) * 100)); }
  return '<div class="screen-frame"><div style="background:' + bg + ';width:100%;height:100%;padding:0.88cqw;display:flex;flex-direction:column;gap:0.59cqw">' +
    '<div style="display:flex;gap:1.17cqw">' +
      '<div style="flex:1">' + tubeSectionLabel('CORE SYSTEMS', core, hostStr, warm) + '</div>' +
      '<div style="flex:1">' + tubeSectionLabel('THERMALS', warm, thermalStatus, thermalStatusColor) + '</div>' +
    '</div>' +
    '<div style="display:flex;gap:1.17cqw;flex:1">' +
      '<div style="flex:1;display:flex;justify-content:space-around;align-items:center">' +
        magicEye(cpuUsage.available?cpuUsage.value:0, 'CPU', 180) +
        magicEye(ramPct.available?ramPct.value:0, 'RAM', 180) +
        magicEye(diskRootPct.available?diskRootPct.value:0, 'SSD', 180) +
      '</div>' +
      '<div style="flex:1;display:flex;flex-direction:column;justify-content:center;gap:0.88cqw">' +
        bargraphBar(tempPct(cpuTemp.available?cpuTemp.value:20), 'CPU', tempColor(cpuTemp.available?cpuTemp.value:20), (cpuTemp.available?cpuTemp.value:0)>=100) +
        bargraphBar(tempPct(mbTemp.available?mbTemp.value:20), 'MB', tempColor(mbTemp.available?mbTemp.value:20), (mbTemp.available?mbTemp.value:0)>=100) +
        bargraphBar(tempPct(gpuTemp.available?gpuTemp.value:20), 'GPU', tempColor(gpuTemp.available?gpuTemp.value:20), (gpuTemp.available?gpuTemp.value:0)>=100) +
        '<div style="margin-left:4.10cqw;position:relative;height:2.34cqw"><div style="position:absolute;top:0;left:0;right:0;height:1px;background:' + core + '33"></div>' +
          [20,50,70,90,110,120].map(function(t) { return '<div style="position:absolute;left:' + (((t-20)/100)*100) + '%;top:0;transform:translateX(-50%)"><div style="width:1px;height:0.88cqw;background:' + core + '66"></div><div style="color:' + core + ';text-shadow:0 0 3px ' + core + '55;font-size:1.46cqw;font-family:' + MONO + ';text-align:center;margin-top:1px">' + t + '</div></div>'; }).join('') +
        '</div>' +
      '</div>' +
    '</div>' +
    '<div style="display:flex;gap:1.17cqw;align-items:center">' +
      '<div style="flex:1">' +
        (diskHome.available ?
          '<div style="position:relative;height:2.93cqw;background:#0C0A06;border:1px solid #33281844;border-radius:10px;overflow:hidden">' +
            '<div style="position:absolute;top:0;left:0;right:0;height:100%;display:flex;justify-content:space-between;align-items:center;padding:0 1.46cqw;font-family:' + MONO + ';font-size:1.46cqw;color:#CCDDFF;text-shadow:0 0 2px #4488DD88;z-index:2;pointer-events:none"><span>SECONDARY</span><span>' + (diskHome.value?(diskHome.value/1000).toFixed(1):'?') + 'T / ' + (diskHome.extra.total?(diskHome.extra.total/1000).toFixed(1):'?') + 'T</span></div>' +
          '</div>' :
          '<div style="height:2.93cqw;background:#0C0A06;border-radius:10px;display:flex;align-items:center;justify-content:center;font-family:' + MONO + ';font-size:1.46cqw;color:' + (N.barDim||'#CC4400') + '">SECONDARY \u2014 NONE</div>') +
      '</div>' +
      '<div style="flex:1;display:flex;justify-content:center;align-items:center;gap:0.88cqw">' +
        (function() {
          var cpuF = m('cpu.fans_cpu'), caseF = m('cpu.fans_case'), gpuFan = m('gpu.fan'), h = '';
          var cpuList = cpuF.available ? cpuF.value : [], caseList = caseF.available ? caseF.value : [];
          if (cpuList.length) {
            h += '<span style="color:' + label + ';font-size:1.76cqw;font-family:' + MONO + '">CPU</span>';
            for (var i = 0; i < cpuList.length; i++) h += dekatron(cpuList[i], 24);
          }
          if (caseList.length) {
            h += '<span style="color:' + label + ';font-size:1.76cqw;font-family:' + MONO + '">CASE</span>';
            for (var i = 0; i < caseList.length; i++) h += dekatron(caseList[i], 24);
          }
          if (gpuFan.available) {
            h += '<span style="color:' + label + ';font-size:1.76cqw;font-family:' + MONO + '">GPU</span>';
            h += dekatron(gpuFan.value * 10, 24);
          }
          if (!h) h = '<span style="color:' + label + ';font-size:1.76cqw;font-family:' + MONO + ';opacity:0.4">NO FANS</span>';
          return h;
        })() +
      '</div>' +
    '</div>' +
    '<div style="display:flex;gap:1.17cqw">' +
      '<div style="flex:1">' + tubeSectionLabel('COMMS', '#4488DD', speedStr) + '</div>' +
      '<div style="flex:1">' + tubeSectionLabel('NPU', warm, 'LLAMA.CPP') + '</div>' +
    '</div>' +
    '<div style="display:flex;gap:1.17cqw;flex:1">' +
      '<div style="flex:1;display:flex;flex-direction:column;justify-content:space-between">' +
        '<div style="display:flex;flex-direction:column;gap:0.29cqw">' +
          '<div style="display:flex;justify-content:space-between;align-items:baseline"><span style="color:' + label + ';font-size:2.64cqw;font-family:' + MONO + '">IP</span>' + nixieDigit(ip.available?ip.value:'N/A', 32) + '</div>' +
          '<div style="display:flex;justify-content:space-between;align-items:baseline"><span style="color:' + label + ';font-size:2.64cqw;font-family:' + MONO + '">MAC</span>' + nixieDigit(mac.available?mac.value:'N/A', 32) + '</div>' +
        '</div>' +
        '<div style="display:flex;gap:1.76cqw;align-items:center">' +
          '<div style="display:flex;align-items:center;gap:0.59cqw"><span style="color:' + eyeStd + ';text-shadow:0 0 4px ' + eyeStd + '88;font-size:1.76cqw;font-family:' + MONO + '">\u25B2</span>' + nixieDigit((ul.available?ul.value:'0'), 32) + '<span style="color:' + label + ';font-size:1.76cqw;font-family:' + MONO + '">' + (ul.available?ul.unit:'B/s') + '</span></div>' +
          '<div style="display:flex;align-items:center;gap:0.59cqw"><span style="color:#4488DD;text-shadow:0 0 4px #4488DD88;font-size:1.76cqw;font-family:' + MONO + '">\u25BC</span>' + nixieDigit((dl.available?dl.value:'0'), 32) + '<span style="color:' + label + ';font-size:1.76cqw;font-family:' + MONO + '">' + (dl.available?dl.unit:'B/s') + '</span></div>' +
        '</div>' +
      '</div>' +
      '<div style="flex:1;display:flex;flex-direction:column;justify-content:space-between">' +
        '<div style="display:flex;flex-direction:column;gap:0.29cqw">' +
          '<div style="display:flex;justify-content:space-between;align-items:baseline"><span style="color:' + label + ';font-size:2.64cqw;font-family:' + MONO + '">MODEL</span>' + nixieDigit(cleanModel(), 26) + '</div>' +
          '<div style="display:flex;justify-content:space-between;align-items:baseline"><span style="color:' + label + ';font-size:2.64cqw;font-family:' + MONO + '">QUANT</span>' + nixieDigit(mv('llama.quant'), 26) + '</div>' +
          '<div style="display:flex;justify-content:space-between;align-items:baseline"><span style="color:' + label + ';font-size:2.64cqw;font-family:' + MONO + '">CTX</span>' + nixieDigit(mv('llama.context'), 26) + '</div>' +
        '</div>' +
        '<div style="display:flex;justify-content:space-between;align-items:baseline">' +
          '<div style="display:flex;align-items:baseline;gap:0.59cqw"><span style="color:' + label + ';font-size:2.64cqw;font-family:' + MONO + '">T/S</span>' + nixieDigit(tokSec.available?Math.round(tokSec.value):'--', 38) + '</div>' +
          '<div style="display:flex;align-items:baseline;gap:0.59cqw"><span style="color:' + label + ';font-size:2.64cqw;font-family:' + MONO + '">VRAM</span>' + nixieDigit(vramStr, 38) + '</div>' +
        '</div>' +
      '</div>' +
    '</div>' +
  '</div></div>';
}

function tubeScreen2(c) {
  const N = PANEL_SPEC.tubes || {};
  const NIXIE = "'Nixie One', cursive";
  const MONO = "'IBM Plex Mono', monospace";
  const bg = N.bg || '#0A0806';
  const barDim = N.barDim || '#CC4400';
  const core = N.core || '#FF8833';
  const dekOrange = N.dekOrange || '#FF6600';
  const warm = N.warm || '#FF9944';
  const eyeStd = N.eyeStd || '#22DD22';
  const now = new Date();
  const hh = String(now.getHours()).padStart(2, '0');
  const mm = String(now.getMinutes()).padStart(2, '0');
  const ss = String(now.getSeconds()).padStart(2, '0');
  const days = ['SUNDAY','MONDAY','TUESDAY','WEDNESDAY','THURSDAY','FRIDAY','SATURDAY'];
  const months = ['JANUARY','FEBRUARY','MARCH','APRIL','MAY','JUNE','JULY','AUGUST','SEPTEMBER','OCTOBER','NOVEMBER','DECEMBER'];
  const dayName = days[now.getDay()];
  const dateStr = months[now.getMonth()] + ' ' + now.getDate() + ', ' + now.getFullYear();

  return '<div class="screen-frame"><div style="background:' + bg + ';width:100%;height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;font-family:' + MONO + '">' +
    '<div style="color:' + barDim + ';text-shadow:0 0 3px ' + barDim + '44;font-size:1.76cqw;letter-spacing:8px;margin-bottom:2.05cqw">NIXIE TUBE CHRONOMETER</div>' +
    '<div style="display:flex;align-items:center;gap:5px">' +
      nixieDigit(hh[0], 125, true) +
      nixieDigit(hh[1], 125, true) +
      '<span style="color:#FFAA55;font-size:11.72cqw;font-family:' + NIXIE + ';text-shadow:0 0 3px #fff,0 0 8px #FFAA55,0 0 16px #FF6600,0 0 32px #FF4400AA,0 0 50px #FF000044;animation:blink 1s infinite;margin:0 3px">:</span>' +
      nixieDigit(mm[0], 125, true) +
      nixieDigit(mm[1], 125, true) +
      '<div style="width:12px"></div>' +
      nixieDigit(ss[0], 70, true) +
      nixieDigit(ss[1], 70, true) +
    '</div>' +
    '<div style="color:#FFAA55;text-shadow:0 0 3px #fff,0 0 8px #FFAA55,0 0 18px #FF660066,0 0 35px #FF440033;font-size:4.69cqw;font-family:' + NIXIE + ';letter-spacing:8px;margin-top:2.05cqw">' + dayName + '</div>' +
    '<div style="color:#FFAA55;text-shadow:0 0 3px #fff,0 0 8px #FFAA55,0 0 18px #FF660066,0 0 35px #FF440033;font-size:5.27cqw;font-family:' + NIXIE + ';letter-spacing:4px;margin-top:0.59cqw">' + dateStr + '</div>' +
    '<div style="display:flex;align-items:center;gap:1.46cqw;margin-top:2.34cqw">' +
      [dekOrange, warm, eyeStd, '#8855DD', eyeStd, warm, dekOrange].map(function(c) { return '<div style="width:0.73cqw;height:0.73cqw;border-radius:50%;background:' + c + ';box-shadow:0 0 4px ' + c + ',0 0 8px ' + c + '66,0 0 14px ' + c + '22;opacity:0.7"></div>'; }).join('') +
    '</div>' +
  '</div></div>';
}

/* ═══════════════════════════════════════
   VINTAGE / VFD (Seven-Segment Redux)
   ═══════════════════════════════════════ */

function vfdPal(colorName) {
  const V = PANEL_SPEC.vfd || {};
  return {
    main: V[colorName] || '#00DDAA',
    bright: V[colorName+'Bright'] || '#44FFCC',
    dim: V[colorName+'Dim'] || '#008866',
    ghost: V[colorName+'Ghost'] || V.ghost || '#0A1A15',
  };
}

function vfdSectionLabel(text, color, rightText, rightColor) {
  const p = vfdPal(color);
  const rp = rightColor ? vfdPal(rightColor) : p;
  const F = "'Share Tech Mono', monospace";
  return '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.59cqw">' +
    '<span style="color:' + p.main + ';text-shadow:0 0 4px ' + p.dim + ';font-size:2.34cqw;font-family:' + F + ';letter-spacing:3px">' + text + '</span>' +
    '<div style="flex:1;height:1px;background:' + p.main + '18;margin:0 1.17cqw"></div>' +
    (rightText ? '<span style="color:' + rp.main + ';text-shadow:0 0 3px ' + rp.dim + ';font-size:1.76cqw;font-family:' + F + ';letter-spacing:2px">' + rightText + '</span>' : '') +
  '</div>';
}

function vfdDonut(pct, label, color, size) {
  const p = vfdPal(color);
  const V = PANEL_SPEC.vfd || {};
  const F = "'Share Tech Mono', monospace";
  const cx = size / 2, cy = size / 2;
  const r = (size - 14) / 2;
  const sw = size * 0.08;
  const circ = 2 * Math.PI * r;
  const offset = circ - (Math.min(100, Math.max(0, pct)) / 100) * circ;
  const isHigh = pct > 80;
  const ac = isHigh ? (V.red || '#FF4433') : p.main;
  const ab = isHigh ? (V.redBright || '#FF7766') : p.bright;
  const fid = 'dn-' + label;

  let ticks = '';
  for (let i = 0; i < 20; i++) {
    const a = (i / 20) * Math.PI * 2 - Math.PI / 2;
    const x1 = cx + (r - sw/2 - 1) * Math.cos(a), y1 = cy + (r - sw/2 - 1) * Math.sin(a);
    const x2 = cx + (r + sw/2 + 1) * Math.cos(a), y2 = cy + (r + sw/2 + 1) * Math.sin(a);
    ticks += '<line x1="' + x1 + '" y1="' + y1 + '" x2="' + x2 + '" y2="' + y2 + '" stroke="' + (V.substrate||'#0A0A08') + '" stroke-width="1"/>';
  }

  return '<div style="text-align:center">' +
    '<div style="color:' + p.main + ';text-shadow:0 0 4px ' + p.dim + ';font-size:2.20cqw;font-family:' + F + ';letter-spacing:3px;margin-bottom:3px">' + label + '</div>' +
    '<svg width="' + size + '" height="' + size + '" viewBox="0 0 ' + size + ' ' + size + '" style="display:block;overflow:visible">' +
      '<defs><filter id="' + fid + '-b" x="-30%" y="-30%" width="160%" height="160%"><feGaussianBlur in="SourceGraphic" stdDeviation="2.5" result="g1"/><feGaussianBlur in="SourceGraphic" stdDeviation="5" result="g2"/><feMerge><feMergeNode in="g2"/><feMergeNode in="g1"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>' +
      '<circle cx="' + cx + '" cy="' + cy + '" r="' + r + '" fill="none" stroke="' + p.ghost + '" stroke-width="' + sw + '"/>' +
      ticks +
      '<circle cx="' + cx + '" cy="' + cy + '" r="' + r + '" fill="none" stroke="' + ac + '" stroke-width="' + sw + '" stroke-dasharray="' + circ + '" stroke-dashoffset="' + offset + '" stroke-linecap="butt" transform="rotate(-90 ' + cx + ' ' + cy + ')" opacity="0.5" filter="url(#' + fid + '-b)" style="transition:stroke-dashoffset 0.8s ease,stroke 0.5s ease"/>' +
      '<circle cx="' + cx + '" cy="' + cy + '" r="' + r + '" fill="none" stroke="' + ac + '" stroke-width="' + sw + '" stroke-dasharray="' + circ + '" stroke-dashoffset="' + offset + '" stroke-linecap="butt" transform="rotate(-90 ' + cx + ' ' + cy + ')" opacity="0.95" style="transition:stroke-dashoffset 0.8s ease,stroke 0.5s ease"/>' +
      '<circle cx="' + cx + '" cy="' + cy + '" r="' + r + '" fill="none" stroke="' + ab + '" stroke-width="' + (sw*0.5) + '" stroke-dasharray="' + circ + '" stroke-dashoffset="' + offset + '" stroke-linecap="butt" transform="rotate(-90 ' + cx + ' ' + cy + ')" opacity="0.3" style="transition:stroke-dashoffset 0.8s ease,stroke 0.5s ease"/>' +
      '<text x="' + cx + '" y="' + (cy+1) + '" text-anchor="middle" dominant-baseline="central" fill="' + ac + '" font-size="' + (size*0.22) + '" font-family="' + F + '" style="filter:drop-shadow(0 0 3px ' + ac + '88)">' + Math.round(pct) + '%</text>' +
    '</svg></div>';
}

function vfdThermalBar(temp, label) {
  const V = PANEL_SPEC.vfd || {};
  const F = "'Share Tech Mono', monospace";
  const pct = ((Math.max(20, Math.min(120, temp)) - 20) / 100) * 100;
  let color = 'blue';
  if (temp >= 90) color = 'red';
  else if (temp >= 70) color = 'yellow';
  else if (temp >= 50) color = 'green';
  const vfdFlash = temp >= 100 ? 'animation:blink 0.5s infinite;' : '';
  const p = vfdPal(color);
  const totalSegs = 16;
  const litSegs = Math.round((pct / 100) * totalSegs);
  const fid = 'tb-' + label;
  const ghost = V.ghost || '#0A1A15';

  let segs = '<defs><filter id="' + fid + '" x="-10%" y="-50%" width="120%" height="200%"><feGaussianBlur in="SourceGraphic" stdDeviation="1.2 0.8"/></filter></defs>';
  for (let i = 0; i < totalSegs; i++) {
    const x = i * (160 / totalSegs) + 0.5;
    const w = (160 / totalSegs) - 1.5;
    segs += '<rect x="' + x + '" y="1" width="' + w + '" height="22" rx="1" fill="' + ghost + '" stroke="' + ghost + '66" stroke-width="0.3"/>';
  }
  for (let i = 0; i < litSegs; i++) {
    const x = i * (160 / totalSegs) + 0.5;
    const w = (160 / totalSegs) - 1.5;
    segs += '<rect x="' + x + '" y="1" width="' + w + '" height="22" rx="1" fill="' + p.main + '" opacity="0.5" filter="url(#' + fid + ')"/>';
    segs += '<rect x="' + x + '" y="1" width="' + w + '" height="22" rx="1" fill="' + p.main + '" opacity="0.9"/>';
    segs += '<rect x="' + x + '" y="3" width="' + w + '" height="16" rx="0.5" fill="' + p.bright + '" opacity="0.25"/>';
  }

  return '<div style="display:flex;align-items:center;gap:0.88cqw;' + vfdFlash + '">' +
    '<span style="color:' + (V.green||'#00DDAA') + ';text-shadow:0 0 3px ' + (V.greenDim||'#008866') + ';font-size:2.20cqw;font-family:' + F + ';width:4.69cqw;text-align:right;flex-shrink:0">' + label + '</span>' +
    '<svg width="100%" height="24" viewBox="0 0 160 24" preserveAspectRatio="none" style="flex:1;overflow:visible">' + segs + '</svg></div>';
}

function vfdPanel(children) {
  const V = PANEL_SPEC.vfd || {};
  const filament = V.filament || '#332211';
  const filamentWarm = V.filamentWarm || '#443322';
  const grid = V.grid || '#1A1A18';
  const substrate = V.substrate || '#0A0A08';
  return '<div style="background:' + substrate + ';border:1px solid #1a1a16;border-radius:3px;position:relative;overflow:hidden;box-shadow:inset 0 0 20px rgba(0,0,0,0.5),inset 0 1px 0 rgba(255,255,255,0.02),0 2px 8px rgba(0,0,0,0.6);width:100%;height:100%">' +
    '<div style="position:absolute;top:0;left:0;right:0;bottom:0;pointer-events:none;z-index:20;background-image:linear-gradient(0deg,transparent 0px,transparent 11px,' + filamentWarm + '18 11px,' + filamentWarm + '18 12px,transparent 12px,transparent 23px,' + filament + '12 23px,' + filament + '12 24px,transparent 24px);background-size:100% 24px"></div>' +
    '<div style="position:absolute;top:0;left:0;right:0;bottom:0;pointer-events:none;z-index:21;opacity:0.06;background-image:linear-gradient(0deg,' + grid + ' 1px,transparent 1px),linear-gradient(90deg,' + grid + ' 1px,transparent 1px);background-size:4px 4px"></div>' +
    '<div style="position:absolute;top:0;left:0;right:0;bottom:0;pointer-events:none;z-index:22;background:radial-gradient(ellipse at 50% 40%,transparent 50%,rgba(0,0,0,0.15) 100%)"></div>' +
    children +
  '</div>';
}

function vfdScreen1(c) {
  const V = PANEL_SPEC.vfd || {};
  const F = "'Share Tech Mono', monospace";
  const cpuUsage = m('cpu.usage'), ramPct = m('mem.ram_percent');
  const diskRootPct = m('disk.root_percent');
  const diskHome = m('disk.home_used'), diskHomePct = m('disk.home_percent');
  const cpuTemp = m('cpu.temp'), mbTemp = m('cpu.mb_temp'), gpuTemp = m('gpu.temp');
  const ip = m('net.ip'), mac = m('net.mac'), netSpeed = m('net.speed');
  const dl = m('net.dl'), ul = m('net.ul');
  const llamaModel = m('llama.model'), tokSec = m('llama.tok_per_sec');
  const vramUsed = m('gpu.vram_used'), vramTotal = m('gpu.vram_total');
  const vramStr = vramUsed.available && vramTotal.available ? (vramUsed.value > 100 ? (vramUsed.value/1024).toFixed(1) : vramUsed.value) + '/' + (vramTotal.value > 100 ? Math.round(vramTotal.value/1024) : vramTotal.value) : '--';
  const speedStr = netSpeed.available ? (netSpeed.value >= 1000 ? Math.floor(netSpeed.value/1000) + ' GBPS' : netSpeed.value + ' MBPS') : '--';
  const hostname = m('sys.hostname');
  const hostStr = hostname.available ? String(hostname.value).toUpperCase() : '--';
  const temps = [cpuTemp.available?cpuTemp.value:0, mbTemp.available?mbTemp.value:0, gpuTemp.available?gpuTemp.value:0];
  const anyDanger = temps.some(t => t >= 110);
  const anyWarn = temps.some(t => t >= 90);
  const thermalStatus = anyDanger ? 'CRITICAL' : anyWarn ? 'WARNING' : 'NOMINAL';
  const statusColor = anyDanger ? 'red' : anyWarn ? 'amber' : 'green';
  const secPct = diskHome.available && diskHomePct.available ? diskHomePct.value : 0;
  const green = V.green || '#00DDAA';
  const greenDim = V.greenDim || '#008866';
  const blue = V.blue || '#00D4CC';
  const blueDim = V.blueDim || '#007A77';

  const content = '<div style="width:100%;height:100%;padding:1.17cqw;display:flex;flex-direction:column;gap:0.59cqw;position:relative;z-index:10">' +
    '<div style="display:flex;gap:1.17cqw">' +
      '<div style="flex:1">' + vfdSectionLabel('CORE SYSTEMS', 'green', hostStr, 'amber') + '</div>' +
      '<div style="flex:1">' + vfdSectionLabel('THERMALS', 'amber', thermalStatus, statusColor) + '</div>' +
    '</div>' +
    '<div style="display:flex;gap:1.46cqw;flex:1">' +
      '<div style="flex:1;display:flex;justify-content:space-around;align-items:center">' +
        vfdDonut(cpuUsage.available?cpuUsage.value:0, 'CPU', 'green', 160) +
        vfdDonut(ramPct.available?ramPct.value:0, 'RAM', 'blue', 160) +
        vfdDonut(diskRootPct.available?diskRootPct.value:0, 'SSD', 'amber', 160) +
      '</div>' +
      '<div style="flex:1;display:flex;flex-direction:column;justify-content:center;gap:1.17cqw">' +
        vfdThermalBar(cpuTemp.available?cpuTemp.value:20, 'CPU') +
        vfdThermalBar(mbTemp.available?mbTemp.value:20, 'MB') +
        vfdThermalBar(gpuTemp.available?gpuTemp.value:20, 'GPU') +
        '<div style="margin-left:4.69cqw;position:relative;height:2.93cqw"><div style="position:absolute;top:0;left:0;right:0;height:1px;background:' + green + '22"></div>' +
          [20,50,70,90,110,120].map(function(t) { return '<div style="position:absolute;left:' + (((t-20)/100)*100) + '%;top:0;transform:translateX(-50%)"><div style="width:1px;height:0.73cqw;background:' + green + '44"></div><div style="color:' + green + ';text-shadow:0 0 2px ' + greenDim + ';font-size:1.95cqw;font-family:' + F + ';text-align:center;margin-top:1px">' + t + '</div></div>'; }).join('') +
        '</div>' +
      '</div>' +
    '</div>' +
    '<div style="display:flex;gap:1.17cqw;align-items:center">' +
      '<div style="flex:1">' +
        (diskHome.available ?
          '<div style="display:flex;align-items:center;gap:0.59cqw">' +
            '<span style="color:' + blue + ';text-shadow:0 0 3px ' + blueDim + ';font-size:1.46cqw;font-family:' + F + ';flex-shrink:0">SEC</span>' +
            '<div style="flex:1;height:3.22cqw;background:' + (V.blueGhost||'#0A1018') + ';border-radius:1px;overflow:hidden;position:relative">' +
              '<div style="position:absolute;top:0;left:0;height:100%;width:' + secPct + '%;background:' + blue + ';border-radius:1px;transition:width 0.8s ease;box-shadow:0 0 4px ' + blue + '66"></div>' +
            '</div>' +
            '<span style="color:' + blue + ';text-shadow:0 0 3px ' + blueDim + ';font-size:1.46cqw;font-family:' + F + ';flex-shrink:0">' + (diskHome.value?(diskHome.value/1000).toFixed(1):'?') + 'T/' + (diskHome.extra.total?(diskHome.extra.total/1000).toFixed(1):'?') + 'T</span>' +
          '</div>' :
          '<span style="color:' + (V.ghost||'#0A1A15') + ';font-size:1.76cqw;font-family:' + F + '">SECONDARY \u2014 NONE</span>') +
      '</div>' +
      '<div style="flex:1;display:flex;justify-content:center;align-items:center;gap:0.88cqw">' +
        (function() {
          var cpuF = m('cpu.fans_cpu'), caseF = m('cpu.fans_case'), gpuFan = m('gpu.fan'), h = '';
          var cpuList = cpuF.available ? cpuF.value : [], caseList = caseF.available ? caseF.value : [];
          if (cpuList.length) {
            h += '<span style="color:' + greenDim + ';text-shadow:0 0 2px ' + greenDim + '44;font-size:1.76cqw;font-family:' + F + '">CPU</span>';
            for (var i = 0; i < cpuList.length; i++) h += fanIcon(green, '2.93cqw', cpuList[i]);
          }
          if (caseList.length) {
            h += '<span style="color:' + greenDim + ';text-shadow:0 0 2px ' + greenDim + '44;font-size:1.76cqw;font-family:' + F + '">CASE</span>';
            for (var i = 0; i < caseList.length; i++) h += fanIcon(green, '2.93cqw', caseList[i]);
          }
          if (gpuFan.available) {
            h += '<span style="color:' + greenDim + ';text-shadow:0 0 2px ' + greenDim + '44;font-size:1.76cqw;font-family:' + F + '">GPU</span>';
            h += fanIcon(green, '2.93cqw', gpuFan.value * 10);
          }
          if (!h) h = '<span style="color:' + greenDim + ';font-size:1.76cqw;font-family:' + F + ';opacity:0.4">NO FANS</span>';
          return h;
        })() +
      '</div>' +
    '</div>' +
    '<div style="display:flex;gap:1.17cqw">' +
      '<div style="flex:1">' + vfdSectionLabel('COMMS', 'blue', speedStr) + '</div>' +
      '<div style="flex:1">' + vfdSectionLabel('NPU', 'amber', 'LLAMA.CPP') + '</div>' +
    '</div>' +
    '<div style="display:flex;gap:1.46cqw;flex:1">' +
      '<div style="flex:1;display:flex;flex-direction:column;justify-content:space-between">' +
        '<div style="display:flex;flex-direction:column;gap:0.29cqw">' +
          '<div style="display:flex;justify-content:space-between"><span style="color:' + greenDim + ';font-size:2.05cqw;font-family:' + F + '">IP</span><span style="color:' + green + ';text-shadow:0 0 4px ' + green + '66;font-size:3.52cqw;font-family:' + F + '">' + (ip.available?ip.value:'N/A') + '</span></div>' +
          '<div style="display:flex;justify-content:space-between"><span style="color:' + greenDim + ';font-size:2.05cqw;font-family:' + F + '">MAC</span><span style="color:' + green + ';text-shadow:0 0 4px ' + green + '66;font-size:3.52cqw;font-family:' + F + '">' + (mac.available?mac.value:'N/A') + '</span></div>' +
        '</div>' +
        '<div style="display:flex;gap:1.76cqw;align-items:center">' +
          '<div style="display:flex;align-items:center;gap:0.59cqw"><span style="color:' + green + ';text-shadow:0 0 4px ' + greenDim + ';font-size:1.76cqw;font-family:' + F + '">\u25B2</span><span style="color:' + green + ';text-shadow:0 0 4px ' + green + '66;font-size:4.10cqw;font-family:' + F + '">' + (ul.available?ul.value:'0') + '</span><span style="color:' + greenDim + ';font-size:1.76cqw;font-family:' + F + '">' + (ul.available?ul.unit:'B/s') + '</span></div>' +
          '<div style="display:flex;align-items:center;gap:0.59cqw"><span style="color:' + blue + ';text-shadow:0 0 4px ' + blueDim + ';font-size:1.76cqw;font-family:' + F + '">\u25BC</span><span style="color:' + blue + ';text-shadow:0 0 4px ' + blue + '66;font-size:4.10cqw;font-family:' + F + '">' + (dl.available?dl.value:'0') + '</span><span style="color:' + blueDim + ';font-size:1.76cqw;font-family:' + F + '">' + (dl.available?dl.unit:'B/s') + '</span></div>' +
        '</div>' +
      '</div>' +
      '<div style="flex:1;display:flex;flex-direction:column;justify-content:space-between">' +
        '<div style="display:flex;flex-direction:column;gap:0.29cqw">' +
          '<div style="display:flex;justify-content:space-between"><span style="color:' + (V.amberDim||'#886611') + ';font-size:2.05cqw;font-family:' + F + '">MODEL</span><span style="color:' + (V.amber||'#FFAA22') + ';text-shadow:0 0 4px ' + (V.amber||'#FFAA22') + '66;font-size:3.81cqw;font-family:' + F + ';overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:right">' + cleanModel() + '</span></div>' +
          '<div style="display:flex;justify-content:space-between"><span style="color:' + (V.amberDim||'#886611') + ';font-size:2.05cqw;font-family:' + F + '">QUANT</span><span style="color:' + (V.amber||'#FFAA22') + ';text-shadow:0 0 4px ' + (V.amber||'#FFAA22') + '66;font-size:3.81cqw;font-family:' + F + '">' + mv('llama.quant') + '</span></div>' +
          '<div style="display:flex;justify-content:space-between"><span style="color:' + (V.amberDim||'#886611') + ';font-size:2.05cqw;font-family:' + F + '">CTX</span><span style="color:' + (V.amber||'#FFAA22') + ';text-shadow:0 0 4px ' + (V.amber||'#FFAA22') + '66;font-size:3.81cqw;font-family:' + F + '">' + mv('llama.context') + '</span></div>' +
        '</div>' +
        '<div style="display:flex;justify-content:space-between;align-items:baseline">' +
          '<div><span style="color:' + greenDim + ';font-size:1.95cqw;font-family:' + F + '">T/S</span> <span style="color:' + green + ';text-shadow:0 0 5px ' + green + '66;font-size:4.39cqw;font-family:' + F + '">' + (tokSec.available?Math.round(tokSec.value):'--') + '</span></div>' +
          '<div><span style="color:' + blueDim + ';font-size:1.95cqw;font-family:' + F + '">VRAM</span> <span style="color:' + blue + ';text-shadow:0 0 5px ' + blue + '66;font-size:4.39cqw;font-family:' + F + '">' + vramStr + '</span></div>' +
        '</div>' +
      '</div>' +
    '</div>' +
  '</div>';

  return '<div class="screen-frame">' + vfdPanel(content) + '</div>';
}

function vfdScreen2(c) {
  const V = PANEL_SPEC.vfd || {};
  const F = "'Share Tech Mono', monospace";
  const green = V.green || '#00DDAA';
  const greenDim = V.greenDim || '#008866';
  const amber = V.amber || '#FFAA22';
  const amberDim = V.amberDim || '#886611';
  const blue = V.blue || '#00D4CC';
  const blueDim = V.blueDim || '#007A77';
  const now = new Date();
  const hh = String(now.getHours()).padStart(2, '0');
  const mm = String(now.getMinutes()).padStart(2, '0');
  const ss = String(now.getSeconds()).padStart(2, '0');
  const days = ['SUNDAY','MONDAY','TUESDAY','WEDNESDAY','THURSDAY','FRIDAY','SATURDAY'];
  const months = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];
  const dateStr = now.getDate() + ' ' + months[now.getMonth()] + ' ' + now.getFullYear();

  const hostname = m('sys.hostname');
  const hostStr = hostname.available ? String(hostname.value).toUpperCase() : '--';
  const content = '<div style="width:100%;height:100%;display:flex;flex-direction:column;justify-content:center;align-items:center;gap:1.76cqw;position:relative;z-index:10">' +
    '<div style="color:' + greenDim + ';text-shadow:0 0 3px ' + greenDim + '44;font-size:2.64cqw;font-family:' + F + ';letter-spacing:8px">VFD CHRONOMETER</div>' +
    '<div style="width:85%;height:1px;background:linear-gradient(90deg,transparent,' + green + '33,' + amber + '33,' + blue + '33,transparent)"></div>' +
    '<div style="display:flex;align-items:baseline">' +
      '<span style="color:' + green + ';text-shadow:0 0 4px ' + greenDim + ',0 0 10px ' + green + '44;font-size:21.48cqw;font-family:' + F + '">' + hh + '</span>' +
      '<span style="color:' + green + ';text-shadow:0 0 4px ' + greenDim + ';font-size:14.65cqw;animation:blink 1s infinite;margin:0 0.59cqw">:</span>' +
      '<span style="color:' + green + ';text-shadow:0 0 4px ' + greenDim + ',0 0 10px ' + green + '44;font-size:21.48cqw;font-family:' + F + '">' + mm + '</span>' +
      '<span style="color:' + amber + ';text-shadow:0 0 4px ' + amberDim + ',0 0 8px ' + amber + '44;font-size:9.77cqw;font-family:' + F + ';margin-left:1.76cqw">' + ss + '</span>' +
    '</div>' +
    '<div style="color:' + amber + ';text-shadow:0 0 5px ' + amberDim + ';font-size:5.86cqw;font-family:' + F + ';letter-spacing:8px">' + days[now.getDay()] + '</div>' +
    '<div style="color:' + blue + ';text-shadow:0 0 5px ' + blueDim + ';font-size:5.57cqw;font-family:' + F + ';letter-spacing:4px">' + dateStr + '</div>' +
    '<div style="width:85%;height:1px;background:linear-gradient(90deg,transparent,' + blue + '33,' + amber + '33,' + green + '33,transparent)"></div>' +
    '<div style="color:' + greenDim + ';text-shadow:0 0 3px ' + greenDim + '44;font-size:1.76cqw;font-family:' + F + ';letter-spacing:6px">' + hostStr + ' \u2022 VACUUM FLUORESCENT DISPLAY</div>' +
  '</div>';

  return '<div class="screen-frame">' + vfdPanel(content) + '</div>';
}

/* ═══ Screen 3 — Claude Usage (shared, adapts to theme) ═══ */

function fmtTok(n) {
  if (n == null || n === 0) return '0';
  if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return String(n);
}

function sparkline(samples, color, w, h) {
  if (!samples || samples.length < 2) {
    const emptyPts = Array.from({length: 30}, (_, i) => `${(i/29)*w},${h}`).join(' ');
    return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><polyline points="${emptyPts}" fill="none" stroke="${color}" stroke-width="1" opacity="0.2"/></svg>`;
  }
  const max = Math.max(1, ...samples);
  const pts = samples.map((v, i) => {
    const x = (i / (samples.length - 1)) * w;
    const y = h - (v / max) * (h - 2);
    return `${x},${y}`;
  }).join(' ');
  const fillPts = pts + ` ${w},${h} 0,${h}`;
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">` +
    `<polygon points="${fillPts}" fill="${color}" opacity="0.15"/>` +
    `<polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linejoin="round"/></svg>`;
}

function claudeScreen3(c) {
  const pri = c.primary || '#00ff41';
  const acc = c.accent || '#ffb000';
  const dim = c.dim || c.border || '#333';
  const hdr = c.header || pri;
  const crit = c.critical || '#ff3333';
  const bg = c.panel || 'rgba(0,0,0,0.3)';
  const font = c.font || 'monospace';
  // Token type colors derived from theme
  const cIn = pri, cOut = acc, cCW = hdr, cCR = crit;

  const tokIn = m('claude.tokens_input');
  const tokOut = m('claude.tokens_output');
  const tokCW = m('claude.tokens_cache_write');
  const tokCR = m('claude.tokens_cache_read');
  const tokTotal = m('claude.tokens_total');

  const sessIn = m('claude.session_input');
  const sessOut = m('claude.session_output');
  const sessCW = m('claude.session_cache_write');
  const sessCR = m('claude.session_cache_read');
  const sessTotal = m('claude.session_total');
  const sessMsgs = m('claude.session_msgs');

  const msgsUser = m('claude.msgs_user');
  const msgsAsst = m('claude.msgs_assistant');
  const msgsTotal = m('claude.msgs_total');
  const monthlyTok = m('claude.monthly_tokens');
  const monthlyMsg = m('claude.monthly_messages');
  const days = m('claude.days_active');
  const sessions = m('claude.sessions');
  const agents = m('claude.agents_active');
  const rate = m('claude.token_rate');
  const spark = m('claude.sparkline');

  // Token breakdown bar
  function tokBar(input, output, cw, cr, total) {
    if (!total || total === 0) return `<div style="height:6px;background:${dim};border-radius:3px"></div>`;
    const t = total;
    const pI = (input / t) * 100;
    const pO = (output / t) * 100;
    const pW = (cw / t) * 100;
    const pR = (cr / t) * 100;
    return `<div style="display:flex;height:6px;border-radius:3px;overflow:hidden;background:${dim}">` +
      `<div style="width:${pI}%;background:${cIn}" title="Input"></div>` +
      `<div style="width:${pO}%;background:${cOut}" title="Output"></div>` +
      `<div style="width:${pW}%;background:${cCW}" title="Cache Write"></div>` +
      `<div style="width:${pR}%;background:${cCR}" title="Cache Read"></div>` +
    `</div>`;
  }

  function statRow(label, val, color) {
    return `<div style="display:flex;justify-content:space-between;align-items:center;padding:1px 0">` +
      `<span style="color:${dim};font-size:1.56cqw">${label}</span>` +
      `<span style="color:${color || pri};font-size:1.76cqw;font-weight:600">${val}</span></div>`;
  }

  function sectionTitle(text) {
    return `<div style="color:${hdr};font-size:1.66cqw;font-weight:700;text-transform:uppercase;letter-spacing:2px;margin-bottom:4px;border-bottom:1px solid ${dim};padding-bottom:2px">${text}</div>`;
  }

  // Legend for token types
  const legend = `<div style="display:flex;gap:8px;margin-top:4px;font-size:1.27cqw">` +
    `<span style="color:${cIn}">\u25A0 Input</span>` +
    `<span style="color:${cOut}">\u25A0 Output</span>` +
    `<span style="color:${cCW}">\u25A0 Cache W</span>` +
    `<span style="color:${cCR}">\u25A0 Cache R</span>` +
  `</div>`;

  const totalVal = tokTotal.available ? tokTotal.value : 0;
  const sTotalVal = sessTotal.available ? sessTotal.value : 0;

  // LEFT COLUMN — All-time
  const left =
    `<div style="flex:1;display:flex;flex-direction:column;gap:8px;padding-right:10px;border-right:1px solid ${dim}22">` +
      `<div>` +
        sectionTitle('All-Time Tokens') +
        `<div style="color:${pri};font-size:3.52cqw;font-weight:700;margin:2px 0">${fmtTok(totalVal)}</div>` +
        tokBar(tokIn.value||0, tokOut.value||0, tokCW.value||0, tokCR.value||0, totalVal) +
        legend +
        `<div style="margin-top:6px">` +
          statRow('Input', fmtTok(tokIn.value||0), cIn) +
          statRow('Output', fmtTok(tokOut.value||0), cOut) +
          statRow('Cache Write', fmtTok(tokCW.value||0), cCW) +
          statRow('Cache Read', fmtTok(tokCR.value||0), cCR) +
        `</div>` +
      `</div>` +
      `<div>` +
        sectionTitle('Messages') +
        `<div style="display:flex;gap:12px;align-items:baseline">` +
          `<span style="color:${pri};font-size:3.52cqw;font-weight:700">${msgsTotal.available ? msgsTotal.value.toLocaleString() : '--'}</span>` +
          `<span style="color:${dim};font-size:1.37cqw">total</span>` +
        `</div>` +
        statRow('User', msgsUser.available ? msgsUser.value.toLocaleString() : '--') +
        statRow('Assistant', msgsAsst.available ? msgsAsst.value.toLocaleString() : '--') +
      `</div>` +
      `<div>` +
        sectionTitle('Monthly Average') +
        statRow('Tokens', fmtTok(monthlyTok.value||0)) +
        statRow('Messages', monthlyMsg.available ? monthlyMsg.value.toLocaleString() : '--') +
        statRow('Active Days', days.available ? days.value : '--') +
        statRow('Sessions', sessions.available ? sessions.value : '--') +
      `</div>` +
    `</div>`;

  // RIGHT COLUMN — Session + Live
  const sparkData = spark.available ? spark.value : [];

  const right =
    `<div style="flex:1;display:flex;flex-direction:column;gap:8px;padding-left:10px">` +
      `<div>` +
        sectionTitle('This Session') +
        `<div style="color:${pri};font-size:3.52cqw;font-weight:700;margin:2px 0">${fmtTok(sTotalVal)}</div>` +
        tokBar(sessIn.value||0, sessOut.value||0, sessCW.value||0, sessCR.value||0, sTotalVal) +
        `<div style="margin-top:6px">` +
          statRow('Input', fmtTok(sessIn.value||0), cIn) +
          statRow('Output', fmtTok(sessOut.value||0), cOut) +
          statRow('Cache Write', fmtTok(sessCW.value||0), cCW) +
          statRow('Cache Read', fmtTok(sessCR.value||0), cCR) +
          statRow('Messages', sessMsgs.available ? sessMsgs.value.toLocaleString() : '--') +
        `</div>` +
      `</div>` +
      `<div>` +
        sectionTitle('Token Rate') +
        `<div style="margin:4px 0">` +
          sparkline(sparkData, pri, 400, 50) +
        `</div>` +
        `<div style="color:${pri};font-size:2.34cqw;font-weight:700">` +
          `${rate.available ? rate.value.toLocaleString() : '0'} <span style="color:${dim};font-size:1.37cqw">tok/min</span>` +
        `</div>` +
      `</div>` +
      `<div>` +
        sectionTitle('Agents') +
        `<div style="display:flex;align-items:center;gap:8px">` +
          `<span style="color:${pri};font-size:3.52cqw;font-weight:700">${agents.available ? agents.value : 0}</span>` +
          `<span style="color:${dim};font-size:1.56cqw">spawned this session</span>` +
        `</div>` +
      `</div>` +
    `</div>`;

  return '<div class="screen-frame" style="font-family:' + font + '">' +
    '<div style="display:flex;height:100%;padding:12px 16px;gap:0;box-sizing:border-box">' +
      left + right +
    '</div></div>';
}
"""


def _build_html() -> str:
    spec = web_spec()
    html = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chiketi Control Panel</title>
<style>
  @font-face {
    font-family: 'Chakra Petch';
    src: url('/assets/fonts/ChakraPetch-Bold.ttf') format('truetype');
    font-weight: bold; font-style: normal;
  }
  @font-face {
    font-family: 'Chakra Petch';
    src: url('/assets/fonts/ChakraPetch-Regular.ttf') format('truetype');
    font-weight: normal; font-style: normal;
  }
  @font-face {
    font-family: 'Antonio';
    src: url('/assets/fonts/Antonio-VariableFont.ttf') format('truetype');
    font-weight: 100 700; font-style: normal;
  }
  @font-face {
    font-family: 'Rajdhani';
    src: url('/assets/fonts/Rajdhani-SemiBold.ttf') format('truetype');
    font-weight: 600; font-style: normal;
  }
  @font-face {
    font-family: 'Rajdhani';
    src: url('/assets/fonts/Rajdhani-Regular.ttf') format('truetype');
    font-weight: normal; font-style: normal;
  }
  @font-face {
    font-family: 'Share Tech Mono';
    src: url('/assets/fonts/ShareTechMono-Regular.ttf') format('truetype');
    font-weight: normal; font-style: normal;
  }
  @font-face {
    font-family: 'Nixie One';
    src: url('/assets/fonts/NixieOne-Regular.ttf') format('truetype');
    font-weight: normal; font-style: normal;
  }
  @font-face {
    font-family: 'IBM Plex Mono';
    src: url('/assets/fonts/IBMPlexMono-Regular.ttf') format('truetype');
    font-weight: normal; font-style: normal;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: #0a0a0a;
    color: #e0e0e0;
    font-family: 'Consolas', 'DejaVu Sans Mono', monospace;
    padding: 24px;
    min-height: 100vh;
  }
  h1 { color: #00ff41; font-size: 20px; margin-bottom: 8px; letter-spacing: 2px; }
  .subtitle { color: #555; font-size: 13px; margin-bottom: 24px; }

  /* ── Main tabs ── */
  .main-tabs { display: flex; gap: 0; margin-bottom: 0; border-bottom: 2px solid #333; }
  .main-tab {
    padding: 10px 28px; border: 2px solid #333; border-bottom: none;
    border-radius: 6px 6px 0 0; background: #111; color: #666;
    cursor: pointer; font-family: inherit; font-size: 14px;
    text-transform: uppercase; letter-spacing: 2px; transition: all 0.2s;
    margin-right: -1px;
  }
  .main-tab:hover { color: #ccc; background: #1a1a1a; }
  .main-tab.active { background: #0a0a0a; color: #00ff41; border-color: #00ff41; border-bottom-color: #0a0a0a; position: relative; z-index: 1; }
  .tab-content { display: none; padding-top: 20px; }
  .tab-content.active { display: block; }

  /* ── Category dropdown + variant row ── */
  .theme-controls { display: flex; align-items: center; gap: 16px; margin-bottom: 20px; }
  .category-select {
    background: #111; color: #00ff41; border: 2px solid #333; border-radius: 6px;
    padding: 8px 16px; font-family: inherit; font-size: 14px;
    text-transform: uppercase; letter-spacing: 1px; cursor: pointer;
    appearance: none; -webkit-appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%2300ff41' stroke-width='2' fill='none'/%3E%3C/svg%3E");
    background-repeat: no-repeat; background-position: right 10px center;
    padding-right: 32px;
  }
  .category-select:hover { border-color: #00ff41; }
  .category-select option { background: #111; color: #ccc; }

  .variant-row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .variant-btn {
    padding: 6px 16px; border: 2px solid #333; border-radius: 6px;
    background: #111; cursor: pointer; font-family: inherit; font-size: 13px;
    transition: all 0.2s; display: flex; align-items: center; gap: 8px;
  }
  .variant-btn:hover { border-color: #666; }
  .variant-btn.active { border-width: 2px; }
  .variant-dot { width: 12px; height: 12px; border-radius: 50%; }

  /* ── Settings panel ── */
  .settings-section {
    margin-bottom: 24px; padding: 16px; border: 1px solid #222;
    border-radius: 6px; background: #0d0d0d;
  }
  .settings-section h3 {
    color: #00ff41; font-size: 13px; text-transform: uppercase;
    letter-spacing: 2px; margin-bottom: 12px;
  }
  .setting-row {
    display: flex; align-items: center; gap: 12px; margin-bottom: 8px;
  }
  .setting-label { color: #888; font-size: 12px; min-width: 80px; }
  .setting-select {
    background: #111; color: #0f0; border: 1px solid #333;
    padding: 4px 10px; font-family: monospace; font-size: 13px;
    border-radius: 4px;
  }
  .setting-btn {
    background: #181818; color: #00ff41; border: 1px solid #333;
    padding: 6px 16px; cursor: pointer; font-family: monospace;
    font-size: 12px; border-radius: 4px; transition: all 0.2s;
  }
  .setting-btn:hover { background: #222; border-color: #00ff41; }

  /* ── Host list ── */
  .host-card {
    display: flex; align-items: center; gap: 12px;
    padding: 10px 14px; border: 1px solid #222; border-radius: 6px;
    background: #111; cursor: pointer; transition: all 0.2s;
  }
  .host-card:hover { border-color: #444; background: #181818; }
  .host-card.active-host { border-color: #00ff41; }
  .host-dot {
    width: 10px; height: 10px; border-radius: 50%;
    flex-shrink: 0;
  }
  .host-dot.online { background: #00ff41; box-shadow: 0 0 6px #00ff41; }
  .host-dot.offline { background: #f44; box-shadow: 0 0 6px #f44; }
  .host-name { color: #ccc; font-size: 13px; font-weight: bold; flex: 1; }
  .host-latency { color: #666; font-size: 11px; }
  .host-active-tag {
    color: #00ff41; font-size: 10px; text-transform: uppercase;
    border: 1px solid #00ff41; padding: 1px 6px; border-radius: 3px;
  }

  /* ── Screen replicas ── */
  .screens { display: flex; flex-direction: column; gap: 24px; max-width: 1024px; }
  .screen-label {
    font-size: 12px; color: #666; text-transform: uppercase;
    letter-spacing: 1px; margin-bottom: 6px;
  }
  .screen-frame {
    width: 100%; aspect-ratio: 1024 / 600; border-radius: 6px;
    overflow: hidden; position: relative; container-type: inline-size;
  }
  /* ── Terminal panels ── */
  .t-screen { display: grid; gap: 6px; padding: 6px; width: 100%; height: 100%; }
  .t-2col-3row {
    grid-template-columns: 1fr 1fr;
    grid-template-rows: 4fr 3fr 3fr;
  }
  .t-1col-3row {
    grid-template-columns: 1fr;
    grid-template-rows: 5fr 3fr 2fr;
  }
  .t-panel { padding: 8px 10px; overflow: hidden; }
  .t-title {
    font-size: 18px; margin-bottom: 6px; white-space: nowrap;
    font-family: 'Consolas', 'DejaVu Sans Mono', monospace;
  }
  .t-row {
    display: flex; align-items: center; gap: 6px;
    font-size: 16px; margin-bottom: 3px; white-space: nowrap;
    font-family: 'Consolas', 'DejaVu Sans Mono', monospace;
  }
  .t-label { flex-shrink: 0; }
  .t-bar {
    flex: 1; height: 16px; display: flex; border-radius: 1px; overflow: hidden;
    font-size: 14px; font-family: 'Consolas', monospace; line-height: 16px;
  }
  .t-val { flex-shrink: 0; }

  /* ── Panel layout (sizes in cqw = % of 1024px container width) ── */
  .l-screen {
    display: grid; gap: 0.78cqw; padding: 0.78cqw; width: 100%; height: 100%;
    font-family: 'Chakra Petch', sans-serif;
  }
  .l-2x2 { grid-template-columns: 1fr 1fr; grid-template-rows: 1fr 1fr; }
  .l-top-2bot {
    grid-template-columns: 1fr 1fr;
    grid-template-rows: 55fr 45fr;
  }
  .l-top-2bot .l-panel:first-child { grid-column: 1 / -1; }
  .l-clock-layout {
    grid-template-columns: 1fr;
    grid-template-rows: 1fr;
  }
  .l-panel { display: flex; flex-direction: column; overflow: hidden; border-radius: 2px; }
  .l-titlebar {
    font-size: 3.32cqw; font-weight: bold; color: #000; padding: 0.29cqw 0.78cqw;
    text-transform: uppercase; letter-spacing: 0.05cqw; white-space: nowrap;
    flex-shrink: 0; font-family: 'Chakra Petch', sans-serif;
  }
  .l-body {
    flex: 1; background: #0a0a0a; padding: 0.39cqw 0.78cqw;
    display: flex; flex-direction: column; justify-content: flex-start; gap: 0.39cqw;
  }
  .l-stat {
    display: flex; justify-content: space-between; align-items: baseline;
    font-size: 2.73cqw; white-space: nowrap;
  }
  .l-stat-label {
    color: #ccc; text-transform: uppercase; font-size: 2.73cqw;
    font-weight: bold; font-family: 'Chakra Petch', sans-serif;
  }
  .l-stat-val {
    font-weight: bold; font-size: 3.13cqw;
    font-family: 'Chakra Petch', sans-serif;
  }
  .l-bar {
    height: 1.17cqw; background: #282828; border-radius: 2px; overflow: hidden;
  }
  .l-bar-fill {
    height: 100%; border-radius: 2px; position: relative;
  }
  .l-bar-fill::after {
    content: ''; position: absolute; top: 0; left: 0; right: 0; height: 15%;
    background: rgba(255,255,255,0.3); border-radius: 2px 2px 0 0;
  }
  .l-kv {
    display: flex; gap: 1.17cqw; font-size: 3.13cqw; align-items: baseline;
  }
  .l-kv-label {
    color: #ccc; text-transform: uppercase; font-size: 2.73cqw;
    font-weight: bold; font-family: 'Chakra Petch', sans-serif;
  }
  .l-kv-val {
    font-weight: bold; font-size: 3.13cqw;
    font-family: 'Chakra Petch', sans-serif;
  }

  /* ── Clock ── */
  .l-clock-body {
    flex: 1; background: #0a0a0a; display: flex; flex-direction: column;
    align-items: center; justify-content: center; gap: 1.56cqw;
  }
  .l-clock-time {
    font-size: 11.72cqw; font-weight: bold; letter-spacing: 0.39cqw;
    font-family: 'Chakra Petch', sans-serif;
  }
  .l-clock-sec {
    font-size: 4.69cqw; font-weight: bold;
    font-family: 'Chakra Petch', sans-serif; margin-left: 0.78cqw;
  }
  .l-clock-day {
    font-size: 2.73cqw; text-transform: uppercase; letter-spacing: 0.2cqw;
    font-weight: normal; font-family: 'Chakra Petch', sans-serif;
  }
  .l-clock-date {
    font-size: 2.73cqw; text-transform: uppercase; letter-spacing: 0.1cqw;
    font-weight: normal; font-family: 'Chakra Petch', sans-serif;
  }

  /* ── 4-col grid inside NPU panel ── */
  .l-4col { display: grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap: 0.78cqw; }

  .status { margin-top: 24px; color: #555; font-size: 12px; }
  .status.ok { color: #00ff41; }

  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.12} }
  @keyframes spin { from{transform:rotate(0deg)} to{transform:rotate(360deg)} }
  @keyframes nixieFlicker { 0%{opacity:1} 5%{opacity:0.98} 10%{opacity:1} 15%{opacity:0.96} 17%{opacity:1} 50%{opacity:1} 52%{opacity:0.97} 54%{opacity:1} 80%{opacity:1} 82%{opacity:0.95} 83%{opacity:1} 90%{opacity:0.98} 100%{opacity:1} }
  @keyframes nixieMicroFlicker { 0%,100%{filter:brightness(1)} 25%{filter:brightness(0.97)} 50%{filter:brightness(1.02)} 75%{filter:brightness(0.98)} }
  @keyframes spinFan { from{transform:rotate(0deg)} to{transform:rotate(360deg)} }
</style>
</head>
<body>

<h1>// CHIKETI</h1>
<p class="subtitle">Dashboard Control Panel</p>

<div class="main-tabs">
  <button class="main-tab active" data-tab="themes" onclick="switchTab('themes')">Themes</button>
  <button class="main-tab" data-tab="hosts" onclick="switchTab('hosts')">Hosts</button>
  <button class="main-tab" data-tab="settings" onclick="switchTab('settings')">Settings</button>
</div>

<div id="tab-themes" class="tab-content active">
  <div class="theme-controls">
    <select id="categorySelect" class="category-select"></select>
    <div class="variant-row" id="variantRow"></div>
  </div>
  <div class="screens" id="screens"></div>
</div>

<div id="tab-hosts" class="tab-content">
  <div class="settings-section">
    <h3>Remote Hosts</h3>
    <div id="hostList" style="display:flex;flex-direction:column;gap:8px"></div>
    <div style="margin-top:12px;border-top:1px solid #333;padding-top:12px">
      <h4 style="color:#999;font-size:12px;margin-bottom:8px">ADD NEW HOST</h4>
      <div style="display:flex;flex-direction:column;gap:6px">
        <input type="text" id="newHostName" placeholder="Friendly name (e.g. gpu-server)" style="background:#1a1a1a;border:1px solid #333;color:#fff;padding:6px 8px;border-radius:4px;font-size:12px">
        <input type="text" id="newHostAddr" placeholder="IP or hostname (e.g. 192.168.1.50)" style="background:#1a1a1a;border:1px solid #333;color:#fff;padding:6px 8px;border-radius:4px;font-size:12px">
        <input type="text" id="newHostUser" placeholder="SSH username" style="background:#1a1a1a;border:1px solid #333;color:#fff;padding:6px 8px;border-radius:4px;font-size:12px">
        <input type="password" id="newHostPassword" placeholder="SSH password (for key setup, not stored)" style="background:#1a1a1a;border:1px solid #333;color:#fff;padding:6px 8px;border-radius:4px;font-size:12px">
        <div style="display:flex;gap:6px">
          <input type="number" id="newHostPort" placeholder="Port" value="22" style="background:#1a1a1a;border:1px solid #333;color:#fff;padding:6px 8px;border-radius:4px;font-size:12px;width:80px">
          <button id="btnTestHost" onclick="cpTestHost()" style="flex:1;background:#222;border:1px solid #444;color:#fff;padding:6px;border-radius:4px;font-size:12px;cursor:pointer">Test</button>
          <button id="btnCopyKey" onclick="cpCopyKey()" style="flex:1;background:#1a2a3a;border:1px solid #2a4a6a;color:#4af;padding:6px;border-radius:4px;font-size:12px;cursor:pointer">Setup Key</button>
          <button id="btnAddHost" onclick="cpAddHost()" style="flex:1;background:#1a3a1a;border:1px solid #2a5a2a;color:#0f0;padding:6px;border-radius:4px;font-size:12px;cursor:pointer">Add</button>
        </div>
        <div id="hostActionStatus" style="font-size:11px;min-height:16px"></div>
      </div>
    </div>
  </div>
</div>

<div id="tab-settings" class="tab-content">
  <div class="settings-section">
    <h3>Display</h3>
    <div class="setting-row">
      <span class="setting-label">Dashboard</span>
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
        <div id="powerToggle" style="width:44px;height:24px;border-radius:12px;background:#333;position:relative;transition:background 0.3s;cursor:pointer">
          <div style="width:20px;height:20px;border-radius:50%;background:#888;position:absolute;top:2px;left:2px;transition:all 0.3s"></div>
        </div>
        <span id="powerLabel" style="color:#888;font-size:13px">OFF</span>
      </label>
    </div>
    <div class="setting-row">
      <span class="setting-label">Output</span>
      <select id="outputSelect" class="setting-select"></select>
      <button id="scanDisplays" class="setting-btn" style="font-size:11px;padding:4px 10px">Scan</button>
    </div>
    <div class="setting-row">
      <span class="setting-label">Brightness</span>
      <input type="range" id="brightnessSlider" min="0.3" max="2.0" step="0.1" value="1.0" style="width:160px">
      <span id="brightnessVal" style="color:#0f0;font-size:13px">1.0</span>
    </div>
    <div class="setting-row">
      <span class="setting-label">Resolution</span>
      <span id="resDisplay" style="color:#0f0;font-size:13px">--</span>
    </div>
  </div>
  <div class="settings-section">
    <h3>Screen Rotation</h3>
    <div id="screenRotationList" style="display:flex;flex-direction:column;gap:8px"></div>
  </div>
  <div class="setting-row" style="margin-top:16px">
    <button id="applySettings" class="setting-btn" style="padding:8px 24px">Apply Settings</button>
    <span id="settingsStatus" style="color:#555;font-size:12px;margin-left:12px"></span>
  </div>
</div>

<p class="status" id="status"></p>

<script>
const API = window.location.origin;
const PANEL_SPEC = __PANEL_SPEC_JSON__;
let currentData = null, metrics = null;
let selectedFamily = null, selectedVariant = null;

/* ── Data helpers ── */
function m(key) {
  if (!metrics || !metrics[key]) return { value: null, available: false, unit: '', extra: {} };
  return metrics[key];
}
function mv(key, suffix) {
  const d = m(key);
  if (!d.available) return 'N/A';
  return suffix ? d.value + suffix : String(d.value);
}

function cleanModel() {
  const d = m('llama.model');
  if (!d.available) return '--';
  return String(d.value).replace(/\.gguf$/i, '').replace(/[-_]Q\d[A-Z0-9_]*$/i, '').replace(/_/g, ' ').replace(/-$/, '');
}

function switchTab(tab) {
  document.querySelectorAll('.main-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.toggle('active', t.id === 'tab-' + tab));
}

async function loadThemes() {
  try {
    const [tr, mr] = await Promise.all([
      fetch(API + '/api/themes'), fetch(API + '/api/metrics')
    ]);
    currentData = await tr.json();
    metrics = await mr.json();
    if (!selectedFamily) selectedFamily = currentData.active_family;
    if (!selectedVariant) selectedVariant = currentData.active_variant;
    renderCategoryDropdown();
    renderVariantRow();
    renderScreens();
    renderScreenRotationUI();
    setStatus('Connected', true);
  } catch(e) { setStatus('Connection failed', false); }
}

function renderCategoryDropdown() {
  const sel = document.getElementById('categorySelect');
  sel.innerHTML = '';
  for (const fam of Object.keys(currentData.families)) {
    const opt = document.createElement('option');
    opt.value = fam;
    opt.textContent = fam;
    if (fam === selectedFamily) opt.selected = true;
    sel.appendChild(opt);
  }
  sel.onchange = () => {
    selectedFamily = sel.value;
    selectedVariant = Object.keys(currentData.families[selectedFamily])[0];
    renderVariantRow();
    renderScreens();
  };
}

function renderVariantRow() {
  const el = document.getElementById('variantRow'); el.innerHTML = '';
  const variants = currentData.families[selectedFamily] || {};
  const isActive = selectedFamily === currentData.active_family;
  for (const [name, c] of Object.entries(variants)) {
    const btn = document.createElement('button');
    const active = name === selectedVariant;
    const live = isActive && name === currentData.active_variant;
    btn.className = 'variant-btn' + (active ? ' active' : '');
    btn.style.borderColor = active ? c.primary : '#333';
    btn.style.color = active ? c.primary : '#888';
    btn.innerHTML = `<span class="variant-dot" style="background:${c.primary}"></span>${name}` +
      (live ? ' <span style="font-size:10px;color:#666">(live)</span>' : '');
    btn.onclick = () => {
      selectedVariant = name;
      selectTheme(selectedFamily, name);
      renderVariantRow();
      renderScreens();
    };
    el.appendChild(btn);
  }
}

/* ═══════════════════════════════════════
   Shared rendering helpers
   ═══════════════════════════════════════ */
function tBar(c, pct) {
  if (pct == null) return '';
  pct = Math.max(0, Math.min(100, pct));
  const filled = Math.round(pct / 5), empty = 20 - filled;
  return `<span class="t-bar"><span style="color:${c.primary}">${'\u2588'.repeat(filled)}</span><span style="color:${c.primary};opacity:0.2">${'\u2591'.repeat(empty)}</span></span>`;
}
function tPanel(c, title, rows) {
  return `<div class="t-panel" style="background:${c.panel};border:1px solid ${c.border}">` +
    `<div class="t-title" style="color:${c.header}">\u2500\u2500[ ${title} ]</div>${rows}</div>`;
}
function tRow(c, label, bar, val, color) {
  color = color || c.primary;
  return `<div class="t-row"><span class="t-label" style="color:${c.primary}">${label}</span>` +
    (bar || '') + `<span class="t-val" style="color:${color}">${val}</span></div>`;
}

const GOLD = PANEL_SPEC.colors.gold;
const AMBER = PANEL_SPEC.colors.amber;
const GREEN = PANEL_SPEC.colors.green;
const TEAL = PANEL_SPEC.colors.teal;
function _thermColor(t) {
  if (t >= 90) return PANEL_SPEC.colors.thermOrange || '#FF7700';
  if (t >= 70) return PANEL_SPEC.colors.thermYellow || '#DDCC00';
  if (t >= 50) return PANEL_SPEC.colors.thermGreen || '#22BB44';
  return PANEL_SPEC.colors.thermBlue || '#2288DD';
}
function lPanel(titleLeft, color, body, titleRight) {
  const right = titleRight ? `<span>${titleRight}</span>` : '';
  return `<div class="l-panel" style="border:2px solid ${color}">` +
    `<div class="l-titlebar" style="background:${color};display:flex;justify-content:space-between;align-items:center">`+
    `<span>${titleLeft}</span>${right}</div>` +
    `<div class="l-body">${body}</div></div>`;
}
function lStat(label, val, color) {
  return `<div class="l-stat"><span class="l-stat-label">${label}</span>` +
    `<span class="l-stat-val" style="color:${color}">${val}</span></div>`;
}
function lBar(color, pct) {
  if (pct == null) return '';
  return `<div class="l-bar"><div class="l-bar-fill" style="width:${Math.max(0,Math.min(100,pct))}%;background:${color}"></div></div>`;
}

__SCREEN_FUNCTIONS__


function getScreenRegistry(c) {
  const isPanel = selectedFamily === 'Panel';
  const isVintage = selectedFamily === 'Vintage';
  const isCoral = isPanel && selectedVariant === 'Coral';
  const isTeal = isPanel && selectedVariant === 'Teal';
  let screens;
  if (isTeal) screens = [{id:'screen1',name:'System Stats',fn:panelTealScreen1},{id:'screen2',name:'Clock',fn:panelTealScreen2}];
  else if (isCoral) screens = [{id:'screen1',name:'System Stats',fn:panelCoralScreen1},{id:'screen2',name:'Clock',fn:panelCoralScreen2}];
  else if (isPanel) screens = [{id:'screen1',name:'System Stats',fn:panelGoldScreen1},{id:'screen2',name:'Clock',fn:panelGoldScreen2}];
  else if (isVintage && selectedVariant === 'Tubes') screens = [{id:'screen1',name:'System Stats',fn:tubeScreen1},{id:'screen2',name:'Clock',fn:tubeScreen2}];
  else if (isVintage && selectedVariant === 'VFD') screens = [{id:'screen1',name:'System Stats',fn:vfdScreen1},{id:'screen2',name:'Clock',fn:vfdScreen2}];
  else if (isVintage) screens = [{id:'screen1',name:'System Stats',fn:scanScreen1},{id:'screen2',name:'Clock',fn:scanScreen2}];
  else screens = [{id:'screen1',name:'System Stats',fn:terminalScreen1},{id:'screen2',name:'AI Monitor',fn:terminalScreen2}];
  screens.push({id:'screen3',name:'Claude Usage',fn:claudeScreen3});
  return screens;
}

function renderScreens() {
  const el = document.getElementById('screens'); el.innerHTML = '';
  const c = (currentData.families[selectedFamily] || {})[selectedVariant];
  if (!c) return;
  const screens = getScreenRegistry(c);
  for (const s of screens) {
    const div = document.createElement('div');
    div.innerHTML = `<div class="screen-label">${s.name}</div>${s.fn(c)}`;
    el.appendChild(div);
  }
}

async function selectTheme(family, variant) {
  try {
    const res = await fetch(API + '/api/theme/' + family + '/' + variant, { method: 'POST' });
    if (res.ok) { await loadThemes(); setStatus('Theme: ' + family + '/' + variant, true); }
  } catch(e) { setStatus('Failed to set theme', false); }
}

function setStatus(msg, ok) {
  const el = document.getElementById('status');
  el.textContent = msg;
  el.className = 'status' + (ok ? ' ok' : '');
}

loadThemes();

// ── Host management ──
let _hostData = null;

async function loadHosts() {
  try {
    const res = await fetch(API + '/api/hosts');
    _hostData = await res.json();
    renderHostList();
  } catch(e) {}
}

function renderHostList() {
  const el = document.getElementById('hostList');
  if (!el || !_hostData) return;
  el.innerHTML = '';
  if (!_hostData.hosts || _hostData.hosts.length === 0) {
    el.innerHTML = '<div style="color:#666;font-size:12px">No hosts configured</div>';
    return;
  }
  for (const h of _hostData.hosts) {
    const isActive = h.name === _hostData.active_host;
    const card = document.createElement('div');
    card.className = 'host-card' + (isActive ? ' active-host' : '');
    card.style.position = 'relative';
    const mainArea = document.createElement('div');
    mainArea.style.cssText = 'display:flex;align-items:center;gap:8px;flex:1;cursor:pointer';
    mainArea.onclick = () => cpSwitchHost(h.name);
    mainArea.innerHTML =
      `<div class="host-dot ${h.online ? 'online' : 'offline'}"></div>` +
      `<span class="host-name">${h.name}</span>` +
      (h.latency_ms != null ? `<span class="host-latency">${h.latency_ms}ms</span>` : '') +
      (isActive ? '<span class="host-active-tag">active</span>' : '');
    card.appendChild(mainArea);
    const removeBtn = document.createElement('button');
    removeBtn.textContent = '\u00d7';
    removeBtn.title = 'Remove host';
    removeBtn.style.cssText = 'background:none;border:1px solid transparent;color:#666;font-size:16px;cursor:pointer;padding:2px 6px;border-radius:3px;line-height:1;transition:all 0.2s';
    removeBtn.onmouseenter = () => { removeBtn.style.color='#f44'; removeBtn.style.borderColor='#f44'; };
    removeBtn.onmouseleave = () => { removeBtn.style.color='#666'; removeBtn.style.borderColor='transparent'; };
    removeBtn.onclick = (e) => { e.stopPropagation(); cpRemoveHost(h.name); };
    card.appendChild(removeBtn);
    el.appendChild(card);
  }
}

async function cpTestHost() {
  const host = document.getElementById('newHostAddr').value.trim();
  const user = document.getElementById('newHostUser').value.trim();
  const port = parseInt(document.getElementById('newHostPort').value) || 22;
  if (!host || !user) { setHostStatus('Host and username required', false); return; }
  setHostStatus('Testing connection...', null);
  try {
    const res = await fetch(API + '/api/setup/test-connection', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({host, user, port})
    });
    const data = await res.json();
    if (data.success) {
      setHostStatus('Connected! Hostname: ' + data.hostname, true);
    } else {
      setHostStatus('Failed: ' + data.error, false);
    }
  } catch(e) { setHostStatus('Error: ' + e, false); }
}

async function cpCopyKey() {
  const host = document.getElementById('newHostAddr').value.trim();
  const user = document.getElementById('newHostUser').value.trim();
  const password = document.getElementById('newHostPassword').value;
  const port = parseInt(document.getElementById('newHostPort').value) || 22;
  if (!host || !user) { setHostStatus('Host and username required', false); return; }
  if (!password) { setHostStatus('Password required to copy SSH key', false); return; }
  setHostStatus('Copying SSH key...', null);
  try {
    const res = await fetch(API + '/api/setup/copy-key', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({host, user, port, password})
    });
    const data = await res.json();
    if (data.success) {
      setHostStatus('Key copied! You can now test without a password.', true);
      document.getElementById('newHostPassword').value = '';
    } else {
      setHostStatus('Failed: ' + data.error, false);
    }
  } catch(e) { setHostStatus('Error: ' + e, false); }
}

async function cpAddHost() {
  const name = document.getElementById('newHostName').value.trim();
  const host = document.getElementById('newHostAddr').value.trim();
  const user = document.getElementById('newHostUser').value.trim();
  const port = parseInt(document.getElementById('newHostPort').value) || 22;
  if (!name || !host || !user) { setHostStatus('All fields required', false); return; }
  try {
    const res = await fetch(API + '/api/setup/add-host', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({name, host, user, port})
    });
    const data = await res.json();
    if (data.success) {
      setHostStatus('Host added!', true);
      document.getElementById('newHostName').value = '';
      document.getElementById('newHostAddr').value = '';
      document.getElementById('newHostUser').value = '';
      document.getElementById('newHostPassword').value = '';
      document.getElementById('newHostPort').value = '22';
      await loadHosts();
    } else {
      setHostStatus(data.error || 'Failed to add host', false);
    }
  } catch(e) { setHostStatus('Error: ' + e, false); }
}

async function cpRemoveHost(name) {
  if (!confirm('Remove host "' + name + '"?')) return;
  try {
    const res = await fetch(API + '/api/setup/remove-host', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({name})
    });
    const data = await res.json();
    if (data.success) {
      setStatus('Host removed', true);
      await loadHosts();
    } else { setStatus(data.error || 'Failed', false); }
  } catch(e) { setStatus('Error', false); }
}

function setHostStatus(msg, ok) {
  const el = document.getElementById('hostActionStatus');
  if (!el) return;
  el.textContent = msg;
  el.style.color = ok === null ? '#999' : ok ? '#0f0' : '#f44';
}

async function cpSwitchHost(name) {
  try {
    const res = await fetch(API + '/api/host/' + encodeURIComponent(name), { method: 'POST' });
    if (res.ok) {
      await loadHosts();
      await loadThemes();
      setStatus('Switched to ' + name, true);
    } else { setStatus('Failed to switch host', false); }
  } catch(e) { setStatus('Error switching host', false); }
}

loadHosts();
// Refresh host status every 5 seconds
setInterval(loadHosts, 5000);

// Settings
let _outputsCache = [];
async function loadSettings() {
  try {
    const res = await fetch(API + '/api/display');
    const data = await res.json();
    _outputsCache = (data.outputs || []).filter(o => o.connected);
    populateOutputs(_outputsCache, data.current_output);
    document.getElementById('brightnessSlider').value = data.brightness || 1.0;
    document.getElementById('brightnessVal').textContent = (data.brightness || 1.0).toFixed(1);
    _serverScreenRotation = data.screen_rotation || {};
    renderScreenRotationUI();
    updatePowerToggle(data.display_on || false);
    updateResDisplay();
    updatePreviewAspectRatio(data.width || 1024, data.height || 600);
  } catch(e) {}
}
function populateOutputs(outputs, current) {
  const sel = document.getElementById('outputSelect');
  sel.innerHTML = '';
  outputs.forEach(o => {
    const opt = document.createElement('option');
    opt.value = o.name;
    opt.textContent = o.name + (o.resolution ? ' (' + o.resolution + ')' : '');
    if (o.name === current) opt.selected = true;
    sel.appendChild(opt);
  });
  updateResDisplay();
}
function getSelectedResolution() {
  const name = document.getElementById('outputSelect').value;
  const o = _outputsCache.find(x => x.name === name);
  return o && o.resolution ? o.resolution : null;
}
function updateResDisplay() {
  const res = getSelectedResolution();
  document.getElementById('resDisplay').textContent = res || 'auto';
}
function parseResolution(res) {
  if (!res) return null;
  const m = res.match(/^(\d+)x(\d+)/);
  return m ? { w: parseInt(m[1]), h: parseInt(m[2]) } : null;
}
let _serverScreenRotation = {};
function renderScreenRotationUI() {
  const el = document.getElementById('screenRotationList');
  el.innerHTML = '';
  const c = (currentData && currentData.families[selectedFamily] || {})[selectedVariant];
  if (!c) return;
  const screens = getScreenRegistry(c);
  for (const s of screens) {
    const cfg = _serverScreenRotation[s.id] || { enabled: true, duration: 10 };
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;align-items:center;gap:10px;padding:4px 0';
    row.innerHTML =
      `<label style="display:flex;align-items:center;gap:6px;color:#ccc;flex:1;cursor:pointer">` +
        `<input type="checkbox" data-screen="${s.id}" class="sr-enable" ${cfg.enabled ? 'checked' : ''} style="accent-color:#0f0;width:16px;height:16px">` +
        `${s.name}` +
      `</label>` +
      `<input type="number" data-screen="${s.id}" class="sr-duration" value="${cfg.duration}" min="3" max="600" ` +
        `style="width:60px;background:#111;border:1px solid #333;color:#0f0;padding:4px 6px;border-radius:4px;font-size:13px;text-align:center">` +
      `<span style="color:#666;font-size:12px">sec</span>`;
    el.appendChild(row);
  }
}
function getScreenRotationFromUI() {
  const result = {};
  document.querySelectorAll('.sr-enable').forEach(cb => {
    const id = cb.dataset.screen;
    const dur = document.querySelector(`.sr-duration[data-screen="${id}"]`);
    result[id] = { enabled: cb.checked, duration: parseInt(dur.value) || 10 };
  });
  return result;
}
let _displayOn = false;
function updatePowerToggle(isOn) {
  _displayOn = isOn;
  const toggle = document.getElementById('powerToggle');
  const knob = toggle.firstElementChild;
  const label = document.getElementById('powerLabel');
  if (isOn) {
    toggle.style.background = '#00aa44';
    knob.style.left = '22px';
    knob.style.background = '#fff';
    label.textContent = 'ON';
    label.style.color = '#00ff41';
  } else {
    toggle.style.background = '#333';
    knob.style.left = '2px';
    knob.style.background = '#888';
    label.textContent = 'OFF';
    label.style.color = '#888';
  }
}
document.getElementById('powerToggle').addEventListener('click', async function() {
  const newState = !_displayOn;
  try {
    const res = await fetch(API + '/api/display', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ display_on: newState }),
    });
    if (res.ok) {
      const data = await res.json();
      updatePowerToggle(data.display_on);
    }
  } catch(e) {}
});
function updatePreviewAspectRatio(w, h) {
  document.querySelectorAll('.screen-frame').forEach(f => {
    f.style.aspectRatio = w + ' / ' + h;
  });
}
document.getElementById('outputSelect').addEventListener('change', updateResDisplay);
document.getElementById('brightnessSlider').addEventListener('input', function() {
  document.getElementById('brightnessVal').textContent = parseFloat(this.value).toFixed(1);
});
document.getElementById('scanDisplays').addEventListener('click', async function() {
  try {
    const res = await fetch(API + '/api/display');
    const data = await res.json();
    _outputsCache = (data.outputs || []).filter(o => o.connected);
    populateOutputs(_outputsCache, data.current_output);
    document.getElementById('settingsStatus').textContent = 'Scanned ' + _outputsCache.length + ' connected';
    document.getElementById('settingsStatus').style.color = '#00ff41';
  } catch(e) {}
});
document.getElementById('applySettings').addEventListener('click', async function() {
  const dims = parseResolution(getSelectedResolution());
  const body = {
    output: document.getElementById('outputSelect').value,
    brightness: parseFloat(document.getElementById('brightnessSlider').value),
    screen_rotation: getScreenRotationFromUI(),
  };
  if (dims) { body.width = dims.w; body.height = dims.h; }
  try {
    const res = await fetch(API + '/api/display', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (res.ok) {
      const data = await res.json();
      updatePreviewAspectRatio(data.width, data.height);
      document.getElementById('settingsStatus').textContent = 'Settings applied';
      document.getElementById('settingsStatus').style.color = '#00ff41';
    } else {
      document.getElementById('settingsStatus').textContent = 'Failed to apply';
      document.getElementById('settingsStatus').style.color = '#ff4444';
    }
  } catch(e) {
    document.getElementById('settingsStatus').textContent = 'Error';
    document.getElementById('settingsStatus').style.color = '#ff4444';
  }
});
loadSettings();

// Refresh metrics every 3 seconds
setInterval(async () => {
  try {
    const res = await fetch(API + '/api/metrics');
    metrics = await res.json();
    renderScreens();
  } catch(e) {}
}, 3000);
</script>
</body>
</html>"""
    screen_fns = _screen_functions_js()
    return (
        html
        .replace("__PANEL_SPEC_JSON__", json.dumps(spec))
        .replace("__PANEL_GOLD__", spec["colors"]["gold"])
        .replace("__SCREEN_FUNCTIONS__", screen_fns)
    )
