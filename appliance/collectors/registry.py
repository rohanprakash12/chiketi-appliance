"""Collector registry — creates RemoteCollector instances from config."""

from __future__ import annotations

from appliance.collectors.remote import RemoteCollector
from appliance.hosts import ApplianceConfig


def create_collectors(config: ApplianceConfig) -> list[RemoteCollector]:
    """Create one RemoteCollector per configured host.

    Does NOT connect — call collector.connect() separately so the
    caller can handle connection failures gracefully.
    """
    return [RemoteCollector(host_config) for host_config in config.hosts]
