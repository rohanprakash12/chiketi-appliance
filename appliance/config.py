"""Display dimensions, timing, and threshold constants."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Timing:
    collect_interval_ms: int = 1500
    rotate_interval_s: int = 10
    pause_duration_s: int = 30


# Display dimensions
DISPLAY_WIDTH = 1024
DISPLAY_HEIGHT = 600

# Threshold percentages for color coding
THRESHOLD_WARNING = 70
THRESHOLD_CRITICAL = 90

TIMING = Timing()
