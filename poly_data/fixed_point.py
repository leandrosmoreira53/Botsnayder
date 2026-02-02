"""
FASE 6: Fixed-point arithmetic for prices and sizes.

All prices stored as int(price * PRICE_SCALE) to avoid float comparison issues
on the hot path. Conversions happen only at system boundaries (API in/out).

Env-controlled:
    PRICE_SCALE=1000   (default: millis — 0.001 resolution)
    USE_FIXED_POINT=true/false (default: true)
"""
import os

PRICE_SCALE: int = int(os.environ.get("PRICE_SCALE", "1000"))
USE_FIXED_POINT: bool = os.environ.get("USE_FIXED_POINT", "true").lower() == "true"

# Precompute inverse for division
_INV_SCALE: float = 1.0 / PRICE_SCALE


def to_fixed(f: float) -> int:
    """Convert float price/size to fixed-point int."""
    return int(round(f * PRICE_SCALE))


def from_fixed(i: int) -> float:
    """Convert fixed-point int back to float."""
    return i * _INV_SCALE


def to_fixed_safe(v) -> int:
    """Accept either float or int, return fixed-point int."""
    if isinstance(v, int):
        return v
    return to_fixed(v)


def from_fixed_safe(v) -> float:
    """Accept either int or float, return float."""
    if isinstance(v, float):
        return v
    return from_fixed(v)


def round_tick_fp(price_fp: int, tick_fp: int) -> int:
    """Round a fixed-point price to the nearest tick (integer division)."""
    if tick_fp <= 0:
        tick_fp = to_fixed(0.01)
    return ((price_fp + tick_fp // 2) // tick_fp) * tick_fp


def fixed_mul(a: int, b: int) -> int:
    """Multiply two fixed-point values: (a * b) / SCALE."""
    return (a * b) // PRICE_SCALE
