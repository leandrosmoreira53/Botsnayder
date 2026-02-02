"""
Thin Python CLOB client for Polymarket.

Mirrors the authentication and order-placement patterns from the existing
Rust api.rs so that the Python strategy layer talks to the *same* endpoints
with the *same* credential format (config.json).

Low-latency optimizations (supersnayder model):
  FASE 1: Connection pooling with keep-alive (TCPConnector)
  FASE 2: Cached auth headers (base dict reuse)
  FASE 3: Conditional logging via _VERBOSE
  FASE 6: orjson serialization (bytes-first), __slots__ on data classes
"""

from __future__ import annotations

import hashlib
import hmac
import base64
import time
import logging
from typing import Optional

import aiohttp

from poly_data import global_state as gstate

# FASE 6: Prefer orjson for zero-copy bytes serialization
try:
    import orjson

    def _dumps(obj: dict) -> bytes:
        return orjson.dumps(obj, option=orjson.OPT_NON_STR_KEYS)

    def _loads(data):
        return orjson.loads(data)
except ImportError:
    import json as _json

    def _dumps(obj: dict) -> bytes:
        return _json.dumps(obj, separators=(",", ":")).encode()

    def _loads(data):
        return _json.loads(data)


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data-classes — FASE 6: __slots__ for hot-path objects
# ---------------------------------------------------------------------------

class OrderBookEntry:
    """Single price level. __slots__ for reduced per-instance overhead."""
    __slots__ = ("price", "size")

    def __init__(self, price: float, size: float):
        self.price = price
        self.size = size


class OrderBook:
    """Parsed orderbook with best-price accessors. __slots__."""
    __slots__ = ("bids", "asks")

    def __init__(self, bids=None, asks=None):
        self.bids: list[OrderBookEntry] = bids or []
        self.asks: list[OrderBookEntry] = asks or []

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2.0
        return self.best_bid or self.best_ask


class OrderResponse:
    """API response for order placement. __slots__."""
    __slots__ = ("order_id", "status", "message")

    def __init__(
        self,
        order_id: Optional[str] = None,
        status: str = "",
        message: Optional[str] = None,
    ):
        self.order_id = order_id
        self.status = status
        self.message = message


class MarketInfo:
    """Market metadata from CLOB API. __slots__."""
    __slots__ = (
        "condition_id", "slug", "active", "closed", "accepting_orders",
        "minimum_order_size", "minimum_tick_size", "end_date_iso", "tokens",
    )

    def __init__(
        self,
        condition_id: str = "",
        slug: str = "",
        active: bool = False,
        closed: bool = False,
        accepting_orders: bool = False,
        minimum_order_size: float = 5.0,
        minimum_tick_size: float = 0.01,
        end_date_iso: str = "",
        tokens: Optional[list] = None,
    ):
        self.condition_id = condition_id
        self.slug = slug
        self.active = active
        self.closed = closed
        self.accepting_orders = accepting_orders
        self.minimum_order_size = minimum_order_size
        self.minimum_tick_size = minimum_tick_size
        self.end_date_iso = end_date_iso
        self.tokens = tokens or []


# ---------------------------------------------------------------------------
# API Client
# ---------------------------------------------------------------------------

class PolymarketClient:
    """Async Python wrapper around the Polymarket CLOB REST API.

    Low-latency:
      FASE 1: TCPConnector with connection pooling + keep-alive
      FASE 2: Cached base auth headers (shallow copy per request)
      FASE 6: orjson for JSON serialization (bytes-first)
    """

    def __init__(self, config: dict):
        pm = config["polymarket"]
        self.gamma_url: str = pm["gamma_api_url"]
        self.clob_url: str = pm["clob_api_url"]
        self.auth_method: int = pm.get("auth_method", 0)
        self.api_key: Optional[str] = pm.get("api_key")
        self.api_secret: Optional[str] = pm.get("api_secret")
        self.api_passphrase: Optional[str] = pm.get("api_passphrase")
        self.private_key: Optional[str] = pm.get("private_key")
        self.wallet_address: Optional[str] = pm.get("funder_address")
        self._session: Optional[aiohttp.ClientSession] = None

        # FASE 2: Pre-build base auth headers (static fields cached)
        self._base_headers: dict = {
            "POLY_API_KEY": self.api_key or "",
            "POLY_PASSPHRASE": self.api_passphrase or "",
            "Content-Type": "application/json",
            "Connection": "keep-alive",
        }

    # -- lifecycle -----------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # FASE 1: Connection pooling with keep-alive
            connector = aiohttp.TCPConnector(
                limit=20,
                keepalive_timeout=30,
                enable_cleanup_closed=True,
            )
            timeout = aiohttp.ClientTimeout(total=10)
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                headers={"Connection": "keep-alive"},
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # -- authentication (L2 HMAC, mirrors Rust generate_hmac_signature_l2) ---

    def _hmac_signature(self, timestamp: str, method: str, path: str, body: str) -> str:
        secret_bytes = base64.b64decode(self.api_secret)
        message = f"{timestamp}{method}{path}{body}"
        sig = hmac.new(secret_bytes, message.encode(), hashlib.sha256).digest()
        return base64.b64encode(sig).decode()

    def _auth_headers(self, method: str, path: str, body: str = "") -> dict:
        """Build auth headers. FASE 2: Shallow copy of cached base dict."""
        ts = str(int(time.time()))
        sig = self._hmac_signature(ts, method, path, body)
        h = self._base_headers.copy()  # reuse cached static fields
        h["POLY_SIGNATURE"] = sig
        h["POLY_TIMESTAMP"] = ts
        return h

    # -- public endpoints (no auth needed) -----------------------------------

    async def get_orderbook(self, token_id: str) -> OrderBook:
        session = await self._get_session()
        url = f"{self.clob_url}/book"
        async with session.get(url, params={"token_id": token_id}) as resp:
            resp.raise_for_status()
            # FASE 6: orjson loads from bytes (avoids decode step)
            raw = await resp.read()
            data = _loads(raw)
        bids = [OrderBookEntry(float(e["price"]), float(e["size"])) for e in data.get("bids", [])]
        asks = [OrderBookEntry(float(e["price"]), float(e["size"])) for e in data.get("asks", [])]
        return OrderBook(bids=bids, asks=asks)

    async def get_book_snapshot(self, token_id: str):
        """FASE 5: Returns ImmutableBookSnapshot directly (avoids OrderBook alloc)."""
        from poly_data.book_state import ImmutableBookSnapshot
        session = await self._get_session()
        url = f"{self.clob_url}/book"
        async with session.get(url, params={"token_id": token_id}) as resp:
            resp.raise_for_status()
            raw = await resp.read()
            data = _loads(raw)
        return ImmutableBookSnapshot.from_raw(
            data.get("bids", []), data.get("asks", []),
        )

    async def get_market_by_slug(self, slug: str) -> Optional[dict]:
        session = await self._get_session()
        url = f"{self.gamma_url}/events/slug/{slug}"
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            raw = await resp.read()
            data = _loads(raw)
        markets = data.get("markets", [])
        return markets[0] if markets else None

    async def get_market(self, condition_id: str) -> MarketInfo:
        session = await self._get_session()
        url = f"{self.clob_url}/markets/{condition_id}"
        async with session.get(url) as resp:
            resp.raise_for_status()
            raw = await resp.read()
            data = _loads(raw)
        return MarketInfo(
            condition_id=data.get("condition_id", ""),
            slug=data.get("market_slug", ""),
            active=data.get("active", False),
            closed=data.get("closed", False),
            accepting_orders=data.get("accepting_orders", False),
            minimum_order_size=float(data.get("minimum_order_size", 5)),
            minimum_tick_size=float(data.get("minimum_tick_size", 0.01)),
            end_date_iso=data.get("end_date_iso", ""),
            tokens=[
                {
                    "token_id": t.get("token_id", ""),
                    "outcome": t.get("outcome", ""),
                    "price": float(t.get("price", 0)),
                    "winner": t.get("winner", False),
                }
                for t in data.get("tokens", [])
            ],
        )

    # -- authenticated endpoints ---------------------------------------------

    async def place_order(
        self,
        token_id: str,
        side: str,
        size: float,
        price: float,
        post_only: bool = True,
    ) -> OrderResponse:
        """Place a LIMIT order via POST /order (mirrors Rust api.rs:place_order)."""
        path = "/order"
        url = f"{self.clob_url}{path}"

        order_dict: dict = {
            "tokenID": token_id,
            "side": side,
            "size": str(size),
            "price": str(price),
            "type": "LIMIT",
        }
        if post_only:
            order_dict["postOnly"] = True

        # FASE 6: orjson serialization (bytes)
        body_bytes = _dumps(order_dict)
        body_str = body_bytes.decode()
        headers = self._auth_headers("POST", path, body_str)

        if gstate._VERBOSE:
            log.info(
                "place_order  token=%s side=%s size=%.4f price=%.4f postOnly=%s",
                token_id[:16], side, size, price, post_only,
            )

        session = await self._get_session()
        async with session.post(url, headers=headers, data=body_bytes) as resp:
            status = resp.status
            raw = await resp.read()

            if status == 401:
                log.error("401 Unauthorized placing order. Check L2 credentials.")
                raise RuntimeError("401 Unauthorized -- stop bot")

            if status >= 400:
                log.error("Order rejected (%d): %s", status, raw.decode())
                raise RuntimeError(f"Order rejected ({status}): {raw.decode()}")

            data = _loads(raw)
            return OrderResponse(
                order_id=data.get("order_id") or data.get("orderID"),
                status=data.get("status", ""),
                message=data.get("message"),
            )

    async def place_order_raw(self, body_bytes: bytes) -> OrderResponse:
        """FASE 6: Fast path — accept pre-serialized payload from sender pipeline."""
        path = "/order"
        url = f"{self.clob_url}{path}"
        body_str = body_bytes.decode()
        headers = self._auth_headers("POST", path, body_str)

        session = await self._get_session()
        async with session.post(url, headers=headers, data=body_bytes) as resp:
            status = resp.status
            raw = await resp.read()

            if status == 401:
                raise RuntimeError("401 Unauthorized -- stop bot")
            if status >= 400:
                raise RuntimeError(f"Order rejected ({status}): {raw.decode()}")

            data = _loads(raw)
            return OrderResponse(
                order_id=data.get("order_id") or data.get("orderID"),
                status=data.get("status", ""),
                message=data.get("message"),
            )

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order via DELETE /order/{order_id}."""
        path = f"/order/{order_id}"
        url = f"{self.clob_url}{path}"
        headers = self._auth_headers("DELETE", path)

        session = await self._get_session()
        async with session.delete(url, headers=headers) as resp:
            if resp.status < 300:
                if gstate._VERBOSE:
                    log.info("Cancelled order %s", order_id)
                return True
            text = await resp.text()
            log.warning("Cancel failed for %s (%d): %s", order_id, resp.status, text)
            return False

    async def cancel_all(self) -> bool:
        """Cancel all open orders via DELETE /cancel-all."""
        path = "/cancel-all"
        url = f"{self.clob_url}{path}"
        headers = self._auth_headers("DELETE", path)

        session = await self._get_session()
        async with session.delete(url, headers=headers) as resp:
            ok = resp.status < 300
            if ok:
                log.info("Cancelled all open orders")
            else:
                text = await resp.text()
                log.warning("cancel_all failed (%d): %s", resp.status, text)
            return ok

    async def get_open_orders(self, market: Optional[str] = None) -> list[dict]:
        """GET /open-orders (authenticated)."""
        path = "/open-orders"
        url = f"{self.clob_url}{path}"
        headers = self._auth_headers("GET", path)
        params = {}
        if market:
            params["market"] = market

        session = await self._get_session()
        async with session.get(url, headers=headers, params=params) as resp:
            if resp.status >= 400:
                text = await resp.text()
                log.warning("get_open_orders failed (%d): %s", resp.status, text)
                return []
            raw = await resp.read()
            data = _loads(raw)
            if isinstance(data, list):
                return data
            return data.get("orders", data.get("data", []))
