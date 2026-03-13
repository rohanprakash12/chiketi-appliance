"""Theme definitions and active theme management."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class Theme:
    name: str
    family: str
    background: str
    panel: str
    border: str
    primary: str
    accent: str
    critical: str
    dim: str
    header: str


THEMES: dict[str, Theme] = {
    # ── Terminal family ──
    "Terminal/hacker": Theme(
        name="hacker", family="Terminal",
        background="#0a0a0a", panel="#111111", border="#333333",
        primary="#00ff41", accent="#ffb000", critical="#ff3333",
        dim="#555555", header="#00ff41",
    ),
    "Terminal/cyan": Theme(
        name="cyan", family="Terminal",
        background="#0a0a0a", panel="#0a1520", border="#1a3a4a",
        primary="#00e5ff", accent="#ffb000", critical="#ff3333",
        dim="#4a6a7a", header="#00e5ff",
    ),
    "Terminal/amber": Theme(
        name="amber", family="Terminal",
        background="#0a0a00", panel="#141408", border="#3a3a1a",
        primary="#ffb000", accent="#ff6600", critical="#ff3333",
        dim="#6a6a3a", header="#ffb000",
    ),
    "Terminal/phosphor": Theme(
        name="phosphor", family="Terminal",
        background="#000800", panel="#001200", border="#004400",
        primary="#33ff33", accent="#aaff00", critical="#ff4444",
        dim="#226622", header="#33ff33",
    ),
    "Terminal/red_alert": Theme(
        name="red_alert", family="Terminal",
        background="#0a0000", panel="#140808", border="#3a1a1a",
        primary="#ff4444", accent="#ff8800", critical="#ff0000",
        dim="#6a3a3a", header="#ff4444",
    ),
    "Terminal/blue": Theme(
        name="blue", family="Terminal",
        background="#000008", panel="#080818", border="#1a1a4a",
        primary="#4488ff", accent="#ffb000", critical="#ff3333",
        dim="#3a3a6a", header="#4488ff",
    ),
    # ── Panel family ──
    "Panel/Gold": Theme(
        name="Gold", family="Panel",
        background="#000000", panel="#0a0a0a", border="#444444",
        primary="#FDCD06", accent="#FF8800", critical="#BF0F0F",
        dim="#cccccc", header="#FDCD06",
    ),
    "Panel/Teal": Theme(
        name="Teal", family="Panel",
        background="#111419", panel="#2F3749", border="#2F3749",
        primary="#2A9D8F", accent="#E7442A", critical="#FF4444",
        dim="#6D748C", header="#2A9D8F",
    ),
    "Panel/Coral": Theme(
        name="Coral", family="Panel",
        background="#000000", panel="#0a0a0a", border="#1a1a2a",
        primary="#FFCC66", accent="#FF9933", critical="#FF2200",
        dim="#CC99CC", header="#FFCC66",
    ),
    # ── Vintage family ──
    "Vintage/Scanlines": Theme(
        name="Scanlines", family="Vintage",
        background="#060810", panel="#0C1018", border="#334455",
        primary="#00FFCC", accent="#FFAA00", critical="#FF3344",
        dim="#334455", header="#00FFCC",
    ),
    "Vintage/Tubes": Theme(
        name="Tubes", family="Vintage",
        background="#0A0806", panel="#181210", border="#332820",
        primary="#FF8833", accent="#FF6E0B", critical="#FF3322",
        dim="#2A1E14", header="#FF8833",
    ),
    "Vintage/VFD": Theme(
        name="VFD", family="Vintage",
        background="#0A0A08", panel="#0A0A08", border="#1a1a16",
        primary="#00DDAA", accent="#FFAA22", critical="#FF4433",
        dim="#556655", header="#00DDAA",
    ),
}

# Backward-compat lookup: short name -> full key (for Terminal variants)
_SHORT_NAME_MAP: dict[str, str] = {}
for _key, _theme in THEMES.items():
    if _theme.family == "Terminal":
        _SHORT_NAME_MAP[_theme.name] = _key

_active_theme: Theme = THEMES["Panel/Gold"]
_listeners: list[Callable[[Theme], None]] = []


def get_active_theme() -> Theme:
    return _active_theme


def get_active_family() -> str:
    return _active_theme.family


def set_active_theme(name: str) -> bool:
    """Set active theme by name. Accepts 'family/variant' or short variant name.

    Returns True if changed.
    """
    global _active_theme

    # Try direct lookup first (family/variant format)
    theme = THEMES.get(name)

    # Fall back to short name (backward compat for Terminal variants)
    if theme is None:
        full_key = _SHORT_NAME_MAP.get(name)
        if full_key:
            theme = THEMES.get(full_key)

    if theme is None:
        return False

    _active_theme = theme
    for listener in _listeners:
        listener(theme)
    return True


def on_theme_change(callback: Callable[[Theme], None]) -> None:
    """Register a callback for theme changes."""
    _listeners.append(callback)


def list_themes() -> list[str]:
    return list(THEMES.keys())


def get_families() -> dict[str, list[Theme]]:
    """Return themes grouped by family."""
    families: dict[str, list[Theme]] = {}
    for theme in THEMES.values():
        families.setdefault(theme.family, []).append(theme)
    return families
