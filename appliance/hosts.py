"""Host configuration management for chiketi-appliance."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class HostConfig:
    """Configuration for a single remote host."""

    name: str
    host: str
    user: str
    port: int = 22
    key_path: str | None = None
    password_env: str | None = None

    @property
    def password(self) -> str | None:
        """Resolve password from environment variable, if configured."""
        if self.password_env:
            return os.environ.get(self.password_env)
        return None


@dataclass
class ApplianceConfig:
    """Full appliance configuration."""

    hosts: list[HostConfig]
    display: dict[str, Any] = field(default_factory=dict)
    server: dict[str, Any] = field(default_factory=dict)


def default_config_path() -> str:
    """Return the default configuration file path."""
    return str(Path("~/.config/chiketi-appliance/config.yaml").expanduser())


def load_config(path: str) -> ApplianceConfig:
    """Parse YAML configuration file and return an ApplianceConfig.

    Expands ~ in key paths and resolves password_env from os.environ.
    Validates that at least one host is defined and required fields are present.

    Raises:
        FileNotFoundError: If the config file does not exist.
        ValueError: If the config is invalid (no hosts, missing fields, etc.).
    """
    config_path = Path(path).expanduser()
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, "r") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"Config file must contain a YAML mapping, got {type(raw).__name__}")

    # Parse hosts
    raw_hosts = raw.get("hosts")
    if not raw_hosts or not isinstance(raw_hosts, list):
        raise ValueError("Config must define at least one host under 'hosts'")

    hosts: list[HostConfig] = []
    for i, h in enumerate(raw_hosts):
        if not isinstance(h, dict):
            raise ValueError(f"Host entry {i} must be a mapping")

        # Validate required fields
        for req in ("name", "host", "user"):
            if req not in h:
                raise ValueError(
                    f"Host entry {i} is missing required field '{req}'"
                )

        # Expand ~ in key path
        key_path = h.get("key")
        if key_path:
            key_path = str(Path(key_path).expanduser())

        hosts.append(
            HostConfig(
                name=h["name"],
                host=h["host"],
                user=h["user"],
                port=h.get("port", 22),
                key_path=key_path,
                password_env=h.get("password_env"),
            )
        )

    # Validate unique host names
    names = [hc.name for hc in hosts]
    if len(names) != len(set(names)):
        raise ValueError("Host names must be unique")

    display = raw.get("display", {})
    if not isinstance(display, dict):
        display = {}
    server = raw.get("server", {})
    if not isinstance(server, dict):
        server = {}

    # Validate server.port
    if "port" in server:
        try:
            port_val = int(server["port"])
            if not (1 <= port_val <= 65535):
                raise ValueError(
                    f"server.port must be between 1 and 65535, got {port_val}"
                )
            server["port"] = port_val
        except (ValueError, TypeError) as exc:
            if isinstance(exc, ValueError) and "server.port" in str(exc):
                raise
            raise ValueError(
                f"server.port must be an integer, got {server['port']!r}"
            )

    # Validate display.rotate_interval
    if "rotate_interval" in display:
        try:
            ri = int(display["rotate_interval"])
            if ri <= 0:
                raise ValueError(
                    f"display.rotate_interval must be a positive number, got {ri}"
                )
            display["rotate_interval"] = ri
        except (ValueError, TypeError) as exc:
            if isinstance(exc, ValueError) and "rotate_interval" in str(exc):
                raise
            raise ValueError(
                f"display.rotate_interval must be a positive number, got {display['rotate_interval']!r}"
            )

    return ApplianceConfig(hosts=hosts, display=display, server=server)


def save_config(config: ApplianceConfig, path: str | None = None) -> str:
    """Save config to YAML. Returns path written to."""
    if path is None:
        path = default_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {
        "hosts": [
            {k: v for k, v in {
                "name": h.name,
                "host": h.host,
                "port": h.port if h.port != 22 else None,
                "user": h.user,
                "key": h.key_path,
            }.items() if v is not None}
            for h in config.hosts
        ],
        "display": config.display,
        "server": config.server,
    }
    # Write atomically
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return path
