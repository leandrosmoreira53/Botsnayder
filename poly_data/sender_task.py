"""
FASE 4 / FASE 6: Non-blocking sender pipeline.

Strategy submits OrderIntentFP into an asyncio.Queue.
Background task drains and batches orders, decoupling strategy tick from I/O.

Design (from supersnayder):
  - submit() is non-blocking (put_nowait)
  - Bounded queue (maxsize) prevents backpressure from blocking strategy
  - In-flight tracking per market
  - Batched flush with configurable window
"""
import asyncio
import logging
import time
from typing import Optional

from poly_data.order_intent import OrderIntentFP
from poly_data.payload_template import stamp_payload
from poly_data import global_state as gstate

log = logging.getLogger(__name__)


class SenderTask:
    """Async order sender pipeline — decouples strategy from network I/O.

    Usage:
        sender = SenderTask(client)
        asyncio.create_task(sender.run())
        ...
        sender.submit(intent_fp)  # non-blocking
    """
    __slots__ = (
        "_client", "_queue", "_cancel_queue",
        "_in_flight", "_max_inflight_per_market",
        "_batch_size", "_flush_interval_ms",
        "_running", "_metrics",
    )

    def __init__(
        self,
        client,
        batch_size: int = 4,
        flush_interval_ms: int = 50,
        max_inflight_per_market: int = 6,
        queue_maxsize: int = 100,
    ):
        self._client = client
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=queue_maxsize)
        self._cancel_queue: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._in_flight: dict = {}  # order_id -> OrderIntentFP
        self._max_inflight_per_market = max_inflight_per_market
        self._batch_size = batch_size
        self._flush_interval_ms = flush_interval_ms
        self._running = False
        self._metrics = {"sent": 0, "failed": 0, "dropped": 0}

    def submit(self, intent: OrderIntentFP) -> bool:
        """Non-blocking submit. Returns False if queue is full (intent dropped)."""
        try:
            self._queue.put_nowait(intent)
            return True
        except asyncio.QueueFull:
            self._metrics["dropped"] += 1
            if gstate._VERBOSE:
                log.warning("Sender queue full, dropping intent %s", intent.label)
            return False

    def submit_cancel(self, order_id: str) -> bool:
        """Non-blocking cancel submit."""
        try:
            self._cancel_queue.put_nowait(order_id)
            return True
        except asyncio.QueueFull:
            return False

    def get_in_flight(self) -> dict:
        """Current in-flight orders (order_id -> intent)."""
        return dict(self._in_flight)

    @property
    def metrics(self) -> dict:
        return dict(self._metrics)

    async def run(self):
        """Background loop: drain queue, batch, send."""
        self._running = True
        flush_s = self._flush_interval_ms / 1000.0

        while self._running:
            try:
                # Process cancels first (higher priority)
                await self._process_cancels()

                # Collect batch of intents
                batch: list[OrderIntentFP] = []
                try:
                    # Wait for first intent (with timeout)
                    intent = await asyncio.wait_for(
                        self._queue.get(), timeout=flush_s
                    )
                    batch.append(intent)
                except asyncio.TimeoutError:
                    continue

                # Drain up to batch_size
                while len(batch) < self._batch_size:
                    try:
                        intent = self._queue.get_nowait()
                        batch.append(intent)
                    except asyncio.QueueEmpty:
                        break

                # Send batch
                await self._send_batch(batch)

            except Exception as exc:
                log.error("Sender loop error: %s", exc)
                await asyncio.sleep(0.1)

    async def stop(self):
        """Graceful shutdown."""
        self._running = False

    async def _process_cancels(self):
        """Drain and execute cancel requests."""
        cancels = []
        while not self._cancel_queue.empty():
            try:
                oid = self._cancel_queue.get_nowait()
                cancels.append(oid)
            except asyncio.QueueEmpty:
                break

        if cancels:
            tasks = [self._client.cancel_order(oid) for oid in cancels]
            await asyncio.gather(*tasks, return_exceptions=True)
            for oid in cancels:
                self._in_flight.pop(oid, None)

    async def _send_batch(self, batch: list[OrderIntentFP]):
        """Send a batch of order intents."""
        for intent in batch:
            # Check in-flight limit per market
            market_count = sum(
                1 for v in self._in_flight.values()
                if v.token_id == intent.token_id
            )
            if market_count >= self._max_inflight_per_market:
                if gstate._VERBOSE:
                    log.debug("In-flight limit for %s, skipping", intent.token_id[:12])
                continue

            t_start = time.monotonic_ns()
            try:
                resp = await self._client.place_order(
                    token_id=intent.token_id,
                    side=intent.side,
                    size=intent.size,
                    price=intent.price,
                    post_only=True,
                )
                t_send = (time.monotonic_ns() - t_start) / 1_000_000  # ms

                if resp.order_id:
                    self._in_flight[resp.order_id] = intent
                    self._metrics["sent"] += 1
                    if gstate._VERBOSE:
                        log.info(
                            "SENT %s %s %.2f @ %.4f -> %s (%.1fms)",
                            intent.side, intent.label, intent.size,
                            intent.price, resp.order_id, t_send,
                        )
                else:
                    self._metrics["failed"] += 1

            except RuntimeError as exc:
                if "401" in str(exc):
                    log.error("Auth failure in sender — stopping pipeline")
                    self._running = False
                    raise
                self._metrics["failed"] += 1
                log.error("Order send failed: %s", exc)
            except Exception as exc:
                self._metrics["failed"] += 1
                log.error("Order send error: %s", exc)
