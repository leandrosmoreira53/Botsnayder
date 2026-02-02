"""
FASE 5: Immutable Book Snapshot for lock-free reads.

Single-writer (WS or HTTP update), immutable snapshot for strategy reads.
Fixed-point prices for zero-float-comparison on hot path.
"""
import time
from typing import Optional, Sequence
from poly_data.fixed_point import to_fixed, from_fixed, PRICE_SCALE


class ImmutableBookSnapshot:
    """Thread-safe, immutable orderbook snapshot.

    FASE 5: Lock-free reads — strategy accesses snapshot without locks.
    FASE 6: Fixed-point prices (int) for fast comparison.
    """
    __slots__ = (
        "bids", "asks",
        "best_bid_fp", "best_ask_fp", "mid_fp",
        "best_bid", "best_ask", "mid",
        "timestamp_ns",
    )

    def __init__(
        self,
        bids: tuple,  # ((price_fp, size_fp), ...) descending by price
        asks: tuple,  # ((price_fp, size_fp), ...) ascending by price
    ):
        self.bids = bids
        self.asks = asks

        # Precompute best prices (fixed-point)
        self.best_bid_fp: int = bids[0][0] if bids else 0
        self.best_ask_fp: int = asks[0][0] if asks else 0

        # Float versions for API / strategy compatibility
        self.best_bid: Optional[float] = from_fixed(self.best_bid_fp) if bids else None
        self.best_ask: Optional[float] = from_fixed(self.best_ask_fp) if asks else None

        # Mid
        if self.best_bid is not None and self.best_ask is not None:
            self.mid: Optional[float] = (self.best_bid + self.best_ask) / 2.0
            self.mid_fp: int = (self.best_bid_fp + self.best_ask_fp) // 2
        else:
            self.mid = self.best_bid or self.best_ask
            self.mid_fp = self.best_bid_fp or self.best_ask_fp

        self.timestamp_ns: int = time.time_ns()

    @classmethod
    def from_raw(cls, bids_raw: list, asks_raw: list) -> "ImmutableBookSnapshot":
        """Build from API response lists of {price, size} dicts or tuples.

        Handles both:
          - [{"price": "0.52", "size": "100"}, ...] (from REST API)
          - [(0.52, 100), ...] (from internal use)
        """
        def _parse(entries):
            result = []
            for e in entries:
                if isinstance(e, dict):
                    p = to_fixed(float(e.get("price", 0)))
                    s = to_fixed(float(e.get("size", 0)))
                elif isinstance(e, (list, tuple)):
                    p = to_fixed(float(e[0]))
                    s = to_fixed(float(e[1]))
                else:
                    continue
                if s > 0:
                    result.append((p, s))
            return result

        bids_parsed = _parse(bids_raw)
        asks_parsed = _parse(asks_raw)

        # Sort: bids descending by price, asks ascending
        bids_parsed.sort(key=lambda x: -x[0])
        asks_parsed.sort(key=lambda x: x[0])

        return cls(bids=tuple(bids_parsed), asks=tuple(asks_parsed))

    @classmethod
    def from_orderbook(cls, ob) -> "ImmutableBookSnapshot":
        """Convert from execution.OrderBook to immutable snapshot."""
        bids_raw = [(e.price, e.size) for e in ob.bids]
        asks_raw = [(e.price, e.size) for e in ob.asks]
        return cls.from_raw(bids_raw, asks_raw)

    def depth_usd(self) -> float:
        """Total depth in USD (sum of price * size across all levels)."""
        total = 0
        for p, s in self.bids:
            total += p * s
        for p, s in self.asks:
            total += p * s
        return total / (PRICE_SCALE * PRICE_SCALE)

    def levels_count(self) -> int:
        """Min of bid/ask level count."""
        return min(len(self.bids), len(self.asks))

    def spread_fp(self) -> int:
        """Spread in fixed-point. 0 if missing data."""
        if self.best_ask_fp and self.best_bid_fp:
            return self.best_ask_fp - self.best_bid_fp
        return 0
