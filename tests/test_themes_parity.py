"""Coverage for the shared themes.py and panel_spec.py modules.

These modules are byte-identical copies shared with the sibling chiketi repo;
this is the kind of parity coverage the sibling lacks. We only assert public
behavior — we do not modify the modules.
"""

import json
import unittest

from appliance import themes
from appliance.panel_spec import web_spec


class TestListThemes(unittest.TestCase):
    def test_list_themes_nonempty(self):
        self.assertTrue(len(themes.list_themes()) > 0)

    def test_list_themes_matches_keys(self):
        self.assertEqual(themes.list_themes(), list(themes.THEMES.keys()))

    def test_keys_use_family_variant_format(self):
        for key in themes.list_themes():
            self.assertIn("/", key, f"theme key {key!r} should be family/variant")


class TestGetFamilies(unittest.TestCase):
    def test_families_group_all_themes(self):
        fams = themes.get_families()
        total = sum(len(v) for v in fams.values())
        self.assertEqual(total, len(themes.THEMES))

    def test_family_keys_match_theme_family_field(self):
        fams = themes.get_families()
        for family, theme_list in fams.items():
            for t in theme_list:
                self.assertEqual(t.family, family)

    def test_each_family_nonempty(self):
        for family, theme_list in themes.get_families().items():
            self.assertTrue(theme_list, f"family {family} empty")


class TestSetActiveTheme(unittest.TestCase):
    def setUp(self):
        self._orig = themes.get_active_theme()

    def tearDown(self):
        # Restore the original active theme by its full key.
        key = f"{self._orig.family}/{self._orig.name}"
        themes.set_active_theme(key)

    def test_set_known_theme_by_full_key(self):
        key = themes.list_themes()[0]
        self.assertTrue(themes.set_active_theme(key))
        active = themes.get_active_theme()
        self.assertEqual(f"{active.family}/{active.name}", key)

    def test_get_active_family_consistent(self):
        key = themes.list_themes()[-1]
        themes.set_active_theme(key)
        self.assertEqual(themes.get_active_family(), key.split("/")[0])

    def test_set_unknown_theme_returns_false(self):
        self.assertFalse(themes.set_active_theme("NoSuch/variant"))

    def test_listener_fires_on_change(self):
        seen = []
        themes.on_theme_change(lambda t: seen.append(t))
        themes.set_active_theme(themes.list_themes()[0])
        self.assertTrue(seen)


class TestThemeFields(unittest.TestCase):
    def test_every_theme_has_color_fields(self):
        fields = ("background", "panel", "border", "primary",
                  "accent", "critical", "dim", "header")
        for key, t in themes.THEMES.items():
            for f in fields:
                val = getattr(t, f)
                self.assertTrue(val, f"{key}.{f} empty")


class TestWebSpec(unittest.TestCase):
    def test_returns_dict(self):
        self.assertIsInstance(web_spec(), dict)

    def test_json_serializable(self):
        # Must round-trip through JSON since it is served via /api/themes.
        s = json.dumps(web_spec())
        self.assertIsInstance(s, str)
        self.assertEqual(json.loads(s), web_spec())

    def test_has_expected_top_level_sections(self):
        spec = web_spec()
        for section in ("colors", "coral", "teal"):
            self.assertIn(section, spec)

    def test_colors_section_has_entries(self):
        spec = web_spec()
        self.assertIn("gold", spec["colors"])
        self.assertTrue(spec["colors"]["gold"])

    def test_stable_across_calls(self):
        self.assertEqual(web_spec(), web_spec())


if __name__ == "__main__":
    unittest.main()
