"""
FASE 6: Payload templates — pre-allocated dicts, avoid per-order dict creation.

Cache templates by (token_id, side). Only price/size mutate per order.
Serialize with orjson (bytes-first) when available.
"""
from typing import Dict, Any
from poly_data.fixed_point import from_fixed

# Prefer orjson for zero-copy bytes serialization
try:
    import orjson

    def _dumps(obj: dict) -> bytes:
        return orjson.dumps(obj, option=orjson.OPT_NON_STR_KEYS)
except ImportError:
    import json

    def _dumps(obj: dict) -> bytes:
        return json.dumps(obj, separators=(",", ":")).encode()


# Module-level template cache
_CACHE: Dict[str, dict] = {}


def get_payload_template(token_id: str, side: str) -> dict:
    """Get or create a cached payload template for (token_id, side).

    Returns a shallow copy — caller sets price/size before sending.
    """
    key = f"{token_id}:{side}"
    if key not in _CACHE:
        _CACHE[key] = {
            "tokenID": token_id,
            "side": side,
            "type": "LIMIT",
            "postOnly": True,
            "price": "0",
            "size": "0",
        }
    return _CACHE[key].copy()


def stamp_payload(token_id: str, side: str, price_fp: int, size_fp: int) -> bytes:
    """Build and serialize an order payload from fixed-point values.

    FASE 6: Uses cached template + orjson for minimal allocation.
    """
    tpl = get_payload_template(token_id, side)
    tpl["price"] = str(from_fixed(price_fp))
    tpl["size"] = str(from_fixed(size_fp))
    return _dumps(tpl)


def stamp_payload_floats(token_id: str, side: str, price: float, size: float) -> bytes:
    """Build and serialize from float values (compatibility path)."""
    tpl = get_payload_template(token_id, side)
    tpl["price"] = str(price)
    tpl["size"] = str(size)
    return _dumps(tpl)
