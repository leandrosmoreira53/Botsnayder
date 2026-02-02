"""
FASE 5: Reconcile Task — periodic fill detection OFF the hot path.

Runs every N seconds, polls open orders, detects fills, feeds into strategy.
Replaces inline poll_fills() in the tick loop.
"""
import asyncio
import logging
from typing import Optional

log = logging.getLogger(__name__)

RECONCILE_INTERVAL_S = 2.0  # default, configurable


class ReconcileTask:
    """Background task for fill detection and order reconciliation.

    FASE 5: Runs outside the hot path — no impact on strategy tick latency.
    """
    __slots__ = (
        "_client", "_strategy", "_interval_s", "_running",
    )

    def __init__(self, client, strategy, interval_s: float = RECONCILE_INTERVAL_S):
        self._client = client
        self._strategy = strategy
        self._interval_s = interval_s
        self._running = False

    async def run(self):
        """Background loop: poll open orders, detect fills."""
        self._running = True
        log.info("Reconcile task started (interval: %.1fs)", self._interval_s)

        while self._running:
            try:
                await asyncio.sleep(self._interval_s)
                await self._reconcile_all_markets()
            except Exception as exc:
                log.error("Reconcile error: %s", exc)
                await asyncio.sleep(1.0)

    async def stop(self):
        self._running = False

    async def _reconcile_all_markets(self):
        """Check all active markets for fills."""
        for cid, ms in list(self._strategy.markets.items()):
            if ms.locked:
                continue
            try:
                await self._reconcile_market(ms)
            except Exception as exc:
                log.error("Reconcile [%s] error: %s", cid[:12], exc)

    async def _reconcile_market(self, ms):
        """Compare live_orders vs open_orders to detect fills."""
        if not ms.live_orders:
            return

        try:
            open_orders = await self._client.get_open_orders(
                market=ms.condition_id,
            )
        except Exception:
            return

        open_ids = set()
        for o in open_orders:
            oid = o.get("id") or o.get("order_id")
            if oid:
                open_ids.add(oid)

        filled_ids = [
            oid for oid in list(ms.live_orders) if oid not in open_ids
        ]

        for oid in filled_ids:
            order = ms.live_orders.pop(oid, None)
            if order is None:
                continue
            tok_side = "YES" if order.get("token_id") == ms.yes_token_id else "NO"
            qty = float(order.get("size", 0))
            px = float(order.get("price", 0))
            if qty > 0:
                self._strategy.on_fill(
                    ms.condition_id, tok_side, qty, px, fill_id=oid,
                )
