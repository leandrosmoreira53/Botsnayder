# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
# cython: cdivision=True

"""
FASE 8: Cython hot path for orderbook operations.
- Spread computation
- Quote generation (price levels)
- Best bid/ask search

Build: python setup_cython.py build_ext --inplace
"""

from libc.stdlib cimport malloc, free
cimport cython

cdef struct PriceLevel:
    long price
    long size


@cython.boundscheck(False)
@cython.wraparound(False)
def compute_spread_fp(list bids_fp, list asks_fp):
    """Compute spread from fixed-point bid/ask lists.

    Args:
        bids_fp: list of (price_fp, size_fp) tuples
        asks_fp: list of (price_fp, size_fp) tuples

    Returns:
        (best_bid_fp, best_ask_fp, spread_fp)
    """
    cdef int num_bids = len(bids_fp)
    cdef int num_asks = len(asks_fp)

    if num_bids == 0 or num_asks == 0:
        return (0, 0, 0)

    cdef long best_bid = 0
    cdef long best_ask = 0
    cdef long price
    cdef int i

    # Find best bid (max price)
    for i in range(num_bids):
        price = bids_fp[i][0]
        if price > best_bid:
            best_bid = price

    # Find best ask (min price)
    best_ask = asks_fp[0][0]
    for i in range(1, num_asks):
        price = asks_fp[i][0]
        if price < best_ask:
            best_ask = price

    cdef long spread = best_ask - best_bid if best_ask > 0 and best_bid > 0 else 0
    return (best_bid, best_ask, spread)


@cython.boundscheck(False)
@cython.wraparound(False)
def compute_quotes_fp(
    long mid_fp,
    long edge_min_fp,
    long level_spacing_fp,
    int levels,
    long tick_fp,
    long best_ask_fp,
):
    """Generate quote prices (fixed-point) for N levels.

    Returns:
        list of (price_fp, level_index) tuples
    """
    cdef list result = []
    cdef int lvl
    cdef long edge, price_fp

    for lvl in range(levels):
        edge = edge_min_fp + lvl * level_spacing_fp
        price_fp = mid_fp - edge

        # Round to tick
        if tick_fp > 0:
            price_fp = ((price_fp + tick_fp // 2) // tick_fp) * tick_fp

        # Post-only: never >= best ask
        if best_ask_fp > 0 and price_fp >= best_ask_fp:
            price_fp = best_ask_fp - tick_fp

        if price_fp > 0:
            result.append((price_fp, lvl))

    return result
