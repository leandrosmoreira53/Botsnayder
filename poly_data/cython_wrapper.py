"""
FASE 8: Cython wrapper with pure-Python fallback.

Tries to import compiled Cython modules. If unavailable, provides
equivalent Python implementations (slower but functional).
"""
from poly_data.fixed_point import PRICE_SCALE

try:
    from poly_data.book_cython import (
        compute_spread_fp as _cy_spread,
        compute_quotes_fp as _cy_quotes,
    )
    CYTHON_AVAILABLE = True
except ImportError:
    CYTHON_AVAILABLE = False
    _cy_spread = None
    _cy_quotes = None


def compute_spread_fp(bids_fp: list, asks_fp: list) -> tuple:
    """Compute spread from fixed-point bid/ask lists.

    Returns: (best_bid_fp, best_ask_fp, spread_fp)
    """
    if CYTHON_AVAILABLE and _cy_spread is not None:
        return _cy_spread(bids_fp, asks_fp)

    # Pure Python fallback
    if not bids_fp or not asks_fp:
        return (0, 0, 0)

    best_bid = max(p for p, _ in bids_fp) if bids_fp else 0
    best_ask = min(p for p, _ in asks_fp) if asks_fp else 0
    spread = best_ask - best_bid if best_ask > 0 and best_bid > 0 else 0
    return (best_bid, best_ask, spread)


def compute_quotes_fp(
    mid_fp: int,
    edge_min_fp: int,
    level_spacing_fp: int,
    levels: int,
    tick_fp: int,
    best_ask_fp: int,
) -> list:
    """Generate quote prices (fixed-point) for N levels.

    Returns: list of (price_fp, level_index) tuples
    """
    if CYTHON_AVAILABLE and _cy_quotes is not None:
        return _cy_quotes(
            mid_fp, edge_min_fp, level_spacing_fp, levels, tick_fp, best_ask_fp,
        )

    # Pure Python fallback
    result = []
    for lvl in range(levels):
        edge = edge_min_fp + lvl * level_spacing_fp
        price_fp = mid_fp - edge

        # Round to tick
        if tick_fp > 0:
            price_fp = ((price_fp + tick_fp // 2) // tick_fp) * tick_fp

        # Post-only safety: never above best ask
        if best_ask_fp > 0 and price_fp >= best_ask_fp:
            price_fp = best_ask_fp - tick_fp

        if price_fp > 0:
            result.append((price_fp, lvl))

    return result


def build_order_payload_fast(
    token_id: str,
    side: str,
    price_fp: int,
    size_fp: int,
) -> dict:
    """Build order payload dict from fixed-point values.

    FASE 8: Returns dict (Cython version avoids Python dict overhead).
    """
    return {
        "tokenID": token_id,
        "side": side,
        "type": "LIMIT",
        "postOnly": True,
        "price": str(price_fp / PRICE_SCALE),
        "size": str(size_fp / PRICE_SCALE),
    }
