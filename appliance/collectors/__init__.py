"""Collectors package — remote SSH-based metric collection."""

from appliance.collectors.base import MetricCollector, MetricValue

__all__ = [
    "MetricCollector",
    "MetricValue",
]


def create_collectors(*args, **kwargs):  # type: ignore[no-untyped-def]
    """Lazy import to avoid requiring paramiko at package import time."""
    from appliance.collectors.registry import create_collectors as _create
    return _create(*args, **kwargs)
