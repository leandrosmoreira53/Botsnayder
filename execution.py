"""
Thin Python CLOB client for Polymarket.

Mirrors the authentication and order-placement patterns from the existing
Rust api.rs so that the Python strategy layer talks to the *same* endpoints
with the *same* credential format (config.json).
"""

import asyncio
import hashlib
import hmac
import base64
import json
import time
import logging
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data-classes (mirror Rust models.rs)
# ---------------------------------------------------------------------------

@dataclass
class OrderBookEntry:
    price: float
    size: float


@dataclass
class OrderBook:
    bids: list[OrderBookEntry] = field(default_factory=list)
    asks: list[OrderBookEntry] = field(default_factory=list)

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


@dataclass
class OrderResponse:
    order_id: Optional[str] = None
    status: str = ""
    message: Optional[str] = None


@dataclass
class MarketInfo:
    condition_id: str = ""
    slug: str = ""
    active: bool = False
    closed: bool = False
    accepting_orders: bool = False
    minimum_order_size: float = 5.0
    minimum_tick_size: float = 0.01
    end_date_iso: str = ""
    tokens: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# API Client
# ---------------------------------------------------------------------------

class PolymarketClient:
    """Async Python wrapper around the Polymarket CLOB REST API."""

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

    # -- lifecycle -----------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=10)
            self._session = aiohttp.ClientSession(timeout=timeout)
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
        ts = str(int(time.time()))
        sig = self._hmac_signature(ts, method, path, body)
        return {
            "POLY_API_KEY": self.api_key,
            "POLY_PASSPHRASE": self.api_passphrase,
            "POLY_SIGNATURE": sig,
            "POLY_TIMESTAMP": ts,
            "Content-Type": "application/json",
        }

    # -- public endpoints (no auth needed) -----------------------------------

    async def get_orderbook(self, token_id: str) -> OrderBook:
        session = await self._get_session()
        url = f"{self.clob_url}/book"
        async with session.get(url, params={"token_id": token_id}) as resp:
            resp.raise_for_status()
            data = await resp.json()
        bids = [OrderBookEntry(float(e["price"]), float(e["size"])) for e in data.get("bids", [])]
        asks = [OrderBookEntry(float(e["price"]), float(e["size"])) for e in data.get("asks", [])]
        return OrderBook(bids=bids, asks=asks)

    async def get_market_by_slug(self, slug: str) -> Optional[dict]:
        session = await self._get_session()
        url = f"{self.gamma_url}/events/slug/{slug}"
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
        markets = data.get("markets", [])
        return markets[0] if markets else None

    async def get_market(self, condition_id: str) -> MarketInfo:
        session = await self._get_session()
        url = f"{self.clob_url}/markets/{condition_id}"
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()
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

        order_json: dict = {
            "tokenID": token_id,
            "side": side,
            "size": str(size),
            "price": str(price),
            "type": "LIMIT",
        }
        if post_only:
            order_json["postOnly"] = True

        body = json.dumps(order_json, separators=(",", ":"))
        headers = self._auth_headers("POST", path, body)

        log.info(
            "place_order  token=%s side=%s size=%.4f price=%.4f postOnly=%s",
            token_id, side, size, price, post_only,
        )

        session = await self._get_session()
        async with session.post(url, headers=headers, data=body) as resp:
            status = resp.status
            text = await resp.text()

            if status == 401:
                log.error("401 Unauthorized placing order. Check L2 credentials. Body: %s", text)
                raise RuntimeError("401 Unauthorized -- stop bot")

            if status >= 400:
                log.error("Order rejected (%d): %s", status, text)
                raise RuntimeError(f"Order rejected ({status}): {text}")

            data = json.loads(text)
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
            data = await resp.json()
            if isinstance(data, list):
                return data
            return data.get("orders", data.get("data", []))
