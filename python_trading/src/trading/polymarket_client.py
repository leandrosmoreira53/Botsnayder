"""
Polymarket CLOB API Client with L1 Authentication.

Uses private key + funder for authentication without requiring
separate L2 API credentials.
"""

import logging
from typing import Optional, Dict, Any, List

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    OrderArgs,
    OrderType,
    OpenOrderParams,
    TradeParams,
)
from py_clob_client.order_builder.constants import BUY, SELL

from config.trading_config import (
    TradingConfig,
    get_private_key,
    get_funder_address,
)

logger = logging.getLogger(__name__)


class PolymarketClient:
    """
    Polymarket CLOB API client with L1 authentication.

    This client uses private key + funder for authentication,
    deriving L2 API credentials automatically from L1.

    Attributes:
        client: The underlying py-clob-client ClobClient
        config: Trading configuration
        is_authenticated: Whether client has valid credentials
    """

    def __init__(
        self,
        private_key: Optional[str] = None,
        funder: Optional[str] = None,
        config: Optional[TradingConfig] = None,
    ):
        """
        Initialize Polymarket client with L1 authentication.

        Args:
            private_key: Wallet private key (defaults to PRIVATE_KEY env var)
            funder: Funder address (defaults to FUNDER_ADDRESS env var)
            config: Trading configuration (defaults to TradingConfig.from_env())
        """
        self.config = config or TradingConfig.from_env()
        self._private_key = private_key or get_private_key()
        self._funder = funder or get_funder_address()
        self.is_authenticated = False
        self.client: Optional[ClobClient] = None

        self._initialize_client()

    def _initialize_client(self) -> None:
        """Initialize the CLOB client with L1 authentication."""
        try:
            logger.info("Initializing Polymarket client with L1 auth...")

            # Create client with L1 credentials
            self.client = ClobClient(
                host=self.config.CLOB_HOST,
                key=self._private_key,
                chain_id=self.config.CHAIN_ID,
                signature_type=self.config.SIGNATURE_TYPE,
                funder=self._funder,
            )

            # CRITICAL: Derive L2 API credentials from L1
            # This is required even when using L1 auth only
            logger.info("Deriving API credentials from L1...")
            api_creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(api_creds)

            self.is_authenticated = True
            logger.info("Polymarket client initialized successfully")

        except Exception as e:
            logger.error(f"Failed to initialize Polymarket client: {e}")
            raise

    def get_wallet_address(self) -> str:
        """Get the wallet address derived from private key."""
        if not self.client:
            raise RuntimeError("Client not initialized")
        # The address is derived from the private key
        from eth_account import Account
        account = Account.from_key(self._private_key)
        return account.address

    def get_funder_address(self) -> str:
        """Get the funder address (address holding funds)."""
        return self._funder or self.get_wallet_address()

    # === Market Data Methods ===

    def get_markets(self, next_cursor: str = "") -> Dict[str, Any]:
        """
        Get list of active markets.

        Args:
            next_cursor: Pagination cursor

        Returns:
            Dict with 'data' (list of markets) and 'next_cursor'
        """
        if not self.client:
            raise RuntimeError("Client not initialized")
        return self.client.get_markets(next_cursor=next_cursor)

    def get_market(self, condition_id: str) -> Dict[str, Any]:
        """
        Get market details by condition ID.

        Args:
            condition_id: Market condition ID

        Returns:
            Market data dict
        """
        if not self.client:
            raise RuntimeError("Client not initialized")
        return self.client.get_market(condition_id)

    def get_orderbook(self, token_id: str) -> Dict[str, Any]:
        """
        Get orderbook for a token.

        Args:
            token_id: Token ID (YES or NO token)

        Returns:
            Orderbook with bids and asks
        """
        if not self.client:
            raise RuntimeError("Client not initialized")
        return self.client.get_order_book(token_id)

    def get_price(self, token_id: str, side: str = "BUY") -> Optional[float]:
        """
        Get current best price for a token.

        Args:
            token_id: Token ID
            side: "BUY" or "SELL"

        Returns:
            Best price or None if no liquidity
        """
        if not self.client:
            raise RuntimeError("Client not initialized")

        orderbook = self.get_orderbook(token_id)

        if side == "BUY":
            asks = orderbook.get("asks", [])
            if asks:
                return float(asks[0]["price"])
        else:
            bids = orderbook.get("bids", [])
            if bids:
                return float(bids[0]["price"])

        return None

    def get_midpoint(self, token_id: str) -> Optional[float]:
        """
        Get midpoint price for a token.

        Args:
            token_id: Token ID

        Returns:
            Midpoint price or None
        """
        if not self.client:
            raise RuntimeError("Client not initialized")
        return self.client.get_midpoint(token_id)

    def get_spread(self, token_id: str) -> Optional[float]:
        """
        Get bid-ask spread for a token.

        Args:
            token_id: Token ID

        Returns:
            Spread or None
        """
        if not self.client:
            raise RuntimeError("Client not initialized")
        return self.client.get_spread(token_id)

    # === Balance Methods ===

    def get_balance(self, asset_type: str = "USDC") -> float:
        """
        Get balance for an asset.

        Args:
            asset_type: "USDC" or token address

        Returns:
            Balance amount
        """
        if not self.client:
            raise RuntimeError("Client not initialized")

        try:
            balance = self.client.get_balance_allowance(
                asset_type=asset_type if asset_type != "USDC" else None
            )
            return float(balance.get("balance", 0))
        except Exception as e:
            logger.warning(f"Failed to get balance: {e}")
            return 0.0

    def get_all_balances(self) -> Dict[str, float]:
        """
        Get all token balances.

        Returns:
            Dict mapping token to balance
        """
        if not self.client:
            raise RuntimeError("Client not initialized")

        try:
            balances = self.client.get_balances()
            return {b["asset_type"]: float(b["balance"]) for b in balances}
        except Exception as e:
            logger.warning(f"Failed to get balances: {e}")
            return {}

    # === Order Methods ===

    def create_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
    ) -> Dict[str, Any]:
        """
        Create and sign a limit order (does not submit).

        Args:
            token_id: Token ID to trade
            side: "BUY" or "SELL"
            price: Limit price (0-1)
            size: Number of shares

        Returns:
            Signed order dict ready for submission
        """
        if not self.client:
            raise RuntimeError("Client not initialized")

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=BUY if side.upper() == "BUY" else SELL,
        )

        signed_order = self.client.create_order(order_args)
        return signed_order

    def submit_order(
        self,
        signed_order: Dict[str, Any],
        order_type: str = "GTC",
    ) -> Dict[str, Any]:
        """
        Submit a signed order to the CLOB.

        Args:
            signed_order: Signed order from create_order()
            order_type: "GTC" (Good Till Canceled), "GTD" (Good Till Date), "FOK" (Fill or Kill)

        Returns:
            Order submission response
        """
        if not self.client:
            raise RuntimeError("Client not initialized")

        ot = OrderType.GTC
        if order_type == "GTD":
            ot = OrderType.GTD
        elif order_type == "FOK":
            ot = OrderType.FOK

        response = self.client.post_order(signed_order, ot)
        return response

    def create_and_submit_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        order_type: str = "GTC",
    ) -> Dict[str, Any]:
        """
        Create, sign, and submit a limit order in one call.

        Args:
            token_id: Token ID to trade
            side: "BUY" or "SELL"
            price: Limit price (0-1)
            size: Number of shares
            order_type: "GTC", "GTD", or "FOK"

        Returns:
            Order submission response
        """
        signed_order = self.create_order(token_id, side, price, size)
        return self.submit_order(signed_order, order_type)

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """
        Cancel an open order.

        Args:
            order_id: Order ID to cancel

        Returns:
            Cancellation response
        """
        if not self.client:
            raise RuntimeError("Client not initialized")
        return self.client.cancel(order_id)

    def cancel_all_orders(self) -> Dict[str, Any]:
        """
        Cancel all open orders.

        Returns:
            Cancellation response
        """
        if not self.client:
            raise RuntimeError("Client not initialized")
        return self.client.cancel_all()

    def get_open_orders(
        self,
        market: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Get all open orders.

        Args:
            market: Optional market/condition ID to filter by

        Returns:
            List of open orders
        """
        if not self.client:
            raise RuntimeError("Client not initialized")

        params = OpenOrderParams(market=market) if market else OpenOrderParams()
        return self.client.get_orders(params)

    def get_trades(
        self,
        market: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Get trade history.

        Args:
            market: Optional market to filter by
            limit: Max trades to return

        Returns:
            List of trades
        """
        if not self.client:
            raise RuntimeError("Client not initialized")

        params = TradeParams(market=market) if market else TradeParams()
        return self.client.get_trades(params)

    # === Utility Methods ===

    def check_connection(self) -> bool:
        """
        Check if client can connect to CLOB API.

        Returns:
            True if connection successful
        """
        try:
            self.get_markets()
            return True
        except Exception as e:
            logger.error(f"Connection check failed: {e}")
            return False

    def get_server_time(self) -> int:
        """
        Get CLOB server time.

        Returns:
            Server timestamp in milliseconds
        """
        if not self.client:
            raise RuntimeError("Client not initialized")
        return self.client.get_server_time()
