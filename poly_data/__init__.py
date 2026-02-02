"""
poly_data — Low-latency infrastructure for Polymarket bot.

Following the supersnayder optimization model:
  FASE 6: Fixed-point arithmetic, __slots__, payload templates
  FASE 7: uvloop event loop
  FASE 8: Cython hot-path (optional)
"""
from poly_data.fixed_point import PRICE_SCALE, to_fixed, from_fixed, round_tick_fp
from poly_data.order_intent import OrderIntentFP
from poly_data.book_state import ImmutableBookSnapshot
from poly_data.payload_template import get_payload_template, stamp_payload
from poly_data.global_state import _VERBOSE, set_verbose

__all__ = [
    "PRICE_SCALE", "to_fixed", "from_fixed", "round_tick_fp",
    "OrderIntentFP",
    "ImmutableBookSnapshot",
    "get_payload_template", "stamp_payload",
    "_VERBOSE", "set_verbose",
]
