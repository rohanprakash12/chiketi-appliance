"""Tests for appliance.collectors.registry.create_collectors."""

import unittest

from appliance.collectors.registry import create_collectors
from appliance.collectors.remote import RemoteCollector
from appliance.hosts import ApplianceConfig, HostConfig


class TestCreateCollectors(unittest.TestCase):
    def test_one_collector_per_host(self):
        cfg = ApplianceConfig(hosts=[
            HostConfig(name="a", host="10.0.0.1", user="u"),
            HostConfig(name="b", host="10.0.0.2", user="u"),
            HostConfig(name="c", host="10.0.0.3", user="u"),
        ])
        collectors = create_collectors(cfg)
        self.assertEqual(len(collectors), 3)

    def test_returns_remote_collectors(self):
        cfg = ApplianceConfig(hosts=[HostConfig(name="a", host="1.1.1.1", user="u")])
        collectors = create_collectors(cfg)
        self.assertIsInstance(collectors[0], RemoteCollector)

    def test_collectors_match_host_names_in_order(self):
        cfg = ApplianceConfig(hosts=[
            HostConfig(name="first", host="10.0.0.1", user="u"),
            HostConfig(name="second", host="10.0.0.2", user="u"),
        ])
        collectors = create_collectors(cfg)
        self.assertEqual([c.name for c in collectors], ["first", "second"])

    def test_collector_carries_host_config(self):
        hc = HostConfig(name="x", host="9.9.9.9", port=2200, user="bob")
        cfg = ApplianceConfig(hosts=[hc])
        collectors = create_collectors(cfg)
        self.assertIs(collectors[0]._config, hc)

    def test_empty_hosts_yields_empty_list(self):
        cfg = ApplianceConfig(hosts=[])
        self.assertEqual(create_collectors(cfg), [])

    def test_does_not_connect(self):
        # create_collectors must not open connections — all start offline.
        cfg = ApplianceConfig(hosts=[HostConfig(name="a", host="1.1.1.1", user="u")])
        collectors = create_collectors(cfg)
        self.assertFalse(collectors[0].online)
        self.assertFalse(collectors[0].is_connected())


if __name__ == "__main__":
    unittest.main()
