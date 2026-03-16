"""CLI entry point for chiketi-appliance."""

from __future__ import annotations

import argparse
import os
import sys


def _parse_host_string(host_str: str) -> tuple[str, str, int]:
    """Parse ``user@host`` or ``user@host:port`` into (user, host, port).

    Raises ``ValueError`` on malformed input.
    """
    if "@" not in host_str:
        raise ValueError(
            f"Invalid host format: {host_str!r}  (expected user@host or user@host:port)"
        )
    user, rest = host_str.split("@", 1)
    if ":" in rest:
        host, port_str = rest.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            raise ValueError(f"Invalid port number in {host_str!r}: {port_str!r}")
        if not (1 <= port <= 65535):
            raise ValueError(f"Port out of range in {host_str!r}: {port} (must be 1-65535)")
    else:
        host = rest
        port = 22
    if not user or not host:
        raise ValueError(f"Invalid host format: {host_str!r}")
    return user, host, port


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="chiketi-appliance",
        description="Remote system monitoring dashboard for Raspberry Pi",
    )
    parser.add_argument(
        "--config",
        "-c",
        help="Path to YAML config file (default: ~/.config/chiketi-appliance/config.yaml)",
    )
    parser.add_argument(
        "--theme",
        "-t",
        help="Override display theme (e.g., 'Panel/Gold', 'Terminal/hacker')",
    )
    parser.add_argument(
        "--rotate-interval",
        type=int,
        help="Override screen rotation interval (seconds)",
    )
    parser.add_argument(
        "--host",
        help="Quick single-host mode: user@host or user@host:port",
    )
    parser.add_argument(
        "--key",
        "-k",
        help="SSH key path for --host mode",
    )
    parser.add_argument(
        "--port",
        "-p",
        type=int,
        help="Override HTTP server port",
    )

    args = parser.parse_args()

    # Import here to avoid heavy imports when just showing --help
    from appliance.app import run
    from appliance.hosts import ApplianceConfig, HostConfig, default_config_path

    if args.host:
        # ---- Quick single-host mode (no YAML file needed) ----
        try:
            user, host, ssh_port = _parse_host_string(args.host)
        except ValueError as exc:
            print(f"chiketi-appliance: {exc}", file=sys.stderr)
            return 1

        key_path = args.key
        if key_path:
            key_path = os.path.expanduser(key_path)

        host_cfg = HostConfig(
            name=host,
            host=host,
            user=user,
            port=ssh_port,
            key_path=key_path,
        )

        display: dict = {}
        if args.theme:
            display["theme"] = args.theme
        if args.rotate_interval is not None:
            display["rotate_interval"] = args.rotate_interval

        server: dict = {}
        if args.port is not None:
            server["port"] = args.port

        config = ApplianceConfig(hosts=[host_cfg], display=display, server=server)
        return run(config=config)

    # ---- Config-file mode ----
    config_path = args.config
    if config_path is None:
        default = default_config_path()
        if os.path.isfile(default):
            config_path = default
        else:
            # No config file found — launch setup wizard
            from appliance.app import run_setup_mode

            port = args.port or 7777
            return run_setup_mode(port=port)

    # Apply CLI overrides by loading config, patching, and passing the object
    from appliance.hosts import load_config

    try:
        config = load_config(config_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"chiketi-appliance: config error: {exc}", file=sys.stderr)
        return 1

    if args.theme:
        config.display["theme"] = args.theme
    if args.rotate_interval is not None:
        config.display["rotate_interval"] = args.rotate_interval
    if args.port is not None:
        config.server["port"] = args.port

    return run(config=config)


if __name__ == "__main__":
    sys.exit(main())
