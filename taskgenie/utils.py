"""Shared formatting utilities used across the TaskGenie codebase."""
from __future__ import annotations


def fmt_count(n: int) -> str:
    """Compact integer: 1234 -> '1.2k', 84200 -> '84.2k', 1230000 -> '1.2M'."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.1f}M"


def fmt_duration(seconds: float) -> str:
    """Format seconds as human-readable: 45 -> '45s', 125 -> '2m05s', 3725 -> '1h02m'."""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{int(seconds)}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"
