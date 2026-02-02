"""
FASE 6: OrderIntent with __slots__ and fixed-point price/size.

Replaces @dataclass OrderIntent for zero-alloc on hot path.
"""
import time
from poly_data.fixed_point import PRICE_SCALE, to_fixed, from_fixed, USE_FIXED_POINT


class OrderIntentFP:
    """Order intent using fixed-point for price/size. No dataclass overhead.

    FASE 6: __slots__ reduces per-instance memory and access time.
    """
    __slots__ = ("token_id", "side", "price_fp", "size_fp", "label", "timestamp_ns")

    def __init__(
        self,
        token_id: str,
        side: str,
        price_fp: int,
        size_fp: int,
        label: str = "",
        timestamp_ns: int = 0,
    ):
        self.token_id = token_id
        self.side = side
        self.price_fp = price_fp
        self.size_fp = size_fp
        self.label = label
        self.timestamp_ns = timestamp_ns or time.monotonic_ns()

    @property
    def price(self) -> float:
        """Float price for API boundary."""
        return from_fixed(self.price_fp)

    @property
    def size(self) -> float:
        """Float size for API boundary."""
        return from_fixed(self.size_fp)

    @classmethod
    def from_floats(
        cls,
        token_id: str,
        side: str,
        price: float,
        size: float,
        label: str = "",
    ) -> "OrderIntentFP":
        """Construct from float values (convenience for strategy layer)."""
        return cls(
            token_id=token_id,
            side=side,
            price_fp=to_fixed(price),
            size_fp=to_fixed(size),
            label=label,
        )
